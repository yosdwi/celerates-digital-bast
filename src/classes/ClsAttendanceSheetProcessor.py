import pandas as pd
import logging
from datetime import datetime
from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials

from src import config

class ClsAttendanceSheetProcessor:
    def __init__(self):
        self.credentials_path = config.SHEETS_CREDENTIALS_PATH
        self.mapping_config = config.ATTENDANCE_SHEET_MAPPING
        self.service = self._authenticate()

    def _authenticate(self):
        if not self.credentials_path:
            raise ValueError("Lokasi kredensial Google Sheets tidak diatur.")
        try:
            scopes = ['https://www.googleapis.com/auth/spreadsheets']
            creds = Credentials.from_service_account_file(self.credentials_path, scopes=scopes)
            return build('sheets', 'v4', credentials=creds)
        except Exception as e:
            logging.error(f"Autentikasi Google Sheets gagal: {e}")
            raise

    def get_sheet_id_from_url(self, sheet_url: str) -> str:
        try:
            return sheet_url.split('/d/')[1].split('/')[0]
        except (IndexError, AttributeError):
            return sheet_url

    def update_attendance_data(self, sheet_id: str, df: pd.DataFrame, employee_name: str, mapping_key: str = "employee_attendance"):
        if not self.service:
            logging.error("Autentikasi gagal, tidak dapat memperbarui sheet.")
            return False

        mapping = self.mapping_config.get(mapping_key)
        if not mapping:
            logging.warning(f"Konfigurasi mapping tidak ditemukan untuk kunci: {mapping_key}")
            return False
            
        if df.empty:
            logging.info(f"DataFrame kosong untuk {employee_name}, tidak ada yang diperbarui.")
            return True

        return self._update_sheet_with_mapping(sheet_id, df, mapping, employee_name)

    def _update_sheet_with_mapping(self, sheet_id: str, df: pd.DataFrame, mapping: dict, sheet_name: str):
        start_row = mapping.get('start_row', 1)
        fields_config = mapping.get('fields', {})
        batch_data = []

        for field, conf in fields_config.items():
            if field not in df.columns: continue
            col = conf.get('column')
            if not col: continue

            values = [[self._format_value(row.get(field, ''), conf)] for _, row in df.iterrows()]
            range_str = f"'{sheet_name}'!{col}{start_row}:{col}{start_row + len(df) - 1}"
            batch_data.append({'range': range_str, 'values': values})

        if not batch_data: return False

        try:
            body = {'valueInputOption': 'USER_ENTERED', 'data': batch_data}
            self.service.spreadsheets().values().batchUpdate(spreadsheetId=sheet_id, body=body).execute()
            self._apply_formatting(sheet_id, df, mapping, start_row, sheet_name)
            return True
        except Exception as e:
            logging.error(f"Gagal memperbarui sheet '{sheet_name}': {e}")
            raise

    def _apply_formatting(self, sheet_id: str, df: pd.DataFrame, mapping: dict, start_row: int, sheet_name: str):
        try:
            sheets_meta = self.service.spreadsheets().get(spreadsheetId=sheet_id).execute().get('sheets', [])
            sheet_id_num = next((s['properties']['sheetId'] for s in sheets_meta if s['properties']['title'] == sheet_name), None)
            if sheet_id_num is None:
                logging.warning(f"Sheet '{sheet_name}' tidak ditemukan untuk pemformatan.")
                return

            requests = []
            
            def hex_to_rgb(hex_color):
                hex_color = hex_color.lstrip('#')
                return {"red": int(hex_color[0:2], 16) / 255.0, "green": int(hex_color[2:4], 16) / 255.0, "blue": int(hex_color[4:6], 16) / 255.0}

            def col_to_index(col_letter):
                return ord(col_letter.upper()) - ord('A')

            manual_edit_color_hex = "#ffd965"
            manual_edit_color_rgb = hex_to_rgb(manual_edit_color_hex)
            
            fields_config = mapping.get('fields', {})
            start_time_col_conf = fields_config.get('Start Time', {})
            end_time_col_conf = fields_config.get('End Time', {})

            if 'Last Modified' in df.columns and start_time_col_conf.get('column') and end_time_col_conf.get('column'):
                start_time_col_idx = col_to_index(start_time_col_conf['column'])
                end_time_col_idx = col_to_index(end_time_col_conf['column'])

                for i, row in df.iterrows():
                    last_modified_by = str(row.get('Last Modified', ''))
                    if last_modified_by and '@system.com' not in last_modified_by:
                        r_idx = start_row + i
                        requests.append({"repeatCell": {"range": {"sheetId": sheet_id_num, "startRowIndex": r_idx - 1, "endRowIndex": r_idx, "startColumnIndex": start_time_col_idx, "endColumnIndex": start_time_col_idx + 1}, "cell": {"userEnteredFormat": {"backgroundColor": manual_edit_color_rgb}}, "fields": "userEnteredFormat.backgroundColor"}})
                        requests.append({"repeatCell": {"range": {"sheetId": sheet_id_num, "startRowIndex": r_idx - 1, "endRowIndex": r_idx, "startColumnIndex": end_time_col_idx, "endColumnIndex": end_time_col_idx + 1}, "cell": {"userEnteredFormat": {"backgroundColor": manual_edit_color_rgb}}, "fields": "userEnteredFormat.backgroundColor"}})

            if requests:
                self.service.spreadsheets().batchUpdate(spreadsheetId=sheet_id, body={"requests": requests}).execute()

        except Exception as e:
            logging.error(f"Gagal menerapkan pemformatan: {e}")


    def _format_value(self, value, config):
        if pd.isna(value) or str(value).strip() == '': return ''
        
        format_type = config.get('format')
        if format_type == 'date': return pd.to_datetime(value).strftime(config.get('date_format', '%Y-%m-%d'))
        if format_type == 'time':
            if not value or str(value).strip() == '': return ''
            return datetime.strptime(str(value), '%H:%M').time().strftime(config.get('time_format', '%H:%M'))
        if format_type == 'number': return float(value) if str(value).strip() else 0.0
        return str(value)
