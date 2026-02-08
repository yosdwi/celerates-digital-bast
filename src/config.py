import os
import json
from dotenv import load_dotenv
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIGS_DIR = BASE_DIR / "configs"
DATABASE_DIR = BASE_DIR / "database"
QUERIES_PATH = DATABASE_DIR / "queries"

load_dotenv(BASE_DIR / ".env")

APP_BASE_ID = os.getenv("APP_BASE_ID")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
MAX_RETRIES = int(os.getenv("MAX_RETRIES", 3))
RETRY_DELAY_SECONDS = int(os.getenv("RETRY_DELAY_SECONDS", 10))

DB_URL = os.getenv("DB_URL")
DB_SERVER = os.getenv("DB_SERVER")
DB_USERNAME = os.getenv("DB_USERNAME")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_NAME = os.getenv("DB_NAME")

REDMINE_DB_SERVER = os.getenv("REDMINE_DB_SERVER")
REDMINE_DB_USERNAME = os.getenv("REDMINE_DB_USERNAME")
REDMINE_DB_PASSWORD = os.getenv("REDMINE_DB_PASSWORD")
REDMINE_DB_NAME = os.getenv("REDMINE_DB_NAME")

NOCODB_BASE_URL = os.getenv("NOCODB_BASE_URL")
NOCODB_API_TOKEN = os.getenv("NOCODB_API_TOKEN")

RESEND_API_KEY = os.getenv("RESEND_API_KEY")
HEALTHCHECK_URL = os.getenv("HEALTHCHECK_URL")

SHEETS_CREDENTIALS_PATH = os.getenv("SHEETS_CREDENTIALS_PATH")
TIMESHEET_URL = os.getenv("TIMESHEET_URL")
ATTENDANCE_SHEET_URL = os.getenv("ATTENDANCE_SHEET_URL")
SCHEDULE_SHIFTING_URL = os.getenv("SCHEDULE_SHIFTING_URL")

def _load_json_config(filename: str):
    path = CONFIGS_DIR / filename
    try:
        with open(path, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None

GENERAL_CONFIG = _load_json_config("config.json")
NOCODB_TABLES = _load_json_config("nocodb_tables.json")
SHEET_MAPPING = _load_json_config("sheetmapping.json")
ATTENDANCE_SHEET_MAPPING = _load_json_config("attendancesheetmapping.json")
TIMESHEET_OPTIONS = _load_json_config("timesheet_options.json")
TASKLIST_IOT = _load_json_config("tasklist-iot.json")

def check_required_variables():
    required_vars = [
        "DB_SERVER", "DB_USERNAME", "DB_PASSWORD",
        "REDMINE_DB_SERVER", "REDMINE_DB_USERNAME", "REDMINE_DB_PASSWORD",
        "NOCODB_BASE_URL", "NOCODB_API_TOKEN", "APP_BASE_ID",
        "SHEETS_CREDENTIALS_PATH", "TIMESHEET_URL", "ATTENDANCE_SHEET_URL", "SCHEDULE_SHIFTING_URL"
    ]
    missing_vars = [var for var in required_vars if not globals().get(var)]
    if missing_vars:
        raise ValueError(f"Missing required environment variables: {', '.join(missing_vars)}")
