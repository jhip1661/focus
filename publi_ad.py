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
import openai  # ✅ OpenAI 모듈 전체 import

# 🔐 환경 변수에서 JSON 문자열 읽고 줄바꿈 처리
# Modified: handle both escaped '\n' and literal newlines in SERVICE_ACCOUNT JSON
RAW_CREDENTIALS_JSON = os.getenv("GSHEET_CREDENTIALS_JSON", "")
try:
    # Try loading directly (expects escaped newlines)
    creds_info = json.loads(RAW_CREDENTIALS_JSON)
except json.JSONDecodeError:
    # Fallback: escape any literal newlines, tabs, or carriage returns
    sanitized = RAW_CREDENTIALS_JSON
    sanitized = sanitized.replace('\n', '\\n')
    sanitized = sanitized.replace('\t', '')
    sanitized = sanitized.replace('\r', '')
    creds_info = json.loads(sanitized)
# After parsing JSON, replace escaped '\n' in private_key with actual newline
if 'private_key' in creds_info:
    creds_info['private_key'] = creds_info['private_key'].replace('\\n', '\n')

SOURCE_DB_ID   = os.getenv("SOURCE_DB_ID")
TARGET_DB_ID   = os.getenv("TARGET_DB_ID")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

SIMILARITY_THRESHOLD = 0.6
MAX_RETRIES          = 5
SELECT_COUNT         = 5

# 🔑 OpenAI 클라이언트 설정
if not OPENAI_API_KEY:
    raise ValueError("❌ 환경변수 'OPENAI_API_KEY'가 없습니다.")
openai.api_key = OPENAI_API_KEY
client = openai


def init_worksheet(sheet_id: str, sheet_name: str, header: List[str] = None):
    """
    Initialize or create a worksheet, with robust JSON credential handling.
    Uses creds_info prepared at import time.
    """
    scope = [
        'https://spreadsheets.google.com/feeds',
        'https://www.googleapis.com/auth/drive'
    ]
    # Use parsed creds_info dict to create credentials
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_info, scope)
    gs = gspread.authorize(creds)
    try:
        ws = gs.open_by_key(sheet_id).worksheet(sheet_name)
    except gspread.exceptions.WorksheetNotFound:
        ws = gs.open_by_key(sheet_id).add_worksheet(
            title=sheet_name, rows="1000", cols="20")
    if header:
        if ws.row_values(1) != header:
            ws.clear()
            ws.append_row(header)
    return ws


def calculate_similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def clean_content(text: str) -> str:
    return re.sub(r'(?m)^(서론|문제 상황|실무 팁|결론)[:\-]?\s*', '', text).strip()


def build_messages_from_prompt(cfg: List[str], title: str, content: str) -> List[dict]:
    purpose, tone, para, emphasis, fmt, etc = cfg
    system = f"""{purpose}

{tone}

{para}

{emphasis}

{fmt}

{etc}"""
    user = f"""다음 글을 중복되지 않도록 재작성해줘:

제목: {title}
내용: {content}"""
    return [
        {"role": "system", "content": system.strip()},
        {"role": "user",   "content": user.strip()}
    ]


def regenerate_unique_post(
    original_title: str,
    original: str,
    existing_texts: List[str],
    prompt_text_cfg: List[str],
    model_name: str
) -> Tuple[str, float, int]:
    messages = build_messages_from_prompt(prompt_text_cfg, original_title, original)
    regen, score = original, 1.0
    for attempt in range(1, MAX_RETRIES + 1):
        max_tokens = 3000
        mlower = model_name.lower()
        if '2500자' in mlower:
            max_tokens = 2500
        elif '2000자' in mlower:
            max_tokens = 2000
        mname = model_name.strip().lower()
        if mname in ('', 'none'):
            mname = 'gpt-3.5-turbo'
        logging.info(f"▶️ 모델 호출: {mname}")  # Debug: show exact model
        try:
            resp = client.ChatCompletion.create(
                model=mname,
                messages=messages,
                temperature=0.8,
                max_tokens=max_tokens
            )
        except openai.error.InvalidRequestError as e:
            logging.error(f"❌ Invalid model '{mname}', fallback to 'gpt-3.5-turbo': {e}")
            resp = client.ChatCompletion.create(
                model='gpt-3.5-turbo',
                messages=messages,
                temperature=0.8,
                max_tokens=max_tokens
            )
        candidate = clean_content(resp.choices[0].message.content)
        sim = max(calculate_similarity(candidate, ex) for ex in existing_texts) if existing_texts else 0
        if sim < SIMILARITY_THRESHOLD:
            return candidate, sim, attempt
        regen, score = candidate, sim
    return regen, score, MAX_RETRIES


def regenerate_title(content: str) -> str:
    system = "너는 마케팅 콘텐츠 전문가야. 아래 내용을 보고 클릭을 유도하는 짧은 제목을 작성해줘."
    resp = client.ChatCompletion.create(
        model="gpt-3.5-turbo",
        messages=[
            {"role": "system", "content": system},
            {"role": "user",   "content": content[:1000]}
        ],
        temperature=0.7,
        max_tokens=800
    )
    return re.sub(r'^.*?:\s*', '', resp.choices[0].message.content.strip())


