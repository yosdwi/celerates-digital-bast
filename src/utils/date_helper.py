from datetime import datetime
import pandas as pd
from src import config

def get_configured_month_dates():
 
    now = datetime.now()
    config_month = config.GENERAL_CONFIG.get('month', 'auto').strip()

    month_map = {
        "Januari": 1, "Februari": 2, "Maret": 3, "April": 4, "Mei": 5, "Juni": 6,
        "Juli": 7, "Agustus": 8, "September": 9, "Oktober": 10, "November": 11, "Desember": 12
    }

    target_date_for_gsheet = now
    if config_month.lower() != 'auto' and config_month.capitalize() in month_map:
        target_month_num = month_map[config_month.capitalize()]
        if now.year == now.year and now.month < target_month_num:
            print(f"Warning: '{config_month}' is a future month. Using current month instead.")
        else:
            target_date_for_gsheet = now.replace(month=target_month_num, day=1)

    start_date = target_date_for_gsheet.replace(day=1)

    if start_date.month == now.month and start_date.year == now.year:
        end_date = now
    else:
        next_month_start = (start_date.replace(day=28) + pd.Timedelta(days=4)).replace(day=1)
        end_date = next_month_start - pd.Timedelta(days=1)

    return start_date, end_date, target_date_for_gsheet

def get_full_month_dates():
    """Like get_configured_month_dates, but end_date is always the last day of
    the target month (not capped at today). Useful for generating a whole month
    of data in a single run."""
    start_date, _, target_date = get_configured_month_dates()

    next_month_start = (start_date.replace(day=28) + pd.Timedelta(days=4)).replace(day=1)
    end_date = next_month_start - pd.Timedelta(days=1)

    return start_date, end_date, target_date

def get_month_name_from_date(date):
    month_names = ["Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli", "Agustus", "September", "Oktober", "November", "Desember"]
    return month_names[date.month - 1]
