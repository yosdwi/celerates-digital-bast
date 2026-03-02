import os
import calendar
import hashlib
import secrets
from datetime import datetime, timedelta
from urllib.parse import urlparse
from typing import Optional, Dict
from pathlib import Path
import re
import psycopg2
import bcrypt

from fastapi import FastAPI, HTTPException, Depends, Request, Form, File, UploadFile, status, Query
from fastapi.responses import HTMLResponse, FileResponse, Response, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from starlette.middleware.sessions import SessionMiddleware

from src import config
from src.classes.ClsNocoDBProcessor import ClsNocoDBProcessor

app = FastAPI(title="Digital BAST Admin", version="1.0.0")

app.add_middleware(SessionMiddleware, secret_key=os.getenv("SESSION_SECRET", secrets.token_urlsafe(32)))

app.mount("/static", StaticFiles(directory="static"), name="static")
# app.mount("/admin/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

active_sessions: Dict[str, Dict] = {}

def get_postgres_connection():
    """Get PostgreSQL connection from DB_URL in .env"""
    try:
        db_url = os.getenv('DB_URL', '')
        if not db_url:
            return None

        parsed = urlparse(db_url)
        conn = psycopg2.connect(
            host=parsed.hostname,
            port=parsed.port or 5432,
            database=parsed.path[1:],
            user=parsed.username,
            password=parsed.password,
            sslmode='require'
        )
        return conn
    except Exception as e:
        return None

def verify_password(plain_password: str, hashed_password: str, salt: str) -> bool:
    """Verify password against NocoDB's hash + salt"""
    try:
        if not hashed_password or not salt:
            return False

        if hashed_password.startswith('$2b$') or hashed_password.startswith('$2a$'):
            try:
                return bcrypt.checkpw(plain_password.encode('utf-8'), hashed_password.encode('utf-8'))
            except:
                pass

        algorithms_to_try = [
            lambda p, s: hashlib.sha256((p + s).encode()).hexdigest(),
            lambda p, s: hashlib.sha1((p + s).encode()).hexdigest(),
            lambda p, s: hashlib.md5((p + s).encode()).hexdigest(),
            lambda p, s: hashlib.sha256((s + p).encode()).hexdigest(),
            lambda p, s: hashlib.sha1((s + p).encode()).hexdigest(),
            lambda p, s: bcrypt.hashpw((p + s).encode('utf-8'), salt.encode('utf-8') if len(salt) >= 22 else bcrypt.gensalt()).decode('utf-8'),
        ]

        for algorithm in algorithms_to_try:
            try:
                computed_hash = algorithm(plain_password, salt)
                if computed_hash == hashed_password:
                    return True
            except Exception:
                continue

        if hashed_password == plain_password:
            return True

        return False

    except Exception as e:
        return False

def authenticate_user(email: str, password: str) -> Optional[Dict]:
    """Authenticate user with NocoDB PostgreSQL users table"""
    try:
        conn = get_postgres_connection()
        if not conn:
            return None

        cursor = conn.cursor()

        query = """
            SELECT
                u.id,
                u.email,
                u.password,
                u.salt,
                u.display_name,
                u.user_name,
                u.blocked,
                u.is_deleted,
                bu.roles as base_role
            FROM nc_users_v2 u
            LEFT JOIN nc_base_users_v2 bu ON u.id = bu.fk_user_id AND bu.base_id = %s
            WHERE u.email = %s
            AND (u.blocked IS NULL OR u.blocked = FALSE)
            AND (u.is_deleted IS NULL OR u.is_deleted = FALSE)
        """

        cursor.execute(query, (config.APP_BASE_ID, email))
        user_data = cursor.fetchone()

        cursor.close()
        conn.close()

        if not user_data:
            return None

        (
            user_id, user_email, stored_password, salt,
            display_name, user_name, blocked, is_deleted, base_role
        ) = user_data

        if base_role != 'owner':
            return None

        if not verify_password(password, stored_password, salt):
            return None

        return {
            'id': user_id,
            'name': display_name or user_name or email.split('@')[0],
            'email': user_email,
            'role': base_role
        }

    except Exception as e:
        return None

def create_user_session(user: Dict) -> str:
    """Create a new user session and return session ID"""
    session_id = secrets.token_urlsafe(32)
    active_sessions[session_id] = {
        'user': user,
        'created_at': datetime.utcnow(),
        'last_accessed': datetime.utcnow()
    }
    return session_id

def get_current_user(request: Request) -> Optional[Dict]:
    """Get current authenticated user from session"""
    session_id = request.session.get('session_id')
    if not session_id or session_id not in active_sessions:
        return None

    session_data = active_sessions[session_id]
    session_data['last_accessed'] = datetime.utcnow()

    if datetime.utcnow() - session_data['created_at'] > timedelta(hours=24):
        del active_sessions[session_id]
        return None

    return session_data['user']

def logout_user(request: Request) -> bool:
    """Logout user and cleanup session"""
    session_id = request.session.get('session_id')
    if session_id and session_id in active_sessions:
        del active_sessions[session_id]
        request.session.clear()
        return True
    return False

def require_auth(request: Request):
    """Dependency to require authentication for protected routes"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user

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

def format_single_nocodb_record(record, all_work_descs, employee_role=None):
    if not record:
        return {}

    def get_field(key, default=''):
        val = record.get(key)
        if isinstance(val, list):
            return ', '.join(map(str, val))
        return str(val) if val is not None else default

    activity_val = record.get('Activity', '')
    activity = ', '.join(map(str, activity_val)) if isinstance(activity_val, list) else str(activity_val or '')

    is_holiday = get_field('Holiday').strip().upper() == 'H'
    
    def get_numeric(key):
        val = record.get(key)
        if isinstance(val, list): return val[0] if val and isinstance(val[0], (int, float)) else 0.0
        return val if isinstance(val, (int, float)) else 0.0

    def get_time(key):
        val = record.get(key)
        if not val: return ''
        actual_val = val[0] if isinstance(val, list) else val
        if actual_val is None or str(actual_val).strip() == '': return ''
        time_str = str(actual_val)
        return ':'.join(time_str.split(' ')[-1].split('+')[0].split(':')[:2])
    
    is_manual_edit = False
    attendance_linked = record.get('Start Time Table')
    if isinstance(attendance_linked, list) and attendance_linked:
        try:
            last_modified = attendance_linked[0].get('fields', {}).get('Last Modified')
            if last_modified and '@system.com' not in last_modified:
                is_manual_edit = True
        except (KeyError, IndexError, AttributeError): pass

    return {
        'Date': datetime.strptime(record.get('Date',''), '%Y-%m-%d').strftime('%a, %b %-d, %Y'),
        'Activity': activity,
        'Project Name': '' if is_holiday else get_field('Project Name'),
        'Internal Project ID': '' if is_holiday else get_field('Internal Project ID'),
        'Customer Name/ID': '' if is_holiday else get_field('Customer Name/ID'),
        'PO/Contract No': '' if is_holiday else get_field('PO/Contract No'),
        'Work Description': '' if is_holiday else all_work_descs,
        'Start Time': '' if is_holiday else get_time('Start Time'),
        'End Time': '' if is_holiday else get_time('End Time'),
        'Break Hours': '' if is_holiday else get_numeric('Break Hours'),
        'Total Hours': '' if is_holiday else get_numeric('Total Hours'),
        'Over Time Hours': '' if is_holiday else get_numeric('Over Time Hours'),
        'Regular Hours': '' if is_holiday else get_numeric('Regular Hours'),
        'Is Holiday': get_field('Holiday'),
        'Remarks': get_field('Remarks'),
        'IsManualEdit': is_manual_edit
    }

@app.get("/login", response_class=HTMLResponse)
async def login(request: Request, error: str = None):
    """Serves the login page."""
    user = get_current_user(request)
    if user:
        return RedirectResponse(url="/admin/", status_code=302)

    return templates.TemplateResponse('login.html', {
        "request": request,
        "error": error
    })

@app.get("/admin/login", response_class=HTMLResponse)
async def admin_login(request: Request, error: str = None):
    """Serves the admin login page - same as /login but with /admin prefix."""
    user = get_current_user(request)
    if user:
        return RedirectResponse(url="/admin/", status_code=302)

    return templates.TemplateResponse('login.html', {
        "request": request,
        "error": error
    })

@app.post("/auth/login")
async def auth_login(request: Request, email: str = Form(...), password: str = Form(...)):
    """Handle login form submission"""
    user = authenticate_user(email, password)

    if not user:
        return templates.TemplateResponse('login.html', {
            "request": request,
            "error": "Invalid email or password, or insufficient permissions"
        })

    session_id = create_user_session(user)
    request.session['session_id'] = session_id

    return RedirectResponse(url="/admin/", status_code=302)

@app.post("/admin/auth/login")
async def admin_auth_login(request: Request, email: str = Form(...), password: str = Form(...)):
    """Handle admin login form submission - same as /auth/login but with /admin prefix"""
    user = authenticate_user(email, password)

    if not user:
        return templates.TemplateResponse('login.html', {
            "request": request,
            "error": "Invalid email or password, or insufficient permissions"
        })

    session_id = create_user_session(user)
    request.session['session_id'] = session_id

    return RedirectResponse(url="/admin/", status_code=302)

@app.get("/auth/logout")
async def auth_logout(request: Request):
    """Handle user logout"""
    logout_user(request)
    return RedirectResponse(url="/login", status_code=302)

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Serves the main page with the report generation form (protected route)."""
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse('index.html', {
        "request": request,
        "user": user
    })

@app.get("/admin", response_class=HTMLResponse)
async def admin_redirect(request: Request):
    """Redirect /admin to /admin/ for consistency."""
    return RedirectResponse(url="/admin/", status_code=301)

@app.get("/admin/", response_class=HTMLResponse)
async def admin_index(request: Request):
    """Serves the admin main page with the report generation form (protected route)."""
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/admin/login", status_code=302)

    from datetime import datetime

    # Get employee list for filter dropdown, needed for the attendance form in index.html
    employee_table = config.NOCODB_TABLES.get("employee_data")
    nocodb_employee = ClsNocoDBProcessor(config.APP_BASE_ID, employee_table)
    employee_mapping = nocodb_employee.get_all_employees()
    employee_list = list(employee_mapping.keys())
    employee_roles = {emp_name: emp_info.get('role', '') for emp_name, emp_info in employee_mapping.items()}

    return templates.TemplateResponse('index.html', {
        "request": request,
        "user": user,
        "employee_list": employee_list,
        "employee_roles": employee_roles,
        "start_date": None,
        "end_date": None,
        "selected_employees": [],
        "datetime": datetime,
        "attendance_data": None  # Use None to indicate it's the initial load
    })

