import os
import json
import datetime
import random
import logging
import re
from typing import List, Tuple
import difflib

import gspread
from google.oauth2.service_account import Credentials as GCredentials
import openai  # ✅ OpenAI 모듈 전체 import

# ── 서비스 계정 JSON 로드 & 검증 ──────────────────────────────────────────────
CREDENTIALS_JSON = os.getenv("GSHEET_CREDENTIALS_JSON", "")
if not CREDENTIALS_JSON:
    raise ValueError("❌ 환경변수 'GSHEET_CREDENTIALS_JSON'이 누락되었습니다.")
try:
    creds_info = json.loads(CREDENTIALS_JSON)
except json.JSONDecodeError as e:
    raise ValueError(f"❌ SERVICE_ACCOUNT_JSON 파싱 실패: {e}")
if ("private_key" not in creds_info or
        not creds_info["private_key"].startswith("-----BEGIN PRIVATE KEY-----")):
    raise ValueError(
        "❌ 잘못된 서비스 계정 JSON입니다. 환경변수에 전체 JSON이 정확히 복사되었는지, "
        "서비스 계정 이메일이 시트에 공유되어 있는지 확인하세요."
    )

# ▶ Google Sheets/Drive 인증 객체 생성
GS_SCOPES = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]
gs_creds = GCredentials.from_service_account_info(creds_info, scopes=GS_SCOPES)

# 🚩 OpenAI 키 설정
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    raise ValueError("❌ 환경변수 'OPENAI_API_KEY'가 누락되었습니다.")
openai.api_key = OPENAI_API_KEY
client = openai

# ── 상수 정의 ─────────────────────────────────────────────────────────────
SOURCE_DB_ID = os.getenv("SOURCE_DB_ID")
TARGET_DB_ID = os.getenv("TARGET_DB_ID")
SIMILARITY_THRESHOLD = 0.6
MAX_RETRIES = 5


def init_worksheet(sheet_id: str, sheet_name: str, header: List[str] = None):
    ws = gspread.authorize(gs_creds).open_by_key(sheet_id).worksheet(sheet_name)
    if header:
        first = ws.get_all_values()[:1]
        if not first or all(cell == "" for cell in first[0]):
            ws.clear()
            ws.append_row(header)
    return ws


def calculate_similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def clean_content(text: str) -> str:
    return re.sub(r'(?m)^(서론|문제 상황|실무 팁|결론)[:\-]?\s*', "", text).strip()


def build_messages_from_prompt(
    prompt_cfg: List[str], title: str, content: str
) -> List[dict]:
    purpose, tone, para, emphasis, fmt, etc = prompt_cfg
    system = f"""{purpose}\n\n{tone}\n\n{para}\n\n{emphasis}\n\n{fmt}\n\n{etc}"""
    user = (
        f"다음 글을 중복되지 않도록 재작성해줘:\n\n제목: {title}\n내용: {content}"
    )
    return [
        {"role": "system", "content": system.strip()},
        {"role": "user", "content": user.strip()},
    ]


def regenerate_unique_post(
    original_title: str,
    original: str,
    existing_texts: List[str],
    prompt_cfg: List[str],
    model_name: str,
) -> Tuple[str, float, int]:
    regen, score = original, 1.0
    for i in range(1, MAX_RETRIES + 1):
        msgs = build_messages_from_prompt(prompt_cfg, original_title, original)
        etc_lower = prompt_cfg[-1].lower()
        if "3000자" in etc_lower:
            max_tokens = 3000
        elif "2500자" in etc_lower:
            max_tokens = 2500
        elif "2000자" in etc_lower:
            max_tokens = 2000
        else:
            max_tokens = 3000

        resp = client.ChatCompletion.create(
            model=model_name,
            messages=msgs,
            temperature=0.8,
            max_tokens=max_tokens,
        )
        candidate = clean_content(resp.choices[0].message.content.strip())
        sim = max(calculate_similarity(candidate, ex) for ex in existing_texts)
        if sim < SIMILARITY_THRESHOLD:
            return candidate, sim, i
        regen, score = candidate, sim
    return regen, score, MAX_RETRIES


