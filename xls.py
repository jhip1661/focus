import os
import datetime
import pandas as pd
from openai import OpenAI
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from google.oauth2.service_account import Credentials as GCredentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import io
import json
from dotenv import load_dotenv
load_dotenv()


GOOGLE_DRIVE_FOLDER_ID = '1SNhQQEyyn9NveFOixl1Ef3PiZzd5vOdg'
GSHEET_ID = '1lH1pZLYMEPab7zthSDYPpzumtIJOgzx-Iu1TBcqkFCQ'
GSHEET_CREDENTIALS_JSON = os.getenv('GSHEET_CREDENTIALS_JSON')
GPT_MODEL = 'gpt-3.5-turbo'

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

def get_drive_service():
    creds_dict = json.loads(GSHEET_CREDENTIALS_JSON)
    creds = GCredentials.from_service_account_info(creds_dict, scopes=["https://www.googleapis.com/auth/drive"])
    return build('drive', 'v3', credentials=creds)

def list_excel_files_in_folder(folder_id):
    service = get_drive_service()
    query = f"'{folder_id}' in parents and trashed=false and mimeType contains 'spreadsheet'"
    results = service.files().list(q=query, fields="files(id, name)").execute()
    return results.get('files', [])

def download_excel_file(file_id):
    service = get_drive_service()
    request = service.files().get_media(fileId=file_id)
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        status, done = downloader.next_chunk()
    fh.seek(0)
    return fh

def init_worksheet(sheet_id, worksheet_name, headers=None):
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive"
    ]
    credentials_dict = json.loads(GSHEET_CREDENTIALS_JSON)
    credentials = ServiceAccountCredentials.from_json_keyfile_dict(credentials_dict, scope)
    gc = gspread.authorize(credentials)
    spreadsheet = gc.open_by_key(sheet_id)
    try:
        worksheet = spreadsheet.worksheet(worksheet_name)
    except gspread.exceptions.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(title=worksheet_name, rows="1000", cols="20")
    if headers:
        current = worksheet.row_values(1)
        if not current or current != headers:
            worksheet.clear()
            worksheet.append_row(headers)
    return worksheet

def load_processed_excel_ids(sheet):
    return set(row[0] for row in sheet.get_all_values()[1:])

def save_processed_excel_id(sheet, file_id):
    sheet.append_row([file_id])

def get_prompt_from_sheet(sheet, use_case="xls"):
    rows = sheet.get_all_records()
    for row in rows:
        if row.get("출처") == use_case and row.get("현재사용여부") == "Y":
            prompt = (
                f"작성자 역할 설명: {row.get('작성자 역할 설명')}\n"
                f"전체 작성 조건: {row.get('전체 작성 조건')}\n"
                f"글 구성방식: {row.get('글 구성방식')}\n"
                f"필수 포함 항목: {row.get('필수 포함 항목')}\n"
                f"마무리 문장: {row.get('마무리 문장')}\n"
                f"추가 지시사항: {row.get('추가 지시사항')}"
            )
            return prompt
    return None

def generate_post(title, body_text, prompt):
    full_prompt = f"{prompt}\n\n제목: {title}\n본문: {body_text}\n이 내용을 바탕으로 블로그 글을 작성해 주세요."
    response = client.chat.completions.create(
        model=GPT_MODEL,
        messages=[{"role": "user", "content": full_prompt}],
        temperature=0.5,
        max_tokens=2500
    )
    return response.choices[0].message.content.strip()

def process_excel_to_sheet():
    processed_sheet = init_worksheet(GSHEET_ID, "ProcessedExcel", ["file_id"])
    processed_ids = load_processed_excel_ids(processed_sheet)
    files = list_excel_files_in_folder(GOOGLE_DRIVE_FOLDER_ID)
    target_sheet = init_worksheet(GSHEET_ID, "xls", ["작성날짜", "제목", "국문"])
    prompt_sheet = init_worksheet(GSHEET_ID, "prompt")

    prompt = get_prompt_from_sheet(prompt_sheet)
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    for file in files:
        if file["id"] in processed_ids:
            continue
        df = pd.read_excel(download_excel_file(file["id"]))
        for _, row in df.iterrows():
            title = str(row.get("제목", "")).strip()
            body = str(row.get("본문", "")).strip()
            if title and body:
                kor = generate_post(title, body, prompt)
                target_sheet.append_row([now_str, title, kor])
        save_processed_excel_id(processed_sheet, file["id"])

if __name__ == "__main__":
    process_excel_to_sheet()
