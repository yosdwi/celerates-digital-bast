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
from fastapi.middleware.gzip import GZipMiddleware
from starlette.middleware.sessions import SessionMiddleware

from src import config
from src.classes.ClsNocoDBProcessor import ClsNocoDBProcessor

app = FastAPI(title="Digital BAST Admin", version="1.0.0")

app.add_middleware(SessionMiddleware, secret_key=os.getenv("SESSION_SECRET", secrets.token_urlsafe(32)))
app.add_middleware(GZipMiddleware, minimum_size=1000)  # Compress responses > 1KB

# app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/admin/static", StaticFiles(directory="static"), name="static")
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
        "rilis": "Detail Aktivitas Waktu Rilis Fitur",
        "support": "Detail Aktivitas Dukungan Support "
    }

    kategori_name = kategori_mapping.get(page)
    if not kategori_name:
        raise HTTPException(400, f"Invalid page: {page}")

    # Use Unique_Key filtering for month-specific records
    current_year = datetime.now().year
    year_month_pattern = f"{current_year}-{month:02d}-"
    where_clause = f"(Unique Key,like,{year_month_pattern}%)~and(Kategori,eq,{kategori_name})~and(Status,eq,Closed)"
    response = nocodb.get_records(limit=2000, where=where_clause)
    records = response.get('list', []) if response else []

    if page == "kualitas":
        return await _generate_dev_kualitas_data(request, records, month_name)
    elif page == "rilis":
        return await _generate_dev_rilis_data(request, records, month_name)
    elif page == "support":
        return await _generate_dev_support_data(request, records, month_name)

async def _generate_dev_kualitas_data(request: Request, records: list, month_name: str, page_number: int = 1, total_pages: int = 1):
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
        # pencapaian = 100

        formatted_start = start_date.replace('-', '/') if start_date else 'N/A'
        formatted_end = end_date.replace('-', '/') if end_date else 'N/A'

        # Use global numbering if available, otherwise page-local numbering
        item_number = record.get('_page_number', i)

        kualitas_data.append({
            "no": item_number,
            "task_list": task_list,
            "requestor": requestor,
            "pic": pic,
            "status": status,
            "start_date": formatted_start,
            "end_date": formatted_end,
            "pencapaian": str(pencapaian)
        })

    # Handle None values in pencapaian calculation - show summary only on last page
    show_summary = (page_number == total_pages)
    summary_pencapaian = ""

    if show_summary:
        valid_pencapaian = [record.get('Pencapaian', 0) for record in records if record.get('Pencapaian') is not None]
        total_pencapaian = sum(int(p) for p in valid_pencapaian if p != 0)
        avg_pencapaian = total_pencapaian // len(valid_pencapaian) if valid_pencapaian else 0
        summary_pencapaian = str(avg_pencapaian)

    # Render template as string instead of TemplateResponse for progressive generation
    template = _get_template_cached('tasklistdeveloper/detail_aktivitas_kualitas_kode.html')
    return template.render({
        "request": request,
        "kualitas_kode_data": kualitas_data,
        "summary_pencapaian": summary_pencapaian,
        "month": month_name
    })

async def _generate_dev_rilis_data(request: Request, records: list, month_name: str, page_number: int = 1, total_pages: int = 1):
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

    # Handle None values in pencapaian calculation
    valid_pencapaian = [record.get('Pencapaian', 0) for record in records if record.get('Pencapaian') is not None]
    total_pencapaian = sum(int(p) for p in valid_pencapaian if p != 0)
    avg_pencapaian = total_pencapaian // len(valid_pencapaian) if valid_pencapaian else 0

    # Render template as string instead of TemplateResponse for progressive generation
    template = _get_template_cached('tasklistdeveloper/detail_aktivitas_waktu_rilis.html')
    return template.render({
        "request": request,
        "waktu_rilis_data": rilis_data,
        "summary_pencapaian": str(avg_pencapaian),
        "month": month_name
    })

async def _generate_dev_support_data(request: Request, records: list, month_name: str, page_number: int = 1, total_pages: int = 1):
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

        # Handle datetime.date objects properly
        if start_date:
            formatted_start = str(start_date).replace('-', '/') if isinstance(start_date, str) else start_date.strftime('%Y/%m/%d')
        else:
            formatted_start = 'N/A'

        if end_date:
            formatted_end = str(end_date).replace('-', '/') if isinstance(end_date, str) else end_date.strftime('%Y/%m/%d')
        else:
            formatted_end = 'N/A'

        # Handle None pencapaian values
        pencapaian_str = str(pencapaian) if pencapaian is not None else "0"
        support_data.append({
            "no": i,
            "task_list": task_list,
            "requestor": requestor,
            "pic": pic,
            "status": status,
            "start_date": formatted_start,
            "end_date": formatted_end,
            "pencapaian": pencapaian_str
        })

    # Handle None values in pencapaian calculation
    valid_pencapaian = [record.get('Pencapaian', 0) for record in records if record.get('Pencapaian') is not None]
    total_pencapaian = sum(int(p) for p in valid_pencapaian if p != 0)
    avg_pencapaian = total_pencapaian // len(valid_pencapaian) if valid_pencapaian else 0

    # Render template as string instead of TemplateResponse for progressive generation
    template = _get_template_cached('tasklistdeveloper/detail_aktivitas_dukungan_support.html')
    return template.render({
        "request": request,
        "dukungan_support_data": support_data,
        "summary_pencapaian": str(avg_pencapaian),
        "month": month_name
    })

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
    template = _get_template_cached('tasklistiotoperation/detail_problem_pihak_kedua.html')
    return template.render({
        "request": request,
        "problem_data": problem_data,
        "month": month_name
    })

