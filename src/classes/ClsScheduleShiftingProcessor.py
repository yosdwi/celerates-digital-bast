import pandas as pd
from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials

from src import config

class ClsScheduleShiftingProcessor:
    def __init__(self):
        self.credentials_path = config.SHEETS_CREDENTIALS_PATH
        self.service = self._authenticate()

    def _authenticate(self):
        if not self.credentials_path:
            raise ValueError("Google Sheets credentials path is not configured.")
        try:
            scopes = ['https://www.googleapis.com/auth/spreadsheets.readonly']
            creds = Credentials.from_service_account_file(self.credentials_path, scopes=scopes)
            return build('sheets', 'v4', credentials=creds)
        except Exception as e:
            print(f"Google Sheets authentication failed: {e}")
            raise

    def get_sheet_id_from_url(self, sheet_url: str) -> str:
        try:
            return sheet_url.split('/d/')[1].split('/')[0]
        except (IndexError, AttributeError):
            return sheet_url

    def read_sheet_to_dataframe(self, sheet_url: str, sheet_name: str) -> pd.DataFrame:
        if not self.service:
            print("Authentication failed. Cannot read sheet.")
            return pd.DataFrame()

        try:
            sheet_id = self.get_sheet_id_from_url(sheet_url)
            sheet = self.service.spreadsheets()
            result = sheet.values().get(spreadsheetId=sheet_id, range=sheet_name).execute()
            values = result.get('values', [])

            if not values:
                return pd.DataFrame()

            return pd.DataFrame(values[1:], columns=values[0])
        except Exception as e:
            print(f"Failed to read sheet '{sheet_name}': {e}")
            return pd.DataFrame()
