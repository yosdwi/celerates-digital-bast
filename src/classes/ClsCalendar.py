import holidays
from datetime import date

from src.classes.ClsNocoDBProcessor import ClsNocoDBProcessor

class ClsCalendar:
    def __init__(self, nocodb_processor: ClsNocoDBProcessor):
        self.nocodb_processor = nocodb_processor

    def sync_holidays(self, year: int = date.today().year):
        try:
            id_holidays = holidays.ID(years=year)
            if not id_holidays:
                return

            print(f"Found {len(id_holidays)} holidays to process for {year}.")
            created_count, updated_count, skipped_count = 0, 0, 0

            for holiday_date, description in id_holidays.items():
                holiday_data = {
                    "Date": holiday_date.strftime('%Y-%m-%d'),
                    "Day Name": holiday_date.strftime('%A'),
                    "Day Type": 'Joint Leave' if "Cuti Bersama" in description else 'National Holiday',
                    "Description": description,
                }
                
                result = self.nocodb_processor.upsert_calendar_record(holiday_data)
                
                if result == "created": created_count += 1
                elif result == "updated": updated_count += 1
                elif result == "skipped": skipped_count += 1

        except Exception as e:
            print(f"An error, holiday sync: {e}")
            raise