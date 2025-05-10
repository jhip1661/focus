import os
import json
import datetime
import random
import logging
import re
import difflib
from typing import List, Tuple

import gspread
from google.oauth2.service_account import Credentials as GCredentials
import openai  # ✅ 변경: 모듈 전체 import

# ── 서비스 계정 JSON 로드 & 검증 ──────────────────────────────────────────────
CREDENTIALS_JSON = os.getenv("GSHEET_CREDENTIALS_JSON", "")
if not CREDENTIALS_JSON:
    raise ValueError("❌ 환경변수 'GSHEET_CREDENTIALS_JSON'가 누락되었습니다.")
try:
    creds_info = json.loads(CREDENTIALS_JSON)
except json.JSONDecodeError as e:
    raise ValueError(f"❌ SERVICE_ACCOUNT_JSON 파싱 실패: {e}")
if "private_key" not in creds_info or not creds_info["private_key"].startswith(
        "-----BEGIN PRIVATE KEY-----"):
    raise ValueError("❌ 잘못된 서비스 계정 JSON입니다.")

# ▶ Google Sheets/Drive 인증 객체 생성
SCOPES = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]
gs_creds = GCredentials.from_service_account_info(creds_info, scopes=SCOPES)

# ✅ OpenAI API 키 설정
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    raise ValueError("❌ 환경변수 'OPENAI_API_KEY'가 누락되었습니다.")
openai.api_key = OPENAI_API_KEY

# 상수 정의
SIMILARITY_THRESHOLD = 0.6
MAX_RETRIES          = 5
SOURCE_DB_ID         = os.getenv("SOURCE_DB_ID")
TARGET_DB_ID         = os.getenv("TARGET_DB_ID")


def init_worksheet(sheet_id: str, sheet_name: str, header: List[str] = None):
    ws = gspread.authorize(gs_creds).open_by_key(sheet_id).worksheet(sheet_name)
    if header:
        first = ws.get_all_values()[:1]
        if not first or all(cell == "" for cell in first[0]):
            ws.clear()
            ws.append_row(header)
    return ws


def calculate_similarity(text1: str, text2: str) -> float:
    return difflib.SequenceMatcher(None, text1, text2).ratio()


def clean_content(text: str) -> str:
    return re.sub(r'(?m)^(서론|문제 상황|실무 팁|결론)[:\-]?\s*', "", text).strip()


def build_messages_from_prompt(
    prompt_config: List[str],
    title: str,
    content: str
) -> List[dict]:
    purpose, tone, para, emphasis, format_, etc = prompt_config
    system_msg = f"""{purpose}

{tone}

{para}

{emphasis}

{format_}

{etc}"""
    user_msg = f"""다음 글을 중복되지 않도록 재작성해줘:

제목: {title}
내용: {content}"""
    return [
        {"role": "system", "content": system_msg.strip()},
        {"role": "user",   "content": user_msg.strip()},
    ]


def regenerate_unique_post(
    original_title: str,
    original: str,
    existing_texts: List[str],
    prompt_config: List[str],
    model: str,
) -> Tuple[str, float, int]:
    regen, score = original, 1.0
    for i in range(1, MAX_RETRIES + 1):
        messages = build_messages_from_prompt(
            prompt_config, original_title, original
        )
        etc_lower = prompt_config[-1].lower()
        if "3000자" in etc_lower:
            max_tokens = 3000
        elif "2500자" in etc_lower:
            max_tokens = 2500
        elif "2000자" in etc_lower:
            max_tokens = 2000
        else:
            max_tokens = 3000

        # 🔧 openai module 호출로 변경
        resp = openai.ChatCompletion.create(
            model=model,
            messages=messages,
            temperature=0.8,
            max_tokens=max_tokens,
        )
        candidate = clean_content(resp.choices[0].message.content.strip())
        sim = max(calculate_similarity(candidate, t) for t in existing_texts)
        if sim < SIMILARITY_THRESHOLD:
            return candidate, sim, i
        regen, score = candidate, sim

    return regen, score, MAX_RETRIES


