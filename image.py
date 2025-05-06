import os
import json
import datetime
import gspread
from google.oauth2.service_account import Credentials as GCredentials
from googleapiclient.discovery import build

# 📌 환경변수에서 정보 불러오기 (Render 기준)
GSHEET_CREDENTIALS_JSON = os.getenv("GSHEET_CREDENTIALS_JSON")
GSHEET_ID = os.getenv("GSHEET_ID")  # ✅ 시트 ID도 환경변수로
FOLDER_ID = os.getenv("GOOGLE_DRIVE_FOLDER_ID", "1bBeSUZJV7r2UyxvDiVZWMtp4FjwHo-l9")  # 기본값 설정 가능
SHEET_NAME = "image"

# 🔐 구글 인증
creds_dict = json.loads(GSHEET_CREDENTIALS_JSON.replace('\\n', '\n'))
creds = GCredentials.from_service_account_info(
    creds_dict,
    scopes=[
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/spreadsheets"
    ]
)

def update_images():
    drive_service = build('drive', 'v3', credentials=creds)
    gc = gspread.authorize(creds)
    sheet = gc.open_by_key(GSHEET_ID).worksheet(SHEET_NAME)

    # 🧾 헤더 설정
    headers = ['생성일자', '파일명', '파일 URL', '사용처', '태그']
    current_headers = sheet.row_values(1)
    if current_headers != headers:
        sheet.clear()
        sheet.append_row(headers)

    # 📂 구글 드라이브 이미지 파일 목록 조회
    query = f"'{FOLDER_ID}' in parents and mimeType contains 'image/' and trashed = false"
    results = drive_service.files().list(q=query, fields="files(id, name, createdTime)").execute()
    items = results.get('files', [])

    # 🧠 기존 시트의 파일명 목록 추출
    existing_rows = sheet.get_all_values()
    existing_names = [row[1] for row in existing_rows[1:]]

    # ➕ 새로운 항목만 추가
    added_count = 0
    for file in items:
        name = file['name']
        if name in existing_names:
            continue

        created_time = file['createdTime'][:10]
        file_url = f"https://drive.google.com/uc?export=download&id={file['id']}"
        usage = "광고이미지"
        tags = ""

        sheet.append_row([created_time, name, file_url, usage, tags])
        added_count += 1

    return added_count

if __name__ == "__main__":
    count = update_images()
    print(f"✅ 새로 추가된 이미지 수: {count}")
