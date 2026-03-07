import psycopg2
import psycopg2.extras
from datetime import datetime
import logging
from src import config

def run():
    """
    Langkah 5: Generate Unique_Key untuk semua table yang membutuhkan
    Format: {Date}_{Employee_ID}

    Tables:
    - Schedule Shifting
    - Tasklist IoT Operations
    - Tasklist Developer
    - Attendance
    - Timesheet
    """
    print("Menjalankan Langkah 5: Generate Unique Keys")

    try:
        # Connect to PostgreSQL
        conn = psycopg2.connect(config.DB_URL)
        cursor = conn.cursor()

        total_updated = 0

        # 1. Update Schedule Shifting
        print("Processing Schedule Shifting...")
        updated_count = update_schedule_shifting_unique_key(cursor)
        total_updated += updated_count
        print(f"  Updated {updated_count} Schedule Shifting records")

        # 2. Update Tasklist IoT Operations
        print("Processing Tasklist IoT Operations...")
        updated_count = update_tasklist_iot_unique_key(cursor)
        total_updated += updated_count
        print(f"  Updated {updated_count} Tasklist IoT Operations records")

        # 3. Update Tasklist Developer
        print("Processing Tasklist Developer...")
        updated_count = update_tasklist_developer_unique_key(cursor)
        total_updated += updated_count
        print(f"  Updated {updated_count} Tasklist Developer records")

        # 4. Update Attendance
        print("Processing Attendance...")
        updated_count = update_attendance_unique_key(cursor)
        total_updated += updated_count
        print(f"  Updated {updated_count} Attendance records")

        # 5. Update Timesheet
        print("Processing Timesheet...")
        updated_count = update_timesheet_unique_key(cursor)
        total_updated += updated_count
        print(f"  Updated {updated_count} Timesheet records")

        # Commit all changes
        conn.commit()
        print(f"✅ Total updated: {total_updated} records")

    except Exception as e:
        print(f"❌ Error: {e}")
        logging.error(f"Error in step_05_generate_unique_keys: {e}")
        if 'conn' in locals():
            conn.rollback()
    finally:
        if 'cursor' in locals():
            cursor.close()
        if 'conn' in locals():
            conn.close()

def update_schedule_shifting_unique_key(cursor):
    """
    Update Schedule Shifting Unique_Key
    Relasi: Schedule Shifting -> _nc_m2m_Schedule Shifti_Employee Data -> Employee Data
    """
    query = '''
        UPDATE "pc38r6u1npuq0ul"."Schedule Shifting"
        SET "Unique_Key" = subquery.new_unique_key
        FROM (
            SELECT
                s.id,
                s."Date_Shifting"::text || '_' || ed.id::text as new_unique_key
            FROM "pc38r6u1npuq0ul"."Schedule Shifting" s
            JOIN "pc38r6u1npuq0ul"."_nc_m2m_Schedule Shifti_Employee Data" m1
                ON s.id = m1."Schedule Shifting_id"
            JOIN "pc38r6u1npuq0ul"."Employee Data" ed
                ON m1."Employee Data_id" = ed.id
            WHERE (s."Unique_Key" IS NULL OR s."Unique_Key" = '')
                AND s."Date_Shifting" IS NOT NULL
            GROUP BY s.id, s."Date_Shifting", ed.id
        ) as subquery
        WHERE "Schedule Shifting".id = subquery.id
    '''

    cursor.execute(query)
    return cursor.rowcount

def update_tasklist_iot_unique_key(cursor):
    """
    Update Tasklist IoT Operations Unique_Key
    Relasi: Tasklist IoT -> _nc_m2m_timesheet_Tasklist IoT Op -> timesheet -> _nc_m2m_timesheet_Employee Data -> Employee Data
    """
    query = '''
        UPDATE "pc38r6u1npuq0ul"."Tasklist IoT Operations"
        SET "Unique_Key" = subquery.new_unique_key
        FROM (
            SELECT
                t.id,
                t."Date"::text || '_' || ed.id::text as new_unique_key
            FROM "pc38r6u1npuq0ul"."Tasklist IoT Operations" t
            JOIN "pc38r6u1npuq0ul"."_nc_m2m_timesheet_Tasklist IoT Op" m1
                ON t.id = m1."Tasklist IoT Operations_id"
            JOIN "pc38r6u1npuq0ul"."_nc_m2m_timesheet_Employee Data" m2
                ON m1.timesheet_id = m2.timesheet_id
            JOIN "pc38r6u1npuq0ul"."Employee Data" ed
                ON m2."Employee Data_id" = ed.id
            WHERE (t."Unique_Key" IS NULL OR t."Unique_Key" = '')
                AND t."Date" IS NOT NULL
            GROUP BY t.id, t."Date", ed.id
        ) as subquery
        WHERE "Tasklist IoT Operations".id = subquery.id
    '''

    cursor.execute(query)
    return cursor.rowcount

