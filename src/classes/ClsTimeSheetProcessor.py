import pandas as pd
from datetime import datetime
from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials

from src import config

class ClsTimeSheetProcessor:
    def __init__(self):
        self.credentials_path = config.SHEETS_CREDENTIALS_PATH
        self.mapping_config = config.SHEET_MAPPING
        self.service = self._authenticate()
        self.iot_task_list = config.TASKLIST_IOT

    def _authenticate(self):
        if not self.credentials_path:
            raise ValueError("Google Sheets credentials path is not configured.")
        try:
            scopes = ['https://www.googleapis.com/auth/spreadsheets']
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

    def update_timesheet_data(self, sheet_id: str, df: pd.DataFrame, employee_name: str, target_date: datetime, mapping_key: str, employee_role: str = None):
        if not self.service:
            print("Authentication failed. Cannot update timesheet.")
            return False

        mapping = self.mapping_config.get(mapping_key)
        if not mapping:
            print(f"No mapping configuration found for key: {mapping_key}")
            return False

        if not self._update_metadata(sheet_id, employee_name, mapping, target_date):
            return False
        
        if df.empty:
            print(f"DataFrame is empty for {employee_name}. Nothing to update.")
            return True

        task_list_column = None
        if 'Task List IoT Table' in df.columns and not df['Task List IoT Table'].iloc[0] is None and df['Task List IoT Table'].iloc[0]:
            task_list_column = 'Task List IoT Table'
        elif 'Task List Table' in df.columns and not df['Task List Table'].iloc[0] is None and df['Task List Table'].iloc[0]:
            task_list_column = 'Task List Table'

        if task_list_column:
            df['Work Description'] = df[task_list_column].apply(
                lambda tasks: "\n".join(
                    [
                        task.get('fields', {}).get('Task List', '')
                        for task in tasks
                        if isinstance(task, dict) and task.get('fields', {}).get('Task List')
                    ]
                ) if isinstance(tasks, list) else ''
            )

        return self._update_sheet_with_mapping(sheet_id, df, mapping, employee_name)

    def _update_metadata(self, sheet_id: str, employee_name: str, mapping: dict, target_date: datetime):
        metadata_config = mapping.get('metadata_fields')
        if not metadata_config:
            return True

        start_of_month = target_date.replace(day=1)
        next_month = start_of_month.replace(day=28) + pd.Timedelta(days=4)
        end_of_month = next_month - pd.Timedelta(days=next_month.day)

        data_map = {"employee_name": employee_name, "start_date": start_of_month, "end_date": end_of_month}
        
        batch_data = []
        for key, conf in metadata_config.items():
            cell, fmt = conf.get('cell'), conf.get('format')
            if not cell or key not in data_map: continue
            
            value = data_map[key]
            formatted_value = value.strftime(fmt) if isinstance(value, datetime) and fmt else str(value)
            batch_data.append({'range': f"'{employee_name}'!{cell}", 'values': [[formatted_value]]})

        if not batch_data: return True
        
        try:
            body = {'valueInputOption': 'USER_ENTERED', 'data': batch_data}
            self.service.spreadsheets().values().batchUpdate(spreadsheetId=sheet_id, body=body).execute()
            return True
        except Exception as e:
            print(f"Failed to update metadata for {employee_name}: {e}")
            raise

    def _update_sheet_with_mapping(self, sheet_id: str, df: pd.DataFrame, mapping: dict, employee_name: str):
        start_row = mapping.get('start_row', 1)
        fields_config = mapping.get('fields', {})
        batch_data = []

        if 'Work Description' in df.columns:
            df['Work Description'] = df['Work Description'].apply(
                lambda tasks: "\n".join(
                    [
                        task.get('fields', {}).get('Task List', '')
                        for task in tasks
                        if isinstance(task, dict) and task.get('fields', {}).get('Task List')
                    ]
                ) if isinstance(tasks, list) else tasks
            )
        
        for field, conf in fields_config.items():
            if field not in df.columns: continue
            col = conf.get('column')
            if not col: continue
            
            values = [[self._format_value(row.get(field, ''), conf)] for _, row in df.iterrows()]
            range_str = f"'{employee_name}'!{col}{start_row}:{col}{start_row + len(df) - 1}"
            batch_data.append({'range': range_str, 'values': values})

        if not batch_data: return False
        
        try:
            body = {'valueInputOption': 'USER_ENTERED', 'data': batch_data}
            self.service.spreadsheets().values().batchUpdate(spreadsheetId=sheet_id, body=body).execute()
            self._apply_formatting(sheet_id, df, mapping, start_row, employee_name)
            return True
        except Exception as e:
            print(f"Failed to update sheet '{employee_name}': {e}")
            raise

    def _apply_formatting(self, sheet_id: str, df: pd.DataFrame, mapping: dict, start_row: int, sheet_name: str):
        try:
            sheets_meta = self.service.spreadsheets().get(spreadsheetId=sheet_id).execute().get('sheets', [])
            sheet_id_num = next((s['properties']['sheetId'] for s in sheets_meta if s['properties']['title'] == sheet_name), None)
            if sheet_id_num is None:
                print(f"Sheet '{sheet_name}' not found for formatting.")
                return

            requests = []
            
            fields_config = mapping.get('fields', {})
            end_row = start_row + len(df) - 1
            
            def hex_to_rgb(hex_color):
                hex_color = hex_color.lstrip('#')
                return {
                    "red": int(hex_color[0:2], 16) / 255.0,
                    "green": int(hex_color[2:4], 16) / 255.0,
                    "blue": int(hex_color[4:6], 16) / 255.0
                }

            def col_to_index(col_letter):
                return ord(col_letter.upper()) - ord('A')

            for field, conf in fields_config.items():
                col = conf.get('column')
                color_hex = conf.get('color')
                if not col or not color_hex:
                    continue

                col_idx = col_to_index(col)
                requests.append({
                    "repeatCell": {
                        "range": {"sheetId": sheet_id_num, "startRowIndex": start_row - 1, "endRowIndex": end_row, "startColumnIndex": col_idx, "endColumnIndex": col_idx + 1},
                        "cell": {"userEnteredFormat": {"backgroundColor": hex_to_rgb(color_hex)}},
                        "fields": "userEnteredFormat.backgroundColor"
                    }
                })

            holiday_rows = [start_row + i for i, row in df.iterrows() if str(row.get('Is Holiday', '')) == 'H']
            if holiday_rows:
                holiday_color = { "red": 244/255, "green": 176/255, "blue": 131/255 } # Corresponds to #f4b083
                start_col_idx = col_to_index(min(c['column'] for c in fields_config.values() if 'column' in c))
                end_col_idx = col_to_index(max(c['column'] for c in fields_config.values() if 'column' in c)) + 1
                
                for r_idx in holiday_rows:
                    requests.append({
                        "repeatCell": {
                            "range": {"sheetId": sheet_id_num, "startRowIndex": r_idx - 1, "endRowIndex": r_idx, "startColumnIndex": start_col_idx, "endColumnIndex": end_col_idx},
                            "cell": {"userEnteredFormat": {"backgroundColor": holiday_color}},
                            "fields": "userEnteredFormat.backgroundColor"
                        }
                    })

            manual_edit_color_hex = "#ffd965"
            manual_edit_color_rgb = hex_to_rgb(manual_edit_color_hex)
            start_time_col_conf = fields_config.get('Start Time', {})
            end_time_col_conf = fields_config.get('End Time', {})

            if start_time_col_conf.get('column') and end_time_col_conf.get('column'):
                start_time_col_idx = col_to_index(start_time_col_conf['column'])
                end_time_col_idx = col_to_index(end_time_col_conf['column'])

                manual_edit_rows = [start_row + i for i, row in df.iterrows() if row.get('IsManualEdit')]
                
                for r_idx in manual_edit_rows:
                    requests.append({
                        "repeatCell": {
                            "range": {"sheetId": sheet_id_num, "startRowIndex": r_idx - 1, "endRowIndex": r_idx, "startColumnIndex": start_time_col_idx, "endColumnIndex": start_time_col_idx + 1},
                            "cell": {"userEnteredFormat": {"backgroundColor": manual_edit_color_rgb}},
                            "fields": "userEnteredFormat.backgroundColor"
                        }
                    })
                    requests.append({
                        "repeatCell": {
                            "range": {"sheetId": sheet_id_num, "startRowIndex": r_idx - 1, "endRowIndex": r_idx, "startColumnIndex": end_time_col_idx, "endColumnIndex": end_time_col_idx + 1},
                            "cell": {"userEnteredFormat": {"backgroundColor": manual_edit_color_rgb}},
                            "fields": "userEnteredFormat.backgroundColor"
                        }
                    })

            if requests:
                self.service.spreadsheets().batchUpdate(spreadsheetId=sheet_id, body={"requests": requests}).execute()
                print(f"Applied formatting to {sheet_name} (defaults, holidays, manual edits).")

        except Exception as e:
            print(f"Failed to apply formatting: {e}")


    def _format_value(self, value, config):
        if pd.isna(value) or str(value).strip() == '': return ''
        
        format_type = config.get('format')
        if format_type == 'date': return pd.to_datetime(value).strftime(config.get('date_format', '%Y-%m-%d'))
        if format_type == 'time':
            if not value or str(value).strip() == '': return ''
            return datetime.strptime(str(value), '%H:%M').strftime(config.get('time_format', '%H:%M'))
        if format_type == 'number': return float(value) if str(value).strip() else 0.0
        return str(value)
