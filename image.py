import os
import json
import datetime
import io
import gspread
from google.oauth2.service_account import Credentials as GCredentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# ✅ 환경변수 불러오기
GSHEET_CREDENTIALS_JSON = os.getenv("GSHEET_CREDENTIALS_JSON")
GSHEET_ID = os.getenv("GSHEET_ID")
FOLDER_ID = os.getenv("GOOGLE_DRIVE_FOLDER_ID",
                      "1bBeSUZJV7r2UyxvDiVZWMtp4FjwHo-l9")
SHEET_NAME = "image"

# ✅ 필수 환경변수 체크
if not GSHEET_CREDENTIALS_JSON:
    raise ValueError("❌ 환경변수 'GSHEET_CREDENTIALS_JSON'이 누락되었습니다.")
if not GSHEET_ID:
    raise ValueError("❌ 환경변수 'GSHEET_ID'가 누락되었습니다.")

# ✅ JSON → dict 로드
try:
    creds_info = json.loads(GSHEET_CREDENTIALS_JSON)
except json.JSONDecodeError as e:
    raise ValueError(f"❌ SERVICE_ACCOUNT_JSON 파싱 실패: {e}")

# 👀 서비스 계정 키 확인
if "private_key" not in creds_info or not creds_info["private_key"].startswith(
        "-----BEGIN PRIVATE KEY-----"):
    raise ValueError("❌ 잘못된 서비스 계정 JSON입니다. "
                     "환경변수에 전체 JSON 내용을 정확히 복사했는지 확인하고, "
                     "서비스 계정 이메일이 스프레드시트에 공유되어 있는지 확인하세요.")

# ✅ 자격 증명 객체 생성
try:
    creds = GCredentials.from_service_account_info(
        creds_info,
        scopes=[
            "https://www.googleapis.com/auth/drive",
            "https://www.googleapis.com/auth/spreadsheets"
        ])
except Exception as e:
    raise RuntimeError(f"❌ 인증 정보 로드 실패: {e}")

# ✅ Google API 클라이언트 생성
try:
    gc = gspread.authorize(creds)
    sheet = gc.open_by_key(GSHEET_ID).worksheet(SHEET_NAME)
    drive_service = build("drive", "v3", credentials=creds)
except Exception as e:
    raise RuntimeError(f"❌ Google API 초기화 실패: {e}")


# ✅ 시트 데이터 확인 함수
def read_image_sheet():
    print("✅ 이미지 시트에서 데이터 읽기 시작")
    rows = sheet.get_all_records()
    for idx, row in enumerate(rows, start=1):
        print(
            f"{idx}. 생성일자: {row.get('생성일자')}, 파일명: {row.get('파일명')}, URL: {row.get('파일 URL')}"
        )


# ✅ Google Drive 폴더 내 파일 리스트 가져오기
def list_drive_files(folder_id):
    print(f"📁 Google Drive 폴더({folder_id}) 내 파일 목록:")
    query = f"'{folder_id}' in parents and trashed = false"
    results = drive_service.files().list(q=query,
                                         fields="files(id, name)").execute()
    items = results.get("files", [])
    for item in items:
        print(f"- {item['name']} (ID: {item['id']})")
    return items


# ✅ 실행
if __name__ == "__main__":
    read_image_sheet()
    list_drive_files(FOLDER_ID)
