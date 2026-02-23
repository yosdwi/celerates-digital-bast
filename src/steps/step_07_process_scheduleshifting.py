from src import config
from src.classes.ClsNocoDBProcessor import ClsNocoDBProcessor
from src.utils.date_helper import get_configured_month_dates, get_month_name_from_date
import pandas as pd

def run():
    print("Executing Step 7: Generate Monthly Schedule Shifting Data")

    schedule_shifting_table_id = config.NOCODB_TABLES.get("schedule_shifting")
    if not schedule_shifting_table_id:
        raise ValueError("Configuration for schedule shifting table ID is missing.")

    employee_table_id = config.NOCODB_TABLES.get("employee_data")
    if not employee_table_id:
        raise ValueError("Configuration for employee table ID is missing.")

    try:
        nocodb_processor = ClsNocoDBProcessor(
            base_id=config.APP_BASE_ID,
            table_id=schedule_shifting_table_id
        )
        employee_processor = ClsNocoDBProcessor(
            base_id=config.APP_BASE_ID,
            table_id=employee_table_id
        )

        employee_mapping = employee_processor.get_all_employees(role_filter="IoT Operations")

        if not employee_mapping:
            print("No employees found with IoT Operations role.")
            return

        # Get configured month dates
        start_date, end_date, target_date = get_configured_month_dates()
        month_name = get_month_name_from_date(target_date)

        print(f"Generating schedule shifting data for {month_name} {target_date.year}")
        print(f"Date range: {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}")
        print(f"Found {len(employee_mapping)} employees in IoT Operations")

        records_created = 0
        records_skipped = 0

        date_range = pd.date_range(start=start_date, end=end_date, freq='D')

        for date in date_range:
            formatted_date = date.strftime('%Y-%m-%d')

            for employee_name, employee_info in employee_mapping.items():
                transformed_data = {
                    "Date": formatted_date,
                    "Employee Data Table": [employee_info["id"]]
                }

                result = nocodb_processor.upsert_schedule_shifting(transformed_data)
                if result:
                    records_created += 1
                else:
                    records_skipped += 1

        print(f"Step 7: Completed. Created/Updated: {records_created}, Skipped: {records_skipped}")
    except Exception as e:
        print(f"Step 7: Failed with error: {e}")
        raise

if __name__ == '__main__':
    try:
        config.check_required_variables()
        run()
    except (ValueError, FileNotFoundError) as e:
        print(f"Execution failed due to configuration error: {e}")
