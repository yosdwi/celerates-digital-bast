import pandas as pd
from datetime import datetime
from src import config
from src.classes.ClsNocoDBProcessor import ClsNocoDBProcessor
from src.classes.ClsAttendanceSheetProcessor import ClsAttendanceSheetProcessor

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

    for name, info in employee_mapping.items():
        try:
            where = f"(Name,like,%{name}%)"
            
            response = nocodb_attendance.get_records(limit=2000, where=where, fields="Date,Start Time,End Time,Last Modified")
            records = response.get('records', []) if response else []

            if not records:
                continue
            
            def get_time(val):
                if not val: return ''
                actual_val = val[0] if isinstance(val, list) else val
                if actual_val is None or str(actual_val).strip() == '':
                    return ''
                time_str = str(actual_val)
                return ':'.join(time_str.split(' ')[-1].split('+')[0].split(':')[:2])

            formatted_data = []
            for r in records:
                fields = r.get('fields', {})
                formatted_data.append({
                    'Employee ID': info.get('employee_id'),
                    'Name': name,
                    'Date': fields.get('Date'),
                    'Start Time': get_time(fields.get('Start Time')),
                    'End Time': get_time(fields.get('End Time')),
                    'Last Modified': fields.get('Last Modified', '')
                })
            
            df = pd.DataFrame(formatted_data)

            if df.empty:
                continue
                
            gsheet_processor.update_attendance_data(
                sheet_id=sheet_id, df=df, employee_name=name
            )
            print(f"Sinkronisasi berhasil: {len(df)} data absensi untuk {name}.")
        except Exception as e:
            print(f"Sinkronisasi absensi gagal untuk {name}. Error: {e}")
            raise

    print("Langkah 6 Selesai.")
