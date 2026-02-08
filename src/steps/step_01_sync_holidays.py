from src import config
from src.classes.ClsNocoDBProcessor import ClsNocoDBProcessor
from src.classes.ClsCalendar import ClsCalendar

def run():
    print("Executing Step 1: Holiday Synchronization")

    calendar_table_id = config.NOCODB_TABLES.get("calendar")
    if not calendar_table_id:
        raise ValueError("Configuration for calendar table ID is missing.")

    try:
        nocodb_calendar = ClsNocoDBProcessor(
            base_id=config.APP_BASE_ID,
            table_id=calendar_table_id
        )
        calendar_processor = ClsCalendar(nocodb_calendar)
        calendar_processor.sync_holidays()
        print("Step 1: Completed.")
    except Exception as e:
        print(f"Step 1: Failed with error: {e}")
        raise

if __name__ == '__main__':
    try:
        config.check_required_variables()
        run()
    except (ValueError, FileNotFoundError) as e:
        print(f"Execution failed due to configuration error: {e}")