async def _generate_iot_aktivitas_page(request: Request, records: list, month_name: str):
    """Generate activities page from Fauzan's Tasklist Developer data"""

    from datetime import datetime
    from src.classes.ClsNocoDBProcessor import ClsNocoDBProcessor

    # Get tasklist developer data using NocoDB API like other sections
    table_id = config.NOCODB_TABLES.get("tasklist")
    if not table_id:
        print("DEBUG: No tasklist_iot table configured")
        aktivitas_data = []
    else:
        nocodb = ClsNocoDBProcessor(config.APP_BASE_ID, table_id)

        # Use Month filter and Unique_Key ending with _100 for Fauzan's tasks
        where_clause = f"(Month,eq,{month_name})~and(Status,eq,Closed)~and(Unique Key,like,%100)"
        response = nocodb.get_records(limit=2000, where=where_clause)
        records_data = response.get('list', []) if response else []

        aktivitas_data = []

        for i, task in enumerate(records_data, 1):
            # Get task fields (using correct column names)
            task_list = task.get('Task List', '')
            start_date = task.get('Start Date', '')
            end_date = task.get('End Date', '')
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

    # Render template
    template = _get_template_cached('tasklistiotoperation/detail_aktivitas_pihak_kedua.html')
    return template.render({
        "request": request,
        "aktivitas_data": aktivitas_data,
        "month": month_name
    })


# Cache for storing IoT respon data to avoid repeated database queries
_iot_respon_cache = {}

# Cache for storing employee data to avoid repeated NocoDB queries
_employee_cache = {}

# Template skeleton cache for faster rendering
_template_skeleton_cache = {}

def _get_template_cached(template_name: str):
    """Get cached template or parse and cache if new"""
    if template_name not in _template_skeleton_cache:
        template = templates.get_template(template_name)
        _template_skeleton_cache[template_name] = template
        print(f"DEBUG: Cached template {template_name}")
    else:
        template = _template_skeleton_cache[template_name]
        print(f"DEBUG: Using cached template {template_name}")
    return template

async def _get_employee_data_cached():
    """Get all employee data with caching"""
    cache_key = "all_employees"

    # Return cached data if available
    if cache_key in _employee_cache:
        print(f"DEBUG: Using cached employee data")
        return _employee_cache[cache_key]

    print(f"DEBUG: Fetching fresh employee data")

    try:
        employee_table = config.NOCODB_TABLES.get("employee_data")
        if not employee_table:
            return {}

        nocodb_employee = ClsNocoDBProcessor(config.APP_BASE_ID, employee_table)
        all_employees = nocodb_employee.get_all_employees()

        # Cache the employee data
        _employee_cache[cache_key] = all_employees
        print(f"DEBUG: Cached {len(all_employees)} employee records")

        return all_employees
    except Exception as e:
        print(f"Error fetching employee data: {e}")
        return {}

def _filter_employees_by_type(all_employees: dict, report_type: str):
    """Filter employees based on report type without additional database queries"""
    employee_mapping = {}

    if report_type == "iotoperation":
        # For IoT Operations, include specific NRPs and IoT Operations role
        for name, info in all_employees.items():
            employee_role = info.get('role', '').strip()
            employee_nrp = info.get('nrp', '').strip()
            # Include if they have IoT Operations role OR are specific NRPs
            if (employee_role == "IoT Operations" or
                employee_nrp in ["JIMT24011", "JIMT24012", "JIMT24001"]):
                employee_mapping[name] = info
    elif report_type == "developer":
        # For Developer, exclude NRPs that are already in IoT Operations
        excluded_nrps = ["JIMT24011", "JIMT24012", "JIMT24001"]
        for name, info in all_employees.items():
            employee_role = info.get('role', '').strip()
            employee_nrp = info.get('nrp', '').strip()
            if employee_role == "Developer" and employee_nrp not in excluded_nrps:
                employee_mapping[name] = info
    else:
        employee_mapping = all_employees.copy()

    return employee_mapping

async def _get_employee_data_cached():
    """Get all employee data with caching"""
    cache_key = "all_employees"

    # Return cached data if available
    if cache_key in _employee_cache:
        print(f"DEBUG: Using cached employee data")
        return _employee_cache[cache_key]

    print(f"DEBUG: Fetching fresh employee data")

    try:
        employee_table = config.NOCODB_TABLES.get("employee_data")
        if not employee_table:
            return {}

        nocodb_employee = ClsNocoDBProcessor(config.APP_BASE_ID, employee_table)
        all_employees = nocodb_employee.get_all_employees()

        # Cache the employee data
        _employee_cache[cache_key] = all_employees
        print(f"DEBUG: Cached {len(all_employees)} employee records")

        return all_employees
    except Exception as e:
        print(f"Error fetching employee data: {e}")
        return {}