def regenerate_title(content: str) -> str:
    system = "너는 마케팅 콘텐츠 전문가야. 아래 내용을 보고 클릭을 유도하는 짧은 제목을 작성해줘."
    resp = client.ChatCompletion.create(
        model="gpt-3.5-turbo",
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": content[:1000]},
        ],
        temperature=0.7,
        max_tokens=800,
    )
    title = resp.choices[0].message.content.strip()
    return re.sub(r"^.*?:\s*", "", title)


def extract_tags(text: str) -> List[str]:
    prompt = f"다음 글에서 실무 중심 명사 5개를 해시태그(#키워드) 형태로 추출해줘. 글: {text}"
    resp = client.ChatCompletion.create(...)
    return re.findall(r"#(\w+)", resp.choices[0].message.content.strip())[:5]


def translate_text(text: str, lang: str) -> str:
    langs = {"English": "English", "Chinese": "Simplified Chinese", "Japanese": "Japanese"}
    target = langs.get(lang, lang)
    resp = client.ChatCompletion.create(...)
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

    src_ws = init_worksheet(SOURCE_DB_ID, "xls")
    prompt_ws = init_worksheet(SOURCE_DB_ID, "prompt")
    image_ws = init_worksheet(SOURCE_DB_ID, "image")
    info_ws = init_worksheet(...)

    rows = src_ws.get_all_values()[1:]

    header = prompt_ws.row_values(1)
    if 'run_count' not in header:
        prompt_ws.add_cols(1)
        prompt_ws.update_cell(1, len(header) + 1, 'run_count')
        run_idx = len(header) + 1
    else:
        run_idx = header.index('run_count') + 1

    total = 0
    prompts = prompt_ws.get_all_values()[1:]
    for pr_idx, cfg in enumerate(prompts, start=2):
        # 필수 컬럼 체크: B='xls', C='정혜특허', E='재생산', F='Y'
        if len(cfg) < 15:
            continue
        if cfg[1].strip() != 'xls' or cfg[2].strip() != '정혜특허' or cfg[4].strip() != '재생산' or cfg[5].strip().upper() != 'Y':
            continue
        site_category = cfg[2].strip()
        origin_tag = cfg[3].strip()
        interval = int(cfg[12])
        basic_model = cfg[13]
        advanced_model = cfg[14]

        prev_count = int(cfg[run_idx - 1]) if len(cfg) >= run_idx and cfg[run_idx-1].isdigit() else 0

        matching = [r for r in rows if len(r)>5 and r[5].strip()==site_category and r[4].strip()==origin_tag]
        if not matching:
            logging.warning(f"⚠️ 매칭되는 소스 없음: site={site_category}, tag={origin_tag}")
            continue
        item = random.choice(matching)

        if prev_count < interval:
            use_model = basic_model
            new_count = prev_count + 1
        else:
            use_model = advanced_model
            new_count = 0

        original_title = item[1] if len(item)>1 else ""
        original = item[2] if len(item)>2 else ""
        content, score, _ = regenerate_unique_post(
            original_title, original,
            [r[2] for r in rows if len(r)>2],
            cfg[5:11], use_model
        )
        new_title = regenerate_title(content)
        tags = extract_tags(content)
        en = translate_text(content, "English")
        zh = translate_text(content, "Chinese")
        ja = translate_text(content, "Japanese")
        img = find_matching_image(tags, image_ws)

        info_ws.append_row([...])
        total += 1

        prompt_ws.update_cell(pr_idx, run_idx, str(new_count))

    logging.info(f"💰 총 저장된 글 수: {total}")
    return total

if __name__ == "__main__":
    process_regeneration()