@app.post("/report/pama/attendance")
async def generate_pama_attendance_report(
    request: Request,
    year: int = Form(...),
    month: int = Form(...)
):
    """
    Generates and serves a consolidated attendance report for ALL employees
    """
    try:
        current_year = datetime.now().year
        if year < 2020 or year > current_year + 1:
            raise HTTPException(400, "Invalid year")
        if month < 1 or month > 12:
            raise HTTPException(400, "Invalid month")

    except ValueError:
        raise HTTPException(400, "Invalid year/month format")

    employee_table = config.NOCODB_TABLES.get("employee_data")
    attendance_table = config.NOCODB_TABLES.get("attendance")
    if not all([employee_table, attendance_table]):
        raise HTTPException(500, "Server configuration error for data tables")

    nocodb_employee = ClsNocoDBProcessor(config.APP_BASE_ID, employee_table)
    nocodb_attendance = ClsNocoDBProcessor(config.APP_BASE_ID, attendance_table)

    employee_mapping = nocodb_employee.get_all_employees()
    if not employee_mapping:
        raise HTTPException(404, "No employee data found")

    start_date, end_date = get_dynamic_month_dates(year, month)

    reports_data = []
    for name, info in employee_mapping.items():
        display_nrp = info.get('nrp') or info.get('employee_id')
        if not display_nrp:
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
            continue

        attendance_data.sort(key=lambda x: datetime.strptime(x['tanggal_kehadiran'], '%d/%m/%Y'))

        reports_data.append({
            'nrp': display_nrp,
            'nama': name.upper(),
            'attendance_rows': attendance_data
        })

    final_context = {
        'periode': f"{start_date.strftime('%d %B %Y')} - {end_date.strftime('%d %B %Y')}",
        'dicetak': datetime.now().strftime('%d %B %Y %H:%M:%S'),
        'reports': reports_data,
        'logo_url': '/static/img/logo_pama.png'
    }

    return templates.TemplateResponse('attendance_report_template.html', {
        "request": request,
        **final_context
    })

@app.post("/report/timesheet")
async def generate_timesheet_report(
    request: Request,
    year: int = Form(...),
    month: int = Form(...)
):
    """
    Generates and serves a consolidated timesheet report for ALL employees.
    """
    try:
        current_year = datetime.now().year
        if year < 2020 or year > current_year + 1:
            raise HTTPException(400, "Invalid year")
        if month < 1 or month > 12:
            raise HTTPException(400, "Invalid month")
    except ValueError:
        raise HTTPException(400, "Invalid year/month format")

    employee_table = config.NOCODB_TABLES.get("employee_data")
    timesheet_table = config.NOCODB_TABLES.get("timesheet")
    if not all([employee_table, timesheet_table]):
        raise HTTPException(500, "Server configuration error for data tables")

    nocodb_employee = ClsNocoDBProcessor(config.APP_BASE_ID, employee_table)
    nocodb_timesheet = ClsNocoDBProcessor(config.APP_BASE_ID, timesheet_table)

    employee_mapping = nocodb_employee.get_all_employees()
    if not employee_mapping:
        raise HTTPException(404, "No employee data found")

    indonesian_months = {
        1: 'Januari', 2: 'Februari', 3: 'Maret', 4: 'April',
        5: 'Mei', 6: 'Juni', 7: 'Juli', 8: 'Agustus',
        9: 'September', 10: 'Oktober', 11: 'November', 12: 'Desember'
    }
    month_name = indonesian_months[month]
    
    reports_data = []
    for name, info in employee_mapping.items():
        where = f"(Calendar Month,eq,{month_name})~and(Employee Name,like,%{name}%)"
        response = nocodb_timesheet.get_records(limit=2000, where=where)
        records = response.get('list', [])
        
        records_by_date = {r['Date']: r for r in records if 'Date' in r}
        
        employee_role = info.get('role')
        work_desc_field = 'Work Description IoT' if employee_role == 'IoT Operations' else 'Work Description'
        all_work_descs = '; '.join(sorted({str(d).strip() for r in records for d in r.get(work_desc_field, []) if str(d).strip()}))

        start_date, end_date = get_dynamic_month_dates(year, month)
        
        full_month_data = []
        current_date = start_date
        while current_date <= end_date:
            date_str = current_date.strftime('%Y-%m-%d')
            record = records_by_date.get(date_str)
            
            if record:
                formatted_row = format_single_nocodb_record(record, all_work_descs, employee_role)
            else:
                formatted_row = {
                    'Date': current_date.strftime('%a, %b %-d, %Y'), 'Activity': '', 'Project Name': '', 
                    'Internal Project ID': '', 'Customer Name/ID': '', 'PO/Contract No': '', 
                    'Work Description': '', 'Start Time': '', 'End Time': '', 'Break Hours': '', 
                    'Total Hours': '', 'Over Time Hours': '', 'Regular Hours': '', 'Is Holiday': '',
                    'Remarks': 'Weekend' if current_date.weekday() >= 5 else '',
                    'IsManualEdit': False
                }
            full_month_data.append(formatted_row)
            current_date += timedelta(days=1)
        
        if not any(row['Activity'] for row in full_month_data):
            continue

        total_break_hours = sum(float(row.get('Break Hours', 0) or 0) for row in full_month_data if row.get('Break Hours'))
        total_hours = sum(float(row.get('Total Hours', 0) or 0) for row in full_month_data if row.get('Total Hours'))
        total_overtime_hours = sum(float(row.get('Over Time Hours', 0) or 0) for row in full_month_data if row.get('Over Time Hours'))
        total_regular_hours = sum(float(row.get('Regular Hours', 0) or 0) for row in full_month_data if row.get('Regular Hours'))

        reports_data.append({
            'nama': name.upper(),
            'nrp': info.get('nrp') or info.get('employee_id', 'N/A'),
            'employee_id': info.get('employee_id', 'N/A'),
            'posisi': info.get('position', 'N/A'),
            'start_date': f"Thu, {month_name} 01, {year}",
            'end_date': f"Sat, {month_name} {end_date.day:02d}, {year}",
            'total_break_hours': f"{total_break_hours:.2f}",
            'total_hours': f"{total_hours:.2f}",
            'total_overtime_hours': f"{total_overtime_hours:.2f}",
            'total_regular_hours': f"{total_regular_hours:.2f}",
            'timesheet_rows': full_month_data
        })

    final_context = {
        'periode': f"{month_name} {year}",
        'reports': reports_data,
        'logo_url': '/static/img/logo_pama.png'
    }

    return templates.TemplateResponse('timesheet_report_template.html', {
        "request": request,
        **final_context
    })


@app.get("/timesheettest", response_class=HTMLResponse)
async def timesheet_test(request: Request):
    """Test endpoint for timesheet template with dummy data"""
    dummy_timesheet_data = []

    for day in range(1, 32):
        from datetime import datetime
        date_obj = datetime(2026, 1, day)
        formatted_date = date_obj.strftime("%a, %b %-d, %Y")
        dummy_timesheet_data.append({
            'Date': formatted_date,
            'Activity': "P01-Development" if day <= 30 else "",
            'Project Name': "MTGPR/2023/6100100-Pampersada Nusantara-Talent Force Jan-Dec 2024 for PAMA" if day <= 30 else "",
            'Internal Project ID': "",
            'Customer Name/ID': "",
            'PO/Contract No': "",
            'Work Description': "Automatic R5232 switch untuk interface 1 PC ke 2 LCD800" if day <= 30 else "",
            'Start Time': "08:00" if day <= 30 else "",
            'End Time': "17:00" if day <= 30 else "",
            'Break Hours': 1.0 if day <= 30 else 0,
            'Total Hours': 8.0 if day <= 30 else 0,
            'Over Time Hours': 0.0,
            'Regular Hours': 8.0 if day <= 30 else 0,
            'Is Holiday': "H" if day > 30 else "",
            'Remarks': "Working Day" if day <= 30 else "Weekend",
            'IsManualEdit': False
        })

    dummy_reports = [{
        'nama': 'OVIANTO',
        'nrp': 'MTG-TF/202411.0017',
        'employee_id': 'MTG-TF/202411.0017',
        'posisi': 'IoT Developer',
        'start_date': 'Thu, Januari 01, 2026',
        'end_date': 'Sat, Januari 31, 2026',
        'total_break_hours': '22.00',
        'total_hours': '176.00',
        'total_overtime_hours': '0.00',
        'total_regular_hours': '176.00',
        'timesheet_rows': dummy_timesheet_data
    }]

    context = {
        'periode': 'Januari 2026',
        'reports': dummy_reports,
        'logo_url': '/static/img/logo_celerates.jpg'
    }

    return templates.TemplateResponse('timesheet_report_template.html', {
        "request": request,
        **context
    })

@app.post("/report/tasklistdeveloper")
async def generate_tasklistdeveloper_report(
    request: Request,
    page: str = Form(...),
    month: int = Form(...)
):
    """
    Generates Developer tasklist report from NocoDB data.

    Args:
        page: "pelaksanaan", "kualitas", "rilis", or "support"
        month: Month number (1-12) from form
    """
    if month < 1 or month > 12:
        raise HTTPException(400, "Invalid month")

    if page not in ["pelaksanaan", "kualitas", "rilis", "support"]:
        raise HTTPException(400, "Page must be 'pelaksanaan', 'kualitas', 'rilis', or 'support'")

    indonesian_months = {
        1: 'Januari', 2: 'Februari', 3: 'Maret', 4: 'April',
        5: 'Mei', 6: 'Juni', 7: 'Juli', 8: 'Agustus',
        9: 'September', 10: 'Oktober', 11: 'November', 12: 'Desember'
    }

    month_name = indonesian_months[month]

    if page == "pelaksanaan":
        return await _generate_dev_pelaksanaan_page(request, month_name)
    else:
        return await _generate_dev_kategori_page(request, page, month_name)

async def _generate_dev_pelaksanaan_page(request: Request, month_name: str):
    """Generate pelaksanaan page with static SLA data"""
    pelaksanaan_data = [
        {
            "sla": "Kualitas Kode",
            "parameter": "95% fitur yang dirilis bebas dari bug mayor",
            "pencapaian": "100"
        },
        {
            "sla": "Waktu Rilis",
            "parameter": "2 minggu untuk fitur minor dan selambatnya 4-6 minggu untuk fitur mayor,",
            "pencapaian": "100"
        },
        {
            "sla": "Dukungan Support",
            "parameter": "95% permintaan dukungan diselesaikan dalam target waktu yang ditetapkan",
            "pencapaian": "100"
        }
    ]

    return templates.TemplateResponse('tasklistdeveloper/pelaksanaan_pekerjaan.html', {
        "request": request,
        "pelaksanaan_data": pelaksanaan_data,
        "month": month_name
    })

async def _generate_dev_kategori_page(request: Request, page: str, month_name: str):
    """Generate kategori-based pages from NocoDB data"""
    table_id = config.NOCODB_TABLES.get("tasklist")
    if not table_id:
        raise HTTPException(500, "Developer tasklist table not found in config")

    nocodb = ClsNocoDBProcessor(config.APP_BASE_ID, table_id)

    kategori_mapping = {
        "kualitas": "Detail Aktivitas Kualitas Kode",
        "rilis": "Detail Aktivitas Waktu Rilis",
        "support": "Detail Aktivitas Dukungan Support"
    }

    kategori_name = kategori_mapping.get(page)
    if not kategori_name:
        raise HTTPException(400, f"Invalid page: {page}")

    where_clause = f"(Month,eq,{month_name})~and(Kategori,eq,{kategori_name})~and(Status,eq,Closed)"
    records = nocodb.get_records(limit=2000, where=where_clause).get('list', [])

    if page == "kualitas":
        return await _generate_dev_kualitas_data(request, records, month_name)
    elif page == "rilis":
        return await _generate_dev_rilis_data(request, records, month_name)
    elif page == "support":
        return await _generate_dev_support_data(request, records, month_name)

