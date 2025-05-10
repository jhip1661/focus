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
import openai  # ✅ 변경: OpenAI 모듈 import

# --- OpenAI 설정 ---
openai.api_key = os.getenv("OPENAI_API_KEY")
client = openai

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
    raise ValueError(
        "❌ 잘못된 서비스 계정 JSON입니다. 환경변수에 전체 JSON을 정확히 복사했는지, "
        "서비스 계정 이메일이 시트에 공유되어 있는지 확인하세요."
    )

# ▶ Google Sheets/Drive 인증 객체 생성
GS_SCOPES = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]
try:
    gs_creds = GCredentials.from_service_account_info(creds_info, scopes=GS_SCOPES)
except Exception as e:
    raise RuntimeError(f"❌ 인증 정보 로드 실패: {e}")

# 🚩 상수
SOURCE_DB_ID = os.getenv("SOURCE_DB_ID")
TARGET_DB_ID = os.getenv("TARGET_DB_ID")
SIMILARITY_THRESHOLD = 0.6
MAX_RETRIES = 5
SELECT_COUNT = 5


def init_worksheet(sheet_id: str, sheet_name: str, header: List[str] = None):
    gs_client = gspread.authorize(gs_creds)
    try:
        ws = gs_client.open_by_key(sheet_id).worksheet(sheet_name)
    except gspread.exceptions.WorksheetNotFound:
        ws = gs_client.open_by_key(sheet_id).add_worksheet(
            title=sheet_name, rows="1000", cols="20"
        )
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


def build_messages_from_prompt(
    prompt_cfg: List[str], title: str, content: str
) -> List[dict]:
    purpose, tone, para, emphasis, fmt, etc, *_ = prompt_cfg
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
    model_name: str = None,
) -> Tuple[str, float, int]:
    if model_name is None:
        model_name = prompt_cfg[8]  # 기본 GPT 모델
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

        resp = client.ChatCompletion.create(
            model=model_name,
            messages=msgs,
            temperature=0.8,
            max_tokens=max_tokens,
        )

        candidate = clean_content(resp.choices[0].message.content.strip())
        sim = max(calculate_similarity(candidate, ex) for ex in existing_texts)
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
            {"role": "user", "content": content[:1000]},
        ],
        temperature=0.7,
        max_tokens=800,
    )
    title = resp.choices[0].message.content.strip()
    return re.sub(r"^.*?:\s*", "", title)


def extract_tags(text: str) -> List[str]:
    prompt = f"다음 글에서 실무 중심 명사 5개를 해시태그(#키워드) 형태로 추출해줘. 글: {text}"
    resp = client.ChatCompletion.create(
        model="gpt-3.5-turbo",
        messages=[
            {"role": "system", "content": "당신은 태그 추출 전문가입니다."},
            {"role": "user", "content": prompt},
        ],
        temperature=0,
        max_tokens=50,
    )
    return re.findall(r"#(\w+)", resp.choices[0].message.content.strip())[:5]


def translate_text(text: str, lang: str) -> str:
    langs = {
        "English": "English",
        "Chinese": "Simplified Chinese",
        "Japanese": "Japanese",
    }
    target = langs.get(lang, lang)
    resp = client.ChatCompletion.create(
        model="gpt-3.5-turbo",
        messages=[
            {"role": "system", "content": f"다음을 {target}로 번역해줘."},
            {"role": "user", "content": text},
        ],
        temperature=0.5,
        max_tokens=2000,
    )
    return resp.choices[0].message.content.strip()


def now_str() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def extract_valid_prompt(prompt_ws) -> List[List[str]]:
    rows = prompt_ws.get_all_values()[1:]
    return [
        [r[2]] + r[4:10] + [r[11], r[12], r[13], r[14]]
        for r in rows
        if r[4].strip() == "Y"
    ]


def pick_rows(src_ws, count=SELECT_COUNT) -> List[Tuple[int, List[str], int]]:
    all_rows = src_ws.get_all_values()
    data = all_rows[1:]
    rows_info = []
    for idx, row in enumerate(data, start=2):
        try:
            usage = int(row[-1])
        except:
            usage = 0
        rows_info.append((idx, row, usage))

    groups = {}
    for info in rows_info:
        groups.setdefault(info[2], []).append(info)

    selected = []
    for usage in sorted(groups.keys()):
        group = groups[usage][:]
        random.shuffle(group)
        for item in group:
            if len(selected) < count:
                selected.append(item)
            else:
                break
        if len(selected) >= count:
            break

    return selected


def process_regeneration():
    logging.basicConfig(level=logging.INFO)
    logging.info("📌 process_regeneration() 시작")

    src_ws = init_worksheet(SOURCE_DB_ID, "xls")
    prompt_ws = init_worksheet(SOURCE_DB_ID, "prompt")
    info_ws = init_worksheet(
        TARGET_DB_ID,
        "information",
        [
            "작성일시",
            "origin tag",
            "사이트 분류",
            "제목",
            "내용",
            "태그",
            "영문",
            "중문",
            "일문",
            "표절률",
            "이미지url",
        ],
    )

    selected = pick_rows(src_ws)
    if not selected:
        logging.warning("⚠️ xls 시트에 데이터가 없습니다.")
        return 0

    prompts = extract_valid_prompt(prompt_ws)
    if not prompts:
        logging.warning("⚠️ prompt 시트에 Y로 설정된 데이터가 없습니다.")
        return 0

    all_texts = [
        r[3] for r in src_ws.get_all_values()[1:] if len(r) > 3
    ]
    total = 0

    for row_idx, row, old_usage in selected:
        original_title = row[3]
        original = row[3]
        origin_tag = ""

        config = random.choice(prompts)
        site_class = config[0]
        prompt_cfg = config[1:7]
        model_style, interval_str, basic_model, advanced_model = config[7:11]

        if model_style.lower() == "하이브리드":
            interval = int(interval_str)
            models = [basic_model] * interval + [advanced_model]
        else:
            models = [basic_model]

        usage_inc = len(models)
        for mdl in models:
            content, _, _ = regenerate_unique_post(
                original_title, original, all_texts, prompt_cfg, model_name=mdl
            )
            tags = extract_tags(content)
            en = translate_text(content, "English")
            zh = translate_text(content, "Chinese")
            ja = translate_text(content, "Japanese")
            plagiarism = row[9] if len(row) > 9 else ""
            image_url = row[10] if len(row) > 10 else ""

            info_ws.append_row(
                [
                    now_str(),
                    origin_tag,
                    site_class,
                    original_title,
                    content,
                    ", ".join(tags),
                    en,
                    zh,
                    ja,
                    plagiarism,
                    image_url,
                ]
            )
            total += 1

        # 🔄 변경: 사용횟수 E열(5번째)에 기록
        new_usage = old_usage + usage_inc
        src_ws.update_cell(row_idx, 5, str(new_usage))

    logging.info(f"🟢 총 {total}건 저장 완료")
    return total


if __name__ == "__main__":
    process_regeneration()
