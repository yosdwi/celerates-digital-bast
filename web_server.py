import logging
import calendar
from datetime import datetime
from flask import Flask, render_template, abort, request, url_for

from src import config
from src.classes.ClsNocoDBProcessor import ClsNocoDBProcessor

# Initialize Flask App
app = Flask(__name__, static_folder='static')
logging.basicConfig(level=logging.INFO)

# --- Helper Functions ---

def get_dynamic_month_dates(year, month):
    """Calculates the start and end dates for a given year and month."""
    _, num_days = calendar.monthrange(year, month)
    start_date = datetime(year, month, 1)
    end_date = datetime(year, month, num_days)
    return start_date, end_date

def format_attendance_time(start_time, end_time):
    """
    Formats attendance time into a list of tuples for structured rendering.
    Example: [('17:00', 'out'), ('08:00', 'in')]
    """
    def get_time(val):
        if not val: return None
        actual_val = val[0] if isinstance(val, list) else val
        if actual_val is None or str(actual_val).strip() == '': return None
        try:
            time_obj = datetime.fromisoformat(str(actual_val).replace('Z', '+00:00'))
            return time_obj.strftime('%H:%M')
        except (ValueError, TypeError):
            time_str = str(actual_val).split(' ')[-1].split('+')[0]
            return ':'.join(time_str.split(':')[:2])

    st = get_time(start_time)
    et = get_time(end_time)
    
    times = []
    if et: times.append((et, 'out'))
    if st: times.append((st, 'in'))
    
    return times

# --- Flask Routes ---

@app.route("/", methods=['GET'])
def index():
    """Serves the main page with the report generation form."""
    return render_template('index.html')

@app.route("/report/all", methods=['POST'])
def generate_all_reports():
    """
    Generates and serves a single, consolidated attendance report for ALL employees
    based on the user's selection from the form.
    """
    try:
        year = int(request.form.get('year'))
        month = int(request.form.get('month'))
    except (TypeError, ValueError):
        return abort(400, description="Invalid or missing year/month.")

    logging.info(f"Generating report for {calendar.month_name[month]} {year}")

    # 1. Fetch Data
    employee_table = config.NOCODB_TABLES.get("employee_data")
    attendance_table = config.NOCODB_TABLES.get("attendance")
    if not all([employee_table, attendance_table]):
        return abort(500, description="Server configuration error for data tables.")

    nocodb_employee = ClsNocoDBProcessor(config.APP_BASE_ID, employee_table)
    nocodb_attendance = ClsNocoDBProcessor(config.APP_BASE_ID, attendance_table)

    employee_mapping = nocodb_employee.get_all_employees()
    if not employee_mapping:
        return abort(404, description="No employee data found.")

    start_date, end_date = get_dynamic_month_dates(year, month)
    
    # 2. Process Each Employee
    reports_data = []
    for name, info in employee_mapping.items():
        display_nrp = info.get('nrp') or info.get('employee_id')
        if not display_nrp:
            logging.warning(f"Skipping {name} as they have no 'nrp' or 'employee_id'.")
            continue

        where_clause = f"(Name,like,%{name.strip().title()}%)"
        records = nocodb_attendance.get_records(limit=2000, where=where_clause).get('list', [])

        attendance_data = []
        for rec in records:
            rec_date_str = rec.get('Date')
            if not rec_date_str: continue
            
            rec_date = datetime.strptime(rec_date_str, '%Y-%m-%d').date()
            if start_date.date() <= rec_date <= end_date.date():
                attendance_data.append({
                    'nrp': display_nrp,
                    'nama': name,
                    'tanggal_kehadiran': rec_date.strftime('%d/%m/%Y'),
                    'jam_kehadiran': format_attendance_time(rec.get('Start Time'), rec.get('End Time'))
                })
        
        if not attendance_data:
            logging.info(f"No attendance data for {name} in the period. Skipping from report.")
            continue

        attendance_data.sort(key=lambda x: datetime.strptime(x['tanggal_kehadiran'], '%d/%m/%Y'))

        reports_data.append({
            'nrp': display_nrp,
            'nama': name.upper(),
            'attendance_rows': attendance_data
        })

    # 3. Render and return
    final_context = {
        'periode': f"{start_date.strftime('%d %B %Y')} - {end_date.strftime('%d %B %Y')}",
        'dicetak': datetime.now().strftime('%d %B %Y %H:%M:%S'),
        'reports': reports_data,
        'logo_url': url_for('static', filename='img/logo_pama.png')
    }
    
    logging.info(f"Successfully generated consolidated report for {len(reports_data)} employees.")
    return render_template('attendance_report_template.html', **final_context)

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=5000)
