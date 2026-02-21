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
                return func()
            except HttpError as e:
                if e.resp.status == 429 and attempt < max_retries - 1:
                    delay = (2 ** attempt) * 5 + random.uniform(1, 3)
                    logging.warning(f"Rate limit hit, retrying in {delay:.1f}s")
                    time.sleep(delay)
                else:
                    raise e
            except Exception as e:
                if "SSL" in str(e) or "connection" in str(e).lower() and attempt < max_retries - 1:
                    delay = (2 ** attempt) * 3 + random.uniform(2, 4)
                    logging.warning(f"Connection error, retrying in {delay:.1f}s: {e}")
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

    def process_noco_records_to_dataframe(self, employee_name: str, employee_id: str, attendance_data: dict, employee_info: dict, month_name: str, target_date: datetime):
        if not attendance_data:
            return pd.DataFrame()

        schedule_data = self._get_schedule_data(employee_name, employee_info)
        timesheet_data = self._get_timesheet_data(employee_name, target_date.replace(day=1), target_date.replace(day=28) + timedelta(days=4), month_name)

        processed_records = []
        for date_str, attendance_record in attendance_data.items():
            try:
                current_date = datetime.strptime(date_str, '%Y-%m-%d')
                timesheet_record = next((t for t in timesheet_data if t.get('Date') == date_str), {})
                
                is_iot_operations = employee_info.get('role') == 'IoT Operations'

                shift_code = self._get_shift_code(schedule_data, current_date, is_iot_operations, timesheet_record, attendance_record)
                schedule_in, schedule_out = self._get_schedule_times(schedule_data, current_date, is_iot_operations)
                attendance_code = self._get_attendance_code(timesheet_record)

                processed_records.append({
                    'Employee ID': employee_id,
                    'Name': employee_name,
                    'Date': date_str,
                    'Shift': shift_code,
                    'Schedule In': schedule_in,
                    'Schedule Out': schedule_out,
                    'Attendance Code': attendance_code,
                    'Start Time': attendance_record.get('Start Time', ''),
                    'End Time': attendance_record.get('End Time', ''),
                    'Keterangan': timesheet_record.get('Remarks', ''),
                    'Last Modified': attendance_record.get('Last Modified', '')
                })
            except Exception as e:
                logging.warning(f"Could not process record for {employee_name} on {date_str}: {e}")
                continue
        
        return pd.DataFrame(processed_records)

    def _get_schedule_data(self, employee_name, employee_info=None):
        if not self.nocodb_schedule:
            return []
        try:
            where = f"(Employee Name,eq,{employee_name.strip().title()})"
            response = self.nocodb_schedule.get_records(limit=1000, where=where)
            return response.get('list', []) if response else []
        except Exception as e:
            logging.warning(f"Failed to get schedule data for {employee_name}: {e}")
            return []

    def _get_timesheet_data(self, employee_name, start_date, end_date, month_name="Januari"):
        if not self.nocodb_timesheet:
            return []
        try:
            where = f"(Calendar Month,eq,{month_name})~and(Employee Name,eq,{employee_name.strip().title()})"
            response = self.nocodb_timesheet.get_records(limit=1000, where=where)
            return response.get('list', []) if response else []
        except Exception as e:
            logging.warning(f"Failed to get timesheet data for {employee_name}: {e}")
            return []

    def _get_shift_code(self, schedule_data, date, is_iot_operations, timesheet_record=None, attendance_record=None):
        if is_iot_operations and schedule_data:
            for schedule in schedule_data:
                if schedule.get('Date') == date.strftime('%Y-%m-%d'):
                    work_type_raw = schedule.get('Work Type', [])
                    work_type = work_type_raw if isinstance(work_type_raw, str) else (work_type_raw[0] if isinstance(work_type_raw, list) and work_type_raw else '')
                    if 'OFF' in str(work_type).upper():
                        return 'Day Off'
                    shift = schedule.get('Shift Code', 'Day Off')
                    return shift[0] if isinstance(shift, list) and shift else shift
            return 'Day Off'

        has_time = attendance_record and (attendance_record.get('Start Time') or attendance_record.get('End Time'))
        is_holiday = timesheet_record and str(timesheet_record.get('Holiday', '')).upper() == 'H'
        return 'Day Off' if is_holiday or not has_time else 'N'

    def _get_schedule_times(self, schedule_data, date, is_iot_operations):
        if is_iot_operations and schedule_data:
            for schedule in schedule_data:
                if schedule.get('Date') == date.strftime('%Y-%m-%d'):
                    start_time_raw = schedule.get('Start Time', '07:30')
                    end_time_raw = schedule.get('End Time', '16:30')

                    start_time = str(start_time_raw[0] if isinstance(start_time_raw, list) and start_time_raw else start_time_raw)
                    end_time = str(end_time_raw[0] if isinstance(end_time_raw, list) and end_time_raw else end_time_raw)

                    start_formatted = ':'.join(start_time.split(' ')[-1].split(':')[:2]) if ':' in start_time else start_time
                    end_formatted = ':'.join(end_time.split(' ')[-1].split(':')[:2]) if ':' in end_time else end_time

                    return (start_formatted, end_formatted)
        return '07:30', '16:30'

    def _get_attendance_code(self, timesheet_record):
        if timesheet_record.get('Holiday') and str(timesheet_record.get('Holiday')).upper() == 'H': return ''
        return 'H' if timesheet_record.get('Start Time') or timesheet_record.get('End Time') else ''

    def update_attendance_data(self, sheet_id: str, df: pd.DataFrame, employee_name: str, mapping_key: str = "employee_attendance"):
        if df.empty:
            logging.info(f"DataFrame kosong untuk {employee_name}, tidak ada yang diperbarui.")
            return True

        mapping = self.mapping_config.get(mapping_key)
        if not mapping:
            logging.warning(f"Konfigurasi mapping tidak ditemukan untuk kunci: {mapping_key}")
            return False

        date_column = mapping.get('fields', {}).get('Date', {}).get('column')
        if not date_column:
            raise ValueError("Kolom 'Date' tidak ditemukan di mapping.")

        date_range_to_read = f"'{employee_name}'!{date_column}1:{date_column}"
        date_col_values = self._retry_with_backoff(
            lambda: self.service.spreadsheets().values().get(spreadsheetId=sheet_id, range=date_range_to_read).execute()
        ).get('values', [])
        
        date_to_row_map = {date[0]: i + 1 for i, date in enumerate(date_col_values) if date}

        return self._update_sheet_with_mapping(sheet_id, df, mapping, employee_name, date_to_row_map)

    def _update_sheet_with_mapping(self, sheet_id: str, df: pd.DataFrame, mapping: dict, sheet_name: str, date_to_row_map: dict):
        batch_data = []
        fields_config = mapping.get('fields', {})
        
        for _, row in df.iterrows():
            date_str = row.get('Date')
            if not date_str or date_str not in date_to_row_map:
                continue
            
            row_num = date_to_row_map[date_str]
            
            for field, conf in fields_config.items():
                if field not in row or conf.get('column') == 'ignore': continue
                
                col = conf.get('column')
                if not col: continue
                
                value = self._format_value(row.get(field), conf)
                range_str = f"'{sheet_name}'!{col}{row_num}"
                batch_data.append({'range': range_str, 'values': [[value]]})
        
        if not batch_data:
            logging.warning(f"Tidak ada data valid untuk di-batch update pada sheet {sheet_name}")
            return False

        try:
            body = {'valueInputOption': 'USER_ENTERED', 'data': batch_data}
            self._retry_with_backoff(
                lambda: self.service.spreadsheets().values().batchUpdate(spreadsheetId=sheet_id, body=body).execute()
            )
            return True
        except Exception as e:
            logging.error(f"Gagal memperbarui sheet '{sheet_name}': {e}")
            raise

    def _format_value(self, value, config):
        if pd.isna(value) or str(value).strip() == '': return ''
        format_type = config.get('format')
        if format_type == 'date': return pd.to_datetime(value).strftime(config.get('date_format', '%Y-%m-%d'))
        if format_type == 'time':
            if not value or str(value).strip() == '': return ''
            val_str = str(value[0] if isinstance(value, list) else value)
            return ':'.join(val_str.split(':')[:2])
        return str(value)

    def prepare_future_attendance_rows(self, sheet_id: str, sheet_name: str, employee_id: str, mapping_key: str = "employee_attendance"):
        try:
            mapping = self.mapping_config.get(mapping_key, {})
            fields_config = mapping.get('fields', {})
            date_column = fields_config.get('Date', {}).get('column')
            
            if not date_column:
                raise ValueError("Date column not found in attendance sheet mapping.")

            range_to_read = f"'{sheet_name}'!{date_column}2:{date_column}"
            response = self._retry_with_backoff(
                lambda: self.service.spreadsheets().values().get(spreadsheetId=sheet_id, range=range_to_read).execute()
            )
            
            values = response.get('values', [])
            last_date_str = next((row[0] for row in reversed(values) if row and row[0]), None)
            last_date = datetime.strptime(last_date_str, '%Y-%m-%d').date() if last_date_str else None
            
            today = datetime.now().date()
            start_date = (last_date + timedelta(days=1)) if last_date else today.replace(day=1)

            future_month_date = (today.replace(day=1) + timedelta(days=62)).replace(day=1)
            _, last_day = monthrange(future_month_date.year, future_month_date.month)
            target_end_date = future_month_date.replace(day=last_day)

            if start_date > target_end_date:
                return

            def col_to_index(col):
                return sum([(ord(c.upper()) - ord('A') + 1) * (26 ** i) for i, c in enumerate(reversed(col))]) - 1

            col_letters = [conf['column'] for conf in fields_config.values() if conf.get('column')]
            num_cols = max(col_to_index(c) for c in col_letters) + 1 if col_letters else 0
            field_to_col_index = {f: col_to_index(c['column']) for f, c in fields_config.items() if c.get('column')}

            new_rows = []
            current_date = start_date
            while current_date <= target_end_date:
                ordered_row = [''] * num_cols
                new_row_data = {'Employee ID': employee_id, 'Name': sheet_name, 'Date': current_date.strftime('%Y-%m-%d')}
                for field, value in new_row_data.items():
                    if field in field_to_col_index:
                        ordered_row[field_to_col_index[field]] = value
                new_rows.append(ordered_row)
                current_date += timedelta(days=1)
            
            if new_rows:
                body = {'values': new_rows}
                self._retry_with_backoff(
                    lambda: self.service.spreadsheets().values().append(
                        spreadsheetId=sheet_id, range=f"'{sheet_name}'!A1",
                        valueInputOption='USER_ENTERED', insertDataOption='INSERT_ROWS', body=body
                    ).execute()
                )
        except Exception as e:
            logging.error(f"Failed to prepare future attendance rows for '{sheet_name}': {e}")
            raise
