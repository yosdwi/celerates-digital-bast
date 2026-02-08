import pandas as pd
from datetime import datetime
from src import config
from src.classes.ClsNocoDBProcessor import ClsNocoDBProcessor
from src.classes.ClsAttendanceSheetProcessor import ClsAttendanceSheetProcessor
from src.utils.date_helper import get_configured_month_dates

def run():
    print("Menjalankan Langkah 6: Sinkronisasi Absensi ke Google Sheets")

    employee_table = config.NOCODB_TABLES.get("employee_data")
    attendance_table = config.NOCODB_TABLES.get("attendance")
    if not all([employee_table, attendance_table, config.ATTENDANCE_SHEET_URL]):
        raise ValueError("Konfigurasi tabel karyawan, absensi, atau URL GSheet tidak ditemukan.")

    nocodb_employee = ClsNocoDBProcessor(config.APP_BASE_ID, employee_table)
    nocodb_attendance = ClsNocoDBProcessor(config.APP_BASE_ID, attendance_table)
    gsheet_processor = ClsAttendanceSheetProcessor()

    employee_mapping = nocodb_employee.get_all_employees()
    if not employee_mapping:
        print("Tidak ada data karyawan ditemukan.")
        return

    sheet_id = gsheet_processor.get_sheet_id_from_url(config.ATTENDANCE_SHEET_URL)

    start_date, end_date, target_date = get_configured_month_dates()
    month_config = config.GENERAL_CONFIG.get('month', 'Januari')

    for name, info in employee_mapping.items():
        try:
            employee_id = info.get('employee_id', '')

            where = f"(Name,like,%{name.strip().title()}%)"
            response = nocodb_attendance.get_records(limit=2000, where=where, fields="Date,Start Time,End Time,Last Modified")
            attendance_records = response.get('records', []) if response else []

            def get_time(val):
                if not val: return ''
                actual_val = val[0] if isinstance(val, list) else val
                if actual_val is None or str(actual_val).strip() == '':
                    return ''
                time_str = str(actual_val)
                return ':'.join(time_str.split(' ')[-1].split('+')[0].split(':')[:2])

            attendance_data = {}
            for record in attendance_records:
                fields = record.get('fields', {})
                date_key = fields.get('Date')
                if date_key:
                    attendance_data[date_key] = {
                        'Start Time': get_time(fields.get('Start Time')),
                        'End Time': get_time(fields.get('End Time')),
                        'Last Modified': fields.get('Last Modified', '')
                    }

            df = gsheet_processor.generate_full_month_attendance_with_actual_times(
                employee_name=name,
                target_date=target_date,
                employee_id=employee_id,
                attendance_data=attendance_data,
                employee_info=info,
                month_name=month_config
            )

            if df.empty:
                print(f"Tidak ada data untuk generate untuk {name}")
                continue

            gsheet_processor.update_attendance_data(
                sheet_id=sheet_id, df=df, employee_name=name
            )
            print(f"Sinkronisasi berhasil: {len(df)} data absensi untuk {name} ({target_date.strftime('%B %Y')}).")
        except Exception as e:
            print(f"Sinkronisasi absensi gagal untuk {name}. Error: {e}")
            raise

    print("Langkah 6 Selesai.")