async def _get_iot_respon_data_cached(month_name: str):
    """Get all IoT respon data for the month with caching"""
    import psycopg2
    from datetime import datetime

    cache_key = f"iot_respon_{month_name}_{datetime.now().year}"

    # Return cached data if available
    if cache_key in _iot_respon_cache:
        print(f"DEBUG: Using cached data for {cache_key}")
        return _iot_respon_cache[cache_key]

    print(f"DEBUG: Fetching fresh data for {cache_key}")

    try:
        # Connect to PostgreSQL with retry logic
        import time
        max_retries = 3
        retry_delay = 2
        conn = None
        for attempt in range(max_retries):
            try:
                conn = psycopg2.connect(config.DB_URL)
                cursor = conn.cursor()
                break
            except psycopg2.OperationalError as e:
                if "recovery mode" in str(e).lower() and attempt < max_retries - 1:
                    print(f"Database in recovery mode, retrying in {retry_delay}s... (attempt {attempt + 1}/{max_retries})")
                    time.sleep(retry_delay)
                    retry_delay *= 2
                else:
                    raise e
        if conn is None:
            raise Exception("Failed to connect to database after all retries")

        # Convert month_name to year-month pattern for filtering
        month_mapping = {
            'Januari': 1, 'Februari': 2, 'Maret': 3, 'April': 4,
            'Mei': 5, 'Juni': 6, 'Juli': 7, 'Agustus': 8,
            'September': 9, 'Oktober': 10, 'November': 11, 'Desember': 12
        }
        month_num = month_mapping.get(month_name, 1)
        current_year = datetime.now().year

        # Get all records for the month
        cursor.execute("""
        SELECT
            problem,
            tanggal_problem,
            waktu_problem,
            tanggal_respon,
            waktu_respon,
            tanggal_penyelesaian,
            waktu_penyelesaian,
            pic_pama,
            engineer_managed_service,
            sla_waktu_respon,
            aktual_waktu_respon,
            sla_waktu_penyelesaian,
            aktual_waktu_penyelesaian,
            respon_achievement,
            penyelesaian_achievement
        FROM public.vw_sla_iot_operations
        WHERE tanggal_problem LIKE %s
        ORDER BY id
        """, (f"{current_year}/{month_num:02d}/%",))

        all_rows = cursor.fetchall()
        cursor.close()
        conn.close()

        print(f"DEBUG: Query executed successfully. Found {len(all_rows)} total rows for {month_name} {current_year}")

        # Process all data and calculate statistics
        processed_data = []
        total_respon_achievement_all = 0
        total_penyelesaian_achievement_all = 0

        for i, row in enumerate(all_rows):
            (problem, tanggal_problem, waktu_problem, tanggal_respon, waktu_respon,
             tanggal_penyelesaian, waktu_penyelesaian, pic_pama, engineer_managed_service,
             sla_waktu_respon, aktual_waktu_respon, sla_waktu_penyelesaian,
             aktual_waktu_penyelesaian, respon_achievement, penyelesaian_achievement) = row

            # Calculate for overall statistics
            total_respon_achievement_all += (respon_achievement or 0)
            total_penyelesaian_achievement_all += (penyelesaian_achievement or 0)

            # Convert times to integers/strings as needed
            waktu_respon_menit = int(round(float(aktual_waktu_respon or 0)))
            waktu_penyelesaian_menit = int(round(float(aktual_waktu_penyelesaian or 0)))

            # Process data for template format
            processed_data.append({
                "problem": problem or 'No Description',
                "tanggal_problem": tanggal_problem or '',
                "waktu_problem": str(waktu_problem or '').split('.')[0],
                "tanggal_respon": tanggal_respon or '',
                "tanggal_penyelesaian": tanggal_penyelesaian or '',
                "waktu_penyelesaian": str(waktu_penyelesaian or '').split('.')[0],
                "pic_pama": pic_pama or 'N/A',
                "engineer": engineer_managed_service or 'N/A',
                "waktu_respon_menit": waktu_respon_menit,
                "aktual_waktu_1": waktu_respon_menit,
                "aktual_waktu_2": waktu_penyelesaian_menit,
                "aktual_waktu_3": sla_waktu_respon or 0,
                "aktual_waktu_4": sla_waktu_penyelesaian or 0,
                "performance_respon_1": (respon_achievement or 0) * 100,
                "performance_respon_2": 100 if (aktual_waktu_respon or 0) <= (sla_waktu_respon or 0) else 0,
                "performance_penyelesaian_1": (penyelesaian_achievement or 0) * 100,
                "performance_penyelesaian_2": 100 if (aktual_waktu_penyelesaian or 0) <= (sla_waktu_penyelesaian or 0) else 0
            })

        # Calculate summary percentages
        overall_summary_percentage = 0.0
        if len(all_rows) > 0:
            avg_respon = (total_respon_achievement_all / len(all_rows)) * 100
            avg_penyelesaian = (total_penyelesaian_achievement_all / len(all_rows)) * 100
            overall_summary_percentage = round((avg_respon + avg_penyelesaian) / 2, 1)

        # Cache the processed data
        cached_data = {
            "all_data": processed_data,
            "total_records": len(all_rows),
            "overall_summary_percentage": overall_summary_percentage
        }
        _iot_respon_cache[cache_key] = cached_data

        return cached_data

    except Exception as e:
        print(f"Error fetching IoT respon data: {e}")
        return {"all_data": [], "total_records": 0, "overall_summary_percentage": 0.0}