def update_tasklist_developer_unique_key(cursor):
    """
    Update Tasklist Developer Unique_Key
    Relasi: Tasklist Developer -> _nc_m2m_tasklist_develo_Employee Data -> Employee Data
    """
    # First try the lowercase table (tasklist_developer_copy_id)
    query1 = '''
        UPDATE "pc38r6u1npuq0ul"."Tasklist Developer"
        SET "Unique_Key" = subquery.new_unique_key
        FROM (
            SELECT
                t.id,
                t."Date"::text || '_' || ed.id::text as new_unique_key
            FROM "pc38r6u1npuq0ul"."Tasklist Developer" t
            JOIN "pc38r6u1npuq0ul"."_nc_m2m_tasklist_develo_Employee Data" m1
                ON t.id = m1.tasklist_developer_copy_id
            JOIN "pc38r6u1npuq0ul"."Employee Data" ed
                ON m1."Employee Data_id" = ed.id
            WHERE (t."Unique_Key" IS NULL OR t."Unique_Key" = '')
                AND t."Date" IS NOT NULL
            GROUP BY t.id, t."Date", ed.id
        ) as subquery
        WHERE "Tasklist Developer".id = subquery.id
    '''

    cursor.execute(query1)
    updated_count1 = cursor.rowcount

    # Then try the uppercase table (Tasklist Developer_id)
    query2 = '''
        UPDATE "pc38r6u1npuq0ul"."Tasklist Developer"
        SET "Unique_Key" = subquery.new_unique_key
        FROM (
            SELECT
                t.id,
                t."Date"::text || '_' || ed.id::text as new_unique_key
            FROM "pc38r6u1npuq0ul"."Tasklist Developer" t
            JOIN "pc38r6u1npuq0ul"."_nc_m2m_Tasklist Develo_Employee Data" m1
                ON t.id = m1."Tasklist Developer_id"
            JOIN "pc38r6u1npuq0ul"."Employee Data" ed
                ON m1."Employee Data_id" = ed.id
            WHERE (t."Unique_Key" IS NULL OR t."Unique_Key" = '')
                AND t."Date" IS NOT NULL
            GROUP BY t.id, t."Date", ed.id
        ) as subquery
        WHERE "Tasklist Developer".id = subquery.id
    '''

    cursor.execute(query2)
    updated_count2 = cursor.rowcount

    return updated_count1 + updated_count2

def update_attendance_unique_key(cursor):
    """
    Update Attendance Unique_Key
    Relasi: Attendance -> _nc_m2m_Attendance_Employee Data -> Employee Data
    """
    query = '''
        UPDATE "pc38r6u1npuq0ul"."Attendance"
        SET "Unique_Key" = subquery.new_unique_key
        FROM (
            SELECT
                a.id,
                a."Date"::text || '_' || ed.id::text as new_unique_key
            FROM "pc38r6u1npuq0ul"."Attendance" a
            JOIN "pc38r6u1npuq0ul"."_nc_m2m_Attendance_Employee Data" m1
                ON a.id = m1."Attendance_id"
            JOIN "pc38r6u1npuq0ul"."Employee Data" ed
                ON m1."Employee Data_id" = ed.id
            WHERE (a."Unique_Key" IS NULL OR a."Unique_Key" = '')
                AND a."Date" IS NOT NULL
            GROUP BY a.id, a."Date", ed.id
        ) as subquery
        WHERE "Attendance".id = subquery.id
    '''

    cursor.execute(query)
    return cursor.rowcount

def update_timesheet_unique_key(cursor):
    """
    Update Timesheet Unique_Key
    Relasi: Timesheet -> _nc_m2m_timesheet_Employee Data -> Employee Data
    """
    query = '''
        UPDATE "pc38r6u1npuq0ul"."timesheet"
        SET "Unique_Key" = subquery.new_unique_key
        FROM (
            SELECT
                t.id,
                t."date"::text || '_' || ed.id::text as new_unique_key
            FROM "pc38r6u1npuq0ul"."timesheet" t
            JOIN "pc38r6u1npuq0ul"."_nc_m2m_timesheet_Employee Data" m1
                ON t.id = m1.timesheet_id
            JOIN "pc38r6u1npuq0ul"."Employee Data" ed
                ON m1."Employee Data_id" = ed.id
            WHERE (t."Unique_Key" IS NULL OR t."Unique_Key" = '')
                AND t."date" IS NOT NULL
            GROUP BY t.id, t."date", ed.id
        ) as subquery
        WHERE "timesheet".id = subquery.id
    '''

    cursor.execute(query)
    return cursor.rowcount

if __name__ == "__main__":
    run()