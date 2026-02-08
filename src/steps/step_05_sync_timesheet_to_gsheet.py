import pandas as pd
from datetime import datetime
from src import config
from src.classes.ClsNocoDBProcessor import ClsNocoDBProcessor
from src.classes.ClsTimeSheetProcessor import ClsTimeSheetProcessor
from src.utils.date_helper import get_configured_month_dates

def format_nocodb_records_for_gsheet(records, employee_role=None):
    if not records:
        return []

    unique_records = {r.get('fields', {}).get('Date', ''): r for r in records}.values()
    
    # Determine which field to use for work description
    work_desc_field = 'Work Description IoT' if employee_role == 'IoT Operations' else 'Work Description'
    
    all_work_descs = {str(d).strip() for r in unique_records for d in r.get('fields', {}).get(work_desc_field, []) if str(d).strip()}
    combined_desc = '; '.join(sorted(all_work_descs))

    formatted_data = []
    for i, record in enumerate(unique_records):
        fields = record.get('fields', {})

        def get_field(key, default=''):
            val = fields.get(key)
            if isinstance(val, list):
                return ', '.join(map(str, val))
            return str(val) if val is not None else default

        # Get activity field properly
        activity_val = fields.get('Activity', '')
        if isinstance(activity_val, list):
            activity = ', '.join(map(str, activity_val))
        else:
            activity = str(activity_val) if activity_val is not None else ''

        # Simplified holiday check. The 'Holiday' field from the timesheet record is now the source of truth.
        is_holiday = get_field('Holiday').strip().upper() == 'H'

        def get_numeric(key):
            val = fields.get(key)
            if isinstance(val, list): return val[0] if val and isinstance(val[0], (int, float)) else 0.0
            return val if isinstance(val, (int, float)) else 0.0

        def get_time(key):
            val = fields.get(key)
            if not val: return ''
            actual_val = val[0] if isinstance(val, list) else val
            if actual_val is None or str(actual_val).strip() == '':
                return ''
            time_str = str(actual_val)
            return ':'.join(time_str.split(' ')[-1].split('+')[0].split(':')[:2])

        # Check for manual edits in attendance records by inspecting the linked record
        is_manual_edit = False
        # 'Start Time Table' is assumed to be the Link-to-Another-Record field pointing to Attendance
        attendance_linked_record_list = fields.get('Start Time Table')
        if isinstance(attendance_linked_record_list, list) and attendance_linked_record_list:
            try:
                linked_record_fields = attendance_linked_record_list[0].get('fields', {})
                # Use .get() to safely handle missing key, which results in None (null)
                last_modified_by = linked_record_fields.get('Last Modified')
                # Only flag as manual edit if the field has a value AND it's not a system user.
                if last_modified_by and '@system.com' not in last_modified_by:
                    is_manual_edit = True
            except (KeyError, IndexError, AttributeError):
                # Handle cases where the structure might not be as expected
                pass

        formatted_data.append({
            'No': i + 1,
            'Date': datetime.strptime(fields.get('Date',''), '%Y-%m-%d').strftime('%m/%d/%Y'),
            'Activity': activity,
            'Project Name': '' if is_holiday else get_field('Project Name'),
            'Internal Project ID': '' if is_holiday else get_field('Internal Project ID'),
            'Customer Name/ID': '' if is_holiday else get_field('Customer Name/ID'),
            'PO/Contract No': '' if is_holiday else get_field('PO/Contract No'),
            'Work Description': '' if is_holiday else combined_desc,
            'Start Time': '' if is_holiday else get_time('Start Time'),
            'End Time': '' if is_holiday else get_time('End Time'),
            'Break Hours': '' if is_holiday else get_numeric('Break Hours'),
            'Total Hours': '' if is_holiday else get_numeric('Total Hours'),
            'Over Time Hours': '' if is_holiday else get_numeric('Over Time Hours'),
            'Regular Hours': '' if is_holiday else get_numeric('Regular Hours'),
            'Is Holiday': get_field('Holiday'),
            'Remarks': get_field('Remarks'),
            'IsManualEdit': is_manual_edit
        })
    return formatted_data

def run():
    print("Menjalankan Langkah 5: Sinkronisasi Timesheet ke Google Sheets")

    employee_table = config.NOCODB_TABLES.get("employee_data")
    timesheet_table = config.NOCODB_TABLES.get("timesheet")
    if not all([employee_table, timesheet_table, config.TIMESHEET_URL]):
        raise ValueError("Konfigurasi tabel karyawan, timesheet, atau URL GSheet tidak ditemukan.")

    nocodb_employee = ClsNocoDBProcessor(config.APP_BASE_ID, employee_table)
    nocodb_timesheet = ClsNocoDBProcessor(config.APP_BASE_ID, timesheet_table)
    gsheet_processor = ClsTimeSheetProcessor()

    _, _, target_date_for_gsheet = get_configured_month_dates()
    month_name = ["Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli", 
                  "Agustus", "September", "Oktober", "November", "Desember"][target_date_for_gsheet.month - 1]
    
    employee_mapping = nocodb_employee.get_all_employees()
    if not employee_mapping:
        print("Tidak ada data karyawan ditemukan.")
        return
        
    sheet_id = gsheet_processor.get_sheet_id_from_url(config.TIMESHEET_URL)

    for name, info in employee_mapping.items():
        try:
            where = f"(Calendar Month,eq,{month_name})~and(Employee Name,like,%{name}%)"
            
            response = nocodb_timesheet.get_records(limit=20000, where=where)
            records = response.get('records', []) if response else []

            if not records:
                continue
            
            employee_role = info.get('role')
            formatted_data = format_nocodb_records_for_gsheet(records, employee_role=employee_role)
            df = pd.DataFrame(formatted_data)
            
            if df.empty:
                continue

            gsheet_processor.update_timesheet_data(
                sheet_id=sheet_id, df=df, employee_name=name,
                target_date=target_date_for_gsheet, mapping_key="timesheet",
                employee_role=employee_role
            )
            print(f"Sinkronisasi berhasil: {len(df)} data untuk {name}.")
        except Exception as e:
            print(f"Sinkronisasi gagal untuk {name}. Error: {e}")
            raise

    print("Langkah 5 Selesai.")