def extract_tags(text: str) -> List[str]:
    prompt = f"다음 글에서 실무 중심 명사 5개를 해시태그(#키워드) 형태로 추출해줘. 글: {text}"
    resp = client.ChatCompletion.create(
        model="gpt-3.5-turbo",
        messages=[
            {"role": "system", "content": "당신은 태그 추출 전문가입니다."},
            {"role": "user",   "content": prompt}
        ],
        temperature=0,
        max_tokens=50
    )
    return re.findall(r'#(\w+)', resp.choices[0].message.content)[:5]


def translate_text(text: str, lang: str) -> str:
    langs = {"English": "English", "Chinese": "Simplified Chinese", "Japanese": "Japanese"}
    target = langs.get(lang, lang)
    resp = client.ChatCompletion.create(
        model="gpt-3.5-turbo",
        messages=[
            {"role": "system", "content": f"다음을 {target}로 번역해줘."},
            {"role": "user",   "content": text}
        ],
        temperature=0.5,
        max_tokens=2000
    )
    return resp.choices[0].message.content.strip()


def find_matching_image(tags: List[str], image_ws) -> str:
    for row in image_ws.get_all_values()[1:]:
        if any(tag in row[0] for tag in tags):
            return row[1]
    return ""


def now_str() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def extract_valid_prompt(prompt_ws, source: str, site_category: str) -> List[List[str]]:
    valid = []
    for r in prompt_ws.get_all_values()[1:]:
        if (r[1].strip() == source and
            r[2].strip() == site_category and
            r[4].strip().lower() not in ('', 'none') and r[4].strip() == 'Y'):
            valid.append(r[5:15])  # F~O
    return valid


def pick_rows(src_ws, count=SELECT_COUNT) -> List[List[str]]:
    rows = src_ws.get_all_values()[1:]
    today = datetime.datetime.now().date()
    valid = []
    for r in rows:
        try:
            deadline = datetime.datetime.strptime(r[1], "%Y-%m-%d").date()
            if deadline >= today:
                valid.append(r)
        except Exception:
            continue
    return random.sample(valid, min(count, len(valid))) if valid else []


def process_regeneration():
    logging.basicConfig(level=logging.INFO)
    logging.info("📌 process_regeneration() 시작")

    src_ws    = init_worksheet(SOURCE_DB_ID, "marketing")
    prompt_ws = init_worksheet(SOURCE_DB_ID, "prompt")
    image_ws  = init_worksheet(SOURCE_DB_ID, "image")
    info_ws   = init_worksheet(
        TARGET_DB_ID, "advertising",
        ["작성일시","제목","내용","태그","영문","중문","일문","표절률","이미지url"]
    )

    selected = pick_rows(src_ws)
    if not selected:
        logging.warning("⚠️ 유효한 마케팅 콘텐츠가 없습니다 (마감일 지남).")
        return 0

    all_texts = [r[4] for r in selected]
    total_tokens = 0

    for row in selected:
        site_cat = row[2].strip()
        prompts = extract_valid_prompt(prompt_ws, source="marketing", site_category=site_cat)
        if not prompts:
            logging.warning(f"⚠️ '{site_cat}'에 대응하는 활성 프롬프트가 없습니다.")
            continue
        config     = random.choice(prompts)
        prompt_cfg = config[:6]
        mode_raw   = config[6].strip().lower()  # L
        gap        = int(config[7]) if config[7].isdigit() else 0  # M
        basic      = config[8].strip().lower()
        adv        = config[9].strip().lower()

        # Determine mode and models
        is_hybrid    = (mode_raw == '하이브리드')
        basic_model  = 'gpt-3.5-turbo' if basic in ('', 'none') else basic
        adv_model    = basic_model if adv in ('', 'none') else adv
        models       = [basic_model]
        if is_hybrid:
            models = [basic_model] * gap + [adv_model]

        for model_name in models:
            logging.info(f"▶️ Using model '{model_name}'")
            content, score, tries = regenerate_unique_post(
                row[0], row[4], all_texts, prompt_cfg, model_name
            )
            total_tokens += tries * 3000
            title = regenerate_title(content)
            tags  = extract_tags(content)
            en    = translate_text(content, "English")
            zh    = translate_text(content, "Chinese")
            ja    = translate_text(content, "Japanese")
            img   = find_matching_image(tags, image_ws)

            info_ws.append_row([
                now_str(), title, content,
                ", ".join(tags), en, zh, ja,
                f"{score:.2f}", img
            ])
            logging.info(f"✅ '{title}' ({model_name}) 저장 완료 | 유사도: {score:.2f} | 재시도: {tries}회")

    logging.info(f"💰 예상 비용: ${round(total_tokens/1000*0.0015,4)}")
    return len(selected)

if __name__ == "__main__":
    process_regeneration()
