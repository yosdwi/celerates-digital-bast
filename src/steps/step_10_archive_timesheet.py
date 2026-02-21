import logging
from datetime import datetime
from src.classes.ClsTimeSheetProcessor import ClsTimeSheetProcessor

def run():
    today = datetime.now()
    if today.day != 10:
        logging.info("Langkah 10 dilewati: bukan tanggal 10.")
        return

    logging.info("Memulai Langkah 10: Arsipkan Timesheet Bulan Lalu")

    try:
        processor = ClsTimeSheetProcessor()
        
        target_date = today.replace(day=1) - datetime.timedelta(days=1)
        month = target_date.strftime("%B")
        year = target_date.year
        
        logging.info(f"Target arsip: {month} {year}")

        processor.archive_timesheet(year, month)

        logging.info("Langkah 10 selesai.")

    except Exception as e:
        logging.error(f"Langkah 10 Gagal: {e}")
        raise