async def _generate_dev_kualitas_data(request: Request, records: list, month_name: str):
    """Generate kualitas kode data"""
    kualitas_data = []
    for i, record in enumerate(records, 1):
        task_list = record.get('Task List', 'No Task Description')
        requestor = record.get('Requestor', 'N/A')
        pic_list = record.get('PIC', [])
        pic = ', '.join(pic_list) if isinstance(pic_list, list) else str(pic_list or 'N/A')
        status = record.get('Status', 'N/A')
        start_date = record.get('Start Date', '')
        end_date = record.get('End Date', '')
        pencapaian = record.get('Pencapaian', 0)

        formatted_start = start_date.replace('-', '/') if start_date else 'N/A'
        formatted_end = end_date.replace('-', '/') if end_date else 'N/A'

        kualitas_data.append({
            "no": i,
            "task_list": task_list,
            "requestor": requestor,
            "pic": pic,
            "status": status,
            "start_date": formatted_start,
            "end_date": formatted_end,
            "pencapaian": str(pencapaian)
        })

    total_pencapaian = sum(int(record.get('Pencapaian', 0)) for record in records if record.get('Pencapaian'))
    avg_pencapaian = total_pencapaian // len(records) if records else 0

    return templates.TemplateResponse('tasklistdeveloper/detail_aktivitas_kualitas_kode.html', {
        "request": request,
        "kualitas_kode_data": kualitas_data,
        "summary_pencapaian": str(avg_pencapaian),
        "month": month_name
    })

async def _generate_dev_rilis_data(request: Request, records: list, month_name: str):
    """Generate waktu rilis data"""
    rilis_data = []
    for i, record in enumerate(records, 1):
        task_list = record.get('Task List', 'No Task Description')
        requestor = record.get('Requestor', 'N/A')
        pic_list = record.get('PIC', [])
        pic = ', '.join(pic_list) if isinstance(pic_list, list) else str(pic_list or 'N/A')
        status = record.get('Status', 'N/A')
        start_date = record.get('Start Date', '')
        end_date = record.get('End Date', '')
        pencapaian = record.get('Pencapaian', 0)

        formatted_start = start_date.replace('-', '/') if start_date else 'N/A'
        formatted_end = end_date.replace('-', '/') if end_date else 'N/A'

        rilis_data.append({
            "no": i,
            "task_list": task_list,
            "requestor": requestor,
            "pic": pic,
            "status": status,
            "start_date": formatted_start,
            "end_date": formatted_end,
            "pencapaian": str(pencapaian)
        })

    total_pencapaian = sum(int(record.get('Pencapaian', 0)) for record in records if record.get('Pencapaian'))
    avg_pencapaian = total_pencapaian // len(records) if records else 0

    return templates.TemplateResponse('tasklistdeveloper/detail_aktivitas_waktu_rilis.html', {
        "request": request,
        "waktu_rilis_data": rilis_data,
        "summary_pencapaian": str(avg_pencapaian),
        "month": month_name
    })

async def _generate_dev_support_data(request: Request, records: list, month_name: str):
    """Generate dukungan support data"""
    support_data = []
    for i, record in enumerate(records, 1):
        task_list = record.get('Task List', 'No Task Description')
        requestor = record.get('Requestor', 'N/A')
        pic_list = record.get('PIC', [])
        pic = ', '.join(pic_list) if isinstance(pic_list, list) else str(pic_list or 'N/A')
        status = record.get('Status', 'N/A')
        start_date = record.get('Start Date', '')
        end_date = record.get('End Date', '')
        pencapaian = record.get('Pencapaian', 0)

        formatted_start = start_date.replace('-', '/') if start_date else 'N/A'
        formatted_end = end_date.replace('-', '/') if end_date else 'N/A'

        support_data.append({
            "no": i,
            "task_list": task_list,
            "requestor": requestor,
            "pic": pic,
            "status": status,
            "start_date": formatted_start,
            "end_date": formatted_end,
            "pencapaian": str(pencapaian)
        })

    total_pencapaian = sum(int(record.get('Pencapaian', 0)) for record in records if record.get('Pencapaian'))
    avg_pencapaian = total_pencapaian // len(records) if records else 0

    return templates.TemplateResponse('tasklistdeveloper/detail_aktivitas_dukungan_support.html', {
        "request": request,
        "dukungan_support_data": support_data,
        "summary_pencapaian": str(avg_pencapaian),
        "month": month_name
    })

@app.get("/tasklistdevelopertest", response_class=HTMLResponse)
async def tasklistdeveloper_test(request: Request, page: str = "pelaksanaan"):
    """Test endpoint for task list developer templates with dummy data"""
    if page == "pelaksanaan":
        dummy_pelaksanaan_data = [
            {"sla": "Kualitas Kode", "parameter": "95% fitur yang dirilis bebas dari bug mayor", "pencapaian": "100"},
            {"sla": "Waktu Rilis", "parameter": "2 minggu untuk fitur minor dan selambatnya 4-6 minggu untuk fitur mayor,", "pencapaian": "100"},
            {"sla": "Dukungan Support", "parameter": "95% permintaan dukungan diselesaikan dalam target waktu yang ditetapkan", "pencapaian": "100"}
        ]

        return templates.TemplateResponse('tasklistdeveloper/pelaksanaan_pekerjaan.html', {
            "request": request,
            "pelaksanaan_data": dummy_pelaksanaan_data
        })

    elif page == "kualitas":
        dummy_kualitas_data = []
        tasks = [
            "Maintenance/Bugfix Health Check Service",
            "Maintenance/Bugfix Health Check Service",
            "Task Scheduler Module Health Check & Generate Batch",
            "Refactoring worker & interface parsing hex fleetsight",
            "OBSMS Get data speed GPS",
            "Adjustment OBSMS GPS Speed, Pentaho TPMS Daily, Enhancement Asset Taking Digi - List & User Access",
            "Maintenance Pentaho Feature Availability, OPA Manage License",
            "Service OPA ST License Compare, Pentaho PA Daily MH02",
            "Service OPA ST License Compare",
            "Pentaho MH02 Replication Daily Shift & SCM, MH02 Api Last Connection",
            "MH02 Api Last Connection Update",
            "Pentaho Replication Production Detail, Bugfix Digi Asset Management",
            "Pentaho dump opr MIR",
            "Pentaho dump opr MIR",
            "Maintenance Health Check",
            "Enhance dashboard Digi Asset Taking detail asset by category"
        ]

        requestors = ["Adi Pranoto", "Adi Pranoto", "Adi Pranoto", "Adi Pranoto", "Adi Pranoto",
                     "Sugiyanto", "Adi Pranoto", "Ifan Saputra", "Adi Pranoto", "Fauzan",
                     "Adi Pranoto", "Adi Pranoto", "Adi Pranoto", "Adi Pranoto", "Adi Pranoto", "Sugiyanto"]

        for i, task in enumerate(tasks):
            dummy_kualitas_data.append({
                "no": i + 1,
                "task_list": task,
                "requestor": requestors[i],
                "pic": "Hanung Rizqi Widianto",
                "status": "Closed",
                "start_date": f"2026/1/{i+1}",
                "end_date": f"2026/1/{i+1}",
                "pencapaian": "100"
            })

        return templates.TemplateResponse('tasklistdeveloper/detail_aktivitas_kualitas_kode.html', {
            "request": request,
            "kualitas_kode_data": dummy_kualitas_data,
            "summary_pencapaian": "100"
        })

    elif page == "rilis":
        dummy_rilis_data = [
            {"no": 1, "task_list": "Deployment Hardware prototype Rover MIR site BRCB", "requestor": "Bagas Eko P", "pic": "Ovianto", "status": "Closed", "start_date": "2026/1/15", "end_date": "2026/1/26", "pencapaian": "100"},
            {"no": 2, "task_list": "Deployment Hardware GPS RTK on device MH02", "requestor": "Bagas Eko P", "pic": "Ovianto", "status": "Closed", "start_date": "2026/1/19", "end_date": "2026/1/21", "pencapaian": "100"},
            {"no": 3, "task_list": "Deploy service send alert overspeed yang masih open via bot wa", "requestor": "Inky Danindra", "pic": "Aris Purnama", "status": "Closed", "start_date": "2026/1/12", "end_date": "2026/1/12", "pencapaian": "100"},
            {"no": 4, "task_list": "Deploy Grup Telegram BRCG", "requestor": "Inky Danindra", "pic": "Aris Purnama", "status": "Closed", "start_date": "2026/1/22", "end_date": "2026/1/22", "pencapaian": "100"}
        ]

        return templates.TemplateResponse('tasklistdeveloper/detail_aktivitas_waktu_rilis.html', {
            "request": request,
            "waktu_rilis_data": dummy_rilis_data,
            "summary_pencapaian": "100"
        })

    elif page == "support":
        dummy_support_data = [
            {"no": 1, "task_list": "Function import export data untuk instalasi aplikasi di perangkat user requester persiapan annual meeting", "requestor": "Ridhwan Wahyudi", "pic": "Muhammad Atsal Rizandri", "status": "Closed", "start_date": "2026/1/23", "end_date": "2026/1/23", "pencapaian": "100"},
            {"no": 2, "task_list": "Laporan dokumentasi riset wearable untuk presentasi annual meeting", "requestor": "Ridhwan Wahyudi", "pic": "Muhammad Atsal Rizandri", "status": "Closed", "start_date": "2026/1/9", "end_date": "2026/1/13", "pencapaian": "100"},
            {"no": 3, "task_list": "Maintenance Server JIEPPPSCU25011 (semua service mati)", "requestor": "Tim MS", "pic": "Aris Purnama", "status": "Closed", "start_date": "2026/1/19", "end_date": "2026/1/19", "pencapaian": "100"},
            {"no": 4, "task_list": "Cek aliran data PM", "requestor": "Fauza", "pic": "Aris Purnama", "status": "Closed", "start_date": "2026/1/19", "end_date": "2026/1/19", "pencapaian": "100"},
            {"no": 5, "task_list": "Cek aliran data mongodb", "requestor": "Lutfi", "pic": "Aris Purnama", "status": "Closed", "start_date": "2026/1/19", "end_date": "2026/1/19", "pencapaian": "100"},
            {"no": 6, "task_list": "Membuat Dokumentasi service yang jalan", "requestor": "Fauzan", "pic": "Aris Purnama", "status": "Closed", "start_date": "2026/1/22", "end_date": "2026/1/22", "pencapaian": "100"},
            {"no": 7, "task_list": "Maintenance Server BRCG storage penuh, enhancement script housekeeping", "requestor": "Tim MS", "pic": "Aris Purnama", "status": "Closed", "start_date": "2026/1/22", "end_date": "2026/1/22", "pencapaian": "100"},
            {"no": 8, "task_list": "Troubleshooting KML Polygon di WEB Overspeed tidak Update", "requestor": "Tim MS", "pic": "Aris Purnama", "status": "Closed", "start_date": "2026/1/28", "end_date": "2026/1/28", "pencapaian": "100"},
            {"no": 9, "task_list": "Troubleshooting WA BOT Tidak bisa jalan", "requestor": "Inky Danindra", "pic": "Aris Purnama", "status": "Closed", "start_date": "2026/1/29", "end_date": "2026/1/29", "pencapaian": "100"},
            {"no": 10, "task_list": '"BRCB" Project MIR', "requestor": "Bagas Eko Prasetyo", "pic": "Ovianto", "status": "Closed", "start_date": "2026/1/19", "end_date": "2026/1/26", "pencapaian": "100"},
            {"no": 11, "task_list": "MH02 - Troubleshoot WA BOT PM, datalog, dan auto reportig yang tiba-tiba berhenti", "requestor": "Bagas", "pic": "Muhammad Taufiq Azra H", "status": "Closed", "start_date": "2026/1/30", "end_date": "2026/1/30", "pencapaian": "100"}
        ]

        return templates.TemplateResponse('tasklistdeveloper/detail_aktivitas_dukungan_support.html', {
            "request": request,
            "dukungan_support_data": dummy_support_data,
            "summary_pencapaian": "100"
        })

    else:
        return await tasklistdeveloper_test(request, "pelaksanaan")

