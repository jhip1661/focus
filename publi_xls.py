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
from openai import OpenAI  # 올바른 임포트

# 🔐 서비스 계정 JSON 로드 & 검증
CREDENTIALS_JSON = os.getenv("GSHEET_CREDENTIALS_JSON", "")
if not CREDENTIALS_JSON:
    raise ValueError("❌ 환경변수 'GSHEET_CREDENTIALS_JSON'이 누락되었습니다.")
try:
    creds_info = json.loads(CREDENTIALS_JSON)
except json.JSONDecodeError as e:
    raise ValueError(f"❌ SERVICE_ACCOUNT_JSON 파싱 실패: {e}")
if ("private_key" not in creds_info or
        not creds_info["private_key"].startswith("-----BEGIN PRIVATE KEY-----")
    ):
    raise ValueError("❌ 잘못된 서비스 계정 JSON입니다. 환경변수에 전체 JSON을 정확히 복사했는지, "
                     "서비스 계정 이메일이 시트에 공유되어 있는지 확인하세요.")

# ▶ Google Sheets/Drive 인증 객체 생성
GS_SCOPES = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]
try:
    gs_creds = GCredentials.from_service_account_info(creds_info,
                                                      scopes=GS_SCOPES)
except Exception as e:
    raise RuntimeError(f"❌ 인증 정보 로드 실패: {e}")

# 🔑 OpenAI 클라이언트 설정 (v1.x)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
if not OPENAI_API_KEY:
    raise ValueError("❌ 환경변수 'OPENAI_API_KEY'가 누락되었습니다.")
client = OpenAI(api_key=OPENAI_API_KEY)

# 🚩 상수
SOURCE_DB_ID = os.getenv("SOURCE_DB_ID")
TARGET_DB_ID = os.getenv("TARGET_DB_ID")
SIMILARITY_THRESHOLD = 0.6
MAX_RETRIES = 5
SELECT_COUNT = 5


def init_worksheet(sheet_id: str, sheet_name: str, header: List[str] = None):
    """워크시트 로드 혹은 생성 후, 헤더 세팅"""
    gs_client = gspread.authorize(gs_creds)
    try:
        ws = gs_client.open_by_key(sheet_id).worksheet(sheet_name)
    except gspread.exceptions.WorksheetNotFound:
        ws = gs_client.open_by_key(sheet_id).add_worksheet(title=sheet_name,
                                                           rows="1000",
                                                           cols="20")
    if header:
        current = ws.get_all_values()
        if not current or all(cell == "" for cell in current[0]):
            ws.clear()
            ws.append_row(header)
    return ws


def calculate_similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def clean_content(text: str) -> str:
    return re.sub(r'(?m)^(서론|문제 상황|실무 팁|결론)[:\-]?\s*', "", text).strip()


def build_messages_from_prompt(prompt_cfg: List[str], title: str,
                               content: str) -> List[dict]:
    purpose, tone, para, emphasis, fmt, etc = prompt_cfg
    system = f"""{purpose}

{tone}

{para}

{emphasis}

{fmt}

{etc}"""
    user = f"""다음 글을 중복되지 않도록 재작성해줘:

제목: {title}
내용: {content}"""
    return [{
        "role": "system",
        "content": system.strip()
    }, {
        "role": "user",
        "content": user.strip()
    }]


def regenerate_unique_post(original_title: str, original: str,
                           existing_texts: List[str],
                           prompt_cfg: List[str]) -> Tuple[str, float, int]:
    regen, score = original, 1.0
    for attempt in range(1, MAX_RETRIES + 1):
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

        resp = client.chat.completions.create(model="gpt-3.5-turbo",
                                              messages=msgs,
                                              temperature=0.8,
                                              max_tokens=max_tokens)
        candidate = clean_content(resp.choices[0].message.content.strip())
        sim = max(calculate_similarity(candidate, ex) for ex in existing_texts)
        if sim < SIMILARITY_THRESHOLD:
            return candidate, sim, attempt
        regen, score = candidate, sim

    return regen, score, MAX_RETRIES


def regenerate_title(content: str) -> str:
    system = "너는 마케팅 콘텐츠 전문가야. 아래 내용을 보고 클릭을 유도하는 짧은 제목을 작성해줘."
    resp = client.chat.completions.create(model="gpt-3.5-turbo",
                                          messages=[{
                                              "role": "system",
                                              "content": system
                                          }, {
                                              "role": "user",
                                              "content": content[:1000]
                                          }],
                                          temperature=0.7,
                                          max_tokens=800)
    title = resp.choices[0].message.content.strip()
    return re.sub(r'^.*?:\s*', '', title)


