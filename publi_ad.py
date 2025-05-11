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
RAW_CREDENTIALS_JSON = os.getenv("GSHEET_CREDENTIALS_JSON", "")
try:
    creds_info = json.loads(RAW_CREDENTIALS_JSON)
except json.JSONDecodeError:
    sanitized = RAW_CREDENTIALS_JSON.replace('\n', '\\n').replace('\t', '').replace('\r', '')
    creds_info = json.loads(sanitized)
if 'private_key' in creds_info:
    creds_info['private_key'] = creds_info['private_key'].replace('\\n', '\n')

SOURCE_DB_ID   = os.getenv("SOURCE_DB_ID")
TARGET_DB_ID   = os.getenv("TARGET_DB_ID")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

SIMILARITY_THRESHOLD = 0.6
MAX_RETRIES          = 5

# 🔑 OpenAI 클라이언트 설정
if not OPENAI_API_KEY:
    raise ValueError("❌ 환경변수 'OPENAI_API_KEY'가 없습니다.")
openai.api_key = OPENAI_API_KEY
client = openai


def init_worksheet(sheet_id: str, sheet_name: str, header: List[str] = None):
    scope = [
        'https://spreadsheets.google.com/feeds',
        'https://www.googleapis.com/auth/drive'
    ]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_info, scope)
    gs = gspread.authorize(creds)
    try:
        ws = gs.open_by_key(sheet_id).worksheet(sheet_name)
    except gspread.exceptions.WorksheetNotFound:
        ws = gs.open_by_key(sheet_id).add_worksheet(title=sheet_name, rows="1000", cols="20")
    if header:
        first = ws.row_values(1)
        if first != header:
            ws.clear()
            ws.append_row(header)
    return ws


def calculate_similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def clean_content(text: str) -> str:
    return re.sub(r'(?m)^(서론|문제 상황|실무 팁|결론)[:\-]?\s*', '', text).strip()


def build_messages_from_prompt(cfg: List[str], title: str, content: str) -> List[dict]:
    purpose, tone, para, emphasis, fmt, etc = cfg
    system = f"""{purpose}\n\n{tone}\n\n{para}\n\n{emphasis}\n\n{fmt}\n\n{etc}"""
    user = f"""다음 글을 중복되지 않도록 재작성해줘:\n\n제목: {title}\n내용: {content}"""
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
        except openai.error.InvalidRequestError:
            resp = client.ChatCompletion.create(
                model='gpt-3.5-turbo',
                messages=msgs,
                temperature=0.8,
                max_tokens=max_tokens,
            )
        candidate = clean_content(resp.choices[0].message.content or '')
        sim = max((calculate_similarity(candidate, ex) for ex in existing_texts), default=0)
        if sim < SIMILARITY_THRESHOLD:
            return candidate, sim, i
        regen, score = candidate, sim
    return regen, score, MAX_RETRIES


def regenerate_title(content: str) -> str:
    resp = client.ChatCompletion.create(
        model="gpt-3.5-turbo",
        messages=[
            {"role": "system", "content": "너는 마케팅 콘텐츠 전문가야. 짧은 제목을 작성해줘."},
            {"role": "user",   "content": content[:1000]},
        ],
        temperature=0.7,
        max_tokens=800,
    )
    return re.sub(r'^.*?:\s*', '', resp.choices[0].message.content.strip())


def extract_tags(text: str) -> List[str]:
    resp = client.ChatCompletion.create(
        model="gpt-3.5-turbo",
        messages=[
            {"role": "system", "content": "당신은 태그 추출 전문가입니다."},
            {"role": "user",   "content": f"다음 글에서 실무 중심 명사 5개를 #키워드 형태로 추출해줘. 글: {text}"},
        ],
        temperature=0,
        max_tokens=50,
    )
    return re.findall(r"#(\w+)", resp.choices[0].message.content)[:5]


def translate_text(text: str, lang: str) -> str:
    resp = client.ChatCompletion.create(
        model="gpt-3.5-turbo",
        messages=[
            {"role": "system", "content": f"다음을 {lang}로 번역해줘."},
            {"role": "user",   "content": text},
        ],
        temperature=0.5,
        max_tokens=2000,
    )
    return resp.choices[0].message.content.strip()