@app.post("/report/tasklistiotoperation")
async def generate_tasklistiotoperation_report(
    request: Request,
    page: str = Form(...),
    month: int = Form(...)
):
    """
    Generates IoT Operations tasklist report from NocoDB data.

    Args:
        page: "problem", "aktivitas", or "respon"
        month: Month number (1-12) from form
    """
    if month < 1 or month > 12:
        raise HTTPException(400, "Invalid month")

    if page not in ["problem", "aktivitas", "respon"]:
        raise HTTPException(400, "Page must be 'problem', 'aktivitas', or 'respon'")

    indonesian_months = {
        1: 'Januari', 2: 'Februari', 3: 'Maret', 4: 'April',
        5: 'Mei', 6: 'Juni', 7: 'Juli', 8: 'Agustus',
        9: 'September', 10: 'Oktober', 11: 'November', 12: 'Desember'
    }

    month_name = indonesian_months[month]

    table_id = config.NOCODB_TABLES.get("tasklist_iot")
    if not table_id:
        raise HTTPException(500, "IoT tasklist table not found in config")

    nocodb = ClsNocoDBProcessor(config.APP_BASE_ID, table_id)

    where_clause = f"(Month,eq,{month_name})~and(Status,eq,Closed)"
    records = nocodb.get_records(limit=2000, where=where_clause).get('list', [])

    if page == "problem":
        return await _generate_iot_problem_page(request, records, month_name)
    elif page == "aktivitas":
        return await _generate_iot_aktivitas_page(request, records, month_name)
    elif page == "respon":
        return await _generate_iot_respon_page(request, records, month_name)

async def _generate_iot_problem_page(request: Request, records: list, month_name: str):
    """Generate problem formulas page - using the specific formula structure from requirements"""
    problem_data = [
        {
            "object": "Aktual Waktu Respon (menit)",
            "formula": "(Tanggal Waktu Respon – Tanggal Problem) x 1440",
            "keterangan": "-"
        },
        {
            "object": "Aktual Waktu Penyelesaian (menit)",
            "formula": "(Tanggal Waktu Penyelesaian – Tanggal Problem) x 1440",
            "keterangan": "-"
        },
        {
            "object": "Performance Waktu Respon (%)",
            "formula": "100%+((100%-(Aktual Waktu Respon : 15)",
            "keterangan": [
                "Jika hasil waktu respon >100%, maka hasil maksimal tetap 100%",
                "Jika hasil waktu respon <0%, maka hasil minimum tetap 0%"
            ]
        },
        {
            "object": "Performance Waktu Penyelesaian (%)",
            "formula": "100%+((100%-(Aktual Waktu Penyelesaian : 30)",
            "keterangan": [
                "Jika hasil waktu respon >100%, maka hasil maksimal tetap 100%",
                "Jika hasil waktu respon <0%, maka hasil minimum tetap 0%"
            ]
        },
        {
            "object": "Rata rata Waktu Respon dan rata rata Waktu Penyelesaian (%)",
            "formula": "Average (Performance Waktu Respon% dan Performance Waktu Penyelesaian %)",
            "keterangan": "-"
        }
    ]

    return templates.TemplateResponse('tasklistiotoperation/detail_problem_pihak_kedua.html', {
        "request": request,
        "problem_data": problem_data,
        "month": month_name
    })

async def _generate_iot_aktivitas_page(request: Request, records: list, month_name: str):
    """Generate activities page from Developer tasklist for Engineer Manage Service"""
    dev_table_id = config.NOCODB_TABLES.get("tasklist")
    if not dev_table_id:
        return await _generate_iot_aktivitas_fallback(request, records, month_name)

    dev_nocodb = ClsNocoDBProcessor(config.APP_BASE_ID, dev_table_id)

    engineer_manage_service = "Muhammad Fauzan Acyuto"
    where_clause = f"(Month,eq,{month_name})~and(PIC,like,%{engineer_manage_service}%)~and(Status,eq,Closed)"

    dev_records = dev_nocodb.get_records(limit=2000, where=where_clause).get('list', [])

    if not dev_records:
        return await _generate_iot_aktivitas_fallback(request, records, month_name)

    aktivitas_data = []
    for i, record in enumerate(dev_records, 1):
        task_list = record.get('Task List', 'No Task Description')
        start_date = record.get('Start Date', '')
        end_date = record.get('End Date', '')
        requestor = record.get('Requestor', 'N/A')

        pic_list = record.get('PIC', [])
        engineer_manage = ', '.join(pic_list) if isinstance(pic_list, list) else str(pic_list or 'N/A')

        lead_time = "N/A"
        if start_date and end_date:
            try:
                from datetime import datetime
                start = datetime.strptime(start_date, '%Y-%m-%d')
                end = datetime.strptime(end_date, '%Y-%m-%d')
                days = (end - start).days + 1
                lead_time = f"{days} Hari" if days > 1 else "1 Hari"
            except ValueError:
                lead_time = "N/A"

        formatted_start = start_date
        formatted_end = end_date
        if start_date:
            try:
                formatted_start = datetime.strptime(start_date, '%Y-%m-%d').strftime('%d %B %Y')
            except:
                pass
        if end_date:
            try:
                formatted_end = datetime.strptime(end_date, '%Y-%m-%d').strftime('%d %B %Y')
            except:
                pass

        aktivitas_data.append({
            "no": i,
            "detail_aktivitas": task_list,
            "tanggal_request": formatted_start,
            "tanggal_penyelesaian": formatted_end,
            "lead_time": lead_time,
            "requestor_pic": "Bagas Eko Prasetyo",
            "engineer_manage": engineer_manage
        })

    return templates.TemplateResponse('tasklistiotoperation/detail_aktivitas_pihak_kedua.html', {
        "request": request,
        "aktivitas_data": aktivitas_data,
        "month": month_name
    })

async def _generate_iot_aktivitas_fallback(request: Request, records: list, month_name: str):
    """Fallback function using IoT records if Developer data not available"""
    aktivitas_data = []
    for i, record in enumerate(records, 1):
        task_list = record.get('Task List', 'No Task Description')
        start_date = record.get('Start Date', '')
        end_date = record.get('End Date', '')
        requestor = record.get('Requestor', 'N/A')

        pic_list = record.get('PIC', [])
        pic_str = ', '.join(pic_list) if isinstance(pic_list, list) else str(pic_list or 'N/A')

        lead_time = "N/A"
        if start_date and end_date:
            try:
                from datetime import datetime
                start = datetime.strptime(start_date, '%Y-%m-%d')
                end = datetime.strptime(end_date, '%Y-%m-%d')
                days = (end - start).days + 1
                lead_time = f"{days} Hari" if days > 1 else "1 Hari"
            except ValueError:
                lead_time = "N/A"

        formatted_start = start_date
        formatted_end = end_date
        if start_date:
            try:
                formatted_start = datetime.strptime(start_date, '%Y-%m-%d').strftime('%d %B %Y')
            except:
                pass
        if end_date:
            try:
                formatted_end = datetime.strptime(end_date, '%Y-%m-%d').strftime('%d %B %Y')
            except:
                pass

        aktivitas_data.append({
            "no": i,
            "detail_aktivitas": task_list,
            "tanggal_request": formatted_start,
            "tanggal_penyelesaian": formatted_end,
            "lead_time": lead_time,
            "requestor_pic": "Bagas Eko Prasetyo",
            "engineer_manage": pic_str
        })

    return templates.TemplateResponse('tasklistiotoperation/detail_aktivitas_pihak_kedua.html', {
        "request": request,
        "aktivitas_data": aktivitas_data,
        "month": month_name
    })

async def _generate_iot_respon_page(request: Request, records: list, month_name: str):
    """Generate response time page from NocoDB records"""
    respon_data = []
    for i, record in enumerate(records, 1):
        task_list = record.get('Task List', 'No Task Description')
        start_date = record.get('Start Date', '')
        end_date = record.get('End Date', '')
        requestor = record.get('Requestor', 'N/A')

        pic_list = record.get('PIC', [])
        engineer = ', '.join(pic_list) if isinstance(pic_list, list) else str(pic_list or 'N/A')

        start_time = "08:00"
        end_time = "17:00"

        response_minutes = "30"

        respon_data.append({
            "no": i,
            "problem": task_list,
            "tanggal_problem": start_date,
            "waktu_problem": start_time,
            "tanggal_respon": start_date,
            "tanggal_penyelesaian": end_date,
            "waktu_penyelesaian": end_time,
            "pic_pama": requestor,
            "engineer": engineer,
            "waktu_respon_menit": response_minutes,
            "aktual_waktu_1": response_minutes,
            "aktual_waktu_2": "180",
            "aktual_waktu_3": "60",
            "aktual_waktu_4": "240",
            "performance_respon_1": "95",
            "performance_respon_2": "98",
            "performance_penyelesaian_1": "97",
            "performance_penyelesaian_2": "99"
        })

    return templates.TemplateResponse('tasklistiotoperation/detail_respon_resolution_time.html', {
        "request": request,
        "respon_data": respon_data,
        "summary_percentage": "97.5",
        "month": month_name
    })

