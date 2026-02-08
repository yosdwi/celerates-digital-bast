import pyodbc
import pandas as pd
from datetime import datetime
from typing import List, Dict

from src import config

class ClsRedMine:
    def __init__(self):
        self.connection_string = (
            f'DRIVER={{ODBC Driver 17 for SQL Server}};'
            f'SERVER={config.REDMINE_DB_SERVER};'
            f'DATABASE={config.REDMINE_DB_NAME or "DB_SATUPAMA_CIS"};'
            f'UID={config.REDMINE_DB_USERNAME};'
            f'PWD={config.REDMINE_DB_PASSWORD};'
        )
        self.connection = None
        self.is_connected = False
        self.query = self._load_query()

    def _load_query(self) -> str:
        query_path = config.DATABASE_DIR / "queries" / "get_redmine_tasks.sql"
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
            print(f"Redmine DB connection failed: {e}")
            self.is_connected = False
            return False

    def disconnect(self):
        if self.connection:
            self.connection.close()
            self.connection = None
            self.is_connected = False

    def get_tasks_data(self, start_date: str, end_date: str) -> List[Dict]:
        try:
            cursor = self.connection.cursor()
            params = (start_date, end_date, start_date, end_date)
            cursor.execute(self.query, params)
            columns = [column[0] for column in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]
        except Exception as e:
            print(f"Get Redmine tasks data failed: {e}")
            return []
        finally:
            if cursor:
                cursor.close()

    def format_tasks_for_nocodb(self, tasks_data: List[Dict], employee_mapping: dict) -> List[Dict]:
        formatted_data = []
        # --- START DIAGNOSTIC ---
        available_nrps = {emp_data.get('nrp') for emp_name, emp_data in employee_mapping.items() if emp_data.get('nrp')}
        print(f"DEBUG: Available NRPs from NocoDB to match against: {available_nrps}")
        print(f"DEBUG: NRPs from Redmine tasks being processed: {[task.get('nrp') for task in tasks_data]}")
        # --- END DIAGNOSTIC ---
        for task in tasks_data:
            start_date_str = None
            if task.get('start_date'):
                date_val = task['start_date']
                start_date_str = date_val.strftime('%Y-%m-%d') if isinstance(date_val, datetime) else str(date_val)[:10]

            end_date_str = None
            if task.get('due_date'):
                date_val = task['due_date']
                end_date_str = date_val.strftime('%Y-%m-%d') if isinstance(date_val, datetime) else str(date_val)[:10]

            employee_data_id = None
            task_nrp = task.get('nrp')
            if employee_mapping and task_nrp:
                for emp_name, emp_data in employee_mapping.items():
                    if emp_data.get('nrp') == task_nrp:
                        employee_data_id = [emp_data['id']]
                        break
            
            formatted_record = {
                'Task List': task.get('isu_subject', ''),
                'Requestor': task.get('author_name', ''),
                'Employee Data': employee_data_id,
                'Status': task.get('status_desc', ''),
                'Start Date': start_date_str,
                'End Date': end_date_str,
                'Pencapaian': task.get('done_ratio', 0),
                'Tracker Name': task.get('tracker_name', ''),
                'NRP': task_nrp
            }
            formatted_data.append(formatted_record)
        return formatted_data

    def get_formatted_tasks_summary(self, start_date: str, end_date: str, employee_mapping: dict) -> pd.DataFrame:
        if not self.is_connected:
            return pd.DataFrame()

        raw_data = self.get_tasks_data(start_date, end_date)
        if not raw_data:
            return pd.DataFrame()

        formatted_data = self.format_tasks_for_nocodb(raw_data, employee_mapping)
        return pd.DataFrame(formatted_data)
