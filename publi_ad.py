import os
import json
import datetime
import random
import logging
import re
from typing import List, Tuple
import difflib

import gspread
from oauth2client.service_account import ServiceAccountCredentials
import openai  # ✅ 수정된 부분

# ✅ 환경변수 직접 사용 + \n 복원 처리
CREDENTIALS_JSON = os.getenv("GSHEET_CREDENTIALS_JSON", "").replace('\\n', '\n')
SOURCE_DB_ID = os.getenv("SOURCE_DB_ID")
TARGET_DB_ID = os.getenv("TARGET_DB_ID")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

openai.api_key = OPENAI_API_KEY  # ✅ API 키 설정

SIMILARITY_THRESHOLD = 0.6
MAX_RETRIES = 5
SELECT_COUNT = 5

def init_worksheet(sheet_id: str, sheet_name: str, header: List[str] = None):
    scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
    credentials_dict = json.loads(CREDENTIALS_JSON)
    creds = ServiceAccountCredentials.from_json_keyfile_dict(credentials_dict, scope)
    client_gs = gspread.authorize(creds)
    try:
        ws = client_gs.open_by_key(sheet_id).worksheet(sheet_name)
    except gspread.exceptions.WorksheetNotFound:
        ws = client_gs.open_by_key(sheet_id).add_worksheet(title=sheet_name, rows="1000", cols="20")
    if header:
        current = ws.get_all_values()
        if not current or all(cell == '' for cell in current[0]):
            ws.clear()
            ws.append_row(header)
    return ws

def calculate_similarity(text1: str, text2: str) -> float:
    return difflib.SequenceMatcher(None, text1, text2).ratio()

def clean_content(text: str) -> str:
    return re.sub(r'(?m)^(서론|문제 상황|실무 팁|결론)[:\-]?\s*', '', text).strip()

def build_messages_from_prompt(prompt_config: List[str], title: str, content: str) -> List[dict]:
    purpose, tone, para, emphasis, format_, etc = prompt_config
    system_msg = f"""{purpose}\n\n{tone}\n\n{para}\n\n{emphasis}\n\n{format_}\n\n{etc}"""
    user_msg = f"""다음 글을 중복되지 않도록 재작성해줘:\n\n제목: {title}\n내용: {content}"""
    return [{"role": "system", "content": system_msg.strip()}, {"role": "user", "content": user_msg.strip()}]

def regenerate_unique_post(original_title: str, original: str, existing_texts: List[str], prompt_config: List[str]) -> Tuple[str, float, int]:
    for i in range(MAX_RETRIES):
        messages = build_messages_from_prompt(prompt_config, original_title, original)
        etc_lower = prompt_config[-1].lower()
        max_tokens = 3000 if "3000자" in etc_lower else 2500 if "2500자" in etc_lower else 2000
        resp = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=messages,
            temperature=0.8,
            max_tokens=max_tokens
        )
        regen = resp.choices[0].message.content.strip()
        regen = clean_content(regen)
        score = max(calculate_similarity(regen, t) for t in existing_texts)
        if score < SIMILARITY_THRESHOLD:
            return regen, score, i + 1
    return regen, score, MAX_RETRIES

def regenerate_title(content: str) -> str:
    system = "너는 광고, 홍보, 마케팅 콘텐츠 전문가야. 아래 내용을 보고 광고, 홍보, 마케팅을 흥미롭게 해서 소비자의 클릭을 유도하는 짧은 제목을 작성해줘."
    resp = openai.ChatCompletion.create(
        model="gpt-3.5-turbo",
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": content[:1000]}
        ],
        temperature=0.7,
        max_tokens=800
    )
    return re.sub(r'^.*?:\s*', '', resp.choices[0].message.content.strip())

