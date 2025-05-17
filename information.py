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
import openai  # 모듈 전체 import

# ── 서비스 계정 JSON: env var 우선, 없으면 로컬 파일에서 로드 ─────────────────
service_json = os.getenv("GSHEET_CREDENTIALS_JSON", "")
if service_json:
    try:
        creds_info = json.loads(service_json)
    except json.JSONDecodeError as e:
        raise ValueError(f"❌ SERVICE_ACCOUNT_JSON 파싱 실패: {e}")
else:
    json_path = os.path.join(os.path.dirname(__file__), "focus-2025-458906-311d04096c93.json")
    if not os.path.exists(json_path):
        raise ValueError("❌ 환경변수 'GSHEET_CREDENTIALS_JSON'이 없고, 로컬 JSON 파일도 찾을 수 없습니다.")
    with open(json_path, "r", encoding="utf-8") as f:
        creds_info = json.load(f)

if "private_key" not in creds_info or not creds_info["private_key"].startswith("-----BEGIN PRIVATE KEY-----"):
    raise ValueError("❌ 잘못된 서비스 계정 JSON입니다.")

# ▶ Google Sheets/Drive 인증 객체 생성
SCOPES = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]
gs_creds = GCredentials.from_service_account_info(creds_info, scopes=SCOPES)

# ✅ OpenAI API 키 설정 (기존 코드 유지)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    OPENAI_API_KEY = os.getenv("OPENAI_KEY") or os.getenv("OPENAI_SECRET")

# ── 추가: .env 파일이 있으면 여기서도 로드 ───────────────────────────────────────
if not OPENAI_API_KEY:
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(env_path):
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                if line.strip().startswith("OPENAI_API_KEY"):
                    key = line.strip().split("=", 1)[1].strip().strip('"')
                    if key:
                        OPENAI_API_KEY = key
                        break

if not OPENAI_API_KEY:
    raise ValueError("❌ 환경변수 'OPENAI_API_KEY'가 누락되었습니다.")
openai.api_key = OPENAI_API_KEY
client = openai

# ── 설정정보시트로부터 SOURCE_DB_ID, TARGET_DB_ID 로드 ───────────────────────────
CONFIG_SHEET_ID = "1h8FcZcDPFsCsdnHaLhDvr8WI2mQpd9raU0YLZ7KW8_8"
config_sheet = gspread.authorize(gs_creds).open_by_key(CONFIG_SHEET_ID).sheet1
config = config_sheet.get_all_records()[0]
input_db_url = config.get("입력 DB 주소")
posting_db_url = config.get("포스팅 DB 주소")
SOURCE_DB_ID = re.search(r"/d/([a-zA-Z0-9-_]+)", input_db_url).group(1) if input_db_url else None
TARGET_DB_ID = re.search(r"/d/([a-zA-Z0-9-_]+)", posting_db_url).group(1) if posting_db_url else None

# 상수 정의
SIMILARITY_THRESHOLD = 0.5
MAX_RETRIES          = 5

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
    return re.sub(r'(?m)^(서론|문제 상황|실무 팁|결론)\[:-]?\s\*', '', text).strip()

