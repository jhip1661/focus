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

# ✅ OpenAI API 키 설정
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    OPENAI_API_KEY = os.getenv("OPENAI_KEY") or os.getenv("OPENAI_SECRET")
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

# ── 설정정보시트 로드 ─────────────────────────────────────────────
CONFIG_SHEET_ID = "1h8FcZcDPFsCsdnHaLhDvr8WI2mQpd9raU0YLZ7KW8_8"
config_sheet = gspread.authorize(gs_creds).open_by_key(CONFIG_SHEET_ID).sheet1
config = config_sheet.get_all_records()[0]
input_db_url = config.get("입력 DB 주소")
posting_db_url = config.get("포스팅 DB 주소")
SOURCE_DB_ID = re.search(r"/d/([a-zA-Z0-9-_]+)", input_db_url).group(1) if input_db_url else None
TARGET_DB_ID = re.search(r"/d/([a-zA-Z0-9-_]+)", posting_db_url).group(1) if posting_db_url else None

# ✅ 번역용 GPT 모델: 가장 저렴한 모델로 하드코딩
TRANSLATION_MODEL = "gpt-3.5-turbo"

SIMILARITY_THRESHOLD = 0.5
MAX_RETRIES = 5

def init_worksheet(sheet_id: str, sheet_name: str, header: List[str] = None):
    ws = gspread.authorize(gs_creds).open_by_key(sheet_id).worksheet(sheet_name)
    if header:
        first = ws.get_all_values()[:1]
        if not first or all(cell == "" for cell in first[0]):
            ws.append_row(header)
    return ws

def calculate_similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()

def clean_content(text: str) -> str:
    return re.sub(r'(?m)^(서론|문제 상황|실무 팁|결론)[:\-]?\s*', '', text).strip()

def build_messages_from_prompt(cfg: List[str], title: str, content: str) -> List[dict]:
    purpose, tone, para, emphasis, fmt, etc = cfg
    system = f"{purpose}\n\n{tone}\n\n{para}\n\n{emphasis}\n\n{fmt}\n\n{etc}"
    user = f"다음 글을 중복되지 않도록 재작성해줘:\n\n제목: {title}\n내용: {content}"
    return [
        {"role": "system", "content": system.strip()},
        {"role": "user", "content": user.strip()},
    ]

def regenerate_unique_post(original_title: str, original: str, existing_texts: List[str], prompt_cfg: List[str], model_name: str) -> Tuple[str, float, int]:
    regen, score = original, 1.0
    threshold = 0.6  # ✅ 60% 이하로 기본 설정

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
            logging.warning(f"⚠️ GPT 요청 실패 (시도 {i}): {e}")
            continue

        candidate = clean_content(resp.choices[0].message.content or '')
        sim = max((calculate_similarity(candidate, ex) for ex in existing_texts), default=0)
        if sim < threshold:
            return candidate, sim, i
        regen, score = candidate, sim

    # ✅ MAX_RETRIES 도달 시, 표절률 기준을 0.7(70%)로 완화해 한 번 더 시도
    logging.info(f"🔁 MAX_RETRIES 도달 - 표절률 기준을 0.7로 완화하여 재시도: {original_title}")
    for i in range(1, MAX_RETRIES + 1):
        try:
            resp = client.ChatCompletion.create(
                model=model_name,
                messages=msgs,
                temperature=0.8,
                max_tokens=max_tokens,
            )
        except Exception as e:
            logging.warning(f"⚠️ GPT 최종 요청 실패 (표절률 70% 기준, 시도 {i}): {e}")
            continue

        candidate = clean_content(resp.choices[0].message.content or '')
        sim = max((calculate_similarity(candidate, ex) for ex in existing_texts), default=0)
        if sim < 0.7:
            return candidate, sim, MAX_RETRIES + i
        regen, score = candidate, sim

    return regen, score, MAX_RETRIES * 2

def regenerate_title(content: str) -> str:
    resp = client.ChatCompletion.create(
        model="gpt-4-turbo",
        messages=[
            {"role": "system", "content": "너는 마케팅 콘텐츠 전문가야. 짧은 제목을 작성해줘."},
            {"role": "user", "content": content[:1000]},
        ],
        temperature=0.7,
        max_tokens=800,
    )
    return re.sub(r'^.*?:\s*', '', resp.choices[0].message.content.strip())

def translate_text(text: str, lang: str) -> str:
    resp = client.ChatCompletion.create(
        model=TRANSLATION_MODEL,
        messages=[
            {"role": "system", "content": f"다음을 {lang}로 번역해줘."},
            {"role": "user", "content": text},
        ],
        temperature=0.5,
        max_tokens=2000,
    )
    return resp.choices[0].message.content.strip()

