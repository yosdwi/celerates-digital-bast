from datetime import datetime, timedelta
from src import config
from src.classes.ClsRedMine import ClsRedMine
from src.classes.ClsNocoDBProcessor import ClsNocoDBProcessor
from src.utils.date_helper import get_configured_month_dates

def get_target_dates():
    now = datetime.now()
    start_date = now.replace(day=1)
    end_date = now
    return start_date.strftime('%Y-%m-%d'), end_date.strftime('%Y-%m-%d')

def run():
    print("Executing Step 3: Redmine Task Processing")

    employee_table_id = config.NOCODB_TABLES.get("employee_data")
    tasklist_table_id = config.NOCODB_TABLES.get("tasklist")
    tasklist_iot_table_id = config.NOCODB_TABLES.get("tasklist_iot")
    schedule_shifting_table_id = config.NOCODB_TABLES.get("schedule_shifting")
    if not all([employee_table_id, tasklist_table_id, tasklist_iot_table_id, schedule_shifting_table_id]):
        raise ValueError("Config for employee, tasklist, IoT tasklist, or schedule shifting table ID is missing.")

    redmine_db = ClsRedMine()
    nocodb_employee = ClsNocoDBProcessor(config.APP_BASE_ID, employee_table_id)
    nocodb_tasklist = ClsNocoDBProcessor(config.APP_BASE_ID, tasklist_table_id)
    nocodb_iot_tasklist = ClsNocoDBProcessor(config.APP_BASE_ID, tasklist_iot_table_id)
    nocodb_shifting = ClsNocoDBProcessor(config.APP_BASE_ID, schedule_shifting_table_id)

    try:
        # Standardize date range fetching
        start_date, end_date, _ = get_configured_month_dates()
        month_start_str = start_date.strftime('%Y-%m-%d')
        month_end_str = end_date.strftime('%Y-%m-%d')

        # --- New Logic for IoT Tasks based on Schedule Shifting ---
        print("Processing IoT tasks based on schedule shifting...")

        # Get employee mapping once and cache it
        employee_mapping = nocodb_employee.get_all_employees()
        valid_employee_ids = [info['id'] for info in employee_mapping.values()]
        print(f"Found {len(valid_employee_ids)} valid employees in employee table")

        # 1. Aggregate tasks from the JSON template
        iot_tasks_template = config.TASKLIST_IOT
        if iot_tasks_template:
            aggregated_task_name = "; ".join([task.get('task_name', '') for task in iot_tasks_template])
            # Assuming a single requestor and category for simplicity, can be enhanced if needed
            requestor = iot_tasks_template[0].get('requestor')
            category = iot_tasks_template[0].get('category')

            # 2. Fetch all SHIFT records and filter by date in the script
            where_clause = f"(Shift Name,like,%SHIFT%)"
            shift_response = nocodb_shifting.get_records(where=where_clause, limit=2000) # Increased limit
            
            all_shift_records = []
            if shift_response and shift_response.get('list'):
                all_shift_records = shift_response.get('list')

            # Filter records by date range within the script
            shift_records = []
            for record in all_shift_records:
                record_date_str = record.get('Date')
                if not record_date_str:
                    continue
                try:
                    record_date = datetime.strptime(record_date_str, '%Y-%m-%d')
                    if start_date <= record_date <= end_date:
                        shift_records.append(record)
                except ValueError:
                    continue # Skip records with invalid date format

            print(f"Found {len(shift_records)} shifts within the date range to process for IoT tasks.")

            # Get IoT Operations employees
            iot_employees = {name: info for name, info in employee_mapping.items() if info.get('role') == 'IoT Operations'}
            print(f"Found {len(iot_employees)} IoT Operations employees to process")

            iot_records_to_create = []
            print("Processing shifts...")
            for idx, shift in enumerate(shift_records):
                if idx % 50 == 0:  # Progress every 50 records
                    print(f"Processed {idx}/{len(shift_records)} shifts...")
                shift_date_str = shift.get('Date')
                start_time_raw = shift.get('Start Time')
                end_time_raw = shift.get('End Time')
                employee_data = shift.get('Employee Data')

                # Handle array values
                start_time_str = start_time_raw[0] if isinstance(start_time_raw, list) and start_time_raw else start_time_raw
                end_time_str = end_time_raw[0] if isinstance(end_time_raw, list) and end_time_raw else end_time_raw

                if not all([shift_date_str, start_time_str, end_time_str, employee_data]):
                    continue # Skip if essential data is missing


                # 3. Handle overnight shifts to determine correct start and end dates
                shift_date = datetime.strptime(shift_date_str, '%Y-%m-%d')
                start_time = datetime.strptime(start_time_str, '%H:%M:%S').time()
                end_time = datetime.strptime(end_time_str, '%H:%M:%S').time()

                start_datetime = datetime.combine(shift_date, start_time)
                # If end time is earlier than start time, it's the next day
                if end_time < start_time:
                    end_datetime = datetime.combine(shift_date + timedelta(days=1), end_time)
                else:
                    end_datetime = datetime.combine(shift_date, end_time)

                record_start_date = start_datetime.strftime('%Y-%m-%d')
                record_end_date = end_datetime.strftime('%Y-%m-%d')
                
                # Use the cached IoT employees
                if not iot_employees:
                    print("WARNING: No IoT Operations employees found. Skipping record...")
                    continue

                # Create one record per IoT employee for this shift
                for emp_name, emp_info in iot_employees.items():
                    employee_id = emp_info['id']
                    id_key = nocodb_iot_tasklist.generate_task_id_key(record_start_date, str(employee_id))

                    record = {
                        "Task List": aggregated_task_name,
                        "Requestor": requestor,
                        "Employee Data": [employee_id],
                        "Status": "Closed",
                        "Start Date": record_start_date,
                        "End Date": record_end_date,
                        "Date": record_start_date, # Or shift_date_str, depending on requirement
                        "Kategori": category,
                        "Pencapaian": 100,
                        "Id Key": id_key
                    }
                    iot_records_to_create.append(record)

            print(f"Finished processing all shifts. Created {len(iot_records_to_create)} records to upsert.")
            print("Starting upsert process...")

            success_count = 0
            failed_count = 0
            for idx, record in enumerate(iot_records_to_create):
                if idx % 100 == 0:  # Progress every 100 upserts
                    print(f"Processing {idx}/{len(iot_records_to_create)} records... (Success: {success_count}, Failed: {failed_count})")

                result = nocodb_iot_tasklist.upsert_redmine_task(record)
                if result:
                    success_count += 1
                else:
                    failed_count += 1
                    if failed_count <= 5:  # Show first 5 failures for debugging
                        print(f"Failed to upsert record: {record.get('Id Key', 'No Id Key')} - Employee: {record.get('Employee Data')}")

            print(f"Successfully upserted {success_count}/{len(iot_records_to_create)} aggregated IoT task records.")

        # --- Existing Logic for Other Employees ---
        print("Processing other employees from Redmine...")
        employee_mapping = nocodb_employee.get_all_employees()
        iot_employees = nocodb_employee.get_all_employees(role_filter="IoT Operations")
        other_employees = {name: info for name, info in employee_mapping.items() if info.get('role') != 'IoT Operations' and name not in iot_employees}

        if other_employees:
            if not redmine_db.connect():
                raise ConnectionError("Failed to connect to the Redmine database.")
            
            redmine_df = redmine_db.get_formatted_tasks_summary(
                start_date=month_start_str, end_date=month_end_str, employee_mapping=other_employees
            )

            if not redmine_df.empty:
                redmine_records_to_create = []
                for _, row in redmine_df.iterrows():
                    tracker_name = row.get('Tracker Name', '')
                    kategori = "Detail Aktivitas Kualitas Kode"
                    if tracker_name == 'DIGI-SI':
                        kategori = "Detail Aktivitas Waktu Rilis Fitur"
                    
                    record = {
                        "Task List": row.get('Task List'),
                        "Requestor": row.get('Requestor'),
                        "Employee Data": row.get('Employee Data'),
                        "Status": row.get('Status'),
                        "Start Date": row.get('Start Date'),
                        "End Date": row.get('End Date'),
                        "Date": row.get('Start Date'),
                        "Kategori": kategori,
                        "Pencapaian": row.get('Pencapaian', 0)
                    }
                    redmine_records_to_create.append(record)
      
                success_count = sum(1 for r in redmine_records_to_create if nocodb_tasklist.upsert_redmine_task(r))
                print(f"Successfully upserted {success_count}/{len(redmine_records_to_create)} Redmine task records.")

        print("Step 3: Completed.")

    except Exception as e:
        print(f"Step 3: Failed with error: {e}")
        raise
    finally:
        if redmine_db.is_connected:
            redmine_db.disconnect()
