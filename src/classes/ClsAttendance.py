import pyodbc
import pandas as pd
from datetime import datetime
from typing import List, Dict, Optional

from src import config

class ClsAttendance:
    def __init__(self):
        self.connection_string = (
            f'DRIVER={{ODBC Driver 17 for SQL Server}};'
            f'SERVER={config.DB_SERVER};'
            f'UID={config.DB_USERNAME};'
            f'PWD={config.DB_PASSWORD};'
        )
        self.connection = None
        self.is_connected = False
        self.query = self._load_query()

    def _load_query(self) -> str:
        query_path = config.DATABASE_DIR / "queries" / "get_attendance_summary.sql"
        try:
            with open(query_path, 'r') as f:
                return f.read()
        except FileNotFoundError:
            raise FileNotFoundError(f"SQL query file not found at: {query_path}")

    def connect(self) -> bool:
        try:
            self.connection = pyodbc.connect(self.connection_string)
            self.is_connected = True
            return True
        except Exception as e:
            print(f"Attendance DB connection failed: {e}")
            self.is_connected = False
            return False

    def disconnect(self):
        if self.connection:
            self.connection.close()
            self.connection = None
            self.is_connected = False

    def get_attendance_data(self, nrp: str, start_date: str, end_date: str) -> List[Dict]:
        try:
            cursor = self.connection.cursor()
            cursor.execute(self.query, (nrp, start_date, end_date))
            columns = [column[0] for column in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]
        except Exception as e:
            print(f"Get attendance data failed: {e}")
            return []
        finally:
            if cursor:
                cursor.close()

    def parse_attendance_hours(self, attendance_hour_group: str) -> Dict[str, Optional[str]]:
        result = {'start_time': None, 'end_time': None}
        if not attendance_hour_group:
            return result

        for entry in attendance_hour_group.split(', '):
            if '(IN)' in entry:
                result['start_time'] = entry.replace(' (IN)', '').strip()
            elif '(OUT)' in entry:
                result['end_time'] = entry.replace(' (OUT)', '').strip()
        return result

    def format_attendance_data(self, attendance_data: List[Dict]) -> List[Dict]:
        formatted_list = []
        for record in attendance_data:
            times = self.parse_attendance_hours(record.get('attendance_hour_group', ''))
            date_obj = record.get('attendance_date')
            
            formatted_record = {
                'Date': date_obj.strftime('%m/%d/%Y') if date_obj else '',
                'Start Time': times.get('start_time', ''),
                'End Time': times.get('end_time', ''),
                'Name': record.get('name'),
                'NRP': record.get('nrp'),
                'District Code': record.get('dstrct_code')
            }
            formatted_list.append(formatted_record)
        return formatted_list

    def get_formatted_attendance_summary(self, nrp: str, start_date: str, end_date: str) -> pd.DataFrame:
        if not self.is_connected:
            print("Not connected to the database. Cannot fetch summary.")
            return pd.DataFrame()
            
        raw_data = self.get_attendance_data(nrp, start_date, end_date)
        if not raw_data:
            return pd.DataFrame()
            
        formatted_data = self.format_attendance_data(raw_data)
        return pd.DataFrame(formatted_data)