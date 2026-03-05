import requests
import json
import hashlib
import logging
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from src import config

class ClsNocoDBProcessor:
    def __init__(self, base_id: str, table_id: str):
        self.base_url = config.NOCODB_BASE_URL.rstrip('/')
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
            endpoint = f"{self.base_url}/api/v2/tables/{self.table_id}/records"
            payload = data
            response = self.session.post(endpoint, headers=self.headers, json=payload, timeout=120)

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
            endpoint = f"{self.base_url}/api/v2/tables/{self.table_id}/records"
            payload = data_list
            response = requests.post(endpoint, headers=self.headers, json=payload, timeout=180)

            if response.status_code in [200, 201]:
                return response.json()
            else:
                logging.error(f"Gagal membuat record massal: {response.status_code} - {response.text}")
                return None
        except Exception as e:
            logging.error(f"Error saat membuat record massal: {e}")
            return None


    def get_records(self, limit: int = 25, offset: int = 0, where: str = None, fields: str = None, sort: str = None):
        try:
            endpoint = f"{self.base_url}/api/v2/tables/{self.table_id}/records"
            params = {"limit": limit, "offset": offset}
            if where:
                params["where"] = where
            if fields:
                params["fields"] = fields
            if sort:
                params["sort"] = sort

            response = self.session.get(endpoint, headers=self.headers, params=params, timeout=120)

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
            endpoint = f"{self.base_url}/api/v2/tables/{self.table_id}/records"
            params = {"limit": 1000}
            if role_filter:
                params["where"] = f"(Role,eq,{role_filter})"
            
            response = self.session.get(endpoint, headers=self.headers, params=params, timeout=120)

            if response.status_code == 200:
                data = response.json()
                if data and 'list' in data:
                    print(f"Found {len(data['list'])} employee records")
                employee_mapping = {}

                for record in data.get('list', []):

                    employee_name = record.get('Employee Name')
                    employee_code = record.get('Employee ID')
                    employee_nrp = record.get('NRP')
                    employee_id = record.get('Id')  # Capital I in v2
                    role = record.get('Role') 

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
        return f"{date}_{employee_id}"

    def upsert_attendance(self, attendance_data: dict):
        try:
            date = attendance_data["Date"]
            employee_id = attendance_data["Employee Data"][0]
            unique_key = self.generate_unique_key(date, str(employee_id))
            attendance_data["Unique Key"] = unique_key

            where_clause = f"(Unique Key,eq,{unique_key})"
            existing_records = self.get_records(limit=1, where=where_clause, fields="Id,Last Modified")

            if existing_records and existing_records.get('list'):
                existing_record = existing_records['list'][0]

                last_modified_by = existing_record.get('Last Modified')
                if last_modified_by is not None and '@system.com' not in str(last_modified_by):
                    logging.info(f"Update dilewati untuk {unique_key}: data diubah manual oleh '{last_modified_by}'.")
                    return "skipped_manual_edit"

                record_id = existing_record['Id']
                endpoint = f"{self.base_url}/api/v2/tables/{self.table_id}/records"
                payload = {"id": record_id, **attendance_data}

                update_response = requests.patch(endpoint, headers=self.headers, json=payload, timeout=120)
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
            employee_id = timesheet_data.get("Name Table")
            if not employee_id:
                return None

            for key in ["Start Time Table", "End Time Table", "Task List Table", "Task List IoT Table"]:
                if timesheet_data.get(key) is None:
                    timesheet_data[key] = None

            date = timesheet_data["Date"]
            unique_key = self.generate_unique_key(date, str(employee_id))
            timesheet_data["Unique Key"] = unique_key

            where_clause = f"(Unique Key,eq,{unique_key})"
            existing_records = self.get_records(limit=1, where=where_clause)

            if existing_records and existing_records.get('list'):
                existing_record = existing_records['list'][0]
                record_id = existing_record['Id']
                endpoint = f"{self.base_url}/api/v2/tables/{self.table_id}/records"
                payload = {"id": record_id, **timesheet_data}

                update_response = requests.patch(endpoint, headers=self.headers, json=payload, timeout=120)
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
            endpoint = f"{self.base_url}/api/v2/tables/{self.table_id}/records"
            payload = {"id": record_id, **data}
            response = requests.patch(endpoint, headers=self.headers, json=payload, timeout=120)
            if response.status_code in [200, 201]:
                return response.json()
            else:
                logging.error(f"Update record gagal: {response.status_code} - {response.text}")
                return None
        except Exception as e:
            logging.error(f"Update record gagal: {e}")
            return None

    def create_record_link(self, record_id: int, link_field_id: str, linked_record_id: int):
        """Create a link between two records using NocoDB Link API"""
        try:
            endpoint = f"{self.base_url}/api/v2/tables/{self.table_id}/links/{link_field_id}/records/{record_id}"
            payload = {"Id": linked_record_id}
            response = self.session.post(endpoint, headers=self.headers, json=payload, timeout=120)
            if response.status_code in [200, 201]:
                return True
            else:
                logging.error(f"Failed to create link: {response.status_code} - {response.text}")
                return False
        except Exception as e:
            logging.error(f"Error creating link: {e}")
            return False

    def batch_upsert_timesheets(self, records: list):
        if not records:
            return 0

        success_count = 0
        records_to_create = []
        records_to_update = {}

        for record in records:
            employee_id = record.get("_employee_id")
            if not employee_id:
                continue

            # Clean record for API (remove linking metadata)
            clean_record = {k: v for k, v in record.items() if not k.startswith('_')}

            date = clean_record["Date"]
            unique_key = self.generate_unique_key(date, str(employee_id))
            clean_record["Unique Key"] = unique_key

            # Store record with Unique Key included
            record_with_uk = record.copy()
            record_with_uk["Unique Key"] = unique_key
            records_to_update[unique_key] = record_with_uk

        unique_keys = list(records_to_update.keys())
        existing_keys = {}

        # Process unique keys in batches to avoid 414 Request-URI Too Large
        batch_size = 100  # Reduce batch size for unique key queries
        for i in range(0, len(unique_keys), batch_size):
            batch_keys = unique_keys[i:i + batch_size]
            where_clause = f"(Unique Key,in,{','.join(batch_keys)})"
            existing_records_response = self.get_records(limit=len(batch_keys), where=where_clause)

            if existing_records_response and existing_records_response.get('list'):
                for record in existing_records_response['list']:
                    existing_keys[record['Unique Key']] = record['Id']

        records_to_create_data = []
        updates_to_process = []

        # Separate records for update vs create
        for unique_key, record_data in records_to_update.items():
            clean_record = {k: v for k, v in record_data.items() if not k.startswith('_')}
            # Ensure Unique Key is always included
            clean_record["Unique Key"] = unique_key

            if unique_key in existing_keys:
                record_id = existing_keys[unique_key]
                updates_to_process.append((record_id, record_data))
            else:
                records_to_create_data.append((clean_record, record_data))  # (clean, with_metadata)

        # Process updates in batches with progress indicator
        if updates_to_process:
            import time
            batch_size = 50  # Process 50 updates at a time
            total_updates = len(updates_to_process)
            print(f"Updating {total_updates} existing timesheet records in batches of {batch_size}...")

            for i in range(0, total_updates, batch_size):
                batch = updates_to_process[i:i + batch_size]
                batch_success = 0

                for record_id, record_data in batch:
                    clean_record = {k: v for k, v in record_data.items() if not k.startswith('_')}
                    # Ensure Unique Key is preserved in updates
                    if "Unique Key" in record_data:
                        clean_record["Unique Key"] = record_data["Unique Key"]
                    if self.update_record(record_id, clean_record):
                        # Create links after successful update
                        self._create_timesheet_links(record_id, record_data)
                        batch_success += 1

                success_count += batch_success
                progress = i + len(batch)
                print(f"Progress: {progress}/{total_updates} updates completed ({batch_success}/{len(batch)} successful in this batch)")

                # Small delay between batches to avoid overwhelming the API
                if i + batch_size < total_updates:
                    time.sleep(1)

        if records_to_create_data:
            clean_records = [clean_record for clean_record, _ in records_to_create_data]
            created_response = self.bulk_create_records(clean_records)
            if created_response:
                created_records = created_response if isinstance(created_response, list) else [created_response]
                # Create links for newly created records
                for i, created_record in enumerate(created_records):
                    record_id = created_record.get('Id') or created_record.get('id')
                    if record_id and i < len(records_to_create_data):
                        _, original_record = records_to_create_data[i]
                        self._create_timesheet_links(record_id, original_record)
                success_count += len(created_records)

        return success_count

    def _create_timesheet_links(self, timesheet_record_id: int, record_data: dict):
        """Create all links for a timesheet record"""
        # Link field IDs from swagger.json
        LINK_FIELD_IDS = {
            "Name Table": "cwlgzzs7wg5fv4d",
            "Start Time Table": "caeg5qn4pbx8jrb",
            "End Time Table": "csobztmen7cmopi",
            "Task List Table": "c80cdrlxfnfbcj5",
            "Task List IoT Table": "cinouioq3wu114o"
        }

        # Link employee
        employee_id = record_data.get("_employee_id")
        if employee_id:
            self.create_record_link(timesheet_record_id, LINK_FIELD_IDS["Name Table"], employee_id)

        # Link attendance for start/end time
        attendance_id = record_data.get("_attendance_id")
        if attendance_id:
            self.create_record_link(timesheet_record_id, LINK_FIELD_IDS["Start Time Table"], attendance_id)
            self.create_record_link(timesheet_record_id, LINK_FIELD_IDS["End Time Table"], attendance_id)

        # Link tasks
        task_ids = record_data.get("_task_ids", [])
        task_field_name = record_data.get("_task_field_name")
        if task_ids and task_field_name and task_field_name in LINK_FIELD_IDS:
            for task_id in task_ids[:1]:  # Only link first task as it's single field
                self.create_record_link(timesheet_record_id, LINK_FIELD_IDS[task_field_name], task_id)

    def generate_task_unique_key(self, date: str, employee_id: str, task_list: str) -> str:
        clean_task = ''.join(c for c in task_list if c.isalnum() or c in ' _-').strip()
        return f"{date}_{employee_id}_{clean_task}"

    def generate_task_id_key(self, start_date: str, employee_id: str) -> str:
        return f"{start_date}_{employee_id}"

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

            if existing_records and existing_records.get('list'):
                existing_record = existing_records['list'][0]
                record_id = existing_record['Id']
                endpoint = f"{self.base_url}/api/v2/tables/{self.table_id}/records"
                payload = {"id": record_id, **task_data}

                update_response = requests.patch(endpoint, headers=self.headers, json=payload, timeout=120)
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

            calendar_data["Unique Key"] = date_str

            where_clause = f"(Unique Key,eq,{date_str})"
            existing_records = self.get_records(limit=1, where=where_clause)

            if existing_records and existing_records.get('list'):
                existing_record = existing_records['list'][0]
                
                if existing_record.get('fields', {}).get('Updated By'):
                    return "skipped"

                record_id = existing_record['Id']
                endpoint = f"{self.base_url}/api/v2/tables/{self.table_id}/records"
                payload = {"id": record_id, **calendar_data}
                
                response = requests.patch(endpoint, headers=self.headers, json=payload, timeout=120)
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

            if existing_records and existing_records.get('list'):
                existing_record = existing_records['list'][0]
                record_id = existing_record['Id']
                endpoint = f"{self.base_url}/api/v2/tables/{self.table_id}/records"
                payload = {"id": record_id, **schedule_data}

                print(payload)

                update_response = requests.patch(endpoint, headers=self.headers, json=payload, timeout=120)
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

    def process_manual_entries_unique_key(self, start_date_str: str, end_date_str: str):
        """
        Process manual entries (Created by != system@system.com)
        and generate Unique Key if Date and Employee Data are not null
        """
        try:
            # Get records with missing Unique Key (remove date filter for now)
            where_clause = f"(Unique Key,null)"

            records_response = self.get_records(limit=2000, where=where_clause)
            records = records_response.get('list', [])

            manual_updates = 0
            processed_count = 0

            for record in records:
                processed_count += 1

                # Check if Created by is NOT system@system.com
                created_by = record.get('Created by', {})
                if isinstance(created_by, dict):
                    creator_email = created_by.get('email', '')
                else:
                    creator_email = str(created_by) if created_by else ''

                # Skip if it's system created
                if 'system@system.com' in creator_email:
                    continue

                # Check if Date and Employee Data exist
                date_value = record.get('Date')
                employee_data = record.get('Employee Data')
                task_list = record.get('Task List', '')

                if not date_value or not employee_data:
                    continue

                # Extract employee ID
                if isinstance(employee_data, list) and len(employee_data) > 0:
                    employee_id = str(employee_data[0])
                elif isinstance(employee_data, str):
                    employee_id = employee_data
                else:
                    continue

                # Generate new Unique Key
                new_unique_key = self.generate_task_unique_key(
                    date_value, employee_id, task_list
                )

                # Update record with new Unique Key
                record_id = record['Id']
                update_data = {
                    "id": record_id,
                    "Unique Key": new_unique_key
                }

                endpoint = f"{self.base_url}/api/v2/tables/{self.table_id}/records"
                update_response = requests.patch(
                    endpoint,
                    headers=self.headers,
                    json=update_data,
                    timeout=120
                )

                if update_response.status_code in [200, 201]:
                    manual_updates += 1
                    logging.info(f"Generated Unique Key for manual entry: Employee {employee_id}, Date {date_value}")
                else:
                    logging.error(f"Failed to update Unique Key for record {record_id}: {update_response.status_code}")

            print(f"Processed {processed_count} records, generated Unique Keys for {manual_updates} manual entries")
            return manual_updates

        except Exception as e:
            logging.error(f"Error processing manual entries for Unique Key: {e}")
            return 0

    def process_manual_attendance_unique_key(self, start_date_str: str, end_date_str: str):
        """
        Process manual attendance entries (Created by != system@system.com)
        and generate Unique Key if Date and Employee Data are not null
        """
        try:
            # Get attendance records with missing Unique Key (remove date filter for now)
            where_clause = f"(Unique Key,null)"

            records_response = self.get_records(limit=2000, where=where_clause)
            records = records_response.get('list', [])

            manual_updates = 0
            processed_count = 0

            for record in records:
                processed_count += 1

                # Check if Date and Employee Data exist
                date_value = record.get('Date')
                employee_data = record.get('Employee Data')

                if not date_value or not employee_data:
                    continue

                # Extract employee ID
                if isinstance(employee_data, list) and len(employee_data) > 0:
                    employee_id = str(employee_data[0])
                elif isinstance(employee_data, str):
                    employee_id = employee_data
                else:
                    continue

                # Generate new Unique Key using attendance format
                new_unique_key = self.generate_unique_key(date_value, employee_id)

                # Update record with new Unique Key
                record_id = record['Id']
                update_data = {
                    "id": record_id,
                    "Unique Key": new_unique_key
                }

                endpoint = f"{self.base_url}/api/v2/tables/{self.table_id}/records"
                update_response = requests.patch(
                    endpoint,
                    headers=self.headers,
                    json=update_data,
                    timeout=120
                )

                if update_response.status_code in [200, 201]:
                    manual_updates += 1
                    logging.info(f"Generated Unique Key for manual attendance: Employee {employee_id}, Date {date_value}")
                else:
                    logging.error(f"Failed to update Unique Key for attendance record {record_id}: {update_response.status_code}")

            print(f"Processed {processed_count} attendance records, generated Unique Keys for {manual_updates} manual entries")
            return manual_updates

        except Exception as e:
            logging.error(f"Error processing manual attendance entries for Unique Key: {e}")
            return 0