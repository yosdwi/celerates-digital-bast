from datetime import datetime, timedelta
from typing import List, Dict, Optional
from collections import defaultdict
import hashlib

from src import config
from src.utils.date_helper import get_month_name_from_date

class ClsTimesheet:
    def __init__(self):
        options = config.TIMESHEET_OPTIONS
        if not options:
            raise ValueError("Timesheet config missing.")
        
        self.default_weekend_activity = options.get("default_activity_weekend")
        self.default_weekday_activity = options.get("default_activity_weekday")
        self.default_activity_iot = options.get("default_activity_iot") 
        self.default_project = options.get("default_project")

    def generate_monthly_timesheet_data(self,
                                      employee_mapping: dict,
                                      target_date: datetime,
                                      attendance_records: List[Dict],
                                      tasklist_records: List[Dict],
                                      task_field_name: str = "Task List Table",
                                      schedule_records: Optional[List[Dict]] = None,
                                      holiday_records: Optional[List[Dict]] = None) -> List[Dict]:
        year = target_date.year
        month = target_date.month

        attendance_lookup = self._create_lookup(attendance_records, 'Unique Key', is_list=False)
        tasklist_lookup = self._create_lookup(tasklist_records, 'Id Key', is_list=True)
        
        schedule_lookup = self._create_lookup(schedule_records, 'Unique Key', is_list=False) if schedule_records else {}
        holiday_lookup = {r.get('Date'): r for r in holiday_records} if holiday_records else {}

        month_days = self._get_month_days(year, month)
        month_str = get_month_name_from_date(target_date)

        timesheet_data = []
        for employee_name, employee_info in employee_mapping.items():
            employee_id = employee_info['id']
            employee_role = employee_info.get('role')

            for day in month_days:
                date_str = day.strftime('%Y-%m-%d')
                
                holiday_val, remarks_val = "", ""

                if schedule_records is not None:
                    schedule_key = self._generate_unique_key(date_str, str(employee_id))
                    schedule_record = schedule_lookup.get(schedule_key, {})
                    work_type_raw = schedule_record.get('Work Type')
                    
                    work_type_str = ""
                    if isinstance(work_type_raw, list) and work_type_raw:
                        work_type_str = str(work_type_raw[0])
                    elif isinstance(work_type_raw, str):
                        work_type_str = work_type_raw
                        
                    work_type = work_type_str.upper() if work_type_str else 'SHIFT'
                    if work_type != 'SHIFT':
                        holiday_val = 'H'
                        remarks_val = work_type_str
                    else:
                        holiday_val = ''
                        remarks_val = 'Working Day'
                elif holiday_records is not None:
                    is_weekend = day.weekday() >= 5
                    if date_str in holiday_lookup:
                        holiday_val = 'H'
                        holiday_record = holiday_lookup.get(date_str, {})
                        remarks_val = holiday_record.get('Day Type', 'Public Holiday')
                    elif is_weekend:
                        holiday_val = 'H'
                        remarks_val = 'Weekend'
                    else:
                        holiday_val = ''
                        remarks_val = 'Working Day'

                activity_val = ""
                if holiday_val != 'H':
                    if employee_role == 'IoT Operations':
                        activity_val = self.default_activity_iot
                    else:
                        activity_val = self._get_default_activity(day)

                lookup_key_attendance = self._generate_unique_key(date_str, str(employee_id))
                lookup_key_tasks = self._generate_task_id_key(date_str, str(employee_id))
                
                attendance_record = attendance_lookup.get(lookup_key_attendance, {})
                tasks = tasklist_lookup.get(lookup_key_tasks, [])

                # Check if attendance was manually edited
                is_manual_edit = False
                if attendance_record:
                    last_modified = attendance_record.get('Last Modified')
                    if last_modified and '@system.com' not in str(last_modified):
                        is_manual_edit = True

                record = {
                    "Date": date_str,
                    "Calendar Month": month_str,
                    "Activity": activity_val,
                    "Project Name": self.default_project,
                    "Holiday": holiday_val,
                    "Remarks": remarks_val,
                    "TTD": "",
                    "IsManualEdit": is_manual_edit,
                    # Linking data for post-processing
                    "_employee_id": employee_id,
                    "_attendance_id": self._get_record_link(attendance_record),
                    "_task_ids": [task_id for task in tasks if (task_id := task.get('Id') or task.get('id'))],
                    "_task_field_name": task_field_name
                }
                timesheet_data.append(record)
        return timesheet_data

    def _get_month_days(self, year: int, month: int) -> List[datetime]:
        start_date = datetime(year, month, 1)
        next_month_start = (start_date.replace(day=28) + timedelta(days=4)).replace(day=1)
        end_date = next_month_start - timedelta(days=1)
        
        return [start_date + timedelta(days=d) for d in range((end_date - start_date).days + 1)]

    def _get_default_activity(self, date: datetime) -> str:
        return self.default_weekend_activity if date.weekday() >= 5 else self.default_weekday_activity

    def _get_record_link(self, record: Dict) -> Optional[int]:
        if record and isinstance(record, dict):
            record_id = record.get('Id') or record.get('id')
            if record_id:
                return int(record_id) if str(record_id).isdigit() else record_id
        return None

    def _create_lookup(self, records: List[Dict], field_key: str, is_list: bool = False) -> Dict:
        lookup = defaultdict(list) if is_list else {}
        if not records:
            return dict(lookup) if is_list else lookup

        for record in records:
            # NocoDB v2 - direct field access without 'fields' wrapper
            key = record.get(field_key)
            if key and str(key).strip() and str(key).strip().lower() != 'none':
                clean_key = str(key).strip()
                if is_list:
                    lookup[clean_key].append(record)
                else:
                    lookup[clean_key] = record

        return dict(lookup) if is_list else lookup

    def _generate_unique_key(self, date: str, employee_id: str) -> str:
        return f"{date}_{employee_id}"

    def _generate_task_id_key(self, start_date: str, employee_id: str) -> str:
        return f"{start_date}_{employee_id}"