from datetime import datetime
from src import config
from src.classes.ClsTimesheet import ClsTimesheet
from src.classes.ClsNocoDBProcessor import ClsNocoDBProcessor
from src.utils.date_helper import get_configured_month_dates, get_month_name_from_date

def run():
    print("Menjalankan Langkah 4: Membuat Timesheet")

    employee_table = config.NOCODB_TABLES.get("employee_data")
    attendance_table = config.NOCODB_TABLES.get("attendance")
    tasklist_table = config.NOCODB_TABLES.get("tasklist")
    tasklist_iot_table = config.NOCODB_TABLES.get("tasklist_iot")
    timesheet_table = config.NOCODB_TABLES.get("timesheet")
    schedule_shifting_table = config.NOCODB_TABLES.get("schedule_shifting")
    calendar_table = config.NOCODB_TABLES.get("calendar")

    if not all([employee_table, attendance_table, tasklist_table, timesheet_table, tasklist_iot_table, schedule_shifting_table, calendar_table]):
        raise ValueError("ID tabel yang dibutuhkan untuk membuat timesheet tidak lengkap.")

    nocodb_employee = ClsNocoDBProcessor(config.APP_BASE_ID, employee_table)
    nocodb_attendance = ClsNocoDBProcessor(config.APP_BASE_ID, attendance_table)
    nocodb_tasklist = ClsNocoDBProcessor(config.APP_BASE_ID, tasklist_table)
    nocodb_iot_tasklist = ClsNocoDBProcessor(config.APP_BASE_ID, tasklist_iot_table)
    nocodb_timesheet = ClsNocoDBProcessor(config.APP_BASE_ID, timesheet_table)
    nocodb_schedule_shifting = ClsNocoDBProcessor(config.APP_BASE_ID, schedule_shifting_table)
    nocodb_calendar = ClsNocoDBProcessor(config.APP_BASE_ID, calendar_table)
    timesheet_generator = ClsTimesheet()

    try:
        employee_mapping = nocodb_employee.get_all_employees()
        if not employee_mapping:
            print("Tidak ada data karyawan, proses dihentikan.")
            return

        _, _, target_date = get_configured_month_dates()
        target_month = get_month_name_from_date(target_date)
        year_month = target_date.strftime('%Y-%m')

        attendance_month_filter = f"(Month,eq,{target_month})"
        uk_date_filter = f"(Unique Key,like,{year_month}%)"

        attendance_response = nocodb_attendance.get_records(limit=1000, where=attendance_month_filter)
        tasklist_response = nocodb_tasklist.get_records(limit=1000, where=uk_date_filter)
        iot_tasklist_response = nocodb_iot_tasklist.get_records(limit=1000, where=uk_date_filter)
        schedule_shifting_response = nocodb_schedule_shifting.get_records(limit=1000, where=uk_date_filter)
        calendar_response = nocodb_calendar.get_records(limit=500, where=f"(Unique Key,like,{year_month}%)")

        attendance_records = attendance_response.get('list', []) if attendance_response else []
        tasklist_records = tasklist_response.get('list', []) if tasklist_response else []
        iot_tasklist_records = iot_tasklist_response.get('list', []) if iot_tasklist_response else []
        schedule_shifting_records = schedule_shifting_response.get('list', []) if schedule_shifting_response else []
        calendar_records = calendar_response.get('list', []) if calendar_response else []

        iot_employees = {name: info for name, info in employee_mapping.items() if info.get('role') == 'IoT Operations'}
        other_employees = {name: info for name, info in employee_mapping.items() if info.get('role') != 'IoT Operations'}
        
        all_timesheet_data = []

        if other_employees:
            print(f"Membuat timesheet untuk {len(other_employees)} karyawan...")
            print("Generating timesheet data...")
            timesheet_data = timesheet_generator.generate_monthly_timesheet_data(
                employee_mapping=other_employees,
                target_date=target_date,
                attendance_records=attendance_records,
                tasklist_records=tasklist_records,
                task_field_name="Task List Table",
                holiday_records=calendar_records
            )
            if timesheet_data:
                print(f"Generated {len(timesheet_data)} timesheet records for regular employees")
                all_timesheet_data.extend(timesheet_data)

        if iot_employees:
            print(f"Membuat timesheet untuk {len(iot_employees)} karyawan IoT...")
            print("Generating IoT timesheet data...")
            iot_timesheet_data = timesheet_generator.generate_monthly_timesheet_data(
                employee_mapping=iot_employees,
                target_date=target_date,
                attendance_records=attendance_records,
                tasklist_records=iot_tasklist_records,
                task_field_name="Task List IoT Table",
                schedule_records=schedule_shifting_records
            )
            if iot_timesheet_data:
                print(f"Generated {len(iot_timesheet_data)} timesheet records for IoT employees")
                all_timesheet_data.extend(iot_timesheet_data)

        if not all_timesheet_data:
            print("Tidak ada data timesheet baru yang dibuat.")
            return
            
        print(f"Starting batch upsert for {len(all_timesheet_data)} timesheet records...")
        success_count = nocodb_timesheet.batch_upsert_timesheets(all_timesheet_data)

        print(f"Berhasil menyimpan {success_count} dari {len(all_timesheet_data)} data timesheet.")
        
        print("Langkah 4 Selesai.")

    except Exception as e:
        print(f"Langkah 4 Gagal: {e}")
        raise
