import os
import json
import datetime
import time
import logging
import requests
from bs4 import BeautifulSoup
from google.oauth2.service_account import Credentials as GCredentials
import openai
import gspread

# ── 로깅 설정 ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ── 환경변수 로드 ─────────────────────────────────────────────────────────────────
RAW_JSON       = os.getenv("GSHEET_CREDENTIALS_JSON")
RAINDROP_TOKEN = os.getenv("RAINDROP_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GPT_MODEL      = os.getenv("GPT_MODEL", "gpt-3.5-turbo")

# ── 하드코딩 시트 정보 ─────────────────────────────────────────────────────────────
SRC_SHEET_ID    = '1EOUsXUfNfdw58mIautpeeidK-ZvybB8ggJy57pMcTwQ'
TGT_SHEET_ID    = '1lH1pZLYMEPab7zthSDYPpzumtIJOgzx-Iu1TBcqkFCQ'
SRC_SHEET_NAME  = '시트1'
TGT_SHEET_NAME  = 'support business'

# ── Google Sheets 인증 ────────────────────────────────────────────────────────────
if not RAW_JSON:
    raise ValueError("환경변수 'GSHEET_CREDENTIALS_JSON'가 설정되지 않았습니다.")
creds_info = json.loads(RAW_JSON)
creds = GCredentials.from_service_account_info(creds_info, scopes=[
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive'
])
gc = gspread.authorize(creds)

# ── OpenAI 설정 ─────────────────────────────────────────────────────────────────
if not OPENAI_API_KEY:
    raise ValueError("환경변수 'OPENAI_API_KEY'가 설정되지 않았습니다.")
openai.api_key = OPENAI_API_KEY
MODEL = GPT_MODEL

# ── 워크시트 객체 가져오기 ─────────────────────────────────────────────────────────
src_ws = gc.open_by_key(SRC_SHEET_ID).worksheet(SRC_SHEET_NAME)
tgt_ws = gc.open_by_key(TGT_SHEET_ID).worksheet(TGT_SHEET_NAME)

def fetch_page_text(url: str) -> str:
    """URL에서 HTML을 가져와 텍스트만 반환"""
    res = requests.get(url, timeout=10)
    res.raise_for_status()
    soup = BeautifulSoup(res.text, 'html.parser')
    for tag in soup(['script', 'style']):
        tag.decompose()
    text = soup.get_text(separator=' ')
    return text[:8000]  # 분량 조절

def summarize(text: str) -> str:
    """OpenAI GPT를 사용해 1000~1500자 분량으로 요약"""
    response = openai.ChatCompletion.create(
        model=MODEL,
        messages=[
            {"role": "system",
             "content": "뉴스 기사를 1000자에서 1500자 분량으로 상세하고 풍부하게 요약하는 전문 요약가입니다."},
            {"role": "user", "content": text}
        ],
        max_tokens=1200,
        temperature=0.5,
    )
    return response.choices[0].message.content.strip()

def sync():
    # 1) 소스 전체 읽기 (헤더 제외)
    all_rows = src_ws.get_all_values()
    src_data = all_rows[1:]  # 첫 행은 헤더

    # 2) 타겟 기존 링크 목록
    existing = {row[3] for row in tgt_ws.get_all_values()[1:] if len(row) > 3}

    for row in src_data:
        collect_date = row[0]  # A열: 수집일자
        title        = row[3]  # D열: 제목
        url          = row[6]  # G열: 네이버뉴스URL
        if not url or url in existing:
            continue

        logging.info(f"Processing URL: {url}")
        page_text = fetch_page_text(url)
        summary   = summarize(page_text)

        # 하이퍼링크로 요약 삽입
        formula = f'=HYPERLINK("{url}", "{summary}")'
        tgt_ws.append_row([
            collect_date,    # 작성일시
            title,           # 제목
            formula,         # 요약 (하이퍼링크)
            url,             # 링크
            "생생-건강정보",  # 태그
            "생생건강정보통", # 사이트분류
            ""               # 컬렉션 ID
        ], value_input_option='USER_ENTERED')
        existing.add(url)

if __name__ == "__main__":
    sync()

logging.info("Sync completed!")
