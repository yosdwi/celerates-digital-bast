import resend
import logging
from sqlalchemy import create_engine, text
from src import config

class ClsNotificationEmail:
    def __init__(self):
        self.db_url = config.DB_URL
        self.resend_key = config.RESEND_API_KEY
        self.engine = create_engine(self.db_url)
        resend.api_key = self.resend_key

        with open(config.QUERIES_PATH / "get_pending_comment_notifications.sql") as f:
            self.comment_details_query = text(f.read())
            
    def get_pending_comments(self) -> list:
        data = []
        try:
            with self.engine.begin() as conn:
                comments = conn.execute(self.comment_details_query)
                data = [dict(row._mapping) for row in comments]
        except Exception as e:
            logging.error(f"Gagal mengambil komentar tertunda: {e}")
        return data

    def send_comment_via_email(self, sender_email: str, target_email: str, message: str, timesheet_id: str):
        html_template = f"""
        <!DOCTYPE html>
        <html lang="en">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>Conform Comment Notification</title>
        </head>
        <body style="margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif; background-color: #f5f5f5;">
            <table width="100%" cellpadding="0" cellspacing="0" style="background-color: #f5f5f5; padding: 40px 20px;">
                <tr>
                    <td align="center">
                        <table width="600" cellpadding="0" cellspacing="0" style="background-color: #ffffff; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); overflow: hidden;">
                            <!-- Header -->
                            <tr>
                                <td style="background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); padding: 30px 40px; text-align: center;">
                                    <h1 style="margin: 0; color: #ffffff; font-size: 24px; font-weight: 600;">
                                        💬 Ada komentar di timesheet mu!
                                    </h1>
                                </td>
                            </tr>

                            <!-- Content -->
                            <tr>
                                <td style="padding: 40px;">
                                    <p style="margin: 0 0 20px 0; color: #333333; font-size: 16px; line-height: 1.5;">
                                        Kamu mendapat komentar pada timesheet di Conform dari <strong style="color: #667eea;">{sender_email}</strong>
                                    </p>

                                    <!-- Comment Box -->
                                    <div style="background-color: #f8f9fa; border-left: 4px solid #667eea; padding: 20px; margin: 20px 0; border-radius: 4px;">
                                        <p style="margin: 0; color: #555555; font-size: 15px; line-height: 1.6; white-space: pre-wrap;">
                                            {message}
                                        </p>
                                    </div>

                                    <!-- Action Button -->
                                    <div style="text-align: center; margin: 30px 0 20px 0;">
                                        <a href="https://conform.celeratesapps.com/dashboard/#/nc/pc38r6u1npuq0ul/m99ucznm06bhtf1/vw8qp9dv90xkg5ty/timesheet-timesheet?rowId={timesheet_id}" 
                                            style="display: inline-block; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: #ffffff; text-decoration: none; padding: 14px 32px; border-radius: 6px; font-weight: 600; font-size: 15px;">
                                            Lihat di Conform
                                        </a>
                                    </div>
                                </td>
                            </tr>

                            <!-- Footer -->
                            <tr>
                                <td style="background-color: #f8f9fa; padding: 20px 40px; text-align: center; border-top: 1px solid #e9ecef;">
                                    <p style="margin: 0; color: #999999; font-size: 13px; line-height: 1.5;">
                                        This is an automated notification from Conform<br>
                                        © 2025 Celerates Apps. All rights reserved.
                                    </p>
                                </td>
                            </tr>
                        </table>
                    </td>
                </tr>
            </table>
        </body>
        </html>
        """
        try:
            r = resend.Emails.send(
                {
                    "from": "yoses.maheswara@gmail.com",
                    "to": target_email,
                    "subject": "Conform Comment Notification",
                    "html": html_template,
                }
            )
            logging.info(f"Email berhasil terkirim: {r}")
            return r
        except Exception as e:
            logging.error(f"Gagal mengirim email ke {target_email}: {e}")
            return None
