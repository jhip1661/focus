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
from openai import OpenAI  # ✅ v1.x 클라이언트 사용

# ----------------------------
# 🔐 서비스 계정 JSON 로드 & 인증
# ----------------------------
GSHEET_CREDENTIALS_JSON = os.getenv("GSHEET_CREDENTIALS_JSON")
if not GSHEET_CREDENTIALS_JSON:
    raise ValueError("❌ 환경변수 'GSHEET_CREDENTIALS_JSON'이 누락되었습니다.")

try:
    creds_info = json.loads(GSHEET_CREDENTIALS_JSON)
except json.JSONDecodeError as e:
    raise ValueError(f"❌ SERVICE_ACCOUNT_JSON 파싱 실패: {e}")

if "private_key" not in creds_info or not creds_info["private_key"].startswith(
        "-----BEGIN PRIVATE KEY-----"):
    raise ValueError("❌ 잘못된 서비스 계정 JSON입니다. 환경변수에 전체 JSON 내용을 정확히 복사했는지 확인하고, "
                     "서비스 계정 이메일이 스프레드시트에 공유되어 있는지 확인하세요.")

try:
    gs_creds = GCredentials.from_service_account_info(
        creds_info,
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"
        ])
except Exception as e:
    raise RuntimeError(f"❌ 인증 정보 로드 실패: {e}")

# ----------------------------
# ✅ OpenAI 클라이언트 설정 (v1.x)
# ----------------------------
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    raise ValueError("❌ 환경변수 'OPENAI_API_KEY'가 누락되었습니다.")
client = OpenAI(api_key=OPENAI_API_KEY)

# ----------------------------
# 환경변수 및 상수
# ----------------------------
SOURCE_DB_ID = os.getenv("SOURCE_DB_ID")
TARGET_DB_ID = os.getenv("TARGET_DB_ID")
SIMILARITY_THRESHOLD = 0.6
MAX_RETRIES = 5
SELECT_COUNT = 5


# ----------------------------
# 시트 초기화 함수
# ----------------------------
def init_worksheet(sheet_id: str, sheet_name: str, header: List[str] = None):
    client_gs = gspread.authorize(gs_creds)
    try:
        ws = client_gs.open_by_key(sheet_id).worksheet(sheet_name)
    except gspread.exceptions.WorksheetNotFound:
        ws = client_gs.open_by_key(sheet_id).add_worksheet(title=sheet_name,
                                                           rows="1000",
                                                           cols="20")
    if header:
        current = ws.get_all_values()
        if not current or all(cell == "" for cell in current[0]):
            ws.clear()
            ws.append_row(header)
    return ws


# ----------------------------
# 유틸리티 함수들
# ----------------------------
def calculate_similarity(text1: str, text2: str) -> float:
    return difflib.SequenceMatcher(None, text1, text2).ratio()


def clean_content(text: str) -> str:
    return re.sub(r'(?m)^(서론|문제 상황|실무 팁|결론)[:\-]?\s*', "", text).strip()


def build_messages_from_prompt(prompt_config: List[str], title: str,
                               content: str) -> List[dict]:
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
        {
            "role": "system",
            "content": system_msg.strip()
        },
        {
            "role": "user",
            "content": user_msg.strip()
        },
    ]


def regenerate_unique_post(
    original_title: str,
    original: str,
    existing_texts: List[str],
    prompt_config: List[str],
) -> Tuple[str, float, int]:
    for i in range(MAX_RETRIES):
        messages = build_messages_from_prompt(prompt_config, original_title,
                                              original)
        etc_lower = prompt_config[-1].lower()
        max_tokens = (3000 if "3000자" in etc_lower else
                      2500 if "2500자" in etc_lower else 2000)
        resp = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=messages,
            temperature=0.8,
            max_tokens=max_tokens,
        )
        regen = resp.choices[0].message.content.strip()
        regen = clean_content(regen)
        score = max(calculate_similarity(regen, t) for t in existing_texts)
        if score < SIMILARITY_THRESHOLD:
            return regen, score, i + 1
    return regen, score, MAX_RETRIES


