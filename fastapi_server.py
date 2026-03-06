import os
import calendar
import hashlib
import secrets
import sqlite3
import json
import uuid
from datetime import datetime, timedelta
from urllib.parse import urlparse
from typing import Optional, Dict
from pathlib import Path
import re
import psycopg2
import bcrypt

from fastapi import FastAPI, HTTPException, Depends, Request, Form, File, UploadFile, status, Query, BackgroundTasks
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

# SQLite database for generation plans
DB_PATH = "generation_plans.db"

# Global cache for Fauzan's timesheet data
fauzan_timesheet_cache = None

def init_db():
    """Initialize SQLite database for generation plans"""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS generation_plans (
                id TEXT PRIMARY KEY,
                plan_data TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.commit()
        conn.close()
    except Exception as e:
        raise

def save_generation_plan(plan_data: dict) -> str:
    """Save generation plan to database and return plan_id"""
    try:
        plan_id = str(uuid.uuid4())
        plan_data['plan_id'] = plan_id

        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            'INSERT INTO generation_plans (id, plan_data) VALUES (?, ?)',
            (plan_id, json.dumps(plan_data))
        )
        conn.commit()
        conn.close()
        return plan_id
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save generation plan: {str(e)}")

def get_generation_plan(plan_id: str) -> Optional[dict]:
    """Get generation plan from database"""
    if not plan_id:
        return None

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT plan_data FROM generation_plans WHERE id = ?', (plan_id,))
    row = cursor.fetchone()
    conn.close()

    if row:
        return json.loads(row[0])
    return None

