import os
import json
import datetime
import time
import logging
import requests
import gspread
from bs4 import BeautifulSoup
from google.oauth2.service_account import Credentials as GCredentials
import openai

# ── 로깅 설정 ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ── 환경변수 로드 ─────────────────────────────────────────────────────────────────
RAW_JSON       = os.getenv("GSHEET_CREDENTIALS_JSON")
RAINDROP_TOKEN = os.getenv("RAINDROP_TOKEN")
GSHEET_ID      = os.getenv("GSHEET_ID")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GPT_MODEL      = "gpt-3.5-turbo"

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
except json.JSONDecodeError:
    creds_info = json.loads(RAW_JSON.replace('\\n', '\n'))

if not creds_info.get("private_key", "").startswith("-----BEGIN PRIVATE KEY-----"):
    raise ValueError("❌ 잘못된 서비스 계정 JSON입니다.")

# ── Google Sheets 인증 ────────────────────────────────────────────────────────────
creds   = GCredentials.from_service_account_info(
    creds_info,
    scopes=["https://www.googleapis.com/auth/drive", "https://www.googleapis.com/auth/spreadsheets"]
)
gclient = gspread.authorize(creds)
logging.info("✅ Google Sheets 인증 완료")

# ── 시트 캐싱 변수 ───────────────────────────────────────────────────────────────
cached_prompt_rows = None

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
def get_raindrop_prompt_by_tag(site_category, tag):
    global cached_prompt_rows
    if cached_prompt_rows is None:
        sheet = gclient.open_by_key(GSHEET_ID).worksheet("prompt")
        # A:생성일자, B:출처, C:사이트분류, D:Tag, E:현재사용여부, F~K:프롬프트 항목
        cached_prompt_rows = sheet.get_values("A1:K100")

    for row in cached_prompt_rows[1:]:
        if len(row) < 11:
            continue

        source    = row[1].strip().lower()   # B열: 출처
        site_cat  = row[2].strip()           # C열: 사이트분류
        tag_val   = row[3].strip()           # D열: Tag
        use_flag  = row[4].strip().upper()   # E열: 현재사용여부

        if (
            source    == "raindrop" and
            site_cat  == site_category and
            tag_val   == tag and
            use_flag  == "Y"
        ):
            return {
                "role":         row[5],  # F열: 작성자 역할 설명
                "conditions":   row[6],  # G열: 전체 작성 조건
                "structure":    row[7],  # H열: 글 구성방식
                "must_include": row[8],  # I열: 필수 포함 항목
                "conclusion":   row[9],  # J열: 마무리 문장
                "extra":        row[10]  # K열: 추가 지시사항
            }

    logging.warning(f"[프롬프트 매칭 실패] site_category='{site_category}', tag='{tag}'")
    return None

# ── GPT 요약 생성 ─────────────────────────────────────────────────────────────────
def generate_blog_style_summary(title, url, text, tags, site_category):
    tag = tags[0] if tags else ""
    prompt_data = get_raindrop_prompt_by_tag(site_category, tag)
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
            resp = client.ChatCompletion.create(
                model=GPT_MODEL,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=2500,
                temperature=0.7
            )
            return resp.choices[0].message.content.strip()
        except Exception as e:
            logging.warning(f"GPT 생성 실패: {e}")
            time.sleep(3)

    return "[GPT 생성 실패]"

# ── Raindrop API 호출 및 처리 ────────────────────────────────────────────────────
def fetch_and_process_raindrop():
    auth_header = {"Authorization": f"Bearer {RAINDROP_TOKEN}"}

    # 1) 컬렉션 제목 매핑
    coll_map = {}
    try:
        coll_res = requests.get("https://api.raindrop.io/rest/v1/collections", headers=auth_header)
        coll_res.raise_for_status()
        for c in coll_res.json().get("items", []):
            cid_val = c.get("id") or c.get("_id") or c.get("$id")
            cid = str(cid_val) if cid_val is not None else ""
            coll_map[cid] = c.get("title", "")
    except Exception as e:
        logging.error("❌ 컬렉션 API 호출 오류: %s", e)

    # 2) Raindrop 항목 불러오기
    res = requests.get("https://api.raindrop.io/rest/v1/raindrops/0", headers=auth_header)
    res.raise_for_status()
    items = res.json().get('items', [])

    # 3) 시트 초기화
    sheet = gclient.open_by_key(GSHEET_ID).worksheet("support business")
    sheet.update(
        values=[["작성일시","제목","요약","링크","태그","사이트분류","컬렉션 ID"]],
        range_name='A1:G1'
    )
    existing_links = set(sheet.col_values(4)[1:])
    added = 0

    for item in items:
        title = item.get("title")
        link  = item.get("link")
        tags  = item.get("tags", [])
        if not (title and link and tags) or link in existing_links:
            continue

        content = extract_main_text(link)
        if not content:
            continue

        # ▶ 컬렉션 ID & 이름 추출 (collection.$id)
        coll = item.get("collection")
        if isinstance(coll, dict):
            raw_id = coll.get("id") or coll.get("_id") or coll.get("$id")
        else:
            raw_id = coll
        cid = str(raw_id) if raw_id is not None else ""
        cname = coll_map.get(cid, "")

        summary = generate_blog_style_summary(title, link, content, tags, cname)

        now    = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        tag_str = ", ".join(tags)
        row    = [now, title, summary, link, tag_str, cname, cid]

        sheet.append_row(row)
        existing_links.add(link)
        added += 1
        logging.info(f"➕ 추가됨: {title} (사이트분류: '{cname}', ID: {cid})")

    logging.info(f"✅ 처리 완료: {added}개 추가됨")
    return added

if __name__ == "__main__":
    fetch_and_process_raindrop()
