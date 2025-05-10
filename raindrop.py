import os
import json
import datetime
import time
import logging
import requests
import gspread
from bs4 import BeautifulSoup
from google.oauth2.service_account import Credentials as GCredentials
import openai  # ✅ 변경: OpenAI 모듈 전체 import

# ── 로깅 설정 ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")

# ── 환경변수 로드 ─────────────────────────────────────────────────────────────────
RAW_JSON = os.getenv("GSHEET_CREDENTIALS_JSON")
RAINDROP_TOKEN = os.getenv("RAINDROP_TOKEN")
GSHEET_ID = os.getenv("GSHEET_ID")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GPT_MODEL = "gpt-3.5-turbo"

# ── 필수 환경변수 체크 ─────────────────────────────────────────────────────────────
for name, val in [
    ("GSHEET_CREDENTIALS_JSON", RAW_JSON),
    ("RAINDROP_TOKEN", RAINDROP_TOKEN),
    ("GSHEET_ID", GSHEET_ID),
    ("OPENAI_API_KEY", OPENAI_API_KEY),
]:
    if not val:
        raise ValueError(f"❌ 환경변수 '{name}'이(가) 누락되었습니다.")

# ── OpenAI 클라이언트 설정 ─────────────────────────────────────────────────────────
openai.api_key = OPENAI_API_KEY
client = openai

# ── 서비스 계정 JSON 파싱 & 검증 ────────────────────────────────────────────────────
try:
    creds_info = json.loads(RAW_JSON)
    logging.info("✅ JSON 파싱(원본) 성공")
except json.JSONDecodeError:
    fixed = RAW_JSON.replace('\\n', '\n')
    creds_info = json.loads(fixed)
    logging.info("✅ JSON 파싱(복원) 성공")

if not creds_info.get("private_key",
                      "").startswith("-----BEGIN PRIVATE KEY-----"):
    raise ValueError("❌ 잘못된 서비스 계정 JSON입니다. "
                     "환경변수에 전체 JSON을 정확히 복사했는지, "
                     "서비스 계정 이메일이 스프레드시트에 공유되어 있는지 확인하세요.")

# ── Google Sheets 인증 ────────────────────────────────────────────────────────────
creds = GCredentials.from_service_account_info(
    creds_info,
    scopes=[
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/spreadsheets"
    ])
gclient = gspread.authorize(creds)
logging.info("✅ Google Sheets 인증 완료")


# ── 본문 추출 함수 ─────────────────────────────────────────────────────────────────
def extract_main_text(url):
    try:
        html = requests.get(url, timeout=10).text
        soup = BeautifulSoup(html, 'html.parser')
        for tag in soup(['script', 'style', 'nav', 'footer', 'aside']):
            tag.decompose()
        return ' '.join(soup.stripped_strings)[:5000]
    except Exception as e:
        logging.warning(f"[본문 추출 실패] {e}")
        return None


# ── 프롬프트 선택 함수 ─────────────────────────────────────────────────────────────
def get_raindrop_prompt_by_tag(tags):
    sheet = gclient.open_by_key(GSHEET_ID).worksheet("prompt")
    rows = sheet.get_all_values()
    domestic_tag = "국내지원사업"
    domestic_prompt = None
    global_prompt = None
    for row in rows[1:]:
        if len(row) >= 9 and row[1].strip().lower(
        ) == "raindrop" and row[3].strip().upper() == "Y":
            prompt_data = {
                "role": row[4],
                "conditions": row[5],
                "structure": row[6],
                "must_include": row[7],
                "conclusion": row[8],
                "extra": row[9] if len(row) > 9 else ""
            }
            if row[2].strip() == domestic_tag:
                domestic_prompt = prompt_data
            else:
                global_prompt = prompt_data
    if any(domestic_tag in t for t in tags):
        return domestic_prompt or global_prompt
    return global_prompt or domestic_prompt


# ── GPT 요약 생성 ─────────────────────────────────────────────────────────────────
def generate_blog_style_summary(title, url, text, tags):
    prompt_data = get_raindrop_prompt_by_tag(tags)
    if not prompt_data:
        return "[프롬프트 정보 없음]"
    prompt = f"""{prompt_data['role']}

✍️ 작성 조건:
{prompt_data['conditions']}

🧭 글 구성 방식:
{prompt_data['structure']}

📌 반드시 포함할 항목:
{prompt_data['must_include']}

🎯 마무리 문장:
{prompt_data['conclusion']}

📎 추가 지시사항:
{prompt_data['extra']}

---
지원사업 제목: {title}
스크랩한 본문:
{text}
"""
    for _ in range(3):
        try:
            resp = client.ChatCompletion.create(model=GPT_MODEL,
                                                messages=[{
                                                    "role": "user",
                                                    "content": prompt
                                                }],
                                                max_tokens=2500,
                                                temperature=0.7)
            return resp.choices[0].message.content.strip()
        except Exception as e:
            logging.warning(f"GPT 생성 실패: {e}")
            time.sleep(3)
    return "[GPT 생성 실패]"


# ── Raindrop API 호출 및 처리 ────────────────────────────────────────────────────
def fetch_and_process_raindrop():
    auth_header = {"Authorization": f"Bearer {RAINDROP_TOKEN}"}

    # 1) 컬렉션 목록 로드 (ID → 제목)
    coll_map = {}
    try:
        coll_res = requests.get("https://api.raindrop.io/rest/v1/collections",
                                headers=auth_header)
        if coll_res.status_code == 200:
            for c in coll_res.json().get("items", []):
                cid_val = c.get("id") or c.get("_id") or c.get("$id")
                cid = str(cid_val) if cid_val is not None else ""
                coll_map[cid] = c.get("title", "")
        else:
            logging.warning("❌ 컬렉션 로드 실패: %s", coll_res.status_code)
    except Exception as e:
        logging.error("❌ 컬렉션 API 호출 오류: %s", e)

    # 2) Raindrop 항목 로드
    res = requests.get("https://api.raindrop.io/rest/v1/raindrops/0",
                       headers=auth_header)
    if res.status_code != 200:
        raise Exception(f"Raindrop API 호출 실패: {res.text}")
    items = res.json().get('items', [])

    # 3) 시트 및 헤더 설정
    sheet = gclient.open_by_key(GSHEET_ID).worksheet("support business")
    header_row = ["작성일시", "제목", "요약", "링크", "태그", "사이트 분류", "컬렉션 ID"]
    sheet.update(values=[header_row], range_name='A1:G1')

    existing_links = set(sheet.col_values(4)[1:])
    added = 0

    for item in items:
        title = item.get("title")
        link = item.get("link")
        tags = item.get("tags", [])
        if not (title and link and tags):
            continue
        if link in existing_links:
            continue

        content = extract_main_text(link)
        if not content:
            continue

        summary = generate_blog_style_summary(title, link, content, tags)
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        tag_str = ", ".join(tags)

        # 4) 컬렉션 ID 및 이름 매핑
        coll = item.get("collection")
        if isinstance(coll, dict):
            raw_id = coll.get("id") or coll.get("_id") or coll.get("$id")
            cid = str(raw_id) if raw_id is not None else ""
        else:
            cid = str(coll or "")
        cname = coll_map.get(cid, "(unknown)")

        # 5) 행 추가
        row = [now, title, summary, link, tag_str, cname, cid]
        sheet.append_row(row)
        existing_links.add(link)
        added += 1
        logging.info(f"➕ 추가됨: {title} (사이트 분류: {cname}, ID: {cid})")

    logging.info(f"✅ 처리 완료: {added}개 추가")
    return added


if __name__ == "__main__":
    fetch_and_process_raindrop()