async def _generate_iot_respon_page(request: Request, records: list, month_name: str, page_number: int = 1, total_pages: int = 1):
    """Generate response time page using cached data with efficient pagination"""
    try:
        # Get cached data
        cached_data = await _get_iot_respon_data_cached(month_name)
        all_data = cached_data["all_data"]
        total_records = cached_data["total_records"]
        overall_summary_percentage = cached_data["overall_summary_percentage"]

        # Apply pagination to cached data
        items_per_page = 50
        start_idx = (page_number - 1) * items_per_page
        end_idx = start_idx + items_per_page
        paginated_data = all_data[start_idx:end_idx]

        print(f"DEBUG: Pagination from cache - showing rows {start_idx+1} to {min(end_idx, total_records)} of {total_records}")

        # Add sequential numbering for the page
        respon_data = []
        for i, item in enumerate(paginated_data):
            item_copy = item.copy()
            item_copy["no"] = start_idx + i + 1  # Global numbering
            respon_data.append(item_copy)

        # Only show summary on the last page
        summary_percentage = None
        if page_number == total_pages and total_records > 0:
            summary_percentage = f"{overall_summary_percentage:.1f}"

        print(f"DEBUG: Summary percentage for page {page_number}/{total_pages}: {summary_percentage}")

        # Render template using cache for faster performance
        template = _get_template_cached('tasklistiotoperation/detail_respon_resolution_time.html')
        return template.render({
            "request": request,
            "respon_data": respon_data,
            "summary_percentage": summary_percentage,
            "month": month_name
        })

    except Exception as e:
        print(f"Error generating IoT respon page: {e}")
        # Fallback to empty data
        template = _get_template_cached('tasklistiotoperation/detail_respon_resolution_time.html')
        return template.render({
            "request": request,
            "respon_data": [],
            "summary_percentage": None,
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
    where_clause = f"(Unique Key,like,{year_month_pattern}%)~and(Evidence Task,notnull)"
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
        "year": gen_data.get("year", 2026),
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

        # Tasklist sections with pagination
        if type == "iotoperation":
            # Apply pagination to IoT tasklist too
            iot_tasklists = [
                {"base_title": "IoT Operations - Problem Report", "section_type": "problem", "base_num": "2.1"},
                {"base_title": "IoT Operations - Aktivitas Report", "section_type": "aktivitas", "base_num": "2.2"},
                {"base_title": "IoT Operations - Respon Resolution Time", "section_type": "respon", "base_num": "2.3"}
            ]

            for tasklist in iot_tasklists:
                # Calculate pages for IoT tasklist (use same logic as developer)
                page_count = await _calculate_iot_tasklist_pages(tasklist["section_type"], month)

                if page_count == 0:
                    # No data, create single empty section
                    sections_plan.append({
                        "id": section_id,
                        "type": "tasklist",
                        "title": f"{tasklist['base_num']} {tasklist['base_title']}",
                        "section_type": tasklist["section_type"],
                        "status": "pending"
                    })
                    section_id += 1
                else:
                    # Create multiple sections for pagination
                    for page_num in range(1, page_count + 1):
                        page_suffix = f" (Halaman {page_num})" if page_count > 1 else ""
                        title = f"{tasklist['base_num']}.{page_num} {tasklist['base_title']}{page_suffix}"

                        sections_plan.append({
                            "id": section_id,
                            "type": "tasklist",
                            "title": title,
                            "section_type": tasklist["section_type"],
                            "page_number": page_num,
                            "total_pages": page_count,
                            "status": "pending"
                        })
                        section_id += 1
        else:
            # For developer, calculate pagination for each task list
            developer_tasklists = [
                {"base_title": "Detail Aktivitas Kualitas Kode", "section_type": "kualitas", "base_num": "2.1"},
                {"base_title": "Detail Aktivitas Waktu Rilis", "section_type": "waktu", "base_num": "2.2"},
                {"base_title": "Detail Aktivitas Dukungan Support", "section_type": "dukungan", "base_num": "2.3"}
            ]

            for tasklist in developer_tasklists:
                # Calculate how many pages needed for this task list
                page_count = await _calculate_tasklist_pages(tasklist["section_type"], month)

                if page_count == 0:
                    # No data, create single empty section
                    sections_plan.append({
                        "id": section_id,
                        "type": "tasklist",
                        "title": f"{tasklist['base_num']} {tasklist['base_title']}",
                        "section_type": tasklist["section_type"],
                        "status": "pending"
                    })
                    section_id += 1
                else:
                    # Create multiple sections for pagination
                    for page_num in range(1, page_count + 1):
                        page_suffix = f" (Halaman {page_num})" if page_count > 1 else ""
                        title = f"{tasklist['base_num']}.{page_num} {tasklist['base_title']}{page_suffix}"

                        sections_plan.append({
                            "id": section_id,
                            "type": "tasklist",
                            "title": title,
                            "section_type": tasklist["section_type"],
                            "page_number": page_num,
                            "total_pages": page_count,
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

        # Convert month number to Indonesian month name
        month_names = {
            1: 'Januari', 2: 'Februari', 3: 'Maret', 4: 'April',
            5: 'Mei', 6: 'Juni', 7: 'Juli', 8: 'Agustus',
            9: 'September', 10: 'Oktober', 11: 'November', 12: 'Desember'
        }
        month_name = month_names.get(int(month), 'Januari')

        evidence_records = await _get_all_evidence_records(evidence_type_param, month_name)
        
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


async def _calculate_tasklist_pages(section_type: str, month: int) -> int:
    """Calculate how many pages needed for a specific task list type - filtered by month"""
    try:
        table_id = config.NOCODB_TABLES.get("tasklist")
        if not table_id:
            return 0

        nocodb = ClsNocoDBProcessor(config.APP_BASE_ID, table_id)
        kategori_mapping = {
            "kualitas": "Detail Aktivitas Kualitas Kode",
            "waktu": "Detail Aktivitas Waktu Rilis Fitur",
            "dukungan": "Detail Aktivitas Dukungan Support"
        }

        kategori_name = kategori_mapping.get(section_type)
        if not kategori_name:
            return 0

        # Convert month number to Indonesian month name for filtering
        month_names = {
            1: 'Januari', 2: 'Februari', 3: 'Maret', 4: 'April',
            5: 'Mei', 6: 'Juni', 7: 'Juli', 8: 'Agustus',
            9: 'September', 10: 'Oktober', 11: 'November', 12: 'Desember'
        }
        month_name = month_names.get(month, 'Januari')

        # Filter by month using Indonesian month name
        where_clause = f"(Month,eq,{month_name})~and(Kategori,eq,{kategori_name})~and(Status,eq,Closed)"
        response = nocodb.get_records(limit=2000, where=where_clause)
        records = response.get('list', []) if response else []

        if not records:
            return 0

        # Calculate pages needed (max 10 items per page)
        items_per_page = 10
        total_items = len(records)
        total_pages = (total_items + items_per_page - 1) // items_per_page  # Ceiling division

        return total_pages

    except Exception as e:
        print(f"Error calculating pages for {section_type}: {e}")
        return 1  # Default to 1 page if error


async def _calculate_iot_tasklist_pages(section_type: str, month: int) -> int:
    """Calculate how many pages needed for IoT task list type - based on data availability"""
    try:
        if section_type == "problem":
            return 1
            
        if section_type == "respon":
            # Use PostgreSQL view for respon section
            import psycopg2
            from datetime import datetime

            conn = psycopg2.connect(config.DB_URL)
            cursor = conn.cursor()

            current_year = datetime.now().year

            # Count records from PostgreSQL view
            cursor.execute("""
            SELECT COUNT(*) FROM public.vw_sla_iot_operations
            WHERE tanggal_problem LIKE %s
            """, (f"{current_year}/{month:02d}/%",))

            count = cursor.fetchone()[0]
            cursor.close()
            conn.close()

            if count == 0:
                return 0

            # Calculate pages needed (max 10 items per page)
            items_per_page = 50
            total_pages = (count + items_per_page - 1) // items_per_page
            return total_pages

        else:
            

            # Apply same filtering logic as the actual generation functions
            if section_type == "aktivitas":
                # For "problem" and "aktivitas", use IoT tasklist data with section-specific logic
                table_id = config.NOCODB_TABLES.get("tasklist")
                if not table_id:
                    return 0

                nocodb = ClsNocoDBProcessor(config.APP_BASE_ID, table_id)

                # Convert month to Indonesian name
                month_names = {
                    1: 'Januari', 2: 'Februari', 3: 'Maret', 4: 'April',
                    5: 'Mei', 6: 'Juni', 7: 'Juli', 8: 'Agustus',
                    9: 'September', 10: 'Oktober', 11: 'November', 12: 'Desember'
                }
                month_name = month_names.get(month, 'Januari')
                # Same filter as _generate_iot_aktivitas_page - Fauzan's tasks with _100
                where_clause = f"(Month,eq,{month_name})~and(Status,eq,Closed)~and(Unique Key,like,%100)"
            else:  # problem section
                # For "problem" and "aktivitas", use IoT tasklist data with section-specific logic
                table_id = config.NOCODB_TABLES.get("tasklist_iot")
                if not table_id:
                    return 0

                nocodb = ClsNocoDBProcessor(config.APP_BASE_ID, table_id)

                # Convert month to Indonesian name
                month_names = {
                    1: 'Januari', 2: 'Februari', 3: 'Maret', 4: 'April',
                    5: 'Mei', 6: 'Juni', 7: 'Juli', 8: 'Agustus',
                    9: 'September', 10: 'Oktober', 11: 'November', 12: 'Desember'
                }
                month_name = month_names.get(month, 'Januari')
                # Use general IoT tasklist data filter for problem section
                where_clause = f"(Month,eq,{month_name})~and(Status,eq,Closed)"

            response = nocodb.get_records(limit=2000, where=where_clause)
            records = response.get('list', []) if response else []

            if not records:
                return 0

            # Calculate pages needed (max 10 items per page)
            items_per_page = 50
            total_items = len(records)
            total_pages = (total_items + items_per_page - 1) // items_per_page

            return total_pages

    except Exception as e:
        print(f"Error calculating IoT pages for {section_type}: {e}")
        return 1  # Default to 1 page if error


async def _generate_section_logic(request: Request, section_id: int, plan_id: str):
    """Logic to generate a specific section by ID"""
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
            # Pass pagination info if available
            page_number = section.get("page_number", 1)
            total_pages = section.get("total_pages", 1)

            section_content = await _generate_single_tasklist_section(
                section["section_type"], plan["month"], plan["type"], request, page_number, total_pages
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

@app.post("/api/generate/section")
async def generate_section(
    request: Request,
    section_id: int = Form(...),
    plan_id: str = Form(...)
):
    """Generate a specific section by ID"""
    return await _generate_section_logic(request, section_id, plan_id)

@app.post("/api/stream/section")
async def stream_section_to_session(request: Request):
    """Store completed section in server session (avoid localStorage overflow)"""
    try:
        data = await request.json()
        plan_id = data["plan_id"]
        section = data["section"]

        # Initialize session storage for report sections
        if "report_sections" not in request.session:
            request.session["report_sections"] = {}

        if plan_id not in request.session["report_sections"]:
            request.session["report_sections"][plan_id] = []

        # Store section without overwhelming localStorage
        request.session["report_sections"][plan_id].append({
            "type": section["type"],
            "title": section["title"],
            "content": section["content"],
            "timestamp": data.get("timestamp", "")
        })

        return {"success": True, "stored_count": len(request.session["report_sections"][plan_id])}

    except Exception as e:
        print(f"Error streaming section to session: {e}")
        return {"success": False, "error": str(e)}


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
        return await _generate_section_logic(request, section_id, plan_id)

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
        # Use cached employee data
        all_employees = await _get_employee_data_cached()
        employee_mapping = _filter_employees_by_type(all_employees, report_type)

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


async def _generate_single_tasklist_section(section_type: str, month: int, report_type: str, request: Request, page_number: int = 1, total_pages: int = 1):
    """Generate single tasklist section"""
    try:
        if report_type == "iotoperation":
            html_content = await _get_iot_tasklist_html_content(month, section_type, request, page_number, total_pages)
        else:
            html_content = await _get_developer_tasklist_html_content(month, section_type, request, page_number, total_pages)

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
        # Use cached employee data
        all_employees = await _get_employee_data_cached()
        employee_mapping = _filter_employees_by_type(all_employees, report_type)

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


async def _get_iot_tasklist_html_content(month: int, section_type: str, request: Request, page_number: int = 1, total_pages: int = 1):
    """Generate IoT tasklist HTML content for specific section with pagination"""
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
        # Use Month filtering to be consistent with pagination calculation
        where_clause = f"(Month,eq,{month_name})~and(Status,eq,Closed)"
        response = nocodb.get_records(limit=2000, where=where_clause)
        raw_records = response.get('list', []) if response else []

        # Additional validation to filter out N/A, empty, or null dates
        records = []
        for record in raw_records:
            start_date_val = record.get('Start Date', '')
            end_date_val = record.get('End Date', '')

            # Skip if dates are empty, N/A, null, or contain invalid values
            if (start_date_val and end_date_val and
                str(start_date_val).strip() not in ['', 'N/A', 'null', 'None'] and
                str(end_date_val).strip() not in ['', 'N/A', 'null', 'None']):
                records.append(record)

        # Apply pagination to records
        items_per_page = 50
        start_idx = (page_number - 1) * items_per_page
        end_idx = start_idx + items_per_page
        paginated_records = records[start_idx:end_idx]

        if section_type == "problem":
            return await _generate_iot_problem_page(request, paginated_records, month_name)
        elif section_type == "aktivitas":
            return await _generate_iot_aktivitas_page(request, paginated_records, month_name)
        else:  # respon
            return await _generate_iot_respon_page(request, paginated_records, month_name, page_number, total_pages)

    except Exception as e:
        print(f"Error generating IoT tasklist section {section_type}: {e}")
        return ""


async def _get_developer_tasklist_html_content(month: int, section_type: str, request: Request, page_number: int = 1, total_pages: int = 1):
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

        # Use NocoDB API
        table_id = config.NOCODB_TABLES.get("tasklist")
        if not table_id:
            print(f"❌ No table ID found for tasklist in config: {config.NOCODB_TABLES}")
            return ""

        nocodb = ClsNocoDBProcessor(config.APP_BASE_ID, table_id)

        kategori_mapping = {
            "kualitas": "Detail Aktivitas Kualitas Kode",
            "waktu": "Detail Aktivitas Waktu Rilis Fitur",
            "dukungan": "Detail Aktivitas Dukungan Support "
        }

        kategori_name = kategori_mapping.get(section_type)
        if not kategori_name:
            return ""

        # Use Month filtering for month-specific records with NocoDB syntax
        where_clause = f"(Month,eq,{month_name})~and(Kategori,eq,{kategori_name})~and(Status,eq,Closed)~and(Start Date,notnull)~and(End Date,notnull)"
        response = nocodb.get_records(limit=2000, where=where_clause)
        raw_records = response.get('list', []) if response else []

        # Additional validation to filter out N/A, empty, or null dates
        records = []
        for record in raw_records:
            start_date_val = record.get('Start Date', '')
            end_date_val = record.get('End Date', '')

            # Skip if dates are empty, N/A, null, or contain invalid values
            if (start_date_val and end_date_val and
                str(start_date_val).strip() not in ['', 'N/A', 'null', 'None'] and
                str(end_date_val).strip() not in ['', 'N/A', 'null', 'None']):
                records.append(record)

        # Apply pagination to records
        items_per_page = 10
        start_idx = (page_number - 1) * items_per_page
        end_idx = start_idx + items_per_page
        paginated_records = records[start_idx:end_idx]

        # Renumber items for the page (adjust numbering)
        for i, record in enumerate(paginated_records):
            record['_page_number'] = start_idx + i + 1  # Global numbering

        if section_type == "kualitas":
            return await _generate_dev_kualitas_data(request, paginated_records, month_name, page_number, total_pages)
        elif section_type == "waktu":
            return await _generate_dev_rilis_data(request, paginated_records, month_name, page_number, total_pages)
        elif section_type == "dukungan":
            return await _generate_dev_support_data(request, paginated_records, month_name, page_number, total_pages)

        return ""

    except Exception as e:
        print(f"Error generating Developer tasklist section {section_type}: {e}")
        import traceback
        traceback.print_exc()
        return ""


async def _get_timesheet_html_sections(month: int, year: int, report_type: str, request: Request):
    """Get timesheet HTML for each employee separately, filtered by role"""
    # Use cached employee data
    all_employees = await _get_employee_data_cached()
    employee_mapping = _filter_employees_by_type(all_employees, report_type)

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
    """Get Developer tasklist HTML sections with pagination for task lists"""
    html_sections = []
    pages = ["pelaksanaan", "kualitas", "rilis", "support"]

    for page in pages:
        try:
            if page == "pelaksanaan":
                # Handle pelaksanaan normally (no pagination needed)
                dev_html = await _call_developer_endpoint(page, month, request)
                if dev_html:
                    title = "Pelaksanaan Pekerjaan"
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
            else:
                # Handle task lists (kualitas, rilis, support) with pagination
                paginated_sections = await _get_paginated_tasklist_sections(page, month, request)
                html_sections.extend(paginated_sections)

        except Exception as e:
            pass

    return html_sections

async def _get_paginated_tasklist_sections(page: str, month: int, request: Request):
    """Get paginated task list sections - max 10 items per page"""
    sections = []

    # Get data directly from nocodb instead of HTML
    table_id = config.NOCODB_TABLES.get("tasklist")
    if not table_id:
        return sections

    nocodb = ClsNocoDBProcessor(config.APP_BASE_ID, table_id)
    kategori_mapping = {
        "kualitas": "Detail Aktivitas Kualitas Kode",
        "rilis": "Detail Aktivitas Waktu Rilis Fitur",
        "support": "Detail Aktivitas Dukungan Support"
    }

    kategori_name = kategori_mapping.get(page)
    if not kategori_name:
        return sections

    # Convert month number to Indonesian month name for filtering
    month_names = {
        1: 'Januari', 2: 'Februari', 3: 'Maret', 4: 'April',
        5: 'Mei', 6: 'Juni', 7: 'Juli', 8: 'Agustus',
        9: 'September', 10: 'Oktober', 11: 'November', 12: 'Desember'
    }
    month_name = month_names.get(month, 'Januari')

    # Filter by month using Indonesian month name
    where_clause = f"(Month,eq,{month_name})~and(Kategori,eq,{kategori_name})~and(Status,eq,Closed)"
    response = nocodb.get_records(limit=2000, where=where_clause)
    records = response.get('list', []) if response else []

    if not records:
        return sections

    # Process data similar to original functions
    task_data = []
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

        task_data.append({
            "no": i,
            "task_list": task_list,
            "requestor": requestor,
            "pic": pic,
            "status": status,
            "start_date": formatted_start,
            "end_date": formatted_end,
            "pencapaian": str(pencapaian)
        })

    # Calculate summary
    valid_pencapaian = [record.get('Pencapaian', 0) for record in records if record.get('Pencapaian') is not None]
    total_pencapaian = sum(int(p) for p in valid_pencapaian if p != 0)
    avg_pencapaian = total_pencapaian // len(valid_pencapaian) if valid_pencapaian else 0

    # Split data into pages with max 10 items per page
    items_per_page = 10
    total_items = len(task_data)
    total_pages = (total_items + items_per_page - 1) // items_per_page

    # Base section numbering
    base_numbers = {
        "kualitas": "2.1",
        "rilis": "2.2",
        "support": "2.3"
    }

    base_num = base_numbers.get(page, "2.1")

    # Template mapping
    template_mapping = {
        "kualitas": "tasklistdeveloper/detail_aktivitas_kualitas_kode.html",
        "rilis": "tasklistdeveloper/detail_aktivitas_waktu_rilis.html",
        "support": "tasklistdeveloper/detail_aktivitas_dukungan_support.html"
    }

    template_name = template_mapping.get(page)
    if not template_name:
        return sections

    data_key_mapping = {
        "kualitas": "kualitas_kode_data",
        "rilis": "waktu_rilis_data",
        "support": "dukungan_support_data"
    }

    data_key = data_key_mapping.get(page)

    for page_num in range(1, total_pages + 1):
        start_idx = (page_num - 1) * items_per_page
        end_idx = min(start_idx + items_per_page, total_items)
        page_data = task_data[start_idx:end_idx]

        # Show summary only on the last page
        show_summary = (page_num == total_pages)
        summary_data = avg_pencapaian if show_summary else ""

        template = templates.get_template(template_name)
        template_data = {
            "request": request,
            data_key: page_data,
            "summary_pencapaian": str(summary_data),
            "month": ["", "Januari", "Februari", "Maret", "April", "Mei", "Juni",
                     "Juli", "Agustus", "September", "Oktober", "November", "Desember"][month]
        }

        page_html = template.render(template_data)

        # Extract body content
        body_content = re.search(r'<body[^>]*>(.*?)</body>', page_html, re.DOTALL)
        if body_content:
            isolated_content = f'<div class="dev-tasklist-section">{body_content.group(1)}</div>'
        else:
            isolated_content = f'<div class="dev-tasklist-section">{page_html}</div>'

        sections.append({
            'type': f'dev_{page}',
            'title': f'{base_num}.{page_num} {kategori_name}' + (f' (Halaman {page_num})' if total_pages > 1 else ''),
            'content': isolated_content
        })

    return sections

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
    where_clause = f"(Unique Key,like,{year_month_pattern}%)~and(Status,eq,Closed)"
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
            "rilis": "Detail Aktivitas Waktu Rilis Fitur",
            "support": "Detail Aktivitas Dukungan Support "
        }
        kategori_name = kategori_mapping.get(page)
        if not kategori_name:
            return ""

        # Use Unique_Key filtering for month-specific records
        current_year = datetime.now().year
        year_month_pattern = f"{current_year}-{month:02d}-"
        where_clause = f"(Unique Key,like,{year_month_pattern}%)~and(Kategori,eq,{kategori_name})~and(Status,eq,Closed)"
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
    where_clause = f"(Unique_Key,like,{year_month_pattern}%)~and(Evidence_Task,notnull)"
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
    # Use cached employee data
    all_employees = await _get_employee_data_cached()
    employee_mapping = _filter_employees_by_type(all_employees, report_type)

    # Setup attendance data processor
    attendance_table = config.NOCODB_TABLES.get("attendance")
    nocodb_attendance = ClsNocoDBProcessor(config.APP_BASE_ID, attendance_table)
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

    # Get employee list for filter dropdown using cache
    employee_mapping = await _get_employee_data_cached()
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

    # Get employee list for filter dropdown using cache
    employee_mapping = await _get_employee_data_cached()
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

    # Get same data as dashboard using cache
    employee_mapping = await _get_employee_data_cached()
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
    where_clause = f"(Unique Key,like,{year_month_pattern}%)~and(Evidence Task,notnull)"
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
@app.get("/testresponresolutiontime")
async def test_response_resolution_time(request: Request):
    """Test endpoint for response resolution time with 50 dummy rows"""
    # Generate 50 dummy data rows
    respon_data = []
    for i in range(1, 51):
        respon_data.append({
            'no': i,
            'problem': f'Pengecekan sistem monitoring server aplikasi dan database utama untuk memastikan performa optimal - Issue {i}',
            'tanggal_problem': '2026/02/01',
            'waktu_problem': f'{10 + (i % 12):02d}:57:00',
            'tanggal_respon': '2026/02/01',
            'tanggal_penyelesaian': '2026/02/01',
            'waktu_penyelesaian': f'{10 + (i % 12):02d}:59:00',
            'pic_pama': 'Bagas Eko Prasetyo',
            'engineer': 'Titin Ervina Sari',
            'waktu_respon_menit': i % 5 + 1,
            'aktual_waktu_1': i % 3 + 1,
            'aktual_waktu_2': i % 4 + 1,
            'aktual_waktu_3': i % 2 + 1,
            'aktual_waktu_4': i % 6 + 1,
            'performance_respon_1': 144 - (i % 20),
            'performance_respon_2': 100 + (i % 10),
            'performance_penyelesaian_1': 100 - (i % 15),
            'performance_penyelesaian_2': 100 + (i % 5)
        })

    # Render the response resolution time template
    template = _get_template_cached('tasklistiotoperation/detail_respon_resolution_time.html')
    response_content = template.render({
        "request": request,
        "respon_data": respon_data,
        "summary_percentage": 99.8
    })

    # Use report_editor container but with our response content
    html_sections = [{
        'type': 'tasklist',
        'title': 'Test Response Resolution Time (50 Rows)',
        'content': response_content
    }]

    return templates.TemplateResponse('report_editor.html', {
        "request": request,
        "html_sections": html_sections,
        "logo_pama_url": '/admin/static/img/logo_pama.png',
        "logo_celerates_url": '/admin/static/img/logo_celerates.jpg'
    })

@app.on_event("startup")
async def startup_event():
    """Initialize database and other startup tasks"""
    try:
        init_db()
        print("✅ Database initialized successfully")

        # Pre-warm caches for faster response times
        print("🔥 Pre-warming caches...")

        # Pre-load employee data
        await _get_employee_data_cached()

        # Pre-load current month IoT respon data
        from datetime import datetime
        current_month = datetime.now().strftime('%B')
        await _get_iot_respon_data_cached(current_month)

        print("✅ Cache pre-warming completed")

    except Exception as e:
        print(f"❌ Startup initialization failed: {e}")
        # Don't fail startup for cache errors, just log them
        if "Database initialization failed" in str(e):
            raise

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=False, timeout_keep_alive=1800)