def regenerate_title(content: str) -> str:
    system = ("너는 광고, 홍보, 마케팅 콘텐츠 전문가야. 아래 내용을 보고 광고, 홍보, 마케팅을 흥미롭게 해서"
              " 소비자의 클릭을 유도하는 짧은 제목을 작성해줘.")
    resp = client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[
            {
                "role": "system",
                "content": system
            },
            {
                "role": "user",
                "content": content[:1000]
            },
        ],
        temperature=0.7,
        max_tokens=800,
    )
    return re.sub(r"^.*?:\s*", "", resp.choices[0].message.content.strip())


def extract_tags(text: str) -> List[str]:
    prompt = (f"다음 글에서 광고, 홍보, 마케팅의 중심 명사 5개를 해시태그(#키워드) 형태로 추출해줘. 글: {text}")
    resp = client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[
            {
                "role": "system",
                "content": "당신은 태그 추출 전문가입니다."
            },
            {
                "role": "user",
                "content": prompt
            },
        ],
        temperature=0,
        max_tokens=50,
    )
    return re.findall(r"#(\w+)", resp.choices[0].message.content.strip())[:5]


def translate_text(text: str, lang: str) -> str:
    langs = {
        "English": "English",
        "Chinese": "Simplified Chinese",
        "Japanese": "Japanese"
    }
    target = langs.get(lang, lang)
    resp = client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[
            {
                "role": "system",
                "content": f"다음을 {target}로 번역해줘."
            },
            {
                "role": "user",
                "content": text
            },
        ],
        temperature=0.5,
        max_tokens=2000,
    )
    return resp.choices[0].message.content.strip()


# ----------------------------
# 메인 프로세스
# ----------------------------
def process_regeneration():
    logging.basicConfig(level=logging.INFO)
    logging.info("📌 process_regeneration() 시작")

    src_ws = init_worksheet(SOURCE_DB_ID, "marketing")
    prompt_ws = init_worksheet(SOURCE_DB_ID, "prompt")
    image_ws = init_worksheet(SOURCE_DB_ID, "image")
    info_ws = init_worksheet(
        TARGET_DB_ID,
        "advertising",
        ["작성일시", "제목", "내용", "태그", "영문", "중문", "일문", "표절률", "이미지url"],
    )

    rows = src_ws.get_all_values()[1:]
    filtered_rows = [
        r for r in rows if len(r) > 1 and r[1].strip().upper() == "Y"
    ]
    selected = random.sample(filtered_rows,
                             min(SELECT_COUNT, len(filtered_rows)))
    logging.info(f"🎯 대상 행 수: {len(selected)}")

    prompts = [
        r[4:10] for r in prompt_ws.get_all_values()[1:]
        if r[1].strip() == "marketing" and r[3].strip() == "Y"
    ]
    if not prompts:
        logging.warning("⚠️ 사용 가능한 프롬프트 없음")
        return 0
    config = random.choice(prompts)
    all_texts = [r[2] for r in filtered_rows if len(r) > 2]
    total_tokens = 0

    for row in selected:
        original_content = row[2] if len(row) > 2 else ""
        image_url = row[5] if len(row) > 5 else ""
        if not original_content:
            logging.warning(f"⚠️ 본문 비어 있음: {row}")
            continue

        content, score, tries = regenerate_unique_post("", original_content,
                                                       all_texts, config)
        total_tokens += tries * 3000
        new_title = regenerate_title(content)
        tags = extract_tags(content)
        en = translate_text(content, "English")
        zh = translate_text(content, "Chinese")
        ja = translate_text(content, "Japanese")
        img = (image_url or next(
            (r[1] for r in image_ws.get_all_values()[1:]
             for tag in tags if tag in r[0]),
            "",
        ))

        try:
            info_ws.append_row([
                datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                new_title,
                content,
                ", ".join(tags),
                en,
                zh,
                ja,
                f"{score:.2f}",
                img,
            ])
            logging.info(
                f"✅ '{new_title}' 저장 완료 | 유사도: {score:.2f} | 재시도: {tries}회")
        except Exception as e:
            logging.error(f"❌ 시트 쓰기 실패: {e}")

    logging.info(f"💰 예상 비용: ${round(total_tokens / 1000 * 0.0015, 4)}")
    return len(selected)


if __name__ == "__main__":
    process_regeneration()