def regenerate_title(content: str) -> str:
    system = "너는 마케팅 콘텐츠 전문가야. 아래 내용을 보고 클릭을 유도하는 짧은 제목을 작성해줘."
    resp = openai.ChatCompletion.create(
        model="gpt-3.5-turbo",
        messages=[
            {"role": "system", "content": system},
            {"role": "user",   "content": content[:1000]},
        ],
        temperature=0.7,
        max_tokens=800,
    )
    return re.sub(r"^.*?:\s*", "", resp.choices[0].message.content.strip())


def extract_tags(text: str) -> List[str]:
    prompt = f"다음 글에서 실무 중심 명사 5개를 해시태그(#키워드) 형태로 추출해줘. 글: {text}"
    resp = openai.ChatCompletion.create(
        model="gpt-3.5-turbo",
        messages=[
            {"role": "system", "content": "당신은 태그 추출 전문가입니다."},
            {"role": "user",   "content": prompt},
        ],
        temperature=0,
        max_tokens=50,
    )
    return re.findall(r"#(\w+)", resp.choices[0].message.content.strip())[:5]


def translate_text(text: str, lang: str) -> str:
    langs  = {"English": "English", "Chinese": "Simplified Chinese", "Japanese": "Japanese"}
    target = langs.get(lang, lang)
    resp = openai.ChatCompletion.create(
        model="gpt-3.5-turbo",
        messages=[
            {"role": "system", "content": f"다음을 {target}로 번역해줘."},
            {"role": "user",   "content": text},
        ],
        temperature=0.5,
        max_tokens=2000,
    )
    return resp.choices[0].message.content.strip()


def find_matching_image(tags: List[str], image_ws) -> str:
    data = image_ws.get_all_values()[1:]
    for row in data:
        if any(tag in row[0] for tag in tags):
            return row[1]
    return ""


def now_str() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def process_regeneration():
    logging.basicConfig(level=logging.INFO)
    logging.info("📌 process_regeneration() 시작")

    src_ws    = init_worksheet(SOURCE_DB_ID, "support business")
    prompt_ws = init_worksheet(SOURCE_DB_ID, "prompt")
    image_ws  = init_worksheet(SOURCE_DB_ID, "image")
    info_ws   = init_worksheet(
        TARGET_DB_ID, "information",
        ["작성일시","origin tag","사이트 분류","제목","내용","태그","영문","중문","일문","표절률","이미지url"]
    )

    rows    = src_ws.get_all_values()[1:]
    prompts = prompt_ws.get_all_values()[1:]
    total   = 0

    for row in rows:
        origin_tag    = row[4] if len(row) > 4 else ""
        site_category = row[5] if len(row) > 5 else ""

        candidates = [
            p for p in prompts
            if len(p) >= 15
            and p[2].strip() == site_category
            and p[3].strip() == origin_tag
            and p[4].strip().upper() == "Y"
        ]
        if not candidates:
            logging.warning(f"⚠️ 매칭되는 프롬프트 없음: site={site_category}, tag={origin_tag}")
            continue

        cfg           = random.choice(candidates)
        prompt_config = cfg[5:11]
        method        = cfg[11]
        interval      = int(cfg[12])
        basic_model   = cfg[13]
        advanced_model= cfg[14]

        count = interval + 1 if method == "하이브리드" else interval
        selected = random.sample(rows, min(count, len(rows)))

        for idx, item in enumerate(selected, start=1):
            use_model      = advanced_model if (method == "하이브리드" and idx == count) else basic_model
            original_title = item[1] if len(item) > 1 else ""
            original       = item[2] if len(item) > 2 else ""

            content, score, _ = regenerate_unique_post(
                original_title, original,
                [r[2] for r in rows if len(r) > 2],
                prompt_config, use_model
            )
            new_title = regenerate_title(content)
            tags      = extract_tags(content)
            en        = translate_text(content, "English")
            zh        = translate_text(content, "Chinese")
            ja        = translate_text(content, "Japanese")
            img       = find_matching_image(tags, image_ws)

            info_ws.append_row([
                now_str(), origin_tag, site_category, new_title, content,
                ", ".join(tags), en, zh, ja, f"{score:.2f}", img
            ])
            total += 1

    logging.info(f"💰 총 저장된 글 수: {total}")
    return total


if __name__ == "__main__":
    process_regeneration()