def build_messages_from_prompt(cfg: List[str], title: str, content: str) -> List[dict]:
    purpose, tone, para, emphasis, fmt, etc = cfg
    system = f"{purpose}\n\n{tone}\n\n{para}\n\n{emphasis}\n\n{fmt}\n\n{etc}"
    user = f"다음 글을 중복되지 않도록 재작성해줘:\n\n제목: {title}\n내용: {content}"
    return [
        {"role": "system", "content": system.strip()},
        {"role": "user",   "content": user.strip()},
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
        max_tokens = 3000
        if '2500자' in etc_lower:
            max_tokens = 2500
        elif '2000자' in etc_lower:
            max_tokens = 2000

        try:
            resp = client.ChatCompletion.create(
                model=model_name,
                messages=msgs,
                temperature=0.8,
                max_tokens=max_tokens,
            )
        except Exception as e:
            from openai.lib._old_api import APIRemovedInV1
            if isinstance(e, APIRemovedInV1) or isinstance(e, AttributeError):
                resp = openai.ChatCompletion.create(
                    model=model_name,
                    messages=msgs,
                    temperature=0.8,
                    max_tokens=max_tokens,
                )
            else:
                raise

        candidate = clean_content(resp.choices[0].message.content or '')
        sim = max((calculate_similarity(candidate, ex) for ex in existing_texts), default=0)
        if sim < SIMILARITY_THRESHOLD:
            return candidate, sim, i
        regen, score = candidate, sim

    return regen, score, MAX_RETRIES

def regenerate_title(content: str) -> str:
    try:
        resp = client.ChatCompletion.create(
            model="gpt-4-turbo",
            messages=[
                {"role": "system", "content": "너는 마케팅 콘텐츠 전문가야. 짧은 제목을 작성해줘."},
                {"role": "user",   "content": content[:1000]},
            ],
            temperature=0.7,
            max_tokens=800,
        )
    except Exception as e:
        from openai.lib._old_api import APIRemovedInV1
        if isinstance(e, APIRemovedInV1) or isinstance(e, AttributeError):
            new_client = openai.OpenAI(api_key=openai.api_key)
            resp = new_client.chat.completions.create(
                model="gpt-4-turbo",
                messages=[
                    {"role": "system", "content": "너는 마케팅 콘텐츠 전문가야. 짧은 제목을 작성해줘."},
                    {"role": "user",   "content": content[:1000]},
                ],
                temperature=0.7,
                max_tokens=800,
            )
        else:
            raise
    return re.sub(r'^.*?:\s*', '', resp.choices[0].message.content.strip())

def translate_text(text: str, lang: str) -> str:
    try:
        resp = client.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": f"다음을 {lang}로 번역해줘."},
                {"role": "user",   "content": text},
            ],
            temperature=0.5,
            max_tokens=2000,
        )
    except Exception as e:
        from openai.lib._old_api import APIRemovedInV1
        if isinstance(e, APIRemovedInV1) or isinstance(e, AttributeError):
            new_client = openai.OpenAI(api_key=openai.api_key)
            resp = new_client.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=[
                    {"role": "system", "content": f"다음을 {lang}로 번역해줘."},
                    {"role": "user",   "content": text},
                ],
                temperature=0.5,
                max_tokens=2000,
            )
        else:
            raise
    return resp.choices[0].message.content.strip()