def extract_tags(text: str) -> List[str]:
    prompt = f"다음 글에서 실무 중심 명사 5개를 해시태그(#키워드) 형태로 추출해줘. 글: {text}"
    resp = client.chat.completions.create(model="gpt-3.5-turbo",
                                          messages=[{
                                              "role":
                                              "system",
                                              "content":
                                              "당신은 태그 추출 전문가입니다."
                                          }, {
                                              "role": "user",
                                              "content": prompt
                                          }],
                                          temperature=0,
                                          max_tokens=50)
    tags = re.findall(r'#(\w+)', resp.choices[0].message.content.strip())
    return tags[:5]


def translate_text(text: str, lang: str) -> str:
    langs = {
        "English": "English",
        "Chinese": "Simplified Chinese",
        "Japanese": "Japanese"
    }
    target = langs.get(lang, lang)
    resp = client.chat.completions.create(model="gpt-3.5-turbo",
                                          messages=[{
                                              "role":
                                              "system",
                                              "content":
                                              f"다음을 {target}로 번역해줘."
                                          }, {
                                              "role": "user",
                                              "content": text
                                          }],
                                          temperature=0.5,
                                          max_tokens=2000)
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
    return [
        r[4:10] for r in rows if r[1].strip() == '재생산' and r[3].strip() == 'Y'
    ]


def pick_rows(src_ws, count=SELECT_COUNT) -> List[List[str]]:
    rows = src_ws.get_all_values()[1:]
    return random.sample(rows, min(count, len(rows))) if rows else []


def estimate_cost(tokens: int, model: str = "gpt-3.5-turbo") -> float:
    rate = 0.0015 if model == "gpt-3.5-turbo" else 0.03
    return round(tokens / 1000 * rate, 4)


def process_regeneration():
    logging.basicConfig(level=logging.INFO)
    logging.info("📌 process_regeneration() 시작")

    src_ws = init_worksheet(SOURCE_DB_ID, "xls")
    prompt_ws = init_worksheet(SOURCE_DB_ID, "prompt")
    image_ws = init_worksheet(SOURCE_DB_ID, "image")
    info_ws = init_worksheet(
        TARGET_DB_ID, "information",
        ["작성일시", "제목", "내용", "태그", "영문", "중문", "일문", "표절률", "이미지url"])

    selected = pick_rows(src_ws)
    logging.info(f"🎯 선택된 행 수: {len(selected)}")
    if not selected:
        logging.warning("⚠️ 본문 시트에서 선택할 수 있는 행이 없습니다.")
        return 0

    prompts = extract_valid_prompt(prompt_ws)
    logging.info(f"🎯 프롬프트 수: {len(prompts)}")
    if not prompts:
        logging.warning("⚠️ 사용 가능한 프롬프트가 없습니다.")
        return 0

    config = random.choice(prompts)
    all_texts = [r[2] for r in src_ws.get_all_values()[1:] if len(r) > 2]
    total_tokens = 0

    for row in selected:
        original_title = row[1] if len(row) > 1 else ""
        original = row[2] if len(row) > 2 else ""
        if not original:
            logging.warning(f"⚠️ 본문이 비어 있음: {row}")
            continue

        content, score, tries = regenerate_unique_post(original_title,
                                                       original, all_texts,
                                                       config)
        total_tokens += tries * 3000
        new_title = regenerate_title(content)
        tags = extract_tags(content)
        en = translate_text(content, "English")
        zh = translate_text(content, "Chinese")
        ja = translate_text(content, "Japanese")
        img = find_matching_image(tags, image_ws)

        try:
            info_ws.append_row([
                now_str(), new_title, content, ", ".join(tags), en, zh, ja,
                f"{score:.2f}", img
            ])
            logging.info(
                f"✅ '{new_title}' 저장 완료 | 유사도: {score:.2f} | 재시도: {tries}회")
        except Exception as e:
            logging.error(f"❌ 시트 쓰기 실패: {e}")

    logging.info(f"💰 예상 비용: ${estimate_cost(total_tokens)}")
    return len(selected)


if __name__ == "__main__":
    process_regeneration()