@app.get("/tasklistiotoperation", response_class=HTMLResponse)
async def tasklistiotoperation_test(request: Request, page: str = "problem"):
    """Test endpoint for IoT operation templates with dummy data"""
    if page == "problem":
        dummy_problem_data = [
            {
                "object": "Availability System",
                "formula": "Total Up Time/(Total Up Time + Total Down Time) x 100%",
                "keterangan": [
                    "Up Time: Waktu sistem berjalan normal tanpa gangguan",
                    "Down Time: Waktu sistem mengalami gangguan atau tidak dapat diakses",
                    "Target minimum: 99.5%"
                ]
            },
            {
                "object": "Response Time Performance",
                "formula": "Total Response Time/Total Request x 100%",
                "keterangan": [
                    "Response Time: Waktu yang dibutuhkan sistem untuk merespon request",
                    "Target maksimum: 3 detik per request",
                    "Diukur dari endpoint utama aplikasi"
                ]
            },
            {
                "object": "Error Rate System",
                "formula": "Total Error/(Total Success + Total Error) x 100%",
                "keterangan": [
                    "Error: Jumlah request yang menghasilkan error (4xx, 5xx)",
                    "Success: Jumlah request yang berhasil diproses (2xx, 3xx)",
                    "Target maksimum: 1%"
                ]
            }
        ]

        return templates.TemplateResponse('tasklistiotoperation/detail_problem_pihak_kedua.html', {
            "request": request,
            "problem_data": dummy_problem_data
        })

    elif page == "aktivitas":
        dummy_aktivitas_data = [
            {"no": 1, "detail_aktivitas": "Site Survey MIR", "tanggal_request": "15 Januari 2026", "tanggal_penyelesaian": "16 Januari 2026", "lead_time": "1 Hari", "requestor_pic": "Bagas Eko Prasetyo", "engineer_manage": "OVIANTO/MUHAMMAD ATSAL/AZANDRI"},
            {"no": 2, "detail_aktivitas": "Wib Project MIR", "tanggal_request": "22 Januari 2026", "tanggal_penyelesaian": "31 Januari 2026", "lead_time": "7 Hari", "requestor_pic": "Bagas Eko Prasetyo", "engineer_manage": "OVIANTO/MUHAMMAD ATSAL/AZANDRI"},
            {"no": 3, "detail_aktivitas": "Wib Project MIR", "tanggal_request": "22 Januari 2026", "tanggal_penyelesaian": "26 Januari 2026", "lead_time": "4 Hari", "requestor_pic": "Bagas Eko Prasetyo", "engineer_manage": "OVIANTO/MUHAMMAD ATSAL/AZANDRI"},
            {"no": 4, "detail_aktivitas": "Wib Project MIR", "tanggal_request": "22 Januari 2026", "tanggal_penyelesaian": "23 Januari 2026", "lead_time": "1 Hari", "requestor_pic": "Bagas Eko Prasetyo", "engineer_manage": "OVIANTO/MUHAMMAD ATSAL/AZANDRI"},
            {"no": 5, "detail_aktivitas": "Wib Project MIR", "tanggal_request": "22 Januari 2026", "tanggal_penyelesaian": "30 Januari 2026", "lead_time": "6 Hari", "requestor_pic": "Bagas Eko Prasetyo", "engineer_manage": "OVIANTO/MUHAMMAD ATSAL/AZANDRI"}
        ]

        return templates.TemplateResponse('tasklistiotoperation/detail_aktivitas_pihak_kedua.html', {
            "request": request,
            "aktivitas_data": dummy_aktivitas_data
        })

    elif page == "respon":
        dummy_respon_data = []
        problems = [
            "Server down pada monitoring system BRCG",
            "Koneksi GPS terputus pada unit MH02-001",
            "Database connection timeout",
            "Sensor temperature tidak merespon",
            "Alert system tidak mengirim notifikasi",
            "Dashboard loading sangat lambat",
            "API endpoint returning error 500",
            "Data logging terhenti mendadak"
        ]

        for i, problem in enumerate(problems):
            dummy_respon_data.append({
                "no": i + 1,
                "problem": problem,
                "tanggal_problem": f"{i+15}/01/2026",
                "waktu_problem": f"{8+i}:00",
                "tanggal_respon": f"{i+15}/01/2026",
                "tanggal_penyelesaian": f"{i+15}/01/2026",
                "waktu_penyelesaian": f"{10+i}:30",
                "pic_pama": "Bagas Eko P",
                "engineer": "Aris Purnama",
                "waktu_respon_menit": f"{15+i*5}",
                "aktual_waktu_1": f"{15+i*5}",
                "aktual_waktu_2": f"{150+i*10}",
                "aktual_waktu_3": f"{30+i*3}",
                "aktual_waktu_4": f"{180+i*15}",
                "performance_respon_1": "95",
                "performance_respon_2": "98",
                "performance_penyelesaian_1": "97",
                "performance_penyelesaian_2": "99"
            })

        return templates.TemplateResponse('tasklistiotoperation/detail_respon_resolution_time.html', {
            "request": request,
            "respon_data": dummy_respon_data,
            "summary_percentage": "97.5"
        })

    else:
        return await tasklistiotoperation_test(request, "problem")

@app.post("/report/evidence")
async def generate_evidence_report(
    request: Request,
    type: str = Form(...),
    month: int = Form(...)
):
    """
    Generates evidence report from NocoDB tasklist data.

    Args:
        type: "iotoperations" for IoT Operations tasklist or "developer" for Developer tasklist
        month: Month number (1-12) from form
    """
    if month < 1 or month > 12:
        raise HTTPException(400, "Invalid month")

    indonesian_months = {
        1: 'Januari', 2: 'Februari', 3: 'Maret', 4: 'April',
        5: 'Mei', 6: 'Juni', 7: 'Juli', 8: 'Agustus',
        9: 'September', 10: 'Oktober', 11: 'November', 12: 'Desember'
    }

    month_name = indonesian_months[month]

    if type == "iotoperations":
        table_key = "tasklist_iot"
    elif type == "developer":
        table_key = "tasklist"
    else:
        raise HTTPException(400, "Type must be 'iotoperations' or 'developer'")

    table_id = config.NOCODB_TABLES.get(table_key)
    if not table_id:
        raise HTTPException(500, f"Table configuration not found for {table_key}")

    nocodb = ClsNocoDBProcessor(config.APP_BASE_ID, table_id)

    where_clause = f"(Month,eq,{month_name})~and(Evidence Task,notnull)"
    records = nocodb.get_records(limit=2000, where=where_clause).get('list', [])

    if not records:
        return templates.TemplateResponse('evidence/evidence_aktivitas.html', {
            "request": request,
            "evidence_data": [],
            "type": type,
            "month": month_name
        })

    evidence_data = []
    for i, record in enumerate(records, 1):
        task_list = record.get('Task List', 'No Task Description')
        evidence_task = record.get('Evidence Task', [])

        image_urls = []
        if evidence_task and isinstance(evidence_task, list):
            for attachment in evidence_task:
                if isinstance(attachment, dict):
                    signed_path = attachment.get('signedPath')
                    if signed_path:
                        full_url = f"{config.NOCODB_BASE_URL.rstrip('/')}/{signed_path}"
                        image_urls.append(full_url)

        if image_urls:
            image_path = image_urls[0]
        else:
            continue

        evidence_data.append({
            "number": i,
            "title": task_list,
            "image_path": image_path,
            "description": task_list
        })

    return templates.TemplateResponse('evidence/evidence_aktivitas.html', {
        "request": request,
        "evidence_data": evidence_data,
        "type": type,
        "month": month_name
    })

@app.get("/evidence", response_class=HTMLResponse)
async def evidence_test(request: Request):
    """Test endpoint for evidence templates with dummy data"""
    dummy_evidence_data = [
        {
            "number": 1,
            "title": "Monitoring dan penyesuaian sistem Web aktivitas MH02 tim site (2 Januari 2026 s/d 28 Januari 2026)",
            "image_path": "/static/img/evidence1.jpg",
            "description": "Dokumentasi kegiatan monitoring sistem web aktivitas MH02 selama periode Januari 2026"
        },
        {
            "number": 2,
            "title": "Diskusi alur aplikasi web untuk aktivitas MH02 yang bisa mengakomodir seluruh product DIGI (5 Januari 2026)",
            "image_path": "/static/img/evidence2.jpg",
            "description": "Meeting dan diskusi terkait pengembangan aplikasi web MH02 untuk integrasi dengan produk DIGI"
        },
        {
            "number": 3,
            "title": "Implementasi fitur reporting otomatis untuk sistem monitoring BRCG (10 Januari 2026)",
            "image_path": "/static/img/evidence3.jpg",
            "description": "Pengembangan dan testing fitur reporting otomatis pada sistem monitoring BRCG"
        },
        {
            "number": 4,
            "title": "Setup dan konfigurasi server backup untuk disaster recovery (15 Januari 2026)",
            "image_path": "/static/img/evidence4.jpg",
            "description": "Proses setup server backup dan testing disaster recovery procedure"
        },
        {
            "number": 5,
            "title": "Training tim teknis untuk maintenance sistem IoT operations (20 Januari 2026)",
            "image_path": "/static/img/evidence5.jpg",
            "description": "Sesi training internal untuk tim teknis terkait maintenance dan troubleshooting sistem IoT"
        }
    ]

    return templates.TemplateResponse('evidence/evidence_aktivitas.html', {
        "request": request,
        "evidence_data": dummy_evidence_data
    })

@app.post("/report/all")
async def generate_all_report(
    request: Request,
    type: str = Form(...),
    month: int = Form(...)
):
    """
    Generates comprehensive report by merging existing HTML reports.

    Args:
        type: "iotoperation" or "developer"
        month: Month number (1-12) from form
    """
    if month < 1 or month > 12:
        raise HTTPException(400, "Invalid month")

    if type not in ["iotoperation", "developer"]:
        raise HTTPException(400, "Type must be 'iotoperation' or 'developer'")

    current_year = datetime.now().year

    try:
        html_sections = []

        timesheet_htmls = await _get_timesheet_html_sections(month, current_year, type, request)
        html_sections.extend(timesheet_htmls)

        if type == "iotoperation":
            iot_htmls = await _get_iot_tasklist_html_sections(month, request)
            html_sections.extend(iot_htmls)
        else:
            dev_htmls = await _get_developer_tasklist_html_sections(month, request)
            html_sections.extend(dev_htmls)

        evidence_type_param = "iotoperations" if type == "iotoperation" else "developer"
        evidence_html = await _get_evidence_html_section(evidence_type_param, month, request)
        if evidence_html:
            html_sections.append(evidence_html)

        attendance_html = await _get_attendance_html_section(month, current_year, type, request)
        if attendance_html:
            html_sections.append(attendance_html)

        return templates.TemplateResponse('all_report_template.html', {
            "request": request,
            "type": type,
            "month": month,
            "year": current_year,
            "html_sections": html_sections,
            "logo_pama_url": '/admin/static/img/logo_pama.png',
            "logo_celerates_url": '/admin/admin/static/img/logo_celerates.jpg',
            "datetime": datetime
        })

    except Exception as e:
        raise HTTPException(500, f"Error generating comprehensive report: {str(e)}")

async def _get_timesheet_html_sections(month: int, year: int, report_type: str, request: Request):
    """Get timesheet HTML for each employee separately, filtered by role"""
    employee_table = config.NOCODB_TABLES.get("employee_data")
    nocodb_employee = ClsNocoDBProcessor(config.APP_BASE_ID, employee_table)

    if report_type == "iotoperation":
        role_filter = "IoT Operations"
    elif report_type == "developer":
        role_filter = "Developer"
    else:
        role_filter = None

    employee_mapping = nocodb_employee.get_all_employees(role_filter=role_filter)

    timesheet_htmls = []

    for name, info in employee_mapping.items():
        try:
            single_employee_data = await _generate_single_employee_timesheet(name, info, month, year)

            if single_employee_data and single_employee_data.get('timesheet_rows'):
                html_content = await _render_single_timesheet_html(single_employee_data, request)
                timesheet_htmls.append({
                    'type': 'timesheet',
                    'employee_name': name.upper(),
                    'content': html_content
                })
        except Exception as e:
            continue

    return timesheet_htmls

