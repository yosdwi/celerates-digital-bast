import pandas as pd
from datetime import datetime

from src import config
from src.classes.ClsAttendance import ClsAttendance
from src.classes.ClsNocoDBProcessor import ClsNocoDBProcessor
from src.utils.date_helper import get_configured_month_dates

def run():
    print("Executing Step 2: Attendance Processing")
    
    employee_table_id = config.NOCODB_TABLES.get("employee_data")
    attendance_table_id = config.NOCODB_TABLES.get("attendance")
    if not all([employee_table_id, attendance_table_id]):
        raise ValueError("Attendance table ID is missing.")

    attendance_db = ClsAttendance()
    nocodb_employee = ClsNocoDBProcessor(config.APP_BASE_ID, employee_table_id)
    nocodb_attendance = ClsNocoDBProcessor(config.APP_BASE_ID, attendance_table_id)
    
    all_records_to_create = []

    try:
        if not attendance_db.connect():
            raise ConnectionError("Failed to connect to the attendance database.")

        employee_mapping = nocodb_employee.get_all_employees()
        print(f"Found {len(employee_mapping)} employees")
        if not employee_mapping:
            print("No employees found, skipping attendance processing")
            return

        start_date, end_date, _ = get_configured_month_dates()
        month_start_str = start_date.strftime('%Y-%m-%d')
        month_end_str = end_date.strftime('%Y-%m-%d')
        print(f"Processing attendance from {month_start_str} to {month_end_str}")
        
        for name, info in employee_mapping.items():
            nrp, employee_id = info['nrp'], info['id']
            
            attendance_df = attendance_db.get_formatted_attendance_summary(
                nrp=nrp, start_date=month_start_str, end_date=month_end_str
            )
            if attendance_df.empty:
                continue

            for _, row in attendance_df.iterrows():
                date_obj = datetime.strptime(row['Date'], '%m/%d/%Y')
                formatted_date = date_obj.strftime('%Y-%m-%d')
                
                record = {
                    "Date": formatted_date,
                    "Employee Data": [employee_id],
                }
                if row['Start Time']:
                    record["Start Time"] = f"{formatted_date} {row['Start Time']}:00"
                if row['End Time']:
                    record["End Time"] = f"{formatted_date} {row['End Time']}:00"
                
                all_records_to_create.append(record)
        
        if not all_records_to_create:
            print("No attendance records found to process")
            return
            
        success_count = sum(1 for r in all_records_to_create if nocodb_attendance.upsert_attendance(r))
        
        print(f"Successfully upserted {success_count}/{len(all_records_to_create)} records.")
        if success_count < len(all_records_to_create):
            pass

        print("Step 2: Completed.")

    except Exception as e:
        print(f"Step 2: Failed. Error: {e}")
        raise
    finally:
        if attendance_db.is_connected:
            attendance_db.disconnect()