def find_matching_image(tags: List[str], image_ws) -> str:
    for row in image_ws.get_all_values()[1:]:
        if any(tag in row[0] for tag in tags):
            return row[1]
    return ""


def now_str() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def process_regeneration():
    logging.basicConfig(level=logging.INFO)
    logging.info("📌 process_regeneration() 시작")

    # 워크시트 초기화
    src_ws    = init_worksheet(SOURCE_DB_ID, "marketing")
    prompt_ws = init_worksheet(SOURCE_DB_ID, "prompt")
    image_ws  = init_worksheet(SOURCE_DB_ID, "image")
    info_ws   = init_worksheet(TARGET_DB_ID, "advertising",
        ["작성일시","제목","내용","태그","영문","중문","일문","표절률","이미지url"]
    )

    # 마감일 기준 필터링된 콘텐츠 목록
    rows = src_ws.get_all_values()[1:]
    today = datetime.datetime.now().date()
    valid_rows = []
    for r in rows:
        norm = re.sub(r'[\.\s]+','-', r[1].strip())
        norm = re.sub(r'-+','-', norm)
        try:
            dl = datetime.datetime.strptime(norm, "%Y-%m-%d").date()
        except ValueError:
            continue
        if dl >= today:
            valid_rows.append(r)
    if not valid_rows:
        logging.warning("⚠️ 유효한 마케팅 콘텐츠가 없습니다.")
        return 0

    # prompt 시트에 run_count 열 확보
    header = prompt_ws.row_values(1)
    if 'run_count' not in header:
        prompt_ws.add_cols(1)
        prompt_ws.update_cell(1, len(header)+1, 'run_count')
        run_idx = len(header)+1
    else:
        run_idx = header.index('run_count')+1

    total = 0
    # 프롬프트별 1사이클 처리
    prompts = prompt_ws.get_all_values()[1:]
    for pr_idx, cfg in enumerate(prompts, start=2):
        # 필터: B='marketing', C=site, E='재생산', F='Y'
        if len(cfg) < 15:
            continue
        if cfg[1].strip()!='marketing' or cfg[4].strip()!='재생산' or cfg[5].strip().upper()!='Y':
            continue
        site_category = cfg[2].strip()
        # 매칭 소스 랜덤 선택
        matching = [r for r in valid_rows if len(r)>2 and r[2].strip()==site_category]
        if not matching:
            logging.warning(f"⚠️ 매칭되는 소스 없음: {site_category}")
            continue
        item = random.choice(matching)

        # interval/모델 전환
        prev_count = int(cfg[run_idx-1]) if cfg[run_idx-1].isdigit() else 0
        interval   = int(cfg[7]) if cfg[7].isdigit() else 1
        basic_mod  = cfg[8].strip() or 'gpt-3.5-turbo'
        adv_mod    = cfg[9].strip() or basic_mod
        if prev_count < interval:
            use_model = basic_mod
            new_count = prev_count + 1
        else:
            use_model = adv_mod
            new_count = 0

        # 콘텐츠 생성
        orig_title = item[0]
        orig_cont  = item[4]
        content, score, _ = regenerate_unique_post(
            orig_title, orig_cont,
            [r[4] for r in valid_rows],
            cfg[5:11], use_model
        )
        title = regenerate_title(content)
        tags  = extract_tags(content)
        en    = translate_text(content, 'English')
        zh    = translate_text(content, 'Chinese')
        ja    = translate_text(content, 'Japanese')
        img   = find_matching_image(tags, image_ws)

        # 결과 저장
        info_ws.append_row([
            now_str(), title, content,
            ", ".join(tags), en, zh, ja,
            f"{score:.2f}", img
        ])
        total += 1

        # run_count 업데이트
        prompt_ws.update_cell(pr_idx, run_idx, str(new_count))

    logging.info(f"💰 총 저장된 글 수: {total}")
    return total

if __name__ == '__main__':
    process_regeneration()