def extract_tags(text: str) -> List[str]:
    prompt = f"다음 글에서 광고, 홍보, 마케팅의 중심 명사 5개를 해시태그(#키워드) 형태로 추출해줘. 글: {text}"
    resp = openai.ChatCompletion.create(
        model="gpt-3.5-turbo",
        messages=[
            {"role": "system", "content": "당신은 태그 추출 전문가입니다."},
            {"role": "user", "content": prompt}
        ],
        temperature=0,
        max_tokens=50
    )
    return re.findall(r'#(\w+)', resp.choices[0].message.content.strip())[:5]

def translate_text(text: str, lang: str) -> str:
    langs = {"English": "English", "Chinese": "Simplified Chinese", "Japanese": "Japanese"}
    target = langs.get(lang, lang)
    resp = openai.ChatCompletion.create(
        model="gpt-3.5-turbo",
        messages=[
            {"role": "system", "content": f"다음을 {target}로 번역해줘."},
            {"role": "user", "content": text}
        ],
        temperature=0.5,
        max_tokens=2000
    )
    return resp.choices[0].message.content.strip()

def find_matching_image(tags: List[str], image_ws) -> str:
    data = image_ws.get_all_values()[1:]
    for row in data:
        for tag in tags:
            if tag in row[0]:
                return row[1]
    return ""

def now_str() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def extract_valid_prompt(prompt_ws) -> List[List[str]]:
    rows = prompt_ws.get_all_values()[1:]
    return [r[4:10] for r in rows if r[1].strip() == 'marketing' and r[3].strip() == 'Y']

def process_regeneration():
    logging.basicConfig(level=logging.INFO)
    logging.info("📌 process_regeneration() 시작")

    src_ws = init_worksheet(SOURCE_DB_ID, "marketing")
    prompt_ws = init_worksheet(SOURCE_DB_ID, "prompt")
    image_ws = init_worksheet(SOURCE_DB_ID, "image")
    info_ws = init_worksheet(TARGET_DB_ID, "advertising",
        ["작성일시", "제목", "내용", "태그", "영문", "중문", "일문", "표절률", "이미지url"])

    rows = src_ws.get_all_values()[1:]
    filtered_rows = [r for r in rows if len(r) > 1 and r[1].strip().upper() == 'Y']

    selected = random.sample(filtered_rows, min(SELECT_COUNT, len(filtered_rows)))
    logging.info(f"🎯 대상 행 수: {len(selected)}")

    prompts = extract_valid_prompt(prompt_ws)
    if not prompts:
        logging.warning("⚠️ 사용 가능한 프롬프트 없음")
        return 0

    config = random.choice(prompts)
    all_texts = [r[2] for r in filtered_rows if len(r) > 2]
    total_tokens = 0

    for row in selected:
        original_content = row[2] if len(row) > 2 else ""
        emphasis = row[3] if len(row) > 3 else ""
        tag_text = row[4] if len(row) > 4 else ""
        image_url = row[5] if len(row) > 5 else ""

        if not original_content:
            logging.warning(f"⚠️ 본문 비어 있음: {row}")
            continue

        content, score, tries = regenerate_unique_post("", original_content, all_texts, config)
        total_tokens += tries * 3000
        new_title = regenerate_title(content)
        tags = extract_tags(content)
        en = translate_text(content, "English")
        zh = translate_text(content, "Chinese")
        ja = translate_text(content, "Japanese")
        img = image_url or find_matching_image(tags, image_ws)

        try:
            info_ws.append_row([
                now_str(), new_title, content,
                ", ".join(tags), en, zh, ja,
                f"{score:.2f}", img
            ])
            logging.info(f"✅ '{new_title}' 저장 완료 | 유사도: {score:.2f} | 재시도: {tries}회")
        except Exception as e:
            logging.error(f"❌ 시트 쓰기 실패: {e}")

    logging.info(f"💰 예상 비용: ${round(total_tokens / 1000 * 0.0015, 4)}")
    return len(selected)

if __name__ == "__main__":
    process_regeneration()
