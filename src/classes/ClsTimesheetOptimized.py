import psycopg2
import psycopg2.extras
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Optional
from collections import defaultdict
from src import config
from src.utils.date_helper import get_month_name_from_date

class ClsTimesheetOptimized:
    def __init__(self):
        self.db_url = config.DB_URL
        self.schema = 'pc38r6u1npuq0ul'

        options = config.TIMESHEET_OPTIONS
        if not options:
            raise ValueError("Timesheet config missing.")

        self.default_weekend_activity = options.get("default_activity_weekend")
        self.default_weekday_activity = options.get("default_activity_weekday")
        self.default_activity_iot = options.get("default_activity_iot")
        self.default_project = options.get("default_project")

    def _get_connection(self):
        return psycopg2.connect(self.db_url)

    def _get_month_days(self, year: int, month: int) -> List[datetime]:
        start_date = datetime(year, month, 1)
        next_month_start = (start_date.replace(day=28) + timedelta(days=4)).replace(day=1)
        end_date = next_month_start - timedelta(days=1)
        return [start_date + timedelta(days=d) for d in range((end_date - start_date).days + 1)]

    def _fetch_employees(self, connection, role_filter=None):
        try:
            with connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
                base_query = f'''
                    SELECT id, "Employee_Name", "Employee_ID", "NRP", "Role"
                    FROM "{self.schema}"."Employee Data"
                    WHERE "Employee_Name" IS NOT NULL
                '''

                params = ()
                if role_filter:
                    base_query += ' AND "Role" = %s'
                    params = (role_filter,)

                cursor.execute(base_query, params)
                records = cursor.fetchall()

                employee_mapping = {}
                for record in records:
                    employee_name = record.get('Employee_Name')
                    if employee_name:
                        employee_mapping[employee_name.strip().title()] = {
                            'id': record.get('id'),
                            'nrp': record.get('NRP'),
                            'employee_id': record.get('Employee_ID'),
                            'role': record.get('Role')
                        }

                return employee_mapping

        except Exception as e:
            logging.error(f"Error fetching employees: {e}")
            return {}

    def _fetch_attendance_records(self, connection, year_month: str):
        try:
            with connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
                cursor.execute(f'''
                    SELECT a."Unique_Key", a."updated_by", a.id,
                           ae."Employee Data_id" as employee_id
                    FROM "{self.schema}"."Attendance" a
                    LEFT JOIN "{self.schema}"."_nc_m2m_Attendance_Employee Data" ae
                        ON a.id = ae."Attendance_id"
                    WHERE a."Unique_Key" LIKE %s
                    LIMIT 3000
                ''', (f'{year_month}%',))

                records = cursor.fetchall()

                lookup = {}
                for record in records:
                    key = record.get('Unique_Key')
                    if key:
                        lookup[key.strip()] = dict(record)
                return lookup

        except Exception as e:
            logging.error(f"Error fetching attendance: {e}")
            return {}

    def _fetch_tasklist_records(self, connection, year_month: str, is_iot: bool = False):
        try:
            table_name = 'Tasklist IoT Operations' if is_iot else 'Tasklist Developer'

            with connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
                cursor.execute(f'''
                    SELECT "Id_Key", id
                    FROM "{self.schema}"."{table_name}"
                    WHERE "Id_Key" LIKE %s
                    LIMIT 3000
                ''', (f'{year_month}%',))

                records = cursor.fetchall()

                lookup = defaultdict(list)
                for record in records:
                    key = record.get('Id_Key')
                    if key:
                        lookup[key.strip()].append(dict(record))
                return dict(lookup)

        except Exception as e:
            logging.error(f"Error fetching tasklist: {e}")
            return {}

    def _fetch_schedule_records(self, connection, year_month: str):
        try:
            with connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
                cursor.execute(f'''
                    SELECT s."Unique_Key", s."Date_Shifting",
                           ms."Shift_Name", ms."Code", ms."Work_Hour"
                    FROM "{self.schema}"."Schedule Shifting" s
                    LEFT JOIN "{self.schema}"."_nc_m2m_Schedule Shifti_Shift Setup" m
                        ON s.id = m."Schedule Shifting_id"
                    LEFT JOIN "{self.schema}"."Master Shift" ms
                        ON m."Shift Setup_id" = ms.id
                    WHERE s."Unique_Key" LIKE %s
                    LIMIT 3000
                ''', (f'{year_month}%',))

                records = cursor.fetchall()

                lookup = {}
                for record in records:
                    key = record.get('Unique_Key')
                    if key:
                        lookup[key.strip()] = dict(record)
                return lookup

        except Exception as e:
            logging.error(f"Error fetching schedule: {e}")
            return {}

    def _fetch_holiday_records(self, connection, year_month: str):
        try:
            with connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
                cursor.execute(f'''
                    SELECT "Date", "Day_Type"
                    FROM "{self.schema}"."Calendar"
                    WHERE "Unique_Key" LIKE %s
                    LIMIT 500
                ''', (f'{year_month}%',))

                records = cursor.fetchall()

                lookup = {}
                for record in records:
                    date_val = record.get('Date')
                    if date_val:
                        lookup[str(date_val)] = dict(record)
                return lookup

        except Exception as e:
            logging.error(f"Error fetching holidays: {e}")
            return {}

    def generate_monthly_timesheet_data(self,
                                      employee_mapping: dict,
                                      target_date: datetime,
                                      attendance_records: Optional[List[Dict]] = None,
                                      tasklist_records: Optional[List[Dict]] = None,
                                      task_field_name: str = "Task List Table",
                                      schedule_records: Optional[List[Dict]] = None,
                                      holiday_records: Optional[List[Dict]] = None) -> List[Dict]:

        try:
            with self._get_connection() as conn:
                year = target_date.year
                month = target_date.month
                year_month = target_date.strftime('%Y-%m')

                print(f"Generating optimized timesheet for {target_date.strftime('%B %Y')}...")

                if not employee_mapping:
                    employee_mapping = self._fetch_employees(conn)

                attendance_lookup = self._fetch_attendance_records(conn, year_month)
                tasklist_lookup = self._fetch_tasklist_records(conn, year_month, is_iot=False)
                iot_tasklist_lookup = self._fetch_tasklist_records(conn, year_month, is_iot=True)
                # Always fetch schedule records for IoT Operations
                schedule_lookup = self._fetch_schedule_records(conn, year_month)
                print(f"Fetched schedule records from DB: {len(schedule_lookup)} records")
                holiday_lookup = self._fetch_holiday_records(conn, year_month) if holiday_records is not None else {}

                print(f"Loaded: {len(employee_mapping)} employees, {len(attendance_lookup)} attendance, {len(tasklist_lookup)} tasks, {len(iot_tasklist_lookup)} IoT tasks {len(schedule_lookup)}")

                month_days = self._get_month_days(year, month)
                month_str = get_month_name_from_date(target_date)

                timesheet_data = []
                for employee_name, employee_info in employee_mapping.items():
                    employee_id = employee_info['id']
                    employee_role = employee_info.get('role')

                    for day in month_days:
                        date_str = day.strftime('%Y-%m-%d')

                        holiday_val, remarks_val = "", ""

                        if employee_role == 'IoT Operations':
                            schedule_key = self._generate_unique_key(date_str, str(employee_id))
                            schedule_record = schedule_lookup.get(schedule_key, {})

                            if schedule_record:
                                shift_name = schedule_record.get('Shift_Name')

                                if shift_name:
                                    # Has shift name = check type
                                    if 'Libur' in shift_name:
                                        holiday_val = 'H'
                                        remarks_val = shift_name
                                    elif 'SHIFT' in shift_name:
                                        holiday_val = ''
                                        remarks_val = shift_name
                                    else:
                                        holiday_val = ''
                                        remarks_val = shift_name
                                else:
                                    # No shift name = OFF day
                                    holiday_val = 'H'
                                    remarks_val = 'OFF'
                            else:
                                # No schedule record = OFF day
                                holiday_val = 'H'
                                remarks_val = 'OFF'
                        elif holiday_records is not None:
                            is_weekend = day.weekday() >= 5
                            if date_str in holiday_lookup:
                                holiday_val = 'H'
                                holiday_record = holiday_lookup.get(date_str, {})
                                remarks_val = holiday_record.get('Day_Type', 'Public Holiday')
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
                        current_tasklist_lookup = iot_tasklist_lookup if employee_role == 'IoT Operations' else tasklist_lookup
                        tasks = current_tasklist_lookup.get(lookup_key_tasks, [])

                        is_manual_edit = False
                        if attendance_record:
                            updated_by = attendance_record.get('updated_by')
                            if updated_by and '@system.com' not in str(updated_by):
                                is_manual_edit = True

                        # Get Start_Time and End_Time from attendance record
                        start_time = attendance_record.get('Start_Time') if attendance_record else None
                        end_time = attendance_record.get('End_Time') if attendance_record else None

                        record = {
                            "Date": date_str,
                            "Calendar Month": month_str,
                            "Activity": activity_val,
                            "Project Name": self.default_project,
                            "Holiday": holiday_val,
                            "Remarks": remarks_val,
                            "Start_Time": start_time,
                            "End_Time": end_time,
                            "TTD": "",
                            "IsManualEdit": is_manual_edit,
                            "_employee_id": employee_id,
                            "_attendance_id": self._get_record_link(attendance_record),
                            "_task_ids": [task_id for task in tasks if (task_id := task.get('id'))],
                            "_task_field_name": task_field_name
                        }
                        timesheet_data.append(record)

                print(f"Generated {len(timesheet_data)} timesheet records")
                return timesheet_data

        except Exception as e:
            logging.error(f"Error generating timesheet data: {e}")
            return []

    def _get_default_activity(self, date: datetime) -> str:
        return self.default_weekend_activity if date.weekday() >= 5 else self.default_weekday_activity

    def _get_record_link(self, record: Dict) -> Optional[int]:
        if record and isinstance(record, dict):
            record_id = record.get('id')
            if record_id:
                return int(record_id) if str(record_id).isdigit() else record_id
        return None

    def _generate_unique_key(self, date: str, employee_id: str) -> str:
        return f"{date}_{employee_id}"

    def _generate_task_id_key(self, start_date: str, employee_id: str) -> str:
        return f"{start_date}_{employee_id}"