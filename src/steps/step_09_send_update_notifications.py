import logging
from datetime import datetime
from datetime import timedelta
from src import config
from src.classes.ClsNotificationEmail import ClsNotificationEmail
from src.classes.ClsNocoDBProcessor import ClsNocoDBProcessor

def run():
    print("Menjalankan Langkah 9: Kirim Notifikasi Update Timesheet")
    logging.info("Memulai Langkah 9: Kirim Notifikasi Update Timesheet")

    try:
        timesheet_table_id = config.NOCODB_TABLES.get("timesheet")
        employee_table_id = config.NOCODB_TABLES.get("employee_data")
        sent_notifs_table_id = config.NOCODB_TABLES.get("sent_notifications")

        if not all([timesheet_table_id, employee_table_id, sent_notifs_table_id]):
            raise ValueError("ID tabel timesheet, employee_data, atau sent_notifications tidak ditemukan di config.")

        email_notifier = ClsNotificationEmail()
        nocodb_timesheet = ClsNocoDBProcessor(config.APP_BASE_ID, timesheet_table_id)
        nocodb_employee = ClsNocoDBProcessor(config.APP_BASE_ID, employee_table_id)
        nocodb_sent_notifs = ClsNocoDBProcessor(config.APP_BASE_ID, sent_notifs_table_id)
        
        yesterday = (datetime.now() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")

        where_clause = f"(Last Modified,gt,{yesterday})"
        response = nocodb_timesheet.get_records(
            limit=1000,
            where=where_clause,
            fields="Id,Employee Name,Date,Last Modified"
        )

        recent_updates = response.get('records', []) if response else []

        if not recent_updates:
            logging.info("Tidak ada update timesheet dalam 24 jam terakhir.")
            print("Tidak ada update timesheet dalam 24 jam terakhir.")
            return

        logging.info(f"Ditemukan {len(recent_updates)} update timesheet untuk diproses.")
        print(f"Ditemukan {len(recent_updates)} update timesheet untuk diproses.")

        success_count = 0
        for record in recent_updates:
            try:
                timesheet_id = record.get('id', '')
                fields = record.get('fields', {})

                employee_name = fields.get('Employee Name', '')
                timesheet_date = fields.get('Date', '')
                last_modified = fields.get('Last Modified', '')

                if not all([timesheet_id, employee_name, timesheet_date]):
                    logging.warning(f"Data tidak lengkap untuk timesheet {timesheet_id}. Melewatkan.")
                    continue

                notification_key = f"update_{timesheet_id}_{last_modified}"
                where_notif = f"(notification_key,eq,{notification_key})"
                existing_notif = nocodb_sent_notifs.get_records(limit=1, where=where_notif)

                if existing_notif and existing_notif.get('records'):
                    logging.info(f"Notifikasi untuk update {timesheet_id} sudah dikirim. Melewatkan.")
                    continue

                employee_where = f"(Name,like,%{employee_name.strip()}%)"
                employee_response = nocodb_employee.get_records(limit=1, where=employee_where)
                employee_records = employee_response.get('records', []) if employee_response else []

                employee_email = ""
                if employee_records:
                    employee_email = employee_records[0].get('fields', {}).get('Email', '')

                if not employee_email:
                    logging.warning(f"Email tidak ditemukan untuk employee {employee_name}. Melewatkan.")
                    continue

                employee_data = {
                    'name': employee_name,
                    'email': employee_email,
                    'date': timesheet_date
                }

                notification_result = email_notifier.notify_timesheet_update(
                    timesheet_id=timesheet_id,
                    employee_data=employee_data,
                    changes=None  
                )

                if notification_result:
                    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    record_data = {
                        "notification_key": notification_key,
                        "timesheet_id": timesheet_id,
                        "notification_type": "timesheet_update",
                        "sent_to": "conform-team@celeratesapps.com",
                        "sent_timestamp": now,
                        "sent_status": True,
                        "employee_name": employee_name,
                        "employee_email": employee_email
                    }

                    creation_response = nocodb_sent_notifs.create_record(record_data)
                    if creation_response:
                        logging.info(f"Notifikasi update berhasil untuk timesheet {timesheet_id} ({employee_name}).")
                        success_count += 1
                    else:
                        logging.error(f"Gagal mencatat notifikasi update untuk timesheet {timesheet_id}.")
                else:
                    logging.error(f"Gagal mengirim notifikasi update untuk timesheet {timesheet_id}.")

            except Exception as e:
                logging.error(f"Error memproses timesheet {timesheet_id}: {e}")
                continue

        print(f"Berhasil memproses dan mengirim {success_count}/{len(recent_updates)} notifikasi update.")
        logging.info(f"Langkah 9 selesai. Memproses {success_count}/{len(recent_updates)} notifikasi update.")

    except Exception as e:
        logging.error(f"Langkah 9 Gagal: {e}")
        print(f"Langkah 9 Gagal: {e}")
        raise