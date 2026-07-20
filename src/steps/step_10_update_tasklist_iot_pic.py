import time
from datetime import datetime

import psycopg2

from src import config

TARGET_HOUR = 1  # scheduler runs every 2 hours; only actually calls the procedure when it lands on this hour, so it fires once/day


def run():
    print("Executing Step 10: Update Tasklist IoT PIC")

    if datetime.now().hour != TARGET_HOUR:
        print(f"Skipping: only runs at {TARGET_HOUR:02d}:00 (server time), current hour is {datetime.now().hour:02d}:00")
        return

    max_retries = 3
    retry_delay = 2
    conn = None
    for attempt in range(max_retries):
        try:
            conn = psycopg2.connect(config.DB_URL)
            break
        except psycopg2.OperationalError as e:
            if "recovery mode" in str(e).lower() and attempt < max_retries - 1:
                print(f"Database in recovery mode, retrying in {retry_delay}s... (attempt {attempt + 1}/{max_retries})")
                time.sleep(retry_delay)
                retry_delay *= 2
            else:
                raise
    if conn is None:
        raise Exception("Failed to connect to database after all retries")

    try:
        cursor = conn.cursor()
        cursor.execute("CALL public.sp_update_tasklist_iot_pic();")
        conn.commit()
        cursor.close()
        print("sp_update_tasklist_iot_pic() executed successfully")
    finally:
        conn.close()
