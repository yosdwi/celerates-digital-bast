import pandas as pd
import logging
import time
import random
from datetime import datetime, timedelta
from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials
from googleapiclient.errors import HttpError
from calendar import monthrange

from src import config
from src.classes.ClsNocoDBProcessor import ClsNocoDBProcessor

class ClsAttendanceSheetProcessor:
    def __init__(self):
        self.credentials_path = config.SHEETS_CREDENTIALS_PATH
        self.mapping_config = config.ATTENDANCE_SHEET_MAPPING
        self.service = self._authenticate()

        schedule_shifting_table = config.NOCODB_TABLES.get("schedule_shifting")
        timesheet_table = config.NOCODB_TABLES.get("timesheet")

        self.nocodb_schedule = ClsNocoDBProcessor(config.APP_BASE_ID, schedule_shifting_table) if schedule_shifting_table else None
        self.nocodb_timesheet = ClsNocoDBProcessor(config.APP_BASE_ID, timesheet_table) if timesheet_table else None

    def _retry_with_backoff(self, func, max_retries=5):
        for attempt in range(max_retries):
            try:
                result = func()
                time.sleep(random.uniform(2.0, 5.0))
                return result
            except HttpError as e:
                if e.resp.status == 429:
                    if attempt == max_retries - 1:
                        raise e
                    delay = (2 ** attempt) * 5 + random.uniform(1, 3)
                    print(f"Rate limit hit, retrying in {delay:.1f} seconds (attempt {attempt + 1}/{max_retries})")
                    time.sleep(delay)
                else:
                    raise e
        raise Exception("Max retries exceeded")

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

    def generate_full_month_attendance(self, employee_name: str, target_date: datetime, employee_id: str = None):
        start_of_month = target_date.replace(day=1)
        next_month = start_of_month.replace(day=28) + timedelta(days=4)
        end_of_month = next_month - timedelta(days=next_month.day)

        dates = []
        current_date = start_of_month
        while current_date <= end_of_month:
            dates.append(current_date)
            current_date += timedelta(days=1)

        schedule_data = self._get_schedule_data(employee_name)
        timesheet_data = self._get_timesheet_data(employee_name, start_of_month, end_of_month)

        full_month_data = []
        for date in dates:
            date_str = date.strftime('%Y-%m-%d')

            timesheet_record = next((t for t in timesheet_data if t.get('Date') == date_str), {})

            is_iot_operations = employee_info.get('role') == 'IoT Operations' if employee_info else False

            shift_code = self._get_shift_code(schedule_data, date, is_iot_operations, timesheet_record, attendance_record)
            schedule_in, schedule_out = self._get_schedule_times(schedule_data, date, is_iot_operations)
            attendance_code = self._get_attendance_code(timesheet_record)

            row_data = {
                'Employee ID': employee_id or '',
                'Name': employee_name,
                'Date': date_str,
                'Shift': shift_code,
                'Schedule In': schedule_in,
                'Schedule Out': schedule_out,
                'Attendance Code': attendance_code,
                'Start Time': timesheet_record.get('Start Time', ''),
                'End Time': timesheet_record.get('End Time', ''),
                'Keterangan': timesheet_record.get('Remarks', '')
            }

            full_month_data.append(row_data)

        return pd.DataFrame(full_month_data)

    def generate_full_month_attendance_with_actual_times(self, employee_name: str, target_date: datetime, employee_id: str = None, attendance_data: dict = None, employee_info: dict = None, month_name: str = "Januari"):
        schedule_data = self._get_schedule_data(employee_name, employee_info)
        timesheet_data = self._get_timesheet_data(employee_name, target_date.replace(day=1), target_date.replace(day=28) + timedelta(days=4), month_name)

        if attendance_data is None:
            attendance_data = {}

        full_month_data = []
        for day in range(1, 32):
            try:
                current_date = target_date.replace(day=day)
                date_str = current_date.strftime('%Y-%m-%d')

                timesheet_record = next((t for t in timesheet_data if t.get('Date') == date_str), {})
                attendance_record = attendance_data.get(date_str, {})

                is_iot_operations = employee_info.get('role') == 'IoT Operations' if employee_info else False

                shift_code = self._get_shift_code(schedule_data, current_date, is_iot_operations, timesheet_record, attendance_record)
                schedule_in, schedule_out = self._get_schedule_times(schedule_data, current_date, is_iot_operations)
                attendance_code = self._get_attendance_code(timesheet_record)

                actual_start_time = attendance_record.get('Start Time', timesheet_record.get('Start Time', ''))
                actual_end_time = attendance_record.get('End Time', timesheet_record.get('End Time', ''))

                row_data = {
                    'Employee ID': employee_id or '',
                    'Name': employee_name,
                    'Date': date_str,
                    'Shift': shift_code,
                    'Schedule In': schedule_in,
                    'Schedule Out': schedule_out,
                    'Attendance Code': attendance_code,
                    'Start Time': actual_start_time,
                    'End Time': actual_end_time,
                    'Keterangan': timesheet_record.get('Remarks', ''),
                    'Last Modified': attendance_record.get('Last Modified', '')
                }
            except ValueError:
                row_data = {
                    'Employee ID': '',
                    'Name': '',
                    'Date': '',
                    'Shift': '',
                    'Schedule In': '',
                    'Schedule Out': '',
                    'Attendance Code': '',
                    'Start Time': '',
                    'End Time': '',
                    'Keterangan': '',
                    'Last Modified': ''
                }

            full_month_data.append(row_data)

        return pd.DataFrame(full_month_data)

    def _get_schedule_data(self, employee_name, employee_info=None):
        if not self.nocodb_schedule:
            return []

        try:
            where = f"(Employee Name,like,%{employee_name.strip().title()}%)"
            response = self.nocodb_schedule.get_records(limit=1000, where=where)
            return response.get('records', []) if response else []
        except Exception as e:
            logging.warning(f"Failed to get schedule data for {employee_name}: {e}")
            return []

    def _get_timesheet_data(self, employee_name, start_date, end_date, month_name="Januari"):
        if not self.nocodb_timesheet:
            return []

        try:
            where = f"(Calendar Month,eq,{month_name})~and(Employee Name,like,%{employee_name.strip().title()}%)"
            response = self.nocodb_timesheet.get_records(limit=1000, where=where, fields="Date,Start Time,End Time,Holiday,Remarks")

            processed_data = []
            for record in response.get('records', []) if response else []:
                fields = record.get('fields', {})
                processed_data.append({
                    'Date': fields.get('Date'),
                    'Start Time': fields.get('Start Time'),
                    'End Time': fields.get('End Time'),
                    'Holiday': fields.get('Holiday'),
                    'Remarks': fields.get('Remarks', '')
                })

            return processed_data
        except Exception as e:
            logging.warning(f"Failed to get timesheet data for {employee_name}: {e}")
            return []

    def _get_shift_code(self, schedule_data, date, is_iot_operations, timesheet_record=None, attendance_record=None):
        if is_iot_operations and schedule_data:
            for schedule in schedule_data:
                schedule_fields = schedule.get('fields', {})
                schedule_date = schedule_fields.get('Date')
                if schedule_date == date.strftime('%Y-%m-%d'):
                    work_type = schedule_fields.get('Work Type', [])
                    if isinstance(work_type, list) and 'OFF' in work_type:
                        return 'Day Off'

                    shift_code = schedule_fields.get('Shift Code', 'Day Off')
                    if isinstance(shift_code, list) and shift_code:
                        return shift_code[0]
                    return shift_code if shift_code else 'Day Off'
            return 'Day Off'
        else:
            has_attendance = (attendance_record and (attendance_record.get('Start Time') or attendance_record.get('End Time')))
            has_timesheet_time = (timesheet_record and (timesheet_record.get('Start Time') or timesheet_record.get('End Time')))
            is_holiday = (timesheet_record and str(timesheet_record.get('Holiday', '')).upper() == 'H')

            if is_holiday or not (has_attendance or has_timesheet_time):
                return 'Day Off'
            else:
                return 'N'  

    def _get_schedule_times(self, schedule_data, date, is_iot_operations):
        if is_iot_operations and schedule_data:
            for schedule in schedule_data:
                schedule_fields = schedule.get('fields', {})
                schedule_date = schedule_fields.get('Date')
                if schedule_date == date.strftime('%Y-%m-%d'):
                    start_time = schedule_fields.get('Start Time') or '07:30'
                    end_time = schedule_fields.get('End Time') or '16:30'
                    if isinstance(start_time, str) and ':' in start_time:
                        start_time = start_time.split(':')[:2]  # Take HH:MM only
                        start_time = ':'.join(start_time)
                    if isinstance(end_time, str) and ':' in end_time:
                        end_time = end_time.split(':')[:2]  # Take HH:MM only
                        end_time = ':'.join(end_time)
                    return start_time, end_time
        return '07:30', '16:30'

    def _get_attendance_code(self, timesheet_record):
        holiday_value = timesheet_record.get('Holiday', '')

        if holiday_value and str(holiday_value).upper() == 'H':
            return ''  # Kosong jika libur (H di timesheet = libur)
        elif timesheet_record.get('Start Time') or timesheet_record.get('End Time'):
            return 'H'  # H jika hadir (ada start/end time)
        else:
            return ''  # Kosong jika tidak ada data

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
            self._retry_with_backoff(
                lambda: self.service.spreadsheets().values().batchUpdate(spreadsheetId=sheet_id, body=body).execute()
            )
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

            white_color = {"red": 1.0, "green": 1.0, "blue": 1.0}
            manual_edit_color_hex = "#ffd965"
            manual_edit_color_rgb = hex_to_rgb(manual_edit_color_hex)

            fields_config = mapping.get('fields', {})
            start_time_col_conf = fields_config.get('Start Time', {})
            end_time_col_conf = fields_config.get('End Time', {})

            if start_time_col_conf.get('column') and end_time_col_conf.get('column'):
                start_time_col_idx = col_to_index(start_time_col_conf['column'])
                end_time_col_idx = col_to_index(end_time_col_conf['column'])
                end_row = start_row + len(df) - 1

                requests.append({
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id_num,
                            "startRowIndex": start_row - 1,
                            "endRowIndex": end_row,
                            "startColumnIndex": start_time_col_idx,
                            "endColumnIndex": start_time_col_idx + 1
                        },
                        "cell": {"userEnteredFormat": {"backgroundColor": white_color}},
                        "fields": "userEnteredFormat.backgroundColor"
                    }
                })

                requests.append({
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id_num,
                            "startRowIndex": start_row - 1,
                            "endRowIndex": end_row,
                            "startColumnIndex": end_time_col_idx,
                            "endColumnIndex": end_time_col_idx + 1
                        },
                        "cell": {"userEnteredFormat": {"backgroundColor": white_color}},
                        "fields": "userEnteredFormat.backgroundColor"
                    }
                })

                if 'Last Modified' in df.columns:
                    for i, row in df.iterrows():
                        last_modified_by = str(row.get('Last Modified', ''))
                        if last_modified_by and '@system.com' not in last_modified_by:
                            r_idx = start_row + i
                            requests.append({"repeatCell": {"range": {"sheetId": sheet_id_num, "startRowIndex": r_idx - 1, "endRowIndex": r_idx, "startColumnIndex": start_time_col_idx, "endColumnIndex": start_time_col_idx + 1}, "cell": {"userEnteredFormat": {"backgroundColor": manual_edit_color_rgb}}, "fields": "userEnteredFormat.backgroundColor"}})
                            requests.append({"repeatCell": {"range": {"sheetId": sheet_id_num, "startRowIndex": r_idx - 1, "endRowIndex": r_idx, "startColumnIndex": end_time_col_idx, "endColumnIndex": end_time_col_idx + 1}, "cell": {"userEnteredFormat": {"backgroundColor": manual_edit_color_rgb}}, "fields": "userEnteredFormat.backgroundColor"}})

            if requests:
                self._retry_with_backoff(
                    lambda: self.service.spreadsheets().batchUpdate(spreadsheetId=sheet_id, body={"requests": requests}).execute()
                )
                logging.info(f"Pemformatan diterapkan untuk {sheet_name}: reset ke putih dan highlight manual edit.")

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
