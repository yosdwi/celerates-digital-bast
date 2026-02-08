import logging
from datetime import datetime
from src import config
from src.classes.ClsNotificationEmail import ClsNotificationEmail
from src.classes.ClsNocoDBProcessor import ClsNocoDBProcessor

def run():
    print("Menjalankan Langkah 8: Kirim Notifikasi Komentar")
    logging.info("Memulai Langkah 8: Kirim Notifikasi Komentar")

    try:
        sent_notifs_table_id = config.NOCODB_TABLES.get("sent_notifications")
        if not sent_notifs_table_id:
            raise ValueError("ID tabel sent_notifications tidak ditemukan di config.")

        email_notifier = ClsNotificationEmail()
        nocodb_processor = ClsNocoDBProcessor(config.APP_BASE_ID, sent_notifs_table_id)
        
        pending_comments = email_notifier.get_pending_comments()

        if not pending_comments:
            logging.info("Tidak ada komentar tertunda untuk dikirim.")
            print("Tidak ada komentar tertunda untuk dikirim.")
            return

        logging.info(f"Ditemukan {len(pending_comments)} komentar untuk diproses.")
        print(f"Ditemukan {len(pending_comments)} komentar untuk diproses.")
        
        success_count = 0
        for comment in pending_comments:
            comment_id = comment['comment_id']
            content = comment['comment']
            from_email = comment['created_by_email']
            to_email = comment.get('Notification_Email') or comment.get('notification_email')
            timesheet_id = comment['row_id']
            
            if not to_email:
                logging.warning(f"Melewatkan comment_id {comment_id} karena email notifikasi tidak ada.")
                continue

            email_response = email_notifier.send_comment_via_email(from_email, to_email, content, timesheet_id)

            if email_response:
                now = datetime.now().strftime(format="%Y-%m-%d %H:%M")
                
                record_data = {
                    "comment_id": comment_id,
                    "message": content,
                    "sent_to": to_email,
                    "sent_timestamp": now,
                    "sent_status": True,
                }
                
                where_clause = f"(comment_id,eq,{comment_id})"
                existing = nocodb_processor.get_records(limit=1, where=where_clause)
                
                if existing and not existing.get('list'):
                    creation_response = nocodb_processor.create_record(record_data)
                    if creation_response:
                        logging.info(f"Berhasil mencatat notifikasi untuk comment_id {comment_id}.")
                        success_count += 1
                    else:
                        raise Exception(f"Gagal mencatat notifikasi ke NocoDB untuk comment_id {comment_id}.")
                else:
                    logging.warning(f"Notifikasi untuk comment_id {comment_id} sudah dicatat. Melewatkan.")
            else:
                raise Exception(f"Gagal mengirim email untuk comment_id {comment_id}.")

        print(f"Berhasil memproses dan mengirim {success_count}/{len(pending_comments)} notifikasi.")
        logging.info(f"Langkah 8 selesai. Memproses {success_count}/{len(pending_comments)} notifikasi.")

    except Exception as e:
        logging.error(f"Langkah 8 Gagal: {e}")
        print(f"Langkah 8 Gagal: {e}")
        raise
