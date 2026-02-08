from src import config
from src.classes.ClsNocoDBProcessor import ClsNocoDBProcessor
from src.classes.ClsScheduleShiftingProcessor import ClsScheduleShiftingProcessor
import pandas as pd

def run():
    print("Executing Step 7: Process Schedule Shifting")

    schedule_shifting_table_id = config.NOCODB_TABLES.get("schedule_shifting")
    if not schedule_shifting_table_id:
        raise ValueError("Configuration for schedule shifting table ID is missing.")

    schedule_shifting_sheet_url = config.SCHEDULE_SHIFTING_URL
    if not schedule_shifting_sheet_url:
        raise ValueError("Configuration for schedule shifting sheet URL is missing.")

    employee_table_id = config.NOCODB_TABLES.get("employee_data")
    if not employee_table_id:
        raise ValueError("Configuration for employee table ID is missing.")

    try:
        # Initialize processors
        nocodb_processor = ClsNocoDBProcessor(
            base_id=config.APP_BASE_ID,
            table_id=schedule_shifting_table_id
        )
        employee_processor = ClsNocoDBProcessor(
            base_id=config.APP_BASE_ID,
            table_id=employee_table_id
        )
        gsheet_processor = ClsScheduleShiftingProcessor()

        # Get employee mapping, filtering for IoT Operations role
        employee_mapping = employee_processor.get_all_employees(role_filter="IoT Operations")

        # Read data from Google Sheet
        df = gsheet_processor.read_sheet_to_dataframe(schedule_shifting_sheet_url, "Schedule Shifting Apps")

        if df.empty:
            print("No data found in the schedule shifting sheet.")
            return

        # Process each row
        for index, row in df.iterrows():
            schedule_data = row.to_dict()
            
            employee_name = schedule_data.get("Employee Name")
            if not employee_name:
                continue
            
            employee_info = employee_mapping.get(employee_name.strip().title())

            if not employee_info:
                print(f"Employee not found or not in IoT Operations: {employee_name}")
                continue

            date_str = schedule_data.get("Date")
            try:
                formatted_date = pd.to_datetime(date_str).strftime('%Y-%m-%d')
            except (ValueError, TypeError):
                print(f"Skipping row due to invalid date: {date_str}")
                continue
            
            start_time = schedule_data.get("Start Time")
            end_time = schedule_data.get("End Time")
            shift_code = schedule_data.get("Shift Code")

            transformed_data = {
                "Date": formatted_date,
                "Employee Data Table": [employee_info["id"]],
                "Start Time": start_time if start_time and start_time.strip() != '-' else None,
                "End Time": end_time if end_time and end_time.strip() != '-' else None,
                "Shift Code": shift_code if shift_code and shift_code.strip() != '-' else None,
                "Work Type": schedule_data.get("Work Type")
            }
            nocodb_processor.upsert_schedule_shifting(transformed_data)

        print("Step 7: Completed.")
    except Exception as e:
        print(f"Step 7: Failed with error: {e}")
        raise

if __name__ == '__main__':
    try:
        config.check_required_variables()
        run()
    except (ValueError, FileNotFoundError) as e:
        print(f"Execution failed due to configuration error: {e}")