def update_generation_plan(plan_id: str, plan_data: dict):
    """Update generation plan in database"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        'UPDATE generation_plans SET plan_data = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?',
        (json.dumps(plan_data), plan_id)
    )
    conn.commit()
    conn.close()

# Initialize database on startup
init_db()

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
            sslmode='prefer'
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

    # Get start and end times for checking null values
    start_time = get_time('Start Time')
    end_time = get_time('End Time')

    # If both Start Time and End Time are null/empty, set Work Description to empty
    work_description = ''
    if not is_holiday:
        if start_time or end_time:  # If at least one time exists
            work_description = all_work_descs
        # else: work_description remains empty when both times are null

    return {
        'Date': datetime.strptime(record.get('Date',''), '%Y-%m-%d').strftime('%a, %b %-d, %Y'),
        'Activity': activity,
        'Project Name': '' if is_holiday else get_field('Project Name'),
        'Internal Project ID': '' if is_holiday else get_field('Internal Project ID'),
        'Customer Name/ID': '' if is_holiday else get_field('Customer Name/ID'),
        'PO/Contract No': '' if is_holiday else get_field('PO/Contract No'),
        'Work Description': work_description,
        'Start Time': '' if is_holiday else start_time,
        'End Time': '' if is_holiday else end_time,
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

@app.get("/admin/auth/logout")
async def admin_auth_logout(request: Request):
    """Handle admin logout"""
    logout_user(request)
    return RedirectResponse(url="/admin/login", status_code=302)

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

    # Skip employee data fetch on initial page load to improve performance
    # Employee data will be fetched only when needed for actual reports
    employee_list = []
    employee_roles = {}

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

    # Use Unique_Key filtering for month-specific records
    current_year = datetime.now().year
    year_month_pattern = f"{current_year}-{month:02d}-"
    where_clause = f"(Unique_Key,like,{year_month_pattern}%)~and(Kategori,eq,{kategori_name})~and(Status,eq,Closed)"
    response = nocodb.get_records(limit=2000, where=where_clause)
    records = response.get('list', []) if response else []

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
        # pencapaian = record.get('Pencapaian', 0)
        pencapaian = 100

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

    # Render template as string instead of TemplateResponse for progressive generation
    template = templates.get_template('tasklistdeveloper/detail_aktivitas_kualitas_kode.html')
    return template.render({
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

    # Render template as string instead of TemplateResponse for progressive generation
    template = templates.get_template('tasklistdeveloper/detail_aktivitas_waktu_rilis.html')
    return template.render({
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

    # Render template as string instead of TemplateResponse for progressive generation
    template = templates.get_template('tasklistdeveloper/detail_aktivitas_dukungan_support.html')
    return template.render({
        "request": request,
        "dukungan_support_data": support_data,
        "summary_pencapaian": str(avg_pencapaian),
        "month": month_name
    })

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

    # Use Unique_Key filtering for month-specific records
    current_year = datetime.now().year
    year_month_pattern = f"{current_year}-{month:02d}-"
    where_clause = f"(Unique_Key,like,{year_month_pattern}%)~and(Status,eq,Closed)"
    response = nocodb.get_records(limit=2000, where=where_clause)
    records = response.get('list', []) if response else []

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

    # Render template as string instead of TemplateResponse for progressive generation
    template = templates.get_template('tasklistiotoperation/detail_problem_pihak_kedua.html')
    return template.render({
        "request": request,
        "problem_data": problem_data,
        "month": month_name
    })

async def _generate_iot_aktivitas_page(request: Request, records: list, month_name: str):
    """Generate activities page from Fauzan's Tasklist Developer data"""

    from datetime import datetime
    from src.classes.ClsPostgreSQLProcessor import ClsPostgreSQLProcessor

    # Convert month name to month number for Unique_Key pattern
    month_mapping = {
        'Januari': 1, 'Februari': 2, 'Maret': 3, 'April': 4,
        'Mei': 5, 'Juni': 6, 'Juli': 7, 'Agustus': 8,
        'September': 9, 'Oktober': 10, 'November': 11, 'Desember': 12
    }

    month = month_mapping.get(month_name, 1)
    current_year = datetime.now().year
    year_month_pattern = f"{current_year}-{month:02d}-"

    # Get tasklist developer data for Fauzan
    tasklist_table = config.NOCODB_TABLES.get("tasklist")

    # Get Fauzan's tasks directly from database using Unique_Key pattern
    import psycopg2

    conn = psycopg2.connect(config.DB_URL)
    cursor = conn.cursor()

    cursor.execute('''
    SELECT "Task_List", "Start_Date", "End_Date", "Requestor"
    FROM "pc38r6u1npuq0ul"."Tasklist Developer"
    WHERE "Unique_Key" LIKE %s
    AND "Status" = 'Closed'
    ORDER BY "Start_Date"
    ''', (f'{year_month_pattern}%_100',))

    task_rows = cursor.fetchall()
    cursor.close()
    conn.close()

    # Convert to dictionary format for easier processing
    fauzan_tasks = []
    for row in task_rows:
        fauzan_tasks.append({
            'Task_List': row[0],
            'Start_Date': row[1],
            'End_Date': row[2],
            'Requestor': row[3]
        })

    print(f"DEBUG: Found {len(fauzan_tasks)} completed tasks for Fauzan in January 2026")

    # Process Fauzan's tasklist data
    aktivitas_data = []

    if fauzan_tasks:
        print(f"DEBUG: Sample task record: {fauzan_tasks[0] if fauzan_tasks else 'None'}")

        for i, task in enumerate(fauzan_tasks, 1):
            # Get task fields (using correct column names)
            task_list = task.get('Task_List', '')
            start_date = task.get('Start_Date', '')
            end_date = task.get('End_Date', '')
            requestor = task.get('Requestor', 'Bagas Eko Prasetyo')  # Default PIC PAMA

            # Skip if no task description
            if not task_list or task_list.strip() in ['', '-', 'N/A']:
                continue

            # Format dates
            formatted_start = start_date
            formatted_end = end_date
            if start_date:
                try:
                    if isinstance(start_date, str):
                        parsed_date = datetime.strptime(start_date, '%Y-%m-%d')
                        formatted_start = parsed_date.strftime('%d %B %Y')
                except:
                    pass

            if end_date:
                try:
                    if isinstance(end_date, str):
                        parsed_date = datetime.strptime(end_date, '%Y-%m-%d')
                        formatted_end = parsed_date.strftime('%d %B %Y')
                except:
                    pass

            aktivitas_data.append({
                "no": i,
                "detail_aktivitas": task_list,
                "tanggal_request": formatted_start,
                "tanggal_penyelesaian": formatted_end,
                "lead_time": "8 Jam",  # Hardcoded as requested
                "requestor_pic": requestor,
                "engineer_manage": "Muhammad Fauzan Acyuto"
            })

        print(f"DEBUG: Generated {len(aktivitas_data)} activities from Fauzan's tasklist")
    else:
        print("DEBUG: No completed tasks found for Fauzan in February 2026")
        aktivitas_data = []

    # Render template
    template = templates.get_template('tasklistiotoperation/detail_aktivitas_pihak_kedua.html')
    return template.render({
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

    # Render template as string instead of TemplateResponse for progressive generation
    template = templates.get_template('tasklistiotoperation/detail_respon_resolution_time.html')
    return template.render({
        "request": request,
        "respon_data": respon_data,
        "summary_percentage": "97.5",
        "month": month_name
    })

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

    # Use Unique_Key filtering for month-specific records with evidence
    current_year = datetime.now().year
    year_month_pattern = f"{current_year}-{month:02d}-"
    where_clause = f"(Unique_Key,like,{year_month_pattern}%)~and(Evidence Task,notnull)"
    response = nocodb.get_records(limit=2000, where=where_clause)
    records = response.get('list', []) if response else []

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

@app.post("/report/all")
async def generate_all_report(
    request: Request,
    type: str = Form(...),
    month: int = Form(...),
    stream: bool = Form(False)
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
        # Check if request comes from form submission (redirect to progressive generator immediately)
        content_type = request.headers.get("content-type", "")
        if "application/x-www-form-urlencoded" in content_type:
            # Store basic info for progressive generator
            month_names = {
                1: 'Januari', 2: 'Februari', 3: 'Maret', 4: 'April',
                5: 'Mei', 6: 'Juni', 7: 'Juli', 8: 'Agustus',
                9: 'September', 10: 'Oktober', 11: 'November', 12: 'Desember'
            }

            request.session["progressive_generation"] = {
                "type": type,
                "month": month,
                "year": current_year,
                "month_name": month_names.get(month, str(month))
            }

            # Redirect to progressive generator immediately (no heavy processing)
            return RedirectResponse(url="/admin/progressive-generator", status_code=303)

        # Only do heavy processing for direct access (not form submission)
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

        # Direct access - return template
        return templates.TemplateResponse('all_report_template.html', {
            "request": request,
            "type": type,
            "month": month,
            "year": current_year,
            "html_sections": html_sections,
            "logo_pama_url": '/admin/static/img/logo_pama.png',
            "logo_celerates_url": '/admin/static/img/logo_celerates.jpg',
            "datetime": datetime
        })

    except Exception as e:
        raise HTTPException(500, f"Error generating comprehensive report: {str(e)}")


@app.get("/admin/progressive-generator")
async def show_progressive_generator(request: Request):
    """Show progressive report generation page"""
    gen_data = request.session.get("progressive_generation")

    if not gen_data:
        # No data, redirect back to admin
        return RedirectResponse(url="/admin", status_code=303)

    return templates.TemplateResponse('progressive_report_generator.html', {
        "request": request,
        "type": gen_data.get("type", ""),
        "month": gen_data.get("month", 1),
        "year": gen_data.get("year", 2024),
        "month_name": gen_data.get("month_name", ""),
        "datetime": datetime
    })


@app.post("/admin/report-editor")
async def proceed_to_editor(request: Request):
    """Transfer completed sections from progressive generator to editor"""
    try:
        # Get generation plan with completed sections
        plan = request.session.get("generation_plan")
        if not plan:
            return RedirectResponse(url="/admin", status_code=303)

        # Filter only completed sections with their generated content
        completed_sections = []
        for section_data in plan.get("sections", []):
            if section_data["status"] == "completed" and "generated_content" in section_data:
                # Use the actual generated content
                content = section_data["generated_content"]
                completed_sections.append({
                    "type": content["type"],
                    "title": content["title"],
                    "content": content["content"],
                    "employee_name": content.get("employee_name"),
                    "section_type": content.get("section_type")
                })

        # Store in session for editor
        request.session["report_data"] = {
            "type": plan["type"],
            "month": plan["month"],
            "year": plan["year"],
            "html_sections": completed_sections,
            "logo_pama_url": '/admin/static/img/logo_pama.png',
            "logo_celerates_url": '/admin/static/img/logo_celerates.jpg'
        }

        return RedirectResponse(url="/admin/report-editor", status_code=303)

    except Exception as e:
        return {"success": False, "error": str(e)}


@app.get("/admin/report-editor")
async def show_report_editor(request: Request, plan_id: str = Query(None)):
    """Show report editor page with generated data"""
    if plan_id:
        # Get data from SQLite database using plan_id
        plan = get_generation_plan(plan_id)
        if plan:
            # Build completed sections data for editor using stored content
            completed_sections = []
            for section in plan.get("sections", []):
                if section.get("status") == "completed":
                    # Use stored content instead of regenerating
                    stored_content = section.get("generated_content")
                    if stored_content:
                        completed_sections.append({
                            "type": stored_content.get("type"),
                            "title": stored_content.get("title"),
                            "content": stored_content.get("content"),
                            "employee_name": stored_content.get("employee_name")  # Add employee_name
                        })
                    else:
                        # Fallback for sections without stored content
                        completed_sections.append({
                            "type": section.get("type"),
                            "title": section.get("title"),
                            "content": f"<div class='section-placeholder'>Section {section.get('title')} - No stored content found</div>",
                            "employee_name": section.get("employee_name")  # Add employee_name from section data
                        })

            report_data = {
                "type": plan.get("type"),
                "month": plan.get("month"),
                "year": plan.get("year"),
                "html_sections": completed_sections,
                "logo_pama_url": '/admin/static/img/logo_pama.png',
                "logo_celerates_url": '/admin/static/img/logo_celerates.jpg'
            }
        else:
            # No plan found, redirect back to admin
            return RedirectResponse(url="/admin", status_code=303)
    else:
        # Fallback to session data (for backward compatibility)
        report_data = request.session.get("report_data")
        if not report_data:
            # No data, redirect back to admin
            return RedirectResponse(url="/admin", status_code=303)

    return templates.TemplateResponse('report_editor.html', {
        "request": request,
        "type": report_data.get("type"),
        "month": report_data.get("month"),
        "year": report_data.get("year"),
        "html_sections": report_data.get("html_sections", []),
        "logo_pama_url": report_data.get("logo_pama_url"),
        "logo_celerates_url": report_data.get("logo_celerates_url"),
        "datetime": datetime
    })


@app.post("/api/generate/plan")
async def generate_plan(
    request: Request,
    type: str = Form(...),
    month: int = Form(...)
):
    """Calculate total sections needed for report generation"""
    try:
        current_year = datetime.now().year

        # Get employee count with role filter
        employee_table = config.NOCODB_TABLES.get("employee_data")
        nocodb_employee = ClsNocoDBProcessor(config.APP_BASE_ID, employee_table)

        if type == "iotoperation":
            # For IoT Operations, include specific NRPs: JIMT24011 and JIMT24012
            # Get all employees first, then filter to include both IoT Operations role and specific NRPs
            all_employees = nocodb_employee.get_all_employees()
            employee_mapping = {}

            for name, info in all_employees.items():
                employee_role = info.get('role', '').strip()
                employee_nrp = info.get('nrp', '').strip()

                # Include if they have IoT Operations role OR are JIMT24011/JIMT24012
                if (employee_role == "IoT Operations" or
                    employee_nrp in ["JIMT24011", "JIMT24012", "JIMT24001"]):
                    employee_mapping[name] = info
        elif type == "developer":
            role_filter = "Developer"
            all_devs = nocodb_employee.get_all_employees(role_filter=role_filter)
            employee_mapping = {}

            # Exclude NRPs that are already included in IoT Operations report
            excluded_nrps = ["JIMT24011", "JIMT24012", "JIMT24001"]

            for name, info in all_devs.items():
                employee_nrp = info.get('nrp', '').strip()
                if employee_nrp not in excluded_nrps:
                    employee_mapping[name] = info
        else:
            employee_mapping = nocodb_employee.get_all_employees()

        # Calculate sections
        sections_plan = []
        section_id = 1

        # Main Timesheet section header
        sections_plan.append({
            "id": section_id,
            "type": "timesheet_header",
            "title": "1. Timesheet",
            "status": "pending"
        })
        section_id += 1

        # Timesheet sections (one per employee)
        timesheet_counter = 1
        for emp_name, emp_info in employee_mapping.items():
            sections_plan.append({
                "id": section_id,
                "type": "timesheet",
                "title": f"1.{timesheet_counter}. Timesheet - {emp_name}",
                "employee_name": emp_name,
                "status": "pending"
            })
            section_id += 1
            timesheet_counter += 1

        # Main Task List section header
        sections_plan.append({
            "id": section_id,
            "type": "tasklist_header",
            "title": "2. Task List",
            "status": "pending"
        })
        section_id += 1

        # Tasklist sections
        if type == "iotoperation":
            tasklist_sections = [
                {"title": "2.1. IoT Operations - Problem Report", "section_type": "problem"},
                {"title": "2.2. IoT Operations - Aktivitas Report", "section_type": "aktivitas"}
            ]
        else:
            tasklist_sections = [
                {"title": "2.1. Developer - Kualitas Kode", "section_type": "kualitas"},
                {"title": "2.2. Developer - Waktu Rilis", "section_type": "waktu"},
                {"title": "2.3. Developer - Dukungan Support", "section_type": "dukungan"}
            ]

        for tasklist in tasklist_sections:
            sections_plan.append({
                "id": section_id,
                "type": "tasklist",
                "title": tasklist["title"],
                "section_type": tasklist["section_type"],
                "status": "pending"
            })
            section_id += 1

        # Main Evidence section header
        sections_plan.append({
            "id": section_id,
            "type": "evidence_header",
            "title": "3. Evidence",
            "status": "pending"
        })
        section_id += 1

        # Pre-fetch evidence records to create a section for each
        evidence_type_param = "iotoperations" if type == "iotoperation" else "developer"
        evidence_records = await _get_all_evidence_records(evidence_type_param, month)
        
        evidence_counter = 1
        for record in evidence_records:
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
                # Create a new section for each evidence item
                sections_plan.append({
                    "id": section_id,
                    "type": "evidence",
                    "title": f"3.{evidence_counter}. {task_list}",
                    "status": "pending",
                    # Store the necessary data to generate this section later
                    "data": {
                        "number": evidence_counter,
                        "title": task_list,
                        "image_path": image_urls[0],
                        "description": task_list,
                        "type": evidence_type_param,
                        "month_name": _get_month_name(month)
                    }
                })
                section_id += 1
                evidence_counter += 1

        # Main Attendance section header
        sections_plan.append({
            "id": section_id,
            "type": "attendance_header",
            "title": "4. Attendance",
            "status": "pending"
        })
        section_id += 1

        # Attendance section (one per employee)
        attendance_counter = 1
        for emp_name, emp_info in employee_mapping.items():
            sections_plan.append({
                "id": section_id,
                "type": "attendance",
                "title": f"4.{attendance_counter}. Attendance - {emp_name}",
                "employee_name": emp_name,
                "status": "pending"
            })
            section_id += 1
            attendance_counter += 1

        # Store plan in SQLite database
        plan_data = {
            "type": type,
            "month": month,
            "year": current_year,
            "sections": sections_plan,
            "total": len(sections_plan),
            "completed": 0,
            "failed": 0
        }

        plan_id = save_generation_plan(plan_data)

        return {
            "success": True,
            "plan_id": plan_id,
            "total_sections": len(sections_plan),
            "sections": sections_plan
        }

    except Exception as e:
        print(f"❌ Generate plan error: {e}")
        import traceback
        print(f"❌ Traceback: {traceback.format_exc()}")
        return {
            "success": False,
            "error": str(e)
        }


@app.post("/api/generate/section")
async def generate_section(
    request: Request,
    section_id: int = Form(...),
    plan_id: str = Form(...)
):
    """Generate a specific section by ID"""
    try:
        # Get plan from SQLite database
        plan = get_generation_plan(plan_id)
        if not plan:
            return {
                "success": False,
                "section_id": section_id,
                "error": f"No generation plan found with ID: {plan_id}"
            }

        # Find section in plan
        section = None
        for s in plan["sections"]:
            if s["id"] == section_id:
                section = s
                break

        if not section:
            return {
                "success": False,
                "section_id": section_id,
                "error": f"Section {section_id} not found"
            }

        # Generate section based on type
        section_content = None

        if section["type"] == "timesheet_header":
            section_content = {
                'type': 'timesheet_header',
                'title': '1. Timesheet',
                'content': ''  # Empty content since title already shows in page header
            }

        elif section["type"] == "timesheet":
            section_content = await _generate_single_timesheet_section(
                section["employee_name"], plan["month"], plan["year"], plan["type"], request
            )
            if section_content:
                section_content["title"] = section["title"]  # Use plan title

        elif section["type"] == "tasklist_header":
            section_content = {
                'type': 'tasklist_header',
                'title': '2. Task List',
                'content': ''  # Empty content since title already shows in page header
            }

        elif section["type"] == "tasklist":
            section_content = await _generate_single_tasklist_section(
                section["section_type"], plan["month"], plan["type"], request
            )
            if section_content:
                section_content["title"] = section["title"]  # Use plan title

        elif section["type"] == "evidence_header":
            section_content = {
                'type': 'evidence_header',
                'title': '3. Evidence',
                'content': ''
            }
        
        elif section["type"] == "evidence":
            # The data for the single evidence item is already in the plan
            section_data = section.get("data")
            if section_data:
                html_content = await _generate_single_evidence_section(section_data, request)
                section_content = {
                    'type': 'evidence',
                    'title': section.get("title"),
                    'content': html_content
                }
            else:
                section_content = None

        elif section["type"] == "attendance_header":
            section_content = {
                'type': 'attendance_header',
                'title': '4. Attendance',
                'content': ''  # Empty content since title already shows in page header
            }

        elif section["type"] == "attendance":
            section_content = await _generate_single_attendance_section(
                section["employee_name"], plan["month"], plan["year"], plan["type"], request
            )
            if section_content:
                section_content["title"] = section["title"]  # Use plan title

        if section_content:
            # Update section status in plan and store content
            for s in plan["sections"]:
                if s["id"] == section_id:
                    s["status"] = "completed"
                    # Store basic info only to avoid JSON serialization issues
                    if isinstance(section_content, dict):
                        s["generated_content"] = {
                            "type": section_content.get("type", "unknown"),
                            "title": section_content.get("title", ""),
                            "content": str(section_content.get("content", "")),  # Convert to string
                            "employee_name": section_content.get("employee_name", "")
                        }
                    else:
                        s["generated_content"] = {"type": "unknown", "title": "Generated", "content": str(section_content), "employee_name": ""}
                    break

            plan["completed"] += 1
            update_generation_plan(plan_id, plan)

            return {
                "success": True,
                "section_id": section_id,
                "section": section_content,
                "progress": {
                    "completed": plan["completed"],
                    "total": plan["total"],
                    "percentage": round((plan["completed"] / plan["total"]) * 100, 2)
                }
            }
        else:
            # Mark as failed
            for s in plan["sections"]:
                if s["id"] == section_id:
                    s["status"] = "failed"
                    break

            plan["failed"] += 1
            update_generation_plan(plan_id, plan)

            return {
                "success": False,
                "section_id": section_id,
                "error": "Failed to generate section content"
            }

    except Exception as e:
        return {
            "success": False,
            "section_id": section_id,
            "error": str(e)
        }


@app.post("/api/generate/retry")
async def retry_section(
    request: Request,
    section_id: int = Form(...),
    plan_id: str = Form(...)
):
    """Retry generating a failed section"""
    try:
        # Get plan from SQLite database
        plan = get_generation_plan(plan_id)
        if not plan:
            return {
                "success": False,
                "section_id": section_id,
                "error": f"No generation plan found with ID: {plan_id}"
            }

        # Reset section status to pending
        for s in plan["sections"]:
            if s["id"] == section_id:
                s["status"] = "pending"
                break

        plan["failed"] = max(0, plan["failed"] - 1)
        update_generation_plan(plan_id, plan)

        # Re-attempt generation
        return await generate_section(request, section_id)

    except Exception as e:
        return {
            "success": False,
            "section_id": section_id,
            "error": str(e)
        }


# Helper functions for progressive generation
async def _generate_single_timesheet_section(employee_name: str, month: int, year: int, report_type: str, request: Request):
    """Generate timesheet section for single employee"""
    try:
        # Get employee info first
        employee_table = config.NOCODB_TABLES.get("employee_data")
        nocodb_employee = ClsNocoDBProcessor(config.APP_BASE_ID, employee_table)

        if report_type == "iotoperation":
            # For IoT Operations, include specific NRPs: JIMT24011 and JIMT24012
            # Get all employees first, then filter to include both IoT Operations role and specific NRPs
            all_employees = nocodb_employee.get_all_employees()
            employee_mapping = {}

            for name, info in all_employees.items():
                employee_role = info.get('role', '').strip()
                employee_nrp = info.get('nrp', '').strip()

                # Include if they have IoT Operations role OR are JIMT24011/JIMT24012
                if (employee_role == "IoT Operations" or
                    employee_nrp in ["JIMT24011", "JIMT24012", "JIMT24001"]):
                    employee_mapping[name] = info
        else:
            role_filter = "Developer"
            employee_mapping = nocodb_employee.get_all_employees(role_filter=role_filter)

        if employee_name not in employee_mapping:
            return None

        employee_info = employee_mapping[employee_name]
        single_employee_data = await _generate_single_employee_timesheet(employee_name, employee_info, month, year)

        if single_employee_data and single_employee_data.get('timesheet_rows'):
            html_content = await _render_single_timesheet_html(single_employee_data, request)
            # Ensure we return only JSON-serializable data
            return {
                'type': 'timesheet',
                'title': f'Timesheet {employee_name}',
                'employee_name': str(employee_name).upper(),
                'content': str(html_content)  # Ensure it's a string
            }

        return None

    except Exception as e:
        print(f"Error generating timesheet for {employee_name}: {e}")
        return None


async def _generate_single_tasklist_section(section_type: str, month: int, report_type: str, request: Request):
    """Generate single tasklist section"""
    try:
        if report_type == "iotoperation":
            html_content = await _get_iot_tasklist_html_content(month, section_type, request)
        else:
            html_content = await _get_developer_tasklist_html_content(month, section_type, request)

        if html_content:
            title_map = {
                "problem": "2.1. IoT Operations - Problem Report",
                "aktivitas": "2.2. IoT Operations - Aktivitas Report",
                "kualitas": "2.1. Developer - Kualitas Kode",
                "waktu": "2.2. Developer - Waktu Rilis",
                "dukungan": "2.3. Developer - Dukungan Support"
            }

            return {
                'type': 'tasklist',
                'title': title_map.get(section_type, f"Tasklist - {section_type}"),
                'section_type': str(section_type),
                'content': str(html_content)  # Ensure it's a string
            }

        return None

    except Exception as e:
        print(f"Error generating tasklist section {section_type}: {e}")
        return None


async def _generate_single_attendance_section(employee_name: str, month: int, year: int, report_type: str, request: Request):
    """Generate attendance section for single employee"""
    try:
        # Get employee info
        employee_table = config.NOCODB_TABLES.get("employee_data")
        nocodb_employee = ClsNocoDBProcessor(config.APP_BASE_ID, employee_table)

        if report_type == "iotoperation":
            # For IoT Operations, include specific NRPs: JIMT24011 and JIMT24012
            # Get all employees first, then filter to include both IoT Operations role and specific NRPs
            all_employees = nocodb_employee.get_all_employees()
            employee_mapping = {}

            for name, info in all_employees.items():
                employee_role = info.get('role', '').strip()
                employee_nrp = info.get('nrp', '').strip()

                # Include if they have IoT Operations role OR are JIMT24011/JIMT24012
                if (employee_role == "IoT Operations" or
                    employee_nrp in ["JIMT24011", "JIMT24012", "JIMT24001"]):
                    employee_mapping[name] = info
        else:
            role_filter = "Developer"
            employee_mapping = nocodb_employee.get_all_employees(role_filter=role_filter)

        if employee_name not in employee_mapping:
            return None

        # Generate attendance data for single employee
        single_attendance_data = await _generate_single_employee_attendance(employee_name, employee_mapping, month, year, request)

        if single_attendance_data:
            return {
                'type': 'attendance',
                'title': f'Attendance {employee_name}',
                'employee_name': str(employee_name),
                'content': str(single_attendance_data)  # Ensure it's a string
            }

        return None

    except Exception as e:
        print(f"Error generating attendance for {employee_name}: {e}")
        return None


async def _generate_single_employee_attendance(employee_name: str, employee_mapping: dict, month: int, year: int, request: Request):
    """Generate attendance HTML for single employee"""
    try:
        attendance_table = config.NOCODB_TABLES.get("attendance")
        nocodb_attendance = ClsNocoDBProcessor(config.APP_BASE_ID, attendance_table)

        start_date, end_date = get_dynamic_month_dates(year, month)

        # Get attendance data for this employee
        attendance_data = []
        emp_info = employee_mapping[employee_name]

        # Query attendance records for this employee (without date filter in query)
        where_clause = f"(Name,like,%{employee_name.strip().title()}%)"
        response = nocodb_attendance.get_records(limit=2000, where=where_clause)
        records = response.get('list', []) if response else []

        if records:
            # Process attendance data for this employee, filtering by date in Python
            for record in records:
                rec_date_str = record.get('Date')
                if not rec_date_str:
                    continue
                rec_date = datetime.strptime(rec_date_str, '%Y-%m-%d').date()
                if start_date.date() <= rec_date <= end_date.date():
                    attendance_data.append({
                        'nrp': emp_info.get('nrp', ''),
                        'nama': employee_name,
                        'tanggal_kehadiran': rec_date.strftime('%d/%m/%Y'),
                        'jam_kehadiran': format_attendance_time(record.get('Start Time'), record.get('End Time'))
                    })

            # Sort attendance data by date like the original function
            attendance_data.sort(key=lambda x: datetime.strptime(x['tanggal_kehadiran'], '%d/%m/%Y'))

            # Render template for this employee
            template = templates.get_template('attendance_report_template.html')
            html_content = template.render(
                request=request,
                reports=[{
                    'nrp': emp_info.get('nrp', ''),
                    'nama': employee_name.upper(),
                    'attendance_rows': attendance_data
                }],
                periode=f"{['', 'Januari', 'Februari', 'Maret', 'April', 'Mei', 'Juni', 'Juli', 'Agustus', 'September', 'Oktober', 'November', 'Desember'][month]} {year}",
                dicetak=datetime.now().strftime('%d %B %Y, %H:%M:%S'),
                logo_url='/admin/static/img/logo_pama.png'
            )

            return html_content

        return None

    except Exception as e:
        print(f"Error generating attendance for {employee_name}: {e}")
        return None


async def _get_iot_tasklist_html_content(month: int, section_type: str, request: Request):
    """Generate IoT tasklist HTML content for specific section"""
    try:
        # Get date range for the month
        current_year = datetime.now().year
        start_date, end_date = get_dynamic_month_dates(current_year, month)
        start_date_str = start_date.strftime('%Y-%m-%d')
        end_date_str = end_date.strftime('%Y-%m-%d')

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
        # Use date range filtering with Start_Date and End_Date validation
        where_clause = f"(Date,gte,{start_date_str})~and(Date,lte,{end_date_str})~and(Status,eq,Closed)"
        response = nocodb.get_records(limit=2000, where=where_clause)
        raw_records = response.get('list', []) if response else []

        # Additional validation to filter out N/A, empty, or null dates
        records = []
        for record in raw_records:
            start_date_val = record.get('Start_Date', '')
            end_date_val = record.get('End_Date', '')

            # Skip if dates are empty, N/A, null, or contain invalid values
            if (start_date_val and end_date_val and
                str(start_date_val).strip() not in ['', 'N/A', 'null', 'None'] and
                str(end_date_val).strip() not in ['', 'N/A', 'null', 'None']):
                records.append(record)

        if section_type == "problem":
            return await _generate_iot_problem_page(request, records, month_name)
        else:  # aktivitas
            return await _generate_iot_aktivitas_page(request, records, month_name)

    except Exception as e:
        print(f"Error generating IoT tasklist section {section_type}: {e}")
        return ""


async def _get_developer_tasklist_html_content(month: int, section_type: str, request: Request):
    """Generate Developer tasklist HTML content for specific section"""
    try:
        # Get date range for the month
        current_year = datetime.now().year
        start_date, end_date = get_dynamic_month_dates(current_year, month)
        start_date_str = start_date.strftime('%Y-%m-%d')
        end_date_str = end_date.strftime('%Y-%m-%d')

        indonesian_months = {
            1: 'Januari', 2: 'Februari', 3: 'Maret', 4: 'April',
            5: 'Mei', 6: 'Juni', 7: 'Juli', 8: 'Agustus',
            9: 'September', 10: 'Oktober', 11: 'November', 12: 'Desember'
        }
        month_name = indonesian_months[month]

        table_id = config.NOCODB_TABLES.get("tasklist")  # Use "tasklist" instead of "tasklist_developer"
        if not table_id:
            print(f"❌ No table ID found for tasklist in config: {config.NOCODB_TABLES}")
            return ""

        nocodb = ClsNocoDBProcessor(config.APP_BASE_ID, table_id)

        kategori_mapping = {
            "kualitas": "Detail Aktivitas Kualitas Kode",
            "waktu": "Detail Aktivitas Waktu Rilis",
            "dukungan": "Detail Aktivitas Dukungan Support"
        }

        kategori_name = kategori_mapping.get(section_type)
        if not kategori_name:
            return ""

        # First, let's check with basic filtering to see field structure
        where_clause_basic = f"(Date,gte,{start_date_str})~and(Date,lte,{end_date_str})~and(Kategori,eq,{kategori_name})~and(Status,eq,Closed)"
        response = nocodb.get_records(limit=5, where=where_clause_basic)
        debug_records = response.get('list', []) if response else []

        # Debug: print available fields
        if debug_records:
            print(f"🔍 Available fields in tasklist record: {list(debug_records[0].keys())}")
            first_record = debug_records[0]
            for key in first_record.keys():
                if 'date' in key.lower() or 'start' in key.lower() or 'end' in key.lower():
                    print(f"🔍 Date-related field '{key}': {first_record.get(key)}")

        # Use the full filtering with proper field names
        where_clause = f"(Date,gte,{start_date_str})~and(Date,lte,{end_date_str})~and(Kategori,eq,{kategori_name})~and(Status,eq,Closed)"
        response = nocodb.get_records(limit=2000, where=where_clause)
        raw_records = response.get('list', []) if response else []

        # Additional validation to filter out N/A, empty, or null dates
        records = []
        for record in raw_records:
            start_date_val = record.get('Start_Date', '')
            end_date_val = record.get('End_Date', '')

            # Skip if dates are empty, N/A, null, or contain invalid values
            if (start_date_val and end_date_val and
                str(start_date_val).strip() not in ['', 'N/A', 'null', 'None'] and
                str(end_date_val).strip() not in ['', 'N/A', 'null', 'None']):
                records.append(record)

        if section_type == "kualitas":
            return await _generate_dev_kualitas_data(request, records, month_name)
        elif section_type == "waktu":
            return await _generate_dev_waktu_data(request, records, month_name)
        elif section_type == "dukungan":
            return await _generate_dev_dukungan_data(request, records, month_name)

        return ""

    except Exception as e:
        print(f"Error generating Developer tasklist section {section_type}: {e}")
        return ""


async def _get_timesheet_html_sections(month: int, year: int, report_type: str, request: Request):
    """Get timesheet HTML for each employee separately, filtered by role"""
    employee_table = config.NOCODB_TABLES.get("employee_data")
    nocodb_employee = ClsNocoDBProcessor(config.APP_BASE_ID, employee_table)

    if report_type == "iotoperation":
        # For IoT Operations, include specific NRPs: JIMT24011 and JIMT24012
        # Get all employees first, then filter to include both IoT Operations role and specific NRPs
        all_employees = nocodb_employee.get_all_employees()
        employee_mapping = {}

        for name, info in all_employees.items():
            employee_role = info.get('role', '').strip()
            employee_nrp = info.get('nrp', '').strip()

            # Include if they have IoT Operations role OR are JIMT24011/JIMT24012
            if (employee_role == "IoT Operations" or
                employee_nrp in ["JIMT24011", "JIMT24012", "JIMT24001"]):
                employee_mapping[name] = info
    elif report_type == "developer":
        role_filter = "Developer"
        employee_mapping = nocodb_employee.get_all_employees(role_filter=role_filter)
    else:
        employee_mapping = nocodb_employee.get_all_employees()

    timesheet_htmls = []

    for name, info in employee_mapping.items():
        try:
            single_employee_data = await _generate_single_employee_timesheet(name, info, month, year)

            if single_employee_data and single_employee_data.get('timesheet_rows'):
                # Cache Fauzan's timesheet data globally
                if "Fauzan" in name or "FAUZAN" in name.upper():
                    global fauzan_timesheet_cache
                    fauzan_timesheet_cache = single_employee_data
                    print(f"DEBUG: Cached Fauzan timesheet data with {len(single_employee_data.get('timesheet_rows', []))} rows")

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
    records = response.get('list', []) if response else []

    if not records:
        return None

    records_by_date = {r['Date']: r for r in records if 'Date' in r}
    employee_role = info.get('role')
    work_desc_field = 'Work Description IoT' if employee_role == 'IoT Operations' else 'Work Description'
    # Get unique work descriptions, limit to 6 tasks max
    unique_work_descs = sorted({str(d).strip() for r in records for d in r.get(work_desc_field, []) if str(d).strip()})
    all_work_descs = '; '.join(unique_work_descs[:6])

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
            "logo_url": '/admin/static/img/logo_pama.png'
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
    # Use Unique_Key filtering for month-specific records
    current_year = datetime.now().year
    year_month_pattern = f"{current_year}-{month:02d}-"
    where_clause = f"(Unique_Key,like,{year_month_pattern}%)~and(Status,eq,Closed)"
    response = nocodb.get_records(limit=2000, where=where_clause)
    records = response.get('list', []) if response else []

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

        # Use Unique_Key filtering for month-specific records
        current_year = datetime.now().year
        year_month_pattern = f"{current_year}-{month:02d}-"
        where_clause = f"(Unique_Key,like,{year_month_pattern}%)~and(Kategori,eq,{kategori_name})~and(Status,eq,Closed)"
        response = nocodb.get_records(limit=2000, where=where_clause)
        records = response.get('list', []) if response else []

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
        return {'type': 'evidence', 'title': '3. Evidence Aktivitas', 'content': '<div>Evidence table not configured.</div>'}

    nocodb = ClsNocoDBProcessor(config.APP_BASE_ID, table_id)
    # Use Unique_Key filtering for month-specific records with evidence
    current_year = datetime.now().year
    year_month_pattern = f"{current_year}-{month:02d}-"
    where_clause = f"(Unique_Key,like,{year_month_pattern}%)~and(Evidence Task,notnull)"
    response = nocodb.get_records(limit=2000, where=where_clause)
    records = response.get('list', []) if response else []
    
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
        'title': '3. Evidence Aktivitas',
        'content': str(isolated_content)  # Ensure it's a string
    }

async def _get_attendance_html_section(month: int, year: int, report_type: str, request: Request):
    """Get attendance HTML section filtered by role"""
    employee_table = config.NOCODB_TABLES.get("employee_data")
    attendance_table = config.NOCODB_TABLES.get("attendance")

    nocodb_employee = ClsNocoDBProcessor(config.APP_BASE_ID, employee_table)
    nocodb_attendance = ClsNocoDBProcessor(config.APP_BASE_ID, attendance_table)

    if report_type == "iotoperation":
        # For IoT Operations, include specific NRPs: JIMT24011 and JIMT24012
        # Get all employees first, then filter to include both IoT Operations role and specific NRPs
        all_employees = nocodb_employee.get_all_employees()
        employee_mapping = {}

        for name, info in all_employees.items():
            employee_role = info.get('role', '').strip()
            employee_nrp = info.get('nrp', '').strip()

            # Include if they have IoT Operations role OR are JIMT24011/JIMT24012
            if (employee_role == "IoT Operations" or
                employee_nrp in ["JIMT24011", "JIMT24012", "JIMT24001"]):
                employee_mapping[name] = info
    elif report_type == "developer":
        role_filter = "Developer"
        employee_mapping = nocodb_employee.get_all_employees(role_filter=role_filter)
    else:
        employee_mapping = nocodb_employee.get_all_employees()
    start_date, end_date = get_dynamic_month_dates(year, month)

    reports_data = []
    for name, info in employee_mapping.items():
        display_nrp = info.get('nrp') or info.get('employee_id')
        if not display_nrp:
            continue

        where_clause = f"(Name,like,%{name.strip().title()}%)"
        response = nocodb_attendance.get_records(limit=2000, where=where_clause)
        records = response.get('list', []) if response else []

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
                "logo_url": '/admin/static/img/logo_pama.png'
            })

            clean_content = html_content

            return {
                'type': 'attendance',
                'title': '4. PAMA Attendance Report',
                'content': clean_content
            }
        except Exception as e:
            pass

    return {
        'type': 'attendance',
        'title': 'PAMA Attendance Report',
        'content': f"<div>No attendance data found for {report_type} employees in month {month}</div>"
    }

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

    from datetime import datetime, date, timedelta

    # Get employee list for filter dropdown
    employee_table = config.NOCODB_TABLES.get("employee_data")
    nocodb_employee = ClsNocoDBProcessor(config.APP_BASE_ID, employee_table)
    employee_mapping = nocodb_employee.get_all_employees()
    employee_list = list(employee_mapping.keys())

    attendance_data = []

    # Only load data if date parameters are provided (when filter is applied)
    if start_date and end_date:
        # Parse date strings
        start_date_obj = datetime.strptime(start_date, '%Y-%m-%d').date()
        end_date_obj = datetime.strptime(end_date, '%Y-%m-%d').date()

        # Get attendance data
        attendance_table = config.NOCODB_TABLES.get("attendance")
        nocodb_attendance = ClsNocoDBProcessor(config.APP_BASE_ID, attendance_table)

        # Filter employees if specific employees selected
        target_employees = employee if employee else employee_list

        for emp_name in target_employees:
            if emp_name not in employee_mapping:
                continue

            emp_info = employee_mapping[emp_name]
            where_clause = f"(Name,like,%{emp_name.strip().title()}%)"
            response = nocodb_attendance.get_records(limit=2000, where=where_clause)
            records = response.get('list', []) if response else []

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
                rec = attendance_by_date.get(date_str)

                rec_date = current_date

                def get_time(val):
                    if not val: return ''
                    actual_val = val[0] if isinstance(val, list) else val
                    if actual_val is None or str(actual_val).strip() == '': return ''
                    time_str = str(actual_val)
                    return ':'.join(time_str.split(' ')[-1].split('+')[0].split(':')[:2])

                if rec is None:
                    last_modified, is_manual_edit, start_time, end_time, holiday, attendance_code, keterangan = '', False, '', '', '', '', ''
                    overtime_fields = {'overtime_check_in': '', 'overtime_check_out': '', 'overtime_before': '', 'overtime_after': ''}
                    timeoff_fields = {'timeoff_check_out': '', 'timeoff_break_before': '', 'timeoff_break_after': ''}
                else:
                    last_modified = rec.get('Last Modified', '')
                    is_manual_edit = '@system.com' not in str(last_modified) if last_modified else False
                    start_time, end_time = get_time(rec.get('Start Time')), get_time(rec.get('End Time'))
                    holiday, attendance_code, keterangan = rec.get('Holiday', ''), rec.get('Attendance_Code', ''), rec.get('Remarks', '')
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

                is_iot_operations = emp_info.get('role') == 'IoT Operations'
                schedule_in_time, schedule_out_time, shift_code = '7:30', '16:30', 'N'

                if is_iot_operations:
                    schedule_table = config.NOCODB_TABLES.get("schedule_shifting")
                    nocodb_schedule = ClsNocoDBProcessor(config.APP_BASE_ID, schedule_table)
                    where_schedule = f"(Employee Name,like,{emp_name.strip().title()})~and(Date,eq,{rec_date.strftime('%Y-%m-%d')})"
                    schedule_response = nocodb_schedule.get_records(limit=5, where=where_schedule)
                    schedule_records = schedule_response.get('list', []) if schedule_response else []

                    if schedule_records:
                        schedule = schedule_records[0]
                        if not schedule.get('Shift Data'):
                            shift_code, schedule_in_time, schedule_out_time = 'Day Off', '', ''
                        else:
                            codes, start_times, end_times = schedule.get('Code', []), schedule.get('Start Time', []), schedule.get('End Time', [])
                            if codes and start_times and end_times:
                                shift_code = codes[0] if isinstance(codes, list) else codes
                                start_time_raw, end_time_raw = (start_times[0] if isinstance(start_times, list) else start_times), (end_times[0] if isinstance(end_times, list) else end_times)
                                schedule_in_time = ':'.join(str(start_time_raw).split(':')[:2]) if start_time_raw else '7:30'
                                schedule_out_time = ':'.join(str(end_time_raw).split(':')[:2]) if end_time_raw else '16:30'
                            else:
                                shift_code, schedule_in_time, schedule_out_time = 'Day Off', '', ''
                    else:
                        shift_code, schedule_in_time, schedule_out_time = 'Day Off', '', ''
                else:
                    if str(holiday).upper() == 'H' or not (start_time or end_time):
                        shift_code, schedule_in_time, schedule_out_time = 'Day Off', '', ''

                attendance_data.append({
                    'employee_id': emp_info.get('employee_id', emp_info.get('nrp', '')), 'full_name': emp_name, 'date': rec_date,
                    'shift': shift_code, 'shift_code': '', 'shift_label': '', 'schedule_in': schedule_in_time, 'schedule_out': schedule_out_time,
                    'attendance_code': attendance_code, 'check_in': start_time, 'check_out': end_time, 'keterangan': keterangan,
                    **overtime_fields, **timeoff_fields, 'holiday_code': holiday, 'is_manual_edit': is_manual_edit
                })

                current_date += timedelta(days=1)

    if attendance_data:
        attendance_data.sort(key=lambda x: (x['full_name'], x['date']))

    employee_roles = {emp_name: emp_info.get('role', '') for emp_name, emp_info in employee_mapping.items()}

    return templates.TemplateResponse('attendance_celerates.html', {
        "request": request, "user": user, "attendance_data": attendance_data, "employee_list": employee_list,
        "employee_roles": employee_roles, "start_date": datetime.strptime(start_date, '%Y-%m-%d').date() if start_date else None,
        "end_date": datetime.strptime(end_date, '%Y-%m-%d').date() if end_date else None,
        "selected_employees": employee if employee else [], "datetime": datetime
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
        response = nocodb_attendance.get_records(limit=2000, where=where_clause)
        records = response.get('list', []) if response else []

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

async def _get_all_evidence_records(evidence_type: str, month_name: str):
    """Get all evidence records for planning purposes"""
    # Convert month name to month number for Unique_Key pattern
    month_mapping = {
        'Januari': 1, 'Februari': 2, 'Maret': 3, 'April': 4,
        'Mei': 5, 'Juni': 6, 'Juli': 7, 'Agustus': 8,
        'September': 9, 'Oktober': 10, 'November': 11, 'Desember': 12
    }

    month = month_mapping.get(month_name, 1)
    current_year = datetime.now().year
    year_month_pattern = f"{current_year}-{month:02d}-"

    if evidence_type == "iotoperations":
        table_key = "tasklist_iot"
    elif evidence_type == "developer":
        table_key = "tasklist"
    else:
        return []

    table_id = config.NOCODB_TABLES.get(table_key)
    if not table_id:
        return []

    nocodb = ClsNocoDBProcessor(config.APP_BASE_ID, table_id)
    # Use Unique_Key filtering for month-specific records with evidence
    where_clause = f"(Unique_Key,like,{year_month_pattern}%)~and(Evidence Task,notnull)"
    response = nocodb.get_records(limit=2000, where=where_clause)
    records = response.get('list', []) if response else []

    return records

async def _generate_single_evidence_section(section_data: dict, request: Request):
    """Generate HTML content for a single evidence item"""
    try:
        # Create evidence data list with single item
        evidence_data = [{
            "number": section_data["number"],
            "title": section_data["title"],
            "image_path": section_data["image_path"],
            "description": section_data["description"]
        }]

        template = templates.get_template('evidence/evidence_aktivitas.html')
        html_content = template.render({
            "request": request,
            "evidence_data": evidence_data,
            "type": section_data["type"],
            "month": section_data["month_name"]
        })

        # Extract body content and wrap in evidence section
        import re
        body_content = re.search(r'<body[^>]*>(.*?)</body>', html_content, re.DOTALL)
        if body_content:
            isolated_content = f'<div class="evidence-section">{body_content.group(1)}</div>'
        else:
            isolated_content = f'<div class="evidence-section">{html_content}</div>'

        return str(isolated_content)

    except Exception as e:
        print(f"❌ Error generating single evidence section: {e}")
        return f'<div class="evidence-section">Error generating evidence content: {str(e)}</div>'

def _get_month_name(month: int) -> str:
    """Get Indonesian month name from month number"""
    indonesian_months = {
        1: 'Januari', 2: 'Februari', 3: 'Maret', 4: 'April',
        5: 'Mei', 6: 'Juni', 7: 'Juli', 8: 'Agustus',
        9: 'September', 10: 'Oktober', 11: 'November', 12: 'Desember'
    }
    return indonesian_months.get(month, 'Unknown')

@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {"status": "healthy", "service": "digital-bast-admin"}

# Initialize database on startup
@app.on_event("startup")
async def startup_event():
    """Initialize database and other startup tasks"""
    try:
        init_db()
        print("✅ Database initialized successfully")
    except Exception as e:
        print(f"❌ Database initialization failed: {e}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=False, timeout_keep_alive=1800)
