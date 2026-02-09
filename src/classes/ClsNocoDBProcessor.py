import requests
import json
import hashlib
import logging
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from src import config

class ClsNocoDBProcessor:
    def __init__(self, base_id: str, table_id: str):
        self.base_url = config.NOCODB_BASE_URL
        self.api_token = config.NOCODB_API_TOKEN
        self.base_id = base_id
        self.table_id = table_id
        self.headers = {
            'xc-token': self.api_token,
            'Content-Type': 'application/json'
        }

        self.session = requests.Session()
        retry_strategy = Retry(
            total=3,
            status_forcelist=[429, 500, 502, 503, 504],
            backoff_factor=1
        )
        adapter = HTTPAdapter(
            pool_connections=10,
            pool_maxsize=20,
            max_retries=retry_strategy
        )
        self.session.mount('http://', adapter)
        self.session.mount('https://', adapter)

    def create_record(self, data: dict):
        try:
            endpoint = f"{self.base_url}/api/v3/data/{self.base_id}/{self.table_id}/records"
            payload = {"fields": data}
            response = self.session.post(endpoint, headers=self.headers, json=payload, timeout=30)

            if response.status_code in [200, 201]:
                return response.json()
            else:
                logging.error(f"Gagal membuat record: {response.status_code} - {response.text}")
                return None
        except Exception as e:
            logging.error(f"Error saat membuat record: {e}")
            return None

    def bulk_create_records(self, data_list: list):
        try:
            endpoint = f"{self.base_url}/api/v3/data/{self.base_id}/{self.table_id}/records"
            payload = [{"fields": data} for data in data_list]
            response = requests.post(endpoint, headers=self.headers, json=payload, timeout=60)

            if response.status_code in [200, 201]:
                return response.json()
            else:
                logging.error(f"Gagal membuat record massal: {response.status_code} - {response.text}")
                return None
        except Exception as e:
            logging.error(f"Error saat membuat record massal: {e}")
            return None


    def get_records(self, limit: int = 25, offset: int = 0, where: str = None, fields: str = None):
        try:
            endpoint = f"{self.base_url}/api/v3/data/{self.base_id}/{self.table_id}/records"
            params = {"limit": limit, "offset": offset}
            if where:
                params["where"] = where
            if fields:
                params["fields"] = fields

            response = self.session.get(endpoint, headers=self.headers, params=params, timeout=30)

            if response.status_code == 200:
                return response.json()
            else:
                logging.error(f"Gagal mengambil records: {response.status_code} - {response.text}")
                return None
        except Exception as e:
            logging.error(f"Error saat mengambil records: {e}")
            return None

    def get_all_employees(self, role_filter: str = None):
        try:
            endpoint = f"{self.base_url}/api/v3/data/{self.base_id}/{self.table_id}/records"
            params = {"limit": 1000}
            if role_filter:
                params["where"] = f"(Role,eq,{role_filter})"
            
            response = self.session.get(endpoint, headers=self.headers, params=params, timeout=30)

            if response.status_code == 200:
                data = response.json()
                employee_mapping = {}

                for record in data.get('records', []):
                    fields = record.get('fields', {})
                    employee_name = fields.get('Employee Name')
                    employee_code = fields.get('Employee ID')
                    employee_nrp = fields.get('NRP')
                    employee_id = record.get('id')
                    role = fields.get('Role') 

                    if employee_name and employee_id:
                        employee_mapping[employee_name.strip().title()] = {
                            'id': employee_id,
                            'nrp': employee_nrp,
                            'employee_id': employee_code,
                            'role': role
                        }
                return employee_mapping
            else:
                logging.error(f"Gagal mengambil data karyawan: {response.status_code} - {response.text}")
                return {}
        except Exception as e:
            logging.error(f"Error saat mengambil data karyawan: {e}")
            return {}

    def generate_unique_key(self, date: str, employee_id: str) -> str:
        key_string = f"{date}_{employee_id}"
        return hashlib.md5(key_string.encode()).hexdigest()[:16]

    def upsert_attendance(self, attendance_data: dict):
        try:
            date = attendance_data["Date"]
            employee_id = attendance_data["Employee Data"][0]
            unique_key = self.generate_unique_key(date, str(employee_id))
            attendance_data["Unique Key"] = unique_key

            where_clause = f"(Unique Key,eq,{unique_key})"
            existing_records = self.get_records(limit=1, where=where_clause, fields="id,Last Modified")

            if existing_records and existing_records.get('records'):
                existing_record = existing_records['records'][0]
                
                last_modified_by = existing_record.get('fields', {}).get('Last Modified')
                if last_modified_by is not None and '@system.com' not in last_modified_by:
                    logging.info(f"Update dilewati untuk {unique_key}: data diubah manual oleh '{last_modified_by}'.")
                    return "skipped_manual_edit"

                record_id = existing_record['id']
                endpoint = f"{self.base_url}/api/v3/data/{self.base_id}/{self.table_id}/records"
                payload = {"id": record_id, "fields": attendance_data}

                update_response = requests.patch(endpoint, headers=self.headers, json=payload, timeout=30)
                if update_response.status_code in [200, 201]:
                    return update_response.json()
                else:
                    logging.error(f"Update absensi gagal: {update_response.status_code} - {update_response.text}")
                    return None
            else:
                return self.create_record(attendance_data)

        except Exception as e:
            logging.error(f"Upsert absensi gagal: {e}")
            return None

    def upsert_timesheet(self, timesheet_data: dict):
        try:
            employee_data = timesheet_data.get("Name Table")
            if not employee_data or not isinstance(employee_data, list) or not employee_data[0]:
                return None

            for key in ["Start Time Table", "End Time Table", "Task List Table"]:
                if timesheet_data.get(key) is None:
                    timesheet_data[key] = []

            date = timesheet_data["Date"]
            employee_id = timesheet_data["Name Table"][0]
            unique_key = self.generate_unique_key(date, str(employee_id))
            timesheet_data["Unique Key"] = unique_key

            where_clause = f"(Unique Key,eq,{unique_key})"
            existing_records = self.get_records(limit=1, where=where_clause)

            if existing_records and existing_records.get('records'):
                existing_record = existing_records['records'][0]
                record_id = existing_record['id']
                endpoint = f"{self.base_url}/api/v3/data/{self.base_id}/{self.table_id}/records"
                payload = {"id": record_id, "fields": timesheet_data}

                update_response = requests.patch(endpoint, headers=self.headers, json=payload, timeout=30)
                if update_response.status_code in [200, 201]:
                    return update_response.json()
                else:
                    logging.error(f"Update timesheet gagal: {update_response.status_code} - {update_response.text}")
                    return None
            else:
                return self.create_record(timesheet_data)

        except requests.exceptions.Timeout:
            logging.warning(f"Upsert timesheet timeout untuk karyawan {employee_id} pada {date}")
            return None
        except Exception as e:
            logging.error(f"Upsert timesheet gagal: {e}")
            return self.create_record(timesheet_data)


    def update_record(self, record_id: int, data: dict):
        try:
            endpoint = f"{self.base_url}/api/v3/data/{self.base_id}/{self.table_id}/records"
            payload = {"id": record_id, "fields": data}
            response = requests.patch(endpoint, headers=self.headers, json=payload, timeout=30)
            if response.status_code in [200, 201]:
                return response.json()
            else:
                logging.error(f"Update record gagal: {response.status_code} - {response.text}")
                return None
        except Exception as e:
            logging.error(f"Update record gagal: {e}")
            return None

    def batch_upsert_timesheets(self, records: list):
        if not records:
            return 0

        success_count = 0
        records_to_create = []
        records_to_update = {}

        for record in records:
            employee_data = record.get("Name Table")
            if not employee_data or not isinstance(employee_data, list) or not employee_data[0]:
                continue
            
            for key in ["Start Time Table", "End Time Table", "Task List Table"]:
                if record.get(key) is None:
                    record[key] = []

            date = record["Date"]
            employee_id = employee_data[0]
            unique_key = self.generate_unique_key(date, str(employee_id))
            record["Unique Key"] = unique_key
            records_to_update[unique_key] = record

        unique_keys = list(records_to_update.keys())
        where_clause = f"(Unique Key,in,{','.join(unique_keys)})"
        existing_records_response = self.get_records(limit=len(unique_keys), where=where_clause)

        existing_keys = {}
        if existing_records_response and existing_records_response.get('records'):
            for record in existing_records_response['records']:
                existing_keys[record['fields']['Unique Key']] = record['id']

        records_to_create_data = []
        for unique_key, record_data in records_to_update.items():
            if unique_key in existing_keys:
                record_id = existing_keys[unique_key]
                if self.update_record(record_id, record_data):
                    success_count += 1
            else:
                records_to_create_data.append(record_data)

        if records_to_create_data:
            created_records = self.bulk_create_records(records_to_create_data)
            if created_records:
                success_count += len(created_records)

        return success_count


    def generate_task_unique_key(self, date: str, employee_id: str, task_list: str) -> str:
        clean_task = ''.join(c for c in task_list if c.isalnum() or c in ' _-').strip()
        key_string = f"{date}_{employee_id}_{clean_task}"
        return hashlib.md5(key_string.encode()).hexdigest()[:16]

    def generate_task_id_key(self, start_date: str, employee_id: str) -> str:
        key_string = f"{start_date}_{employee_id}"
        return hashlib.md5(key_string.encode()).hexdigest()[:16]

    def upsert_redmine_task(self, task_data: dict):
        try:
            employee_data = task_data.get("Employee Data")
            if not employee_data or not isinstance(employee_data, list) or not employee_data[0]:
                return None

            task_list = task_data.get("Task List", "")
            start_date = task_data.get("Start Date")
            employee_id = str(employee_data[0])

            if start_date:
                task_data["Id Key"] = self.generate_task_id_key(start_date, employee_id)
            
            unique_key = self.generate_task_unique_key(start_date, employee_id, task_list)
            task_data["Unique Key"] = unique_key

            where_clause = f"(Unique Key,eq,{unique_key})"
            existing_records = self.get_records(limit=1, where=where_clause)

            if existing_records and existing_records.get('records'):
                existing_record = existing_records['records'][0]
                record_id = existing_record['id']
                endpoint = f"{self.base_url}/api/v3/data/{self.base_id}/{self.table_id}/records"
                payload = {"id": record_id, "fields": task_data}

                update_response = requests.patch(endpoint, headers=self.headers, json=payload, timeout=30)
                if update_response.status_code in [200, 201]:
                    return update_response.json()
                else:
                    logging.error(f"Update tugas gagal: {update_response.status_code} - {update_response.text}")
                    return None
            else:
                return self.create_record(task_data)
        except Exception as e:
            logging.error(f"Upsert tugas gagal: {e}")
            return None
    
    def upsert_calendar_record(self, calendar_data: dict):
        try:
            date_str = calendar_data.get("Date")
            if not date_str:
                return None

            unique_key = hashlib.md5(date_str.encode()).hexdigest()
            calendar_data["Unique Key"] = unique_key
            
            where_clause = f"(Unique Key,eq,{unique_key})"
            existing_records = self.get_records(limit=1, where=where_clause)

            if existing_records and existing_records.get('records'):
                existing_record = existing_records['records'][0]
                
                if existing_record.get('fields', {}).get('Updated By'):
                    return "skipped"

                record_id = existing_record['id']
                endpoint = f"{self.base_url}/api/v3/data/{self.base_id}/{self.table_id}/records"
                payload = {"id": record_id, "fields": calendar_data}
                
                response = requests.patch(endpoint, headers=self.headers, json=payload, timeout=30)
                if response.status_code in [200, 201]:
                    return "updated"
                else:
                    logging.error(f"Update kalender gagal: {response.status_code} - {response.text}")
                    return None
            else:
                if self.create_record(calendar_data):
                    return "created"
                else:
                    return None
        except Exception as e:
            logging.error(f"Upsert kalender gagal: {e}")
            return None

    def upsert_schedule_shifting(self, schedule_data: dict):
        try:
            date = schedule_data.get("Date")
            employee_id_list = schedule_data.get("Employee Data Table")

            if not date or not employee_id_list or not employee_id_list[0]:
                return None

            employee_id = employee_id_list[0]
            unique_key = self.generate_unique_key(date, str(employee_id))
            schedule_data["Unique Key"] = unique_key

            where_clause = f"(Unique Key,eq,{unique_key})"
            existing_records = self.get_records(limit=1, where=where_clause)

            if existing_records and existing_records.get('records'):
                existing_record = existing_records['records'][0]
                record_id = existing_record['id']
                endpoint = f"{self.base_url}/api/v3/data/{self.base_id}/{self.table_id}/records"
                payload = {"id": record_id, "fields": schedule_data}

                update_response = requests.patch(endpoint, headers=self.headers, json=payload, timeout=30)
                if update_response.status_code in [200, 201]:
                    return update_response.json()
                else:
                    logging.error(f"Update jadwal shifting gagal: {update_response.status_code} - {update_response.text}")
                    return None
            else:
                return self.create_record(schedule_data)

        except Exception as e:
            logging.error(f"Upsert jadwal shifting gagal: {e}")
            return self.create_record(schedule_data)