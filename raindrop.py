import os, json, datetime, time, requests, logging, gspread
from bs4 import BeautifulSoup
from google.oauth2.service_account import Credentials as GCredentials
from dotenv import load_dotenv
from openai import OpenAI

# 📌 환경변수 로드 및 설정
load_dotenv()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# 📌 환경변수 불러오기
RAINDROP_TOKEN = os.getenv("RAINDROP_TOKEN")
GSHEET_CREDENTIALS_JSON = os.getenv("GSHEET_CREDENTIALS_JSON")
GSHEET_ID = os.getenv("GSHEET_ID")
GPT_MODEL = "gpt-3.5-turbo"

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

def get_raindrop_prompt_by_tag(tags):
    creds_dict = json.loads(GSHEET_CREDENTIALS_JSON)
    creds = GCredentials.from_service_account_info(creds_dict, scopes=["https://www.googleapis.com/auth/spreadsheets"])
    gclient = gspread.authorize(creds)
    sheet = gclient.open_by_key(GSHEET_ID).worksheet("prompt")
    rows = sheet.get_all_values()

    domestic_tag = "국내지원사업"
    domestic_prompt = None
    global_prompt = None

    for row in rows[1:]:
        if len(row) >= 9 and row[1].strip().lower() == "raindrop" and row[3].strip().upper() == "Y":
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

    # 태그에 따라 프롬프트 분기
    if domestic_tag in tags:
        return domestic_prompt or global_prompt
    else:
        return global_prompt or domestic_prompt

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
            res = client.chat.completions.create(
                model=GPT_MODEL,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=2500,
                temperature=0.7
            )
            return res.choices[0].message.content.strip()
        except Exception as e:
            logging.warning(f"GPT 생성 실패, 재시도 중: {e}")
            time.sleep(3)
    return "[GPT 생성 실패]"

def append_to_fixed_sheet(row):
    creds_dict = json.loads(GSHEET_CREDENTIALS_JSON)
    creds = GCredentials.from_service_account_info(creds_dict, scopes=["https://www.googleapis.com/auth/spreadsheets"])
    gclient = gspread.authorize(creds)
    sheet = gclient.open_by_key(GSHEET_ID).worksheet("support business")
    existing_titles = set(sheet.col_values(2))
    if row[1] not in existing_titles:
        sheet.append_row(row)

def fetch_and_process_raindrop():
    headers = {"Authorization": f"Bearer {RAINDROP_TOKEN}"}
    res = requests.get("https://api.raindrop.io/rest/v1/raindrops/0", headers=headers)
    if res.status_code != 200:
        raise Exception(f"Raindrop API 호출 실패: {res.text}")

    items = res.json().get("items", [])
    added = 0
    for item in items:
        title = item.get("title")
        link = item.get("link")
        tags = item.get("tags", [])
        if not title or not link or not tags:
            continue
        content = extract_main_text(link)
        if not content:
            continue
        summary = generate_blog_style_summary(title, link, content, tags)
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        tag_string = ", ".join(tags)
        row = [now, title, summary, link, tag_string]
        append_to_fixed_sheet(row)
        added += 1
    return added

if __name__ == "__main__":
    fetch_and_process_raindrop()