async def _generate_single_employee_timesheet(name: str, info: dict, month: int, year: int):
    """Generate timesheet data for single employee using existing logic"""
    timesheet_table = config.NOCODB_TABLES.get("timesheet")
    nocodb_timesheet = ClsNocoDBProcessor(config.APP_BASE_ID, timesheet_table)

    indonesian_months = {
        1: 'Januari', 2: 'Februari', 3: 'Maret', 4: 'April',
        5: 'Mei', 6: 'Juni', 7: 'Juli', 8: 'Agustus',
        9: 'September', 10: 'Oktober', 11: 'November', 12: 'Desember'
    }
    month_name = indonesian_months[month]

    where = f"(Calendar Month,eq,{month_name})~and(Employee Name,like,%{name}%)"
    response = nocodb_timesheet.get_records(limit=2000, where=where)
    records = response.get('list', [])

    if not records:
        return None

    records_by_date = {r['Date']: r for r in records if 'Date' in r}
    employee_role = info.get('role')
    work_desc_field = 'Work Description IoT' if employee_role == 'IoT Operations' else 'Work Description'
    all_work_descs = '; '.join(sorted({str(d).strip() for r in records for d in r.get(work_desc_field, []) if str(d).strip()}))

    start_date, end_date = get_dynamic_month_dates(year, month)

    full_month_data = []
    current_date = start_date
    while current_date <= end_date:
        date_str = current_date.strftime('%Y-%m-%d')
        record = records_by_date.get(date_str)

        if record:
            formatted_row = format_single_nocodb_record(record, all_work_descs, employee_role)
        else:
            formatted_row = {
                'Date': current_date.strftime('%a, %b %-d, %Y'), 'Activity': '', 'Project Name': '',
                'Internal Project ID': '', 'Customer Name/ID': '', 'PO/Contract No': '',
                'Work Description': '', 'Start Time': '', 'End Time': '', 'Break Hours': '',
                'Total Hours': '', 'Over Time Hours': '', 'Regular Hours': '', 'Is Holiday': '',
                'Remarks': 'Weekend' if current_date.weekday() >= 5 else '',
                'IsManualEdit': False
            }
        full_month_data.append(formatted_row)
        current_date += timedelta(days=1)

    if not any(row['Activity'] for row in full_month_data):
        return None

    total_break_hours = sum(float(row.get('Break Hours', 0) or 0) for row in full_month_data if row.get('Break Hours'))
    total_hours = sum(float(row.get('Total Hours', 0) or 0) for row in full_month_data if row.get('Total Hours'))
    total_overtime_hours = sum(float(row.get('Over Time Hours', 0) or 0) for row in full_month_data if row.get('Over Time Hours'))
    total_regular_hours = sum(float(row.get('Regular Hours', 0) or 0) for row in full_month_data if row.get('Regular Hours'))

    return {
        'nama': name.upper(),
        'nrp': info.get('nrp') or info.get('employee_id', 'N/A'),
        'employee_id': info.get('employee_id', 'N/A'),
        'posisi': info.get('position', 'N/A'),
        'start_date': f"Thu, {month_name} 01, {year}",
        'end_date': f"Sat, {month_name} {end_date.day:02d}, {year}",
        'total_break_hours': f"{total_break_hours:.2f}",
        'total_hours': f"{total_hours:.2f}",
        'total_overtime_hours': f"{total_overtime_hours:.2f}",
        'total_regular_hours': f"{total_regular_hours:.2f}",
        'timesheet_rows': full_month_data,
        'periode': f"{month_name} {year}"
    }

async def _render_single_timesheet_html(employee_data: dict, request: Request):
    """Render single employee timesheet using FastAPI templates (same approach as evidence)"""
    try:
        template = templates.get_template('timesheet_report_template.html')
        html_content = template.render({
            "request": request,
            "reports": [employee_data],
            "periode": employee_data['periode'],
            "logo_url": '/static/img/logo_pama.png'
        })

        return html_content
    except Exception as e:
        return f"<div>Error rendering timesheet for {employee_data.get('nama', 'Unknown')}</div>"

async def _get_iot_tasklist_html_sections(month: int, request: Request):
    """Get IoT tasklist HTML sections"""
    html_sections = []
    pages = ["problem", "aktivitas", "respon"]

    for page in pages:
        try:
            iot_html = await _call_iot_endpoint(page, month, request)
            if iot_html:
                title = f'Detail {page.replace("_", " ").title()}'
                if page == "problem":
                    title = "Detail Problem Pihak Kedua"
                elif page == "aktivitas":
                    title = "Detail Aktivitas Pihak Kedua"
                elif page == "respon":
                    title = "Detail Respon Resolution Time"
                
                body_content = re.search(r'<body[^>]*>(.*?)</body>', iot_html, re.DOTALL)
                if body_content:
                    isolated_content = f'<div class="iot-tasklist-section">{body_content.group(1)}</div>'
                else:
                    isolated_content = f'<div class="iot-tasklist-section">{iot_html}</div>'

                html_sections.append({
                    'type': f'iot_{page}',
                    'title': title,
                    'content': isolated_content
                })
        except Exception as e:
            pass

    return html_sections

async def _get_developer_tasklist_html_sections(month: int, request: Request):
    """Get Developer tasklist HTML sections"""
    html_sections = []
    pages = ["pelaksanaan", "kualitas", "rilis", "support"]

    for page in pages:
        try:
            dev_html = await _call_developer_endpoint(page, month, request)
            if dev_html:
                title = f'Developer {page.title()}'
                if page == "pelaksanaan":
                    title = "Pelaksanaan Pekerjaan"
                elif page == "kualitas":
                    title = "Detail Aktivitas Kualitas Kode"
                elif page == "rilis":
                    title = "Detail Aktivitas Waktu Rilis"
                elif page == "support":
                    title = "Detail Aktivitas Dukungan Support"

                body_content = re.search(r'<body[^>]*>(.*?)</body>', dev_html, re.DOTALL)
                if body_content:
                    isolated_content = f'<div class="dev-tasklist-section">{body_content.group(1)}</div>'
                else:
                    isolated_content = f'<div class="dev-tasklist-section">{dev_html}</div>'

                html_sections.append({
                    'type': f'dev_{page}',
                    'title': title,
                    'content': isolated_content
                })
        except Exception as e:
            pass

    return html_sections

async def _call_iot_endpoint(page: str, month: int, request: Request):
    """Call IoT endpoint logic and return HTML content"""
    indonesian_months = {
        1: 'Januari', 2: 'Februari', 3: 'Maret', 4: 'April',
        5: 'Mei', 6: 'Juni', 7: 'Juli', 8: 'Agustus',
        9: 'September', 10: 'Oktober', 11: 'November', 12: 'Desember'
    }
    month_name = indonesian_months[month]

    table_id = config.NOCODB_TABLES.get("tasklist_iot")
    if not table_id:
        return ""

    nocodb = ClsNocoDBProcessor(config.APP_BASE_ID, table_id)
    where_clause = f"(Month,eq,{month_name})~and(Status,eq,Closed)"
    records = nocodb.get_records(limit=2000, where=where_clause).get('list', [])

    response = None
    if page == "problem":
        response = await _generate_iot_problem_page(request, records, month_name)
    elif page == "aktivitas":
        response = await _generate_iot_aktivitas_page(request, records, month_name)
    elif page == "respon":
        response = await _generate_iot_respon_page(request, records, month_name)

    if response and hasattr(response, 'template') and hasattr(response, 'context'):
        return response.template.render(response.context)
    
    return ""

async def _call_developer_endpoint(page: str, month: int, request: Request):
    """Call Developer endpoint logic and return HTML content"""
    indonesian_months = {
        1: 'Januari', 2: 'Februari', 3: 'Maret', 4: 'April',
        5: 'Mei', 6: 'Juni', 7: 'Juli', 8: 'Agustus',
        9: 'September', 10: 'Oktober', 11: 'November', 12: 'Desember'
    }
    month_name = indonesian_months[month]

    response = None
    if page == "pelaksanaan":
        response = await _generate_dev_pelaksanaan_page(request, month_name)
    else:
        table_id = config.NOCODB_TABLES.get("tasklist")
        if not table_id:
            return ""

        nocodb = ClsNocoDBProcessor(config.APP_BASE_ID, table_id)
        kategori_mapping = {
            "kualitas": "Detail Aktivitas Kualitas Kode",
            "rilis": "Detail Aktivitas Waktu Rilis",
            "support": "Detail Aktivitas Dukungan Support"
        }
        kategori_name = kategori_mapping.get(page)
        if not kategori_name:
            return ""

        where_clause = f"(Month,eq,{month_name})~and(Kategori,eq,{kategori_name})~and(Status,eq,Closed)"
        records = nocodb.get_records(limit=2000, where=where_clause).get('list', [])

        if page == "kualitas":
            response = await _generate_dev_kualitas_data(request, records, month_name)
        elif page == "rilis":
            response = await _generate_dev_rilis_data(request, records, month_name)
        elif page == "support":
            response = await _generate_dev_support_data(request, records, month_name)
    
    if response and hasattr(response, 'template') and hasattr(response, 'context'):
        return response.template.render(response.context)
        
    return ""

async def _get_evidence_html_section(evidence_type: str, month: int, request: Request):
    """Get evidence HTML section by reusing logic from generate_evidence_report."""
    indonesian_months = {
        1: 'Januari', 2: 'Februari', 3: 'Maret', 4: 'April',
        5: 'Mei', 6: 'Juni', 7: 'Juli', 8: 'Agustus',
        9: 'September', 10: 'Oktober', 11: 'November', 12: 'Desember'
    }
    month_name = indonesian_months[month]

    table_key = "tasklist_iot" if evidence_type == "iotoperations" else "tasklist"
    table_id = config.NOCODB_TABLES.get(table_key)
    if not table_id:
        return {'type': 'evidence', 'title': 'Evidence Aktivitas', 'content': '<div>Evidence table not configured.</div>'}

    nocodb = ClsNocoDBProcessor(config.APP_BASE_ID, table_id)
    where_clause = f"(Month,eq,{month_name})~and(Evidence Task,notnull)"
    records = nocodb.get_records(limit=2000, where=where_clause).get('list', [])
    
    evidence_data = []
    if records:
        for i, record in enumerate(records, 1):
            task_list = record.get('Task List', 'No Task Description')
            evidence_task = record.get('Evidence Task', [])
            image_urls = []
            if evidence_task and isinstance(evidence_task, list):
                for attachment in evidence_task:
                    if isinstance(attachment, dict):
                        signed_path = attachment.get('signedPath')
                        if signed_path:
                            full_url = f"{config.NOCODB_BASE_URL.rstrip('/')}/{signed_path}"
                            image_urls.append(full_url)
            if not image_urls:
                continue
            evidence_data.append({
                "number": i,
                "title": task_list,
                "image_path": image_urls[0],
                "description": task_list
            })

    template = templates.get_template('evidence/evidence_aktivitas.html')
    html_content = template.render({
        "request": request,
        "evidence_data": evidence_data,
        "type": evidence_type,
        "month": month_name
    })

    body_content = re.search(r'<body[^>]*>(.*?)</body>', html_content, re.DOTALL)
    if body_content:
        isolated_content = f'<div class="evidence-section">{body_content.group(1)}</div>'
    else:
        isolated_content = f'<div class="evidence-section">{html_content}</div>'

    return {
        'type': 'evidence',
        'title': 'Evidence Aktivitas',
        'content': isolated_content
    }

