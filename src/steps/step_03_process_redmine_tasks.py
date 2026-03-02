from datetime import datetime, timedelta
from src import config
from src.classes.ClsRedMine import ClsRedMine
from src.classes.ClsNocoDBProcessor import ClsNocoDBProcessor
from src.utils.date_helper import get_configured_month_dates

def run():
    print("Executing Step 3: Redmine Task Processing")

    employee_table_id = config.NOCODB_TABLES.get("employee_data")
    tasklist_table_id = config.NOCODB_TABLES.get("tasklist")
    if not all([employee_table_id, tasklist_table_id]):
        raise ValueError("Config for employee or tasklist table ID is missing.")

    redmine_db = ClsRedMine()
    nocodb_employee = ClsNocoDBProcessor(config.APP_BASE_ID, employee_table_id)
    nocodb_tasklist = ClsNocoDBProcessor(config.APP_BASE_ID, tasklist_table_id)

    try:
        # Standardize date range fetching
        start_date, end_date, _ = get_configured_month_dates()
        month_start_str = start_date.strftime('%Y-%m-%d')
        month_end_str = end_date.strftime('%Y-%m-%d')

        # --- Processing Developer Tasks from Redmine ---
        print("Processing developer tasks from Redmine...")
        developer_employees = nocodb_employee.get_all_employees(role_filter="Developer")

        if developer_employees:
            if not redmine_db.connect():
                raise ConnectionError("Failed to connect to the Redmine database.")

            redmine_df = redmine_db.get_formatted_tasks_summary(
                start_date=month_start_str, end_date=month_end_str, employee_mapping=developer_employees
            )

            if not redmine_df.empty:
                redmine_records_to_create = []
                for _, row in redmine_df.iterrows():
                    tracker_name = row.get('Tracker Name', '')
                    kategori = "Detail Aktivitas Kualitas Kode"
                    if tracker_name == 'DIGI-SI':
                        kategori = "Detail Aktivitas Waktu Rilis Fitur"
                    
                    start_date = row.get('Start Date')
                    record = {
                        "Task List": row.get('Task List'),
                        "Requestor": row.get('Requestor'),
                        "Employee Data": row.get('Employee Data'),
                        "Status": row.get('Status'),
                        "Start Date": start_date,
                        "End Date": row.get('End Date'),
                        "Date": start_date,
                        "Kategori": kategori,
                        "Pencapaian": row.get('Pencapaian', 0),
                    }
                    redmine_records_to_create.append(record)
      
                success_count = 0
                failed_count = 0
                for i, record in enumerate(redmine_records_to_create):
                    result = nocodb_tasklist.upsert_redmine_task(record)
                    if result:
                        success_count += 1
                    else:
                        failed_count += 1
                        if failed_count <= 3:  # Show first 3 failures for debugging
                            print(f"Failed record {i+1}: Employee Data: {record.get('Employee Data')}, Start Date: {record.get('Start Date')}, Task: {record.get('Task List', '')[:50]}")

                print(f"Successfully upserted {success_count}/{len(redmine_records_to_create)} Developer task records.")
                if failed_count > 0:
                    print(f"Failed: {failed_count} records (check logs above for details)")

        # --- Process manual entries (Created by != system@system.com) ---
        print("Processing manual task entries for Unique Key generation...")
        updated_count = nocodb_tasklist.process_manual_entries_unique_key(month_start_str, month_end_str)
        print(f"Generated Unique Keys for {updated_count} manual task entries")

        print("Step 3: Completed.")

    except Exception as e:
        print(f"Step 3: Failed with error: {e}")
        raise
    finally:
        if redmine_db.is_connected:
            redmine_db.disconnect()
