import psycopg2
import psycopg2.extras
import logging
from typing import List, Dict, Optional
from datetime import datetime
from src import config

class ClsPostgreSQLProcessor:
    def __init__(self, base_id: str = None, table_name: str = None):
        self.db_url = config.DB_URL
        self.schema = 'pc38r6u1npuq0ul'
        self.base_id = base_id
        self.table_mappings = {
            'mlry0nxyj59wae8': 'Attendance',
            'mv68ycnivgq29ya': 'Tasklist Developer',
            'mdkq9q59nej56qo': 'Tasklist IoT Operations',
            'm99ucznm06bhtf1': 'timesheet',
            'mhlxl5x984zuxyj': 'Schedule Shifting',
            'mmpeld09v7jxgs7': 'Calendar',
            'mhwyla9uh1ici8j': 'Employee Data'
        }
        self.table_name = self.table_mappings.get(table_name, table_name)

    def _get_connection(self):
        return psycopg2.connect(self.db_url)

    def get_all_employees(self, role_filter: str = None) -> Dict[str, Dict]:
        try:
            with self._get_connection() as conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
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

                    print(f"Found {len(records)} employee records")
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
            logging.error(f"Error getting employees: {e}")
            return {}

    def get_records(self, limit: int = 25, offset: int = 0, where: str = None,
                   fields: str = None, sort: str = None) -> Optional[Dict]:
        try:
            with self._get_connection() as conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
                    # Special handling for Schedule Shifting to include Master Shift data
                    if self.table_name == 'Schedule Shifting':
                        query = f'''
                            SELECT s.*, ms."Shift_Name", ms."Code", ms."Work_Hour"
                            FROM "{self.schema}"."Schedule Shifting" s
                            LEFT JOIN "{self.schema}"."_nc_m2m_Schedule Shifti_Shift Setup" m
                                ON s.id = m."Schedule Shifting_id"
                            LEFT JOIN "{self.schema}"."Master Shift" ms
                                ON m."Shift Setup_id" = ms.id
                        '''
                    else:
                        select_fields = fields.replace(',', ', ') if fields else "*"
                        query = f'SELECT {select_fields} FROM "{self.schema}"."{self.table_name}"'

                    if where:
                        pg_where = self._convert_where_clause(where)
                        if pg_where:
                            query += f" WHERE {pg_where}"

                    if sort:
                        pg_sort = self._convert_sort_clause(sort)
                        if pg_sort:
                            query += f" ORDER BY {pg_sort}"

                    query += f" LIMIT {limit} OFFSET {offset}"

                    cursor.execute(query)
                    records = cursor.fetchall()

                    return {
                        'list': [dict(record) for record in records] if records else [],
                        'pageInfo': {
                            'totalRows': len(records) if records else 0,
                            'page': (offset // limit) + 1,
                            'pageSize': limit
                        }
                    }

        except Exception as e:
            logging.error(f"Error getting records: {e}")
            return None

    def _convert_where_clause(self, where: str) -> str:
        where = where.strip()
        if where.startswith('(') and where.endswith(')'):
            where = where[1:-1]

        parts = where.split(',', 2)
        if len(parts) >= 2:
            field_name = parts[0].strip()

            field_mappings = {
                "Unique Key": "Unique_Key",
                "Month": "Date",  # For attendance, filter by Date instead of Month
                "Calendar Month": "Calendar_Month",
                "Employee Data": "Employee_Data",
                "Task List": "Task_List",
                "Id Key": "Id_Key"
            }

            field_name = field_mappings.get(field_name, field_name)
            field = f'"{field_name}"'
            operator = parts[1].strip()
            value = parts[2].strip() if len(parts) > 2 else None

            if operator == 'eq':
                if field_name == "Date" and self.table_name == "Attendance":
                    month_mapping = {
                        "Januari": "01", "Februari": "02", "Maret": "03", "April": "04",
                        "Mei": "05", "Juni": "06", "Juli": "07", "Agustus": "08",
                        "September": "09", "Oktober": "10", "November": "11", "Desember": "12"
                    }
                    if value in month_mapping:
                        month_num = month_mapping[value]
                        return f"EXTRACT(MONTH FROM {field}) = {month_num}"
                return f"{field} = '{value}'"
            elif operator == 'like':
                return f"{field} LIKE '{value}'"
            elif operator == 'null':
                return f"{field} IS NULL"
            elif operator == 'in':
                values = [f"'{v.strip()}'" for v in value.split(',')]
                return f"{field} IN ({','.join(values)})"

        return ""

    def _convert_sort_clause(self, sort: str) -> str:
        if sort.startswith('-'):
            field = sort[1:]
            return f'"{field}" DESC'
        else:
            return f'"{sort}" ASC'

    def generate_unique_key(self, date: str, employee_id: str) -> str:
        return f"{date}_{employee_id}"

    def batch_upsert_timesheets(self, records: List[Dict]) -> int:
        if not records:
            return 0

        try:
            success_count = 0

            with self._get_connection() as conn:
                with conn.cursor() as cursor:
                    batch_size = 50
                    total_records = len(records)
                    print(f"Processing {total_records} timesheet records in batches of {batch_size}...")

                    for i in range(0, total_records, batch_size):
                        batch = records[i:i + batch_size]
                        batch_success = 0

                        for record in batch:
                            try:
                                clean_record = {k: v for k, v in record.items() if not k.startswith('_')}
                                employee_id = record.get("_employee_id")
                                if not employee_id:
                                    continue

                                date_str = clean_record.get('Date')
                                unique_key = self.generate_unique_key(date_str, str(employee_id))
                                clean_record['Unique Key'] = unique_key

                                update_query = f'''
                                UPDATE "{self.schema}"."timesheet"
                                SET "date" = %s, "Calendar_Month" = %s, "activity" = %s,
                                    "project_name" = %s, "holiday" = %s, "remarks" = %s,
                                    "ttd" = %s, "IsManualEdit" = %s, "updated_at" = %s
                                WHERE "Unique_Key" = %s
                                '''

                                update_values = (
                                    clean_record.get('Date'),
                                    clean_record.get('Calendar Month'),
                                    clean_record.get('Activity'),
                                    clean_record.get('Project Name'),
                                    clean_record.get('Holiday'),
                                    clean_record.get('Remarks'),
                                    clean_record.get('TTD'),
                                    clean_record.get('IsManualEdit'),
                                    datetime.now(),
                                    unique_key
                                )

                                cursor.execute(update_query, update_values)

                                # If update successful, also update attendance link
                                if cursor.rowcount > 0:
                                    # Get timesheet ID for linking
                                    cursor.execute(f'SELECT id FROM "{self.schema}"."timesheet" WHERE "Unique_Key" = %s', (unique_key,))
                                    timesheet_result = cursor.fetchone()
                                    if timesheet_result:
                                        timesheet_id = timesheet_result[0]
                                        attendance_id = record.get("_attendance_id")
                                        if attendance_id:
                                            cursor.execute(f'''
                                                UPDATE "{self.schema}"."Attendance"
                                                SET "timesheet_id" = %s, "timesheet_id1" = %s
                                                WHERE id = %s
                                            ''', (timesheet_id, timesheet_id, attendance_id))

                                if cursor.rowcount == 0:
                                    insert_query = f'''
                                    INSERT INTO "{self.schema}"."timesheet"
                                    ("date", "Calendar_Month", "activity", "project_name",
                                     "holiday", "remarks", "ttd", "Unique_Key", "IsManualEdit", "created_at")
                                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                                    '''

                                    insert_values = (
                                        clean_record.get('Date'),
                                        clean_record.get('Calendar Month'),
                                        clean_record.get('Activity'),
                                        clean_record.get('Project Name'),
                                        clean_record.get('Holiday'),
                                        clean_record.get('Remarks'),
                                        clean_record.get('TTD'),
                                        unique_key,
                                        clean_record.get('IsManualEdit'),
                                        datetime.now()
                                    )

                                    cursor.execute(insert_query, insert_values)

                                # Get the inserted timesheet ID
                                cursor.execute(f'SELECT id FROM "{self.schema}"."timesheet" WHERE "Unique_Key" = %s', (unique_key,))
                                timesheet_id = cursor.fetchone()[0]

                                # Update attendance record to link to this timesheet
                                attendance_id = record.get("_attendance_id")
                                if attendance_id:
                                    cursor.execute(f'''
                                        UPDATE "{self.schema}"."Attendance"
                                        SET "timesheet_id" = %s, "timesheet_id1" = %s
                                        WHERE id = %s
                                    ''', (timesheet_id, timesheet_id, attendance_id))

                                batch_success += 1

                            except Exception as e:
                                logging.error(f"Error processing record: {e}")

                        success_count += batch_success
                        progress = i + len(batch)
                        print(f"Progress: {progress}/{total_records} processed ({batch_success}/{len(batch)} successful in this batch)")

                    conn.commit()
                    print(f"Successfully processed {success_count} timesheet records")
                    return success_count

        except Exception as e:
            logging.error(f"Batch upsert error: {e}")
            return 0