import os
import pandas as pd

from src import config
from src.classes.ClsNocoDBProcessor import ClsNocoDBProcessor

RETENTION_MONTHS = 6
DELETE_BATCH_SIZE = 500
RETENTION_TABLES = ["attendance", "attendance_raw", "tasklist", "timesheet"]


def _purge_table(table_key: str, cutoff_str: str, dry_run: bool) -> int:
    table_id = config.NOCODB_TABLES.get(table_key)
    if not table_id:
        print(f"{table_key}: table id not configured, skipping")
        return 0

    nocodb = ClsNocoDBProcessor(config.APP_BASE_ID, table_id)
    where_clause = f"(Date,lt,{cutoff_str})"

    ids_to_delete = []
    offset = 0
    page_size = 1000
    while True:
        response = nocodb.get_records(limit=page_size, offset=offset, where=where_clause, fields="Id,Date")
        batch = response.get('list', []) if response else []
        ids_to_delete.extend(r['Id'] for r in batch if 'Id' in r)
        if len(batch) < page_size:
            break
        offset += page_size

    if not ids_to_delete:
        print(f"{table_key}: nothing older than {cutoff_str}")
        return 0

    print(f"{table_key}: {len(ids_to_delete)} record(s) older than {cutoff_str} found")

    if dry_run:
        print(f"{table_key}: DRY RUN — not deleting")
        return len(ids_to_delete)

    deleted = 0
    for i in range(0, len(ids_to_delete), DELETE_BATCH_SIZE):
        chunk = ids_to_delete[i:i + DELETE_BATCH_SIZE]
        deleted += nocodb.bulk_delete_records(chunk)

    print(f"{table_key}: deleted {deleted}/{len(ids_to_delete)} record(s)")
    return deleted


def run():
    print("Executing Step 9: Data Retention")

    dry_run = os.getenv("RETENTION_DRY_RUN", "false").strip().lower() in ("1", "true", "yes")
    cutoff_str = (pd.Timestamp.now() - pd.DateOffset(months=RETENTION_MONTHS)).strftime('%Y-%m-%d')
    print(f"Retention window: {RETENTION_MONTHS} months, cutoff date {cutoff_str} (dry_run={dry_run})")

    total = 0
    for table_key in RETENTION_TABLES:
        total += _purge_table(table_key, cutoff_str, dry_run)

    verb = "would be deleted" if dry_run else "deleted"
    print(f"Step 9 complete: {total} record(s) {verb} across {len(RETENTION_TABLES)} table(s)")
