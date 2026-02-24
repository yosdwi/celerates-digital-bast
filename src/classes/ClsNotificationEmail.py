import resend
import logging
import re
from pathlib import Path
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

        comment_template_path = Path(__file__).parent.parent.parent / "templates" / "comment_notification.html"
        with open(comment_template_path, 'r', encoding='utf-8') as f:
            self.comment_template = f.read()

        update_template_path = Path(__file__).parent.parent.parent / "templates" / "update_notification.html"
        with open(update_template_path, 'r', encoding='utf-8') as f:
            self.update_template = f.read()

    def get_pending_comments(self) -> list:
        data = []
        try:
            with self.engine.begin() as conn:
                comments = conn.execute(self.comment_details_query)
                data = [dict(row._mapping) for row in comments]
        except Exception as e:
            logging.error(f"Gagal mengambil komentar tertunda: {e}")
        return data

    def _get_sender_initial(self, email: str) -> str:
        if '@' in email:
            return email.split('@')[0][:2].upper()
        return email[:2].upper()

    def send_comment_via_email(self, sender_email: str, target_email: str, message: str, timesheet_id: str):
        timesheet_url = f"https://conform.celeratesapps.com/dashboard/#/nc/pc38r6u1npuq0ul/m99ucznm06bhtf1/vw8qp9dv90xkg5ty/timesheet-timesheet?rowId={timesheet_id}"

        template_vars = {
            'sender_initial': self._get_sender_initial(sender_email),
            'sender_email': sender_email,
            'message': message,
            'timesheet_url': timesheet_url,
        }

        html_content = self.comment_template
        for var_name, var_value in template_vars.items():
            html_content = html_content.replace(f"{{{{{var_name}}}}}", str(var_value))
        try:
            r = resend.Emails.send(
                {
                    "from": "Conform Team <conformnotification@celeratesapps.com>",
                    "to": target_email,
                    "subject": "💬 New comment on your timesheet",
                    "html": html_content,
                }
            )
            logging.info(f"Email berhasil terkirim: {r}")
            return r
        except Exception as e:
            logging.error(f"Gagal mengirim email ke {target_email}: {e}")
            return None

    def send_update_notification_to_team(self, employee_name: str, employee_email: str, timesheet_date: str, timesheet_id: str, changes_data: dict = None):
        from datetime import datetime

        timesheet_url = f"https://conform.celeratesapps.com/dashboard/#/nc/pc38r6u1npuq0ul/m99ucznm06bhtf1/vw8qp9dv90xkg5ty/timesheet-timesheet?rowId={timesheet_id}"

        changes_html = ""
        if changes_data:
            changes_html = self._format_changes_html(changes_data)

        template_vars = {
            'employee_initial': self._get_sender_initial(employee_name or employee_email),
            'employee_name': employee_name or 'Unknown Employee',
            'employee_email': employee_email,
            'timesheet_date': timesheet_date,
            'update_time': datetime.now().strftime('%B %d, %Y at %I:%M %p'),
            'timesheet_url': timesheet_url,
            'has_changes': 'true' if changes_data else 'false',
            'changes_content': changes_html,
        }

        html_content = self.update_template
        for var_name, var_value in template_vars.items():
            html_content = html_content.replace(f"{{{{{var_name}}}}}", str(var_value))

        html_content = self._process_conditionals(html_content, changes_data is not None)

        try:
            team_emails = [
                "conform-team@celeratesapps.com",
                "yoses.maheswara@gmail.com"
            ]

            results = []
            for email in team_emails:
                r = resend.Emails.send(
                    {
                        "from": "Conform System <conformnotification@celeratesapps.com>",
                        "to": email,
                        "subject": f"📊 {employee_name} updated timesheet for {timesheet_date}",
                        "html": html_content,
                    }
                )
                results.append(r)
                logging.info(f"Update notification sent to team: {email}")

            return results

        except Exception as e:
            logging.error(f"Failed to send update notification to team: {e}")
            return None

    def _format_changes_html(self, changes: dict) -> str:
        if not changes:
            return ""

        html_parts = []
        for field, change in changes.items():
            old_value = change.get('old', '')
            new_value = change.get('new', '')

            html_parts.append(f"""
                <div class="change-item">
                    <span class="change-field">{field}</span>
                    <div class="change-values">
                        <span class="old-value">{old_value}</span> →
                        <span class="new-value">{new_value}</span>
                    </div>
                </div>
            """)

        return ''.join(html_parts)

    def _process_conditionals(self, html: str, has_changes: bool) -> str:
        import re

        if_pattern = r'\{\{#if has_changes\}\}(.*?)\{\{/if\}\}'
        if has_changes:
            html = re.sub(if_pattern, r'\1', html, flags=re.DOTALL)
        else:
            html = re.sub(if_pattern, '', html, flags=re.DOTALL)

        return html

    def notify_timesheet_update(self, timesheet_id: str, employee_data: dict = None, changes: dict = None):
        if not employee_data:
            logging.warning("No employee data provided for update notification")
            return False

        employee_name = employee_data.get('name', '')
        employee_email = employee_data.get('email', '')
        timesheet_date = employee_data.get('date', '')

        if not all([employee_email, timesheet_date]):
            logging.warning("Missing required employee data for update notification")
            return False

        result = self.send_update_notification_to_team(
            employee_name=employee_name,
            employee_email=employee_email,
            timesheet_date=timesheet_date,
            timesheet_id=timesheet_id,
            changes_data=changes
        )

        return result is not None