async def _get_attendance_html_section(month: int, year: int, report_type: str, request: Request):
    """Get attendance HTML section filtered by role"""
    employee_table = config.NOCODB_TABLES.get("employee_data")
    attendance_table = config.NOCODB_TABLES.get("attendance")

    nocodb_employee = ClsNocoDBProcessor(config.APP_BASE_ID, employee_table)
    nocodb_attendance = ClsNocoDBProcessor(config.APP_BASE_ID, attendance_table)

    if report_type == "iotoperation":
        role_filter = "IoT Operations"
    elif report_type == "developer":
        role_filter = "Developer"
    else:
        role_filter = None

    employee_mapping = nocodb_employee.get_all_employees(role_filter=role_filter)
    start_date, end_date = get_dynamic_month_dates(year, month)

    reports_data = []
    for name, info in employee_mapping.items():
        display_nrp = info.get('nrp') or info.get('employee_id')
        if not display_nrp:
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
            continue

        attendance_data.sort(key=lambda x: datetime.strptime(x['tanggal_kehadiran'], '%d/%m/%Y'))

        reports_data.append({
            'nrp': display_nrp,
            'nama': name.upper(),
            'attendance_rows': attendance_data
        })

    if reports_data:
        try:
            indonesian_months = {
                1: 'Januari', 2: 'Februari', 3: 'Maret', 4: 'April',
                5: 'Mei', 6: 'Juni', 7: 'Juli', 8: 'Agustus',
                9: 'September', 10: 'Oktober', 11: 'November', 12: 'Desember'
            }

            template = templates.get_template('attendance_report_template.html')
            html_content = template.render({
                "request": request,
                "periode": f"{start_date.strftime('%d %B %Y')} - {end_date.strftime('%d %B %Y')}",
                "dicetak": datetime.now().strftime('%d %B %Y %H:%M:%S'),
                "reports": reports_data,
                "logo_url": '/static/img/logo_pama.png'
            })

            clean_content = html_content

            return {
                'type': 'attendance',
                'title': 'PAMA Attendance Report',
                'content': clean_content
            }
        except Exception as e:
            pass

    return {
        'type': 'attendance',
        'title': 'PAMA Attendance Report',
        'content': f"<div>No attendance data found for {report_type} employees in month {month}</div>"
    }

@app.post("/export/pdf")
async def export_to_pdf_weasy(
    html_content: str = Form(...),
    type: str = Form(...),
    month: int = Form(...),
    year: int = Form(...),
    berita_acara: UploadFile = File(None)
):
    """
    Simple PDF export - HTML sudah matang dengan page breaks yang benar
    """
    try:
        try:
            from weasyprint import HTML, CSS
            from weasyprint.text.fonts import FontConfiguration
        except ImportError:
            raise HTTPException(500, "WeasyPrint not installed. Run: pip install weasyprint")

        import tempfile
        import uuid
        import io

        temp_dir = Path(tempfile.mkdtemp())
        export_id = str(uuid.uuid4())[:8]

        clean_html = html_content
        clean_html = re.sub(r'<button[^>]*export-btn[^>]*>.*?</button>', '', clean_html, flags=re.DOTALL)
        clean_html = re.sub(r'<div[^>]*id="exportModal"[^>]*>.*?</div>', '', clean_html, flags=re.DOTALL)
        clean_html = re.sub(r'<script\b[^<]*(?:(?!<\/script>)<[^<]*)*<\/script>', '', clean_html, flags=re.DOTALL)
        clean_html = re.sub(r'onclick="[^"]*"', '', clean_html)
        clean_html = clean_html.replace('src="/static/', f'src="http://localhost:8000/static/')

        pdf_css = """
        <style>
        @media print {
            body { margin: 0; background: white; }
            .page-break { page-break-before: always !important; }
            .timesheet-employee { page-break-before: always !important; }
            .timesheet-employee:first-child { page-break-before: auto !important; }
            .timesheet-employee {
                page-break-inside: avoid !important;
            }
            .export-btn, #exportModal { display: none !important; }
            * { -webkit-print-color-adjust: exact !important; print-color-adjust: exact !important; }
        }
        </style>
        """

        clean_html = clean_html.replace('</head>', pdf_css + '</head>')

        html_file = temp_dir / f"report_{export_id}.html"
        with open(html_file, 'w', encoding='utf-8') as f:
            f.write(clean_html)

        font_config = FontConfiguration()
        html_doc = HTML(filename=str(html_file))
        pdf_bytes = html_doc.write_pdf(font_config=font_config, optimize_images=True)

        if berita_acara and berita_acara.filename:
            try:
                from PyPDF2 import PdfReader, PdfWriter

                berita_content = await berita_acara.read()

                if berita_acara.filename.lower().endswith(('.jpg', '.jpeg', '.png')):
                    try:
                        import img2pdf
                        berita_pdf_bytes = img2pdf.convert(berita_content)
                    except ImportError:
                        raise HTTPException(500, "img2pdf required for image conversion")
                else:
                    berita_pdf_bytes = berita_content

                berita_reader = PdfReader(io.BytesIO(berita_pdf_bytes))
                report_reader = PdfReader(io.BytesIO(pdf_bytes))

                writer = PdfWriter()

                for page in berita_reader.pages:
                    writer.add_page(page)

                for page in report_reader.pages:
                    writer.add_page(page)

                merged_pdf = io.BytesIO()
                writer.write(merged_pdf)
                pdf_bytes = merged_pdf.getvalue()

            except Exception as e:
                pass

        try:
            import shutil
            shutil.rmtree(temp_dir)
        except:
            pass

        filename = f"{type.title()}_Report_{month}_{year}.pdf"

        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )

    except Exception as e:
        raise HTTPException(500, f"PDF export failed: {str(e)}")

@app.get("/admin/attendance-celerates", response_class=HTMLResponse)
async def attendance_celerates_dashboard_get(request: Request):
    """Attendance Celerates Dashboard - Initial Load"""
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/admin/login", status_code=302)

    from datetime import datetime, timedelta

    # Get employee list for filter dropdown
    employee_table = config.NOCODB_TABLES.get("employee_data")
    nocodb_employee = ClsNocoDBProcessor(config.APP_BASE_ID, employee_table)
    employee_mapping = nocodb_employee.get_all_employees()
    employee_list = list(employee_mapping.keys())

    # Create employee_roles mapping for template
    employee_roles = {emp_name: emp_info.get('role', '') for emp_name, emp_info in employee_mapping.items()}

    return templates.TemplateResponse('attendance_celerates.html', {
        "request": request,
        "user": user,
        "attendance_data": [],  # Empty on initial load
        "employee_list": employee_list,
        "employee_roles": employee_roles,
        "start_date": None,
        "end_date": None,
        "selected_employees": [],
        "datetime": datetime
    })