def now_str() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def process_regeneration():
    logging.basicConfig(level=logging.INFO)
    logging.info("📌 process_regeneration() 시작")

    src_ws    = init_worksheet(SOURCE_DB_ID, "홍보시트")
    prompt_ws = init_worksheet(SOURCE_DB_ID, "프롬프트시트")
    image_ws  = init_worksheet(SOURCE_DB_ID, "이미지 시트")
    info_ws   = init_worksheet(TARGET_DB_ID, "홍보시트")

    # ── 소스 시트 헤더 맵 생성
    src_header  = src_ws.row_values(1)
    src_col_map = {name: idx for idx, name in enumerate(src_header)}

    # ── 스크랩 데이터(날짜 필터링)
    rows  = src_ws.get_all_values()[1:]
    today = datetime.datetime.now().date()
    filtered_rows = []
    for r in rows:
        norm = re.sub(r'[\.\s]+', '-', r[1].strip())
        norm = re.sub(r'-+', '-', norm)
        try:
            dl = datetime.datetime.strptime(norm, "%Y-%m-%d").date()
        except ValueError:
            continue
        if dl >= today:
            filtered_rows.append(r)
    if not filtered_rows:
        logging.warning("⚠️ 유효한 마케팅 콘텐츠가 없습니다.")
        return 0

    # ── valid_rows 와 item 정의 추가
    valid_rows = filtered_rows  # 수정: 필터링된 행을 valid_rows로 지정
    item = random.choice(valid_rows)  # 수정: 랜덤으로 아이템 선택

    # ── 기존 글 리스트에서 자기 자신 제외 (중복 생성 방지)
    existing_texts = [r[4] for r in valid_rows if r != item]  # 수정: '요약' 열을 헤더가 아니라 인덱스 4로 고정

    # ── 프롬프트시트에서 3개 조건 모두 일치하는 행 찾기 (수정)
    prompt_header    = prompt_ws.row_values(1)
    col_map          = {name: idx for idx, name in enumerate(prompt_header)}
    run_idx          = col_map.get("run_count", len(prompt_header))
    all_prompts      = prompt_ws.get_all_values()[1:]
    matching_prompts = [
        cfg for cfg in all_prompts
        if len(cfg) > run_idx
           and cfg[col_map["출처"]].strip() == "홍보시트"
           and cfg[col_map["현재사용여부"]].strip().upper() == "Y"
           and cfg[col_map["구분태그"]].strip()
    ]
    if not matching_prompts:
        raise RuntimeError("❌ 프롬프트가 존재하지 않습니다: 출처·사용여부·구분태그 모두 일치하는 행을 찾을 수 없습니다.")
    cfg      = matching_prompts[0]
    category = cfg[col_map["구분태그"]].strip()

    # ── 프롬프트 구성
    prompt_fields = [
        "작성자 역할 설명", "전체 작성 조건", "글 구성방식",
        "필수 포함 항목", "마무리 문장", "추가 지시사항"
    ]
    prompt_cfg    = [cfg[col_map[f]] for f in prompt_fields]

    # ── 글 생성 파라미터 계산
    orig_title = item[0]
    orig_cont  = item[4]
    prev_count = int(cfg[run_idx]) if cfg[run_idx].isdigit() else 0
    interval   = int(cfg[col_map["글 간격"]]) if cfg[col_map["글 간격"]].isdigit() else 1
    basic_mod  = cfg[col_map["기본 gpt"]].strip() or "gpt-3.5-turbo"
    adv_mod    = cfg[col_map["고급 gpt"]].strip() or basic_mod
    use_model  = basic_mod if prev_count < interval else adv_mod
    new_count  = prev_count + 1 if prev_count < interval else 0

    content, score, _ = regenerate_unique_post(
        orig_title, orig_cont, existing_texts, prompt_cfg, use_model
    )
    title = regenerate_title(content)

    # ── 이미지 태그 매칭
    image_tag = cfg[col_map["이미지태그"]].strip()
    img_header  = image_ws.row_values(1)
    img_col_map = {name: idx for idx, name in enumerate(img_header)}
    d_idx = img_col_map.get("이미지태그")
    c_idx = img_col_map.get("이미지url")
    img   = ""
    if d_idx is not None and c_idx is not None and image_tag:
        candidates = [
            row[c_idx].strip() for row in image_ws.get_all_values()[1:]
            if len(row) > d_idx and row[d_idx].strip() == image_tag
               and len(row) > c_idx and row[c_idx].strip()
        ]
        if candidates:
            img = random.choice(candidates)

    # ── 다국어 번역
    en = translate_text(content, 'English')
    zh = translate_text(content, 'Chinese')
    ja = translate_text(content, 'Japanese')

    # ── 결과 기록
    info_ws.append_row([
        now_str(), title, content, category, en, zh, ja, f"{score:.2f}", img
    ])

    # ── run_count 업데이트
    prompt_ws.update_cell(
        all_prompts.index(cfg) + 2,  # 실제 시트 행 번호
        run_idx + 1,
        str(new_count)
    )

    logging.info("💰 한 건의 마케팅 콘텐츠가 성공적으로 생성되었습니다.")
    return 1

if __name__ == "__main__":
    process_regeneration()
