import pandas as pd
from datetime import datetime
from src import config
from src.classes.ClsNocoDBProcessor import ClsNocoDBProcessor
from src.classes.ClsAttendanceSheetProcessor import ClsAttendanceSheetProcessor
from src.utils.date_helper import get_configured_month_dates
import logging

def run():
    print("Menjalankan Langkah 6: Sinkronisasi Absensi ke Google Sheets")
    logging.info("Memulai Langkah 6: Sinkronisasi Absensi ke Google Sheets")

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
        logging.warning("Tidak ada data karyawan yang ditemukan dari NocoDB.")
        return

    sheet_id = gsheet_processor.get_sheet_id_from_url(config.ATTENDANCE_SHEET_URL)

    start_date, end_date, target_date = get_configured_month_dates()
    month_config = config.GENERAL_CONFIG.get('month', 'Januari')

    import concurrent.futures
    from threading import Lock

    sheet_lock = Lock()

    def get_time(val):
        if not val: return ''
        actual_val = val[0] if isinstance(val, list) else val
        if actual_val is None or str(actual_val).strip() == '':
            return ''
        time_str = str(actual_val)
        return ':'.join(time_str.split(' ')[-1].split('+')[0].split(':')[:2])

    def process_attendance(name, info):
        employee_id = info.get('employee_id', '')
        
        try:
            gsheet_processor.prepare_future_attendance_rows(
                sheet_id=sheet_id,
                sheet_name=name,
                employee_id=employee_id
            )
        except Exception as e:
            logging.error(f"Gagal mempersiapkan baris masa depan untuk {name}: {e}")
            # Continue to update current month even if preparation fails
        
        where = f"(Name,like,%{name.strip().title()}%)"
        response = nocodb_attendance.get_records(limit=2000, where=where)
        attendance_records = response.get('list', []) if response else []

        attendance_data = {}
        for record in attendance_records:
            date_key = record.get('Date')
            if date_key:
                attendance_data[date_key] = {
                    'Start Time': get_time(record.get('Start Time')),
                    'End Time': get_time(record.get('End Time')),
                    'Last Modified': record.get('Last Modified', '')
                }

        df = gsheet_processor.process_noco_records_to_dataframe(
            employee_name=name,
            employee_id=employee_id,
            attendance_data=attendance_data,
            employee_info=info,
            month_name=month_config,
            target_date=target_date
        )

        if df.empty:
            return name, 0, True

        with sheet_lock:
            try:
                gsheet_processor.update_attendance_data(
                    sheet_id=sheet_id, df=df, employee_name=name
                )
            except Exception as e:
                if "SSL" in str(e) or "connection" in str(e).lower():
                    logging.warning(f"Connection issue for {name}, retrying once: {e}")
                    import time
                    time.sleep(5)
                    gsheet_processor.update_attendance_data(
                        sheet_id=sheet_id, df=df, employee_name=name
                    )
                else:
                    raise

        return name, len(df), True

    employees = list(employee_mapping.items())
    batch_size = 3
    success_count = 0

    for i in range(0, len(employees), batch_size):
        batch = employees[i:i + batch_size]

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            futures = {executor.submit(process_attendance, name, info): name for name, info in batch}

            for future in concurrent.futures.as_completed(futures):
                try:
                    name, count, success = future.result()
                    if success and count > 0:
                        success_count += 1
                        print(f"Sinkronisasi berhasil: {count} data absensi untuk {name} ({target_date.strftime('%B %Y')}).")
                        logging.info(f"Sinkronisasi berhasil untuk {name}.")
                except Exception as e:
                    print(f"Sinkronisasi absensi gagal untuk {futures[future]}. Error: {e}")
                    logging.error(f"Sinkronisasi absensi gagal untuk {futures[future]}: {e}")

    print(f"Completed: {success_count} employees synced successfully.")
    logging.info(f"Langkah 6 Selesai. {success_count} karyawan tersinkronisasi.")
