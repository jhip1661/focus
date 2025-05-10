import os
import json
import datetime
import logging
import io

import pandas as pd
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import openai  # Changed import to use legacy OpenAI module

# ── 로깅 설정 ───────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

# ── 서비스 계정 JSON 로드 & 검증 ─────────────────────────────────────────────
RAW_JSON = os.getenv("GSHEET_CREDENTIALS_JSON")
if not RAW_JSON:
    raise ValueError("❌ 환경변수 'GSHEET_CREDENTIALS_JSON'이 누락되었습니다.")
try:
    creds_info = json.loads(RAW_JSON)
    logging.info("✅ 서비스 계정 JSON 파싱 성공")
except json.JSONDecodeError as e:
    raise ValueError(f"❌ SERVICE_ACCOUNT_JSON 파싱 실패: {e}")

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]
creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)

# ── Google Sheets & Drive 클라이언트 생성 ─────────────────────────────────
gc = gspread.authorize(creds)
drive = build("drive", "v3", credentials=creds)

# ── OpenAI API 키 설정 ────────────────────────────────────────────────────
openai_key = os.getenv("OPENAI_API_KEY")
if not openai_key:
    raise ValueError("❌ 환경변수 'OPENAI_API_KEY'가 없습니다.")
openai.api_key = openai_key

# ── 필수 환경변수 및 하드코딩된 폴더 ID ─────────────────────────────────────
SOURCE_DB_ID   = os.getenv("SOURCE_DB_ID")
if not SOURCE_DB_ID:
    raise ValueError("❌ 환경변수 'SOURCE_DB_ID'가 없습니다.")

DRIVE_FOLDER_ID = "1SNhQQEyyn9NveFOixl1Ef3PiZzd5vOdg"
GPT_MODEL       = "gpt-3.5-turbo"

# ── 의존성 확인 ───────────────────────────────────────────────────────────────
try:
    import openpyxl
except ImportError:
    raise ImportError(
        "❌ Missing dependency 'openpyxl'.\n"
        "   Replit Shell에서 아래를 실행하세요:\n"
        "     upm add openpyxl"
    )

# ── 시트 초기화 ───────────────────────────────────────────────────────────────
def init_worksheet(sheet_id: str, title: str, header: list = None):
    ss = gc.open_by_key(sheet_id)
    try:
        ws = ss.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        ws = ss.add_worksheet(title=title, rows="1000", cols="20")
    if header and ws.row_values(1) != header:
        ws.clear()
        ws.append_row(header)
    return ws

# ── 프롬프트 설정 추출 ───────────────────────────────────────────────────────
def extract_prompt_configs(ws):
    configs = []
    for r in ws.get_all_values()[1:]:
        if len(r) >= 11 and r[1].strip() == "xls" and r[4].strip() == "Y":
            configs.append(r[5:11])
    return configs

# ── Drive 내 XLSX 파일 목록 조회 ─────────────────────────────────────────────
def list_xlsx_files(folder_id: str):
    q = (
        f"'{folder_id}' in parents and "
        "mimeType='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' "
        "and trashed=false"
    )
    res = drive.files().list(q=q, fields="files(id,name)").execute()
    return res.get("files", [])

# ── 파일 다운로드 함수 ─────────────────────────────────────────────────────────
def download_xlsx(file_id: str):
    req = drive.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, req)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    buf.seek(0)
    return buf

# ── GPT 호출 ────────────────────────────────────────────────────────────────
def generate_text(prompt_cfg, title, content):
    system_msg = "\n\n".join(prompt_cfg)
    user_msg   = f"다음 글을 중복되지 않도록 재작성해줘:\n\n제목: {title}\n내용: {content}"
    resp = openai.ChatCompletion.create(
        model=GPT_MODEL,
        messages=[
            {"role": "system", "content": system_msg},
            {"role": "user",   "content": user_msg}
        ],
        temperature=0.8,
        max_tokens=3000
    )
    return resp.choices[0].message.content.strip()

# ── 메인 프로세스 ───────────────────────────────────────────────────────────
def main():
    xls_ws    = init_worksheet(
        SOURCE_DB_ID, "xls", header=["file_id", "작성일시", "제목", "국문"]
    )
    prompt_ws = init_worksheet(SOURCE_DB_ID, "prompt")

    existing_ids = {
        row[0]
        for row in xls_ws.get_all_values()[1:]
        if row and row[0]
    }

    prompt_configs = extract_prompt_configs(prompt_ws)
    if not prompt_configs:
        logging.warning("⚠️ 프롬프트 설정이 없습니다. (B열=xls, E열=Y 확인)")
        return
    prompt_cfg = prompt_configs[0]

    files     = list_xlsx_files(DRIVE_FOLDER_ID)
    new_files = [f for f in files if f["id"] not in existing_ids]
    if not new_files:
        logging.info("ℹ️ 신규 처리할 XLSX 파일이 없습니다.")
        return

    for f in new_files:
        logging.info(f"📥 처리 시작: {f['name']} ({f['id']})")
        buf = download_xlsx(f["id"])
        df  = pd.read_excel(buf, engine="openpyxl")

        for _, row in df.iterrows():
            title   = row.get("제목", "")
            content = row.get("본문", "")
            if not content:
                logging.warning(f"⚠️ 본문이 비어있어 건너뜀: {row}")
                continue

            gen_text  = generate_text(prompt_cfg, title, content)
            timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            xls_ws.append_row([f["id"], timestamp, title, gen_text])
            logging.info(f"✅ 저장 완료: {title}")

if __name__ == "__main__":
    main()