def now_str() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def process_regeneration():
    logging.basicConfig(level=logging.INFO)
    logging.info("📌 process_regeneration() 시작")

    src_ws    = init_worksheet(SOURCE_DB_ID, "스크랩 시트")
    prompt_ws = init_worksheet(SOURCE_DB_ID, "프롬프트시트")
    image_ws  = init_worksheet(SOURCE_DB_ID, "이미지 시트")
    info_ws   = init_worksheet(TARGET_DB_ID, "정보시트")

    prompt_header = prompt_ws.row_values(1)
    col_map       = {name: idx for idx, name in enumerate(prompt_header)}
    required = [
        "생성일자", "출처", "이미지태그", "구분태그", "현재사용여부",
        "작성자 역할 설명", "전체 작성 조건", "글 구성방식",
        "필수 포함 항목", "마무리 문장", "추가 지시사항",
        "GPT 모델방식", "글 간격", "기본 gpt", "고급 gpt"
    ]
    for c in required:
        if c not in col_map:
            raise RuntimeError(f"⚠️ 프롬프트시트에 '{c}' 컬럼이 없습니다.")
    if "run_count" not in col_map:
        prompt_ws.add_cols(1)
        prompt_ws.update_cell(1, len(prompt_header) + 1, "run_count")
        col_map["run_count"] = len(prompt_header)
        prompt_header.append("run_count")
    run_idx = col_map["run_count"]

    rows = src_ws.get_all_values()[1:]
    if not rows:
        logging.warning("⚠️ 유효한 스크랩 콘텐츠가 없습니다.")
        return 0

    src_header  = src_ws.row_values(1)
    src_col_map = {name: idx for idx, name in enumerate(src_header)}

    total = 0
    prompts = prompt_ws.get_all_values()[1:]

    # ◆수정: 먼저 ‘출처’, ‘현재사용여부’, ‘구분태그’가 모두 일치하는 행을 찾는다.
    matched_idx = None
    for i, cfg in enumerate(prompts, start=2):
        source_val = cfg[col_map["출처"]].strip()
        use_val    = cfg[col_map["현재사용여부"]].strip().upper()
        category   = cfg[col_map["구분태그"]].strip()
        if source_val == "스크랩 시트" and use_val == "Y" and category:
            matched_idx = i
            break
    if matched_idx is None:
        logging.error("❌ 일치하는 프롬프트 행이 없습니다. 전체 처리 중단.")
        return 0

    # ◆수정: 찾은 한 행만 처리하도록 루프 인덱스를 고정
    i   = matched_idx
    cfg = prompts[i - 2]

    # 같은 구분태그를 가진 스크랩 행만 골라내기
    rows = [row for row in rows
            if len(row) > src_col_map["구분태그"]
               and row[src_col_map["구분태그"]].strip() == cfg[col_map["구분태그"]].strip()]
    if not rows:
        logging.warning(f"[행 {i}] '{cfg[col_map['구분태그']]}'에 해당하는 스크랩 콘텐츠가 없습니다. 전체 처리 중지.")
        return 0

    item     = random.choice(rows)
    existing = [r[src_col_map["요약"]] for r in rows if len(r) > src_col_map["요약"]]

    prev_count = int(cfg[run_idx]) if cfg[run_idx].isdigit() else 0
    interval   = int(cfg[col_map["글 간격"]]) if cfg[col_map["글 간격"]].isdigit() else 1
    basic_mod  = cfg[col_map["기본 gpt"]].strip() or "gpt-3.5-turbo"
    adv_mod    = cfg[col_map["고급 gpt"]].strip() or basic_mod

    if prev_count < interval:
        use_model = basic_mod
        new_count = prev_count + 1
    else:
        use_model = adv_mod
        new_count = 0

    prompt_fields = [
        "작성자 역할 설명", "전체 작성 조건", "글 구성방식",
        "필수 포함 항목", "마무리 문장", "추가 지시사항"
    ]
    prompt_cfg = [cfg[col_map[f]] for f in prompt_fields]

    orig_title = item[src_col_map["제목"]]
    orig_cont  = item[src_col_map["요약"]]

    content, score, _ = regenerate_unique_post(
        orig_title, orig_cont, existing, prompt_cfg, use_model
    )
    title = regenerate_title(content)

    image_tag = cfg[col_map["이미지태그"]].strip()
    img = ""
    img_header  = image_ws.row_values(1)
    img_col_map = {name: idx for idx, name in enumerate(img_header)}
    d_idx = img_col_map.get("이미지태그")
    c_idx = img_col_map.get("이미지url")
    if d_idx is not None and c_idx is not None and image_tag:
        candidates = [
            row[c_idx].strip() for row in image_ws.get_all_values()[1:]
            if len(row) > d_idx and row[d_idx].strip() == image_tag
               and len(row) > c_idx and row[c_idx].strip()
        ]
        if candidates:
            img = random.choice(candidates)

    en = translate_text(content, 'English')
    zh = translate_text(content, 'Chinese')
    ja = translate_text(content, 'Japanese')

    info_ws.append_row([
        now_str(),       # A: 작성일시
        cfg[col_map["구분태그"]].strip(),  # B: 구분태그
        "",              # C: 사이트 분류 (빈칸)
        title,           # D: 제목
        content,         # E: 내용
        image_tag,       # F: 이미지태그
        en,              # G: 영문
        zh,              # H: 중문
        ja,              # I: 일문
        f"{score:.2f}",  # J: 표절률
        img              # K: 이미지url
    ])

    prompt_ws.update_cell(i, run_idx + 1, str(new_count))
    total += 1

    logging.info(f"💰 총 저장된 글 수: {total}")
    return total

if __name__ == "__main__":
    process_regeneration()