@app.post("/admin/attendance-celerates", response_class=HTMLResponse)
async def attendance_celerates_dashboard_post(
    request: Request,
    start_date: str = Form(...),
    end_date: str = Form(...),
    employee: list = Form(default=[])
):
    """Attendance Celerates Dashboard - like GSheet format with date range"""
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/admin/login", status_code=302)

    from datetime import datetime, date

    # Get employee list for filter dropdown
    employee_table = config.NOCODB_TABLES.get("employee_data")
    nocodb_employee = ClsNocoDBProcessor(config.APP_BASE_ID, employee_table)
    employee_mapping = nocodb_employee.get_all_employees()
    employee_list = list(employee_mapping.keys())

    attendance_data = []

    # Only load data if date parameters are provided (when filter is applied)
    if start_date and end_date:
        print(f"Loading attendance data from {start_date} to {end_date}")
        # Parse date strings
        start_date_obj = datetime.strptime(start_date, '%Y-%m-%d').date()
        end_date_obj = datetime.strptime(end_date, '%Y-%m-%d').date()

        # Get attendance data
        attendance_table = config.NOCODB_TABLES.get("attendance")
        nocodb_attendance = ClsNocoDBProcessor(config.APP_BASE_ID, attendance_table)

        # Filter employees if specific employees selected
        target_employees = employee if employee else employee_list
        print(f"Target employees count: {len(target_employees)}")

        for emp_name in target_employees:
            if emp_name not in employee_mapping:
                continue

            emp_info = employee_mapping[emp_name]
            where_clause = f"(Name,like,%{emp_name.strip().title()}%)"
            records = nocodb_attendance.get_records(limit=2000, where=where_clause).get('list', [])

            # Create a lookup dict for attendance records by date
            attendance_by_date = {}
            for rec in records:
                rec_date_str = rec.get('Date')
                if rec_date_str:
                    attendance_by_date[rec_date_str] = rec

            # Generate all dates in range
            current_date = start_date_obj
            while current_date <= end_date_obj:
                date_str = current_date.strftime('%Y-%m-%d')
                rec = attendance_by_date.get(date_str)  # Get attendance record for this date, or None

                # Use current_date instead of rec_date since we're iterating through date range
                rec_date = current_date

                def get_time(val):
                    if not val: return ''
                    actual_val = val[0] if isinstance(val, list) else val
                    if actual_val is None or str(actual_val).strip() == '': return ''
                    time_str = str(actual_val)
                    return ':'.join(time_str.split(' ')[-1].split('+')[0].split(':')[:2])

                # Handle case when no attendance record exists (Day Off)
                if rec is None:
                    # No attendance record - treat as Day Off
                    last_modified = ''
                    is_manual_edit = False
                    start_time = ''
                    end_time = ''
                    holiday = ''
                    attendance_code = ''
                    keterangan = ''
                    overtime_fields = {'overtime_check_in': '', 'overtime_check_out': '',
                                     'overtime_before': '', 'overtime_after': ''}
                    timeoff_fields = {'timeoff_check_out': '', 'timeoff_break_before': '', 'timeoff_break_after': ''}
                else:
                    # Attendance record exists - process normally
                    last_modified = rec.get('Last Modified', '')
                    is_manual_edit = False
                    if last_modified and '@system.com' not in str(last_modified):
                        is_manual_edit = True

                    start_time = get_time(rec.get('Start Time'))
                    end_time = get_time(rec.get('End Time'))
                    holiday = rec.get('Holiday', '')
                    attendance_code = rec.get('Attendance_Code', '')
                    keterangan = rec.get('Remarks', '')
                    overtime_fields = {
                        'overtime_check_in': get_time(rec.get('Overtime_Check_In')),
                        'overtime_check_out': get_time(rec.get('Overtime_Check_Out')),
                        'overtime_before': get_time(rec.get('Overtime_Before')),
                        'overtime_after': get_time(rec.get('Overtime_After'))
                    }
                    timeoff_fields = {
                        'timeoff_check_out': get_time(rec.get('TimeOff_Check_Out')),
                        'timeoff_break_before': get_time(rec.get('TimeOff_Break_Before')),
                        'timeoff_break_after': get_time(rec.get('TimeOff_Break_After'))
                    }

                # Check if employee is IoT Operations (applies to both cases)
                is_iot_operations = emp_info.get('role') == 'IoT Operations'

                # Get schedule data from schedule_shifting table for IoT Operations
                schedule_in_time = '7:30'  # Default for Developer
                schedule_out_time = '16:30'  # Default for Developer
                shift_code = 'N'  # Default

                if is_iot_operations:
                    # Get schedule from schedule_shifting table
                    schedule_table = config.NOCODB_TABLES.get("schedule_shifting")
                    nocodb_schedule = ClsNocoDBProcessor(config.APP_BASE_ID, schedule_table)

                    where_schedule = f"(Employee Name,like,{emp_name.strip().title()})~and(Date,eq,{rec_date.strftime('%Y-%m-%d')})"
                    schedule_response = nocodb_schedule.get_records(limit=5, where=where_schedule)
                    schedule_records = schedule_response.get('list', []) if schedule_response else []

                    if schedule_records:
                        schedule = schedule_records[0]
                        shift_data = schedule.get('Shift Data', 0)

                        if shift_data == 0:
                            # No shift assigned = Day Off
                            shift_code = 'Day Off'
                            schedule_in_time = ''
                            schedule_out_time = ''
                        else:
                            # Has shift assigned
                            codes = schedule.get('Code', [])
                            start_times = schedule.get('Start Time', [])
                            end_times = schedule.get('End Time', [])

                            if codes and start_times and end_times:
                                # Use the Code field directly
                                shift_code = codes[0] if isinstance(codes, list) else codes
                                start_time_raw = start_times[0] if isinstance(start_times, list) else start_times
                                end_time_raw = end_times[0] if isinstance(end_times, list) else end_times

                                # Format schedule times
                                schedule_in_time = ':'.join(str(start_time_raw).split(':')[:2]) if start_time_raw else '7:30'
                                schedule_out_time = ':'.join(str(end_time_raw).split(':')[:2]) if end_time_raw else '16:30'
                            else:
                                shift_code = 'Day Off'
                                schedule_in_time = ''
                                schedule_out_time = ''
                    else:
                        # No schedule record found = Day Off
                        shift_code = 'Day Off'
                        schedule_in_time = ''
                        schedule_out_time = ''
                else:
                    # For Developer role - use existing logic
                    has_time = start_time or end_time
                    is_holiday = str(holiday).upper() == 'H'
                    shift_code = 'Day Off' if is_holiday or not has_time else 'N'

                    if is_holiday or not has_time:
                        schedule_in_time = ''
                        schedule_out_time = ''

                attendance_data.append({
                    'employee_id': emp_info.get('employee_id', emp_info.get('nrp', '')),
                    'full_name': emp_name,
                    'date': rec_date,
                    'shift': shift_code,
                    'shift_code': '',  # Not available in basic attendance table
                    'shift_label': '',  # Not available in basic attendance table
                    'schedule_in': schedule_in_time,
                    'schedule_out': schedule_out_time,
                    'attendance_code': attendance_code,
                    'check_in': start_time,
                    'check_out': end_time,
                    'keterangan': keterangan,
                    'overtime_check_in': overtime_fields['overtime_check_in'],
                    'overtime_check_out': overtime_fields['overtime_check_out'],
                    'overtime_before': overtime_fields['overtime_before'],
                    'overtime_after': overtime_fields['overtime_after'],
                    'timeoff_check_out': timeoff_fields['timeoff_check_out'],
                    'timeoff_break_before': timeoff_fields['timeoff_break_before'],
                    'timeoff_break_after': timeoff_fields['timeoff_break_after'],
                    'holiday_code': holiday,
                    'is_manual_edit': is_manual_edit
                })

                # Move to next date
                current_date += timedelta(days=1)

    # Sort by employee name and date
    if attendance_data:
        attendance_data.sort(key=lambda x: (x['full_name'], x['date']))

    # Create employee_roles mapping for template
    employee_roles = {emp_name: emp_info.get('role', '') for emp_name, emp_info in employee_mapping.items()}

    return templates.TemplateResponse('attendance_celerates.html', {
        "request": request,
        "user": user,
        "attendance_data": attendance_data,
        "employee_list": employee_list,
        "employee_roles": employee_roles,
        "start_date": datetime.strptime(start_date, '%Y-%m-%d').date() if start_date else None,
        "end_date": datetime.strptime(end_date, '%Y-%m-%d').date() if end_date else None,
        "selected_employees": employee if employee else [],
        "datetime": datetime
    })

@app.post("/admin/attendance-celerates/export-csv")
async def export_attendance_celerates_csv(
    request: Request,
    start_date: str = Form(...),
    end_date: str = Form(...),
    employee: str = Form(""),
    role_filter: str = Form("")
):
    """Export Attendance Celerates data as CSV with custom date range"""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")

    from datetime import datetime
    import io
    import csv

    # Parse date strings
    start_date_obj = datetime.strptime(start_date, '%Y-%m-%d').date()
    end_date_obj = datetime.strptime(end_date, '%Y-%m-%d').date()

    # Get same data as dashboard
    employee_table = config.NOCODB_TABLES.get("employee_data")
    nocodb_employee = ClsNocoDBProcessor(config.APP_BASE_ID, employee_table)
    employee_mapping = nocodb_employee.get_all_employees()
    employee_list = list(employee_mapping.keys())

    attendance_table = config.NOCODB_TABLES.get("attendance")
    nocodb_attendance = ClsNocoDBProcessor(config.APP_BASE_ID, attendance_table)

    csv_data = []

    # Apply role filtering to employee list
    filtered_employee_list = employee_list
    if role_filter and role_filter != "all":
        filtered_employee_list = [emp for emp in employee_list if employee_mapping[emp].get('role') == role_filter]

    target_employees = [employee] if employee else filtered_employee_list

    for emp_name in target_employees:
        if emp_name not in employee_mapping:
            continue

        emp_info = employee_mapping[emp_name]
        where_clause = f"(Name,like,%{emp_name.strip().title()}%)"
        records = nocodb_attendance.get_records(limit=2000, where=where_clause).get('list', [])

        for rec in records:
            rec_date_str = rec.get('Date')
            if not rec_date_str:
                continue

            rec_date = datetime.strptime(rec_date_str, '%Y-%m-%d').date()
            if start_date_obj <= rec_date <= end_date_obj:

                def get_time(val):
                    if not val: return ''
                    actual_val = val[0] if isinstance(val, list) else val
                    if actual_val is None or str(actual_val).strip() == '': return ''
                    time_str = str(actual_val)
                    return ':'.join(time_str.split(' ')[-1].split('+')[0].split(':')[:2])

                # Check for manual edit
                last_modified = rec.get('Last Modified', '')
                is_manual_edit = 'Yes' if last_modified and '@system.com' not in str(last_modified) else 'No'

                # Determine shift code and schedule times based on business logic
                start_time = get_time(rec.get('Start Time'))
                end_time = get_time(rec.get('End Time'))
                holiday = rec.get('Holiday', '')

                # Check if employee is IoT Operations
                is_iot_operations = emp_info.get('role') == 'IoT Operations'

                # Get schedule data from schedule_shifting table for IoT Operations
                schedule_in_time = '7:30'  # Default for Developer
                schedule_out_time = '16:30'  # Default for Developer
                shift_code = 'N'  # Default

                if is_iot_operations:
                    # Get schedule from schedule_shifting table
                    schedule_table = config.NOCODB_TABLES.get("schedule_shifting")
                    nocodb_schedule = ClsNocoDBProcessor(config.APP_BASE_ID, schedule_table)

                    where_schedule = f"(Employee Name,like,{emp_name.strip().title()})~and(Date,eq,{rec_date.strftime('%Y-%m-%d')})"
                    schedule_response = nocodb_schedule.get_records(limit=5, where=where_schedule)
                    schedule_records = schedule_response.get('list', []) if schedule_response else []

                    if schedule_records:
                        schedule = schedule_records[0]
                        shift_data = schedule.get('Shift Data', 0)

                        if shift_data == 0:
                            shift_code = 'Day Off'
                            schedule_in_time = ''
                            schedule_out_time = ''
                        else:
                            codes = schedule.get('Code', [])
                            start_times = schedule.get('Start Time', [])
                            end_times = schedule.get('End Time', [])

                            if codes and start_times and end_times:
                                shift_code = codes[0] if isinstance(codes, list) else codes
                                start_time_raw = start_times[0] if isinstance(start_times, list) else start_times
                                end_time_raw = end_times[0] if isinstance(end_times, list) else end_times

                                schedule_in_time = ':'.join(str(start_time_raw).split(':')[:2]) if start_time_raw else '7:30'
                                schedule_out_time = ':'.join(str(end_time_raw).split(':')[:2]) if end_time_raw else '16:30'
                            else:
                                shift_code = 'Day Off'
                                schedule_in_time = ''
                                schedule_out_time = ''
                    else:
                        shift_code = 'Day Off'
                        schedule_in_time = ''
                        schedule_out_time = ''
                else:
                    # For Developer role - use existing logic
                    has_time = start_time or end_time
                    is_holiday = str(holiday).upper() == 'H'
                    shift_code = 'Day Off' if is_holiday or not has_time else 'N'

                    if is_holiday or not has_time:
                        schedule_in_time = ''
                        schedule_out_time = ''

                csv_data.append({
                    'Employee ID': emp_info.get('employee_id', emp_info.get('nrp', '')),
                    'Full Name': emp_name,
                    'Date': rec_date.strftime('%Y-%m-%d'),
                    'Shift': shift_code,
                    'Shift Code': '',  # Not available in basic attendance table
                    'Shift Label': '',  # Not available in basic attendance table
                    'Schedule In': schedule_in_time,
                    'Schedule Out': schedule_out_time,
                    'Attendance Code': rec.get('Attendance_Code', ''),
                    'Check In': get_time(rec.get('Start Time')),
                    'Check Out': get_time(rec.get('End Time')),
                    'Keterangan': rec.get('Remarks', ''),
                    'Overtime Check In': get_time(rec.get('Overtime_Check_In')),
                    'Overtime Check Out': get_time(rec.get('Overtime_Check_Out')),
                    'Overtime Before': get_time(rec.get('Overtime_Before')),
                    'Overtime After': get_time(rec.get('Overtime_After')),
                    'TimeOff Check Out': get_time(rec.get('TimeOff_Check_Out')),
                    'TimeOff Break Before': get_time(rec.get('TimeOff_Break_Before')),
                    'TimeOff Break After': get_time(rec.get('TimeOff_Break_After')),
                    'Holiday Code': rec.get('Holiday', '')
                })

    # Sort by employee name and date
    csv_data.sort(key=lambda x: (x['Full Name'], x['Date']))

    # Create CSV
    if not csv_data:
        raise HTTPException(status_code=404, detail="No attendance data found for the selected period")

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=csv_data[0].keys())
    writer.writeheader()
    writer.writerows(csv_data)

    filename = f"Attendance_Celerates_{start_date}_to_{end_date}.csv"
    if employee:
        filename = f"Attendance_Celerates_{employee}_{start_date}_to_{end_date}.csv"
    elif role_filter and role_filter != "all":
        filename = f"Attendance_Celerates_{role_filter}_{start_date}_to_{end_date}.csv"

    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )

@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {"status": "healthy", "service": "digital-bast-admin"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=False)
