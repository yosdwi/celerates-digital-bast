import pandas as pd
import logging
import socket
import time
import random
from datetime import datetime
from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials
from googleapiclient.errors import HttpError

from src import config

class ClsIoTSheetProcessor:
    def __init__(self):
        self.credentials_path = config.SHEETS_CREDENTIALS_PATH
        self.service = self._authenticate()
        # IoT tasks sheet URL
        self.iot_sheet_url = "https://docs.google.com/spreadsheets/d/1bzAndOjRR-9GOrB8a2_FD5ayE5uPLLrg7gK4bKcmKbo/edit?usp=sharing"

    def _retry_with_backoff(self, func, max_retries=5):
        """Retry function with exponential backoff for rate limiting"""
        for attempt in range(max_retries):
            try:
                return func()
            except HttpError as e:
                if e.resp.status == 429 and attempt < max_retries - 1:
                    delay = (2 ** attempt) * 5 + random.uniform(1, 3)
                    logging.warning(f"Rate limit hit, retrying in {delay:.1f}s")
                    time.sleep(delay)
                else:
                    raise e
            except (socket.timeout, TimeoutError) as e:
                if attempt < max_retries - 1:
                    delay = (2 ** attempt) * 3 + random.uniform(2, 4)
                    logging.warning(f"Timeout, retrying in {delay:.1f}s: {e}")
                    time.sleep(delay)
                else:
                    raise e
            except Exception as e:
                msg = str(e).lower()
                is_transient = "ssl" in msg or "connection" in msg or "timed out" in msg or "timeout" in msg
                if is_transient and attempt < max_retries - 1:
                    delay = (2 ** attempt) * 3 + random.uniform(2, 4)
                    logging.warning(f"Connection error, retrying in {delay:.1f}s: {e}")
                    time.sleep(delay)
                else:
                    raise e
        raise Exception("Max retries exceeded")

    def _authenticate(self):
        """Authenticate with Google Sheets API"""
        if not self.credentials_path:
            raise ValueError("Google Sheets credentials path not configured.")
        try:
            scopes = ['https://www.googleapis.com/auth/spreadsheets.readonly']
            creds = Credentials.from_service_account_file(self.credentials_path, scopes=scopes)
            return build('sheets', 'v4', credentials=creds)
        except Exception as e:
            logging.error(f"Google Sheets authentication failed: {e}")
            raise

    def get_sheet_id_from_url(self, sheet_url: str) -> str:
        """Extract sheet ID from Google Sheets URL"""
        try:
            return sheet_url.split('/d/')[1].split('/')[0]
        except (IndexError, AttributeError):
            return sheet_url

    def read_iot_tasks(self, start_date: str, end_date: str) -> pd.DataFrame:
        """
        Read IoT tasks from Google Sheets within the specified date range

        Column mapping:
        - Date (2D)
        - Start Time (2E)
        - Close Time (2P)
        - Response Time (2F)
        - First Responder (2H)
        - Issue Type (2K)
        - Issue Description (2M)
        """
        sheet_id = self.get_sheet_id_from_url(self.iot_sheet_url)

        try:
            # Read specific columns from the sheet
            # Using batch get to read multiple ranges efficiently
            # First need to get sheet name
            sheet_metadata = self._retry_with_backoff(
                lambda: self.service.spreadsheets().get(
                    spreadsheetId=sheet_id
                ).execute()
            )

            # Use Master Support Ticket MS sheet specifically
            sheet_name = 'Master Support Ticket MS'

            ranges = [
                f"'{sheet_name}'!D:D",  # Date
                f"'{sheet_name}'!E:E",  # Start Time
                f"'{sheet_name}'!P:P",  # Close Time
                f"'{sheet_name}'!F:F",  # Response Time
                f"'{sheet_name}'!H:H",  # First Responder
                f"'{sheet_name}'!K:K",  # Issue Type
                f"'{sheet_name}'!M:M"   # Issue Description
            ]

            result = self._retry_with_backoff(
                lambda: self.service.spreadsheets().values().batchGet(
                    spreadsheetId=sheet_id, ranges=ranges
                ).execute()
            )

            value_ranges = result.get('valueRanges', [])

            if not value_ranges:
                logging.warning("No data found in IoT tasks sheet")
                return pd.DataFrame()

            # Extract data from each range and combine
            data_dict = {
                'Date': [],
                'Start Time': [],
                'Close Time': [],
                'Response Time': [],
                'First Responder': [],
                'Issue Type': [],
                'Issue Description': []
            }

            field_names = ['Date', 'Start Time', 'Close Time', 'Response Time', 'First Responder', 'Issue Type', 'Issue Description']

            # Find the maximum number of rows across all ranges
            max_rows = max(len(vr.get('values', [])) for vr in value_ranges) if value_ranges else 0

            # Populate data dictionary
            for i, field_name in enumerate(field_names):
                if i < len(value_ranges):
                    column_values = value_ranges[i].get('values', [])
                    # Pad with empty strings if this column has fewer rows
                    padded_values = [row[0] if row else '' for row in column_values]
                    padded_values.extend([''] * (max_rows - len(padded_values)))
                    data_dict[field_name] = padded_values
                else:
                    data_dict[field_name] = [''] * max_rows

            # Create DataFrame
            df = pd.DataFrame(data_dict)

            # Clean and filter data
            if len(df) > 0:
                # Remove rows where Date or Issue Description is empty
                df = df[
                    (df['Date'].notna()) &
                    (df['Date'].str.strip() != '') &
                    (df['Issue Description'].notna()) &
                    (df['Issue Description'].str.strip() != '')
                ]

                if len(df) > 0:
                    # Convert Date from "2026/02/01" to "2026-02-01" format for consistency FIRST
                    df['Date'] = df['Date'].str.replace('/', '-', regex=False)

                    # Convert time format from "0:02" to "YYYY-MM-DD HH:mm" format for NocoDB
                    def convert_time_format(time_str, date_str):
                        if not time_str or str(time_str).strip() == '' or not date_str:
                            return None

                        parts = str(time_str).split(':')
                        if len(parts) >= 2:
                            hours = parts[0].zfill(2)
                            minutes = parts[1].zfill(2)
                            # Combine with date: "2026-03-01 00:02"
                            return f'{date_str} {hours}:{minutes}'
                        else:
                            return None

                    # Apply time format conversion with corrected date
                    df['Start Time'] = df.apply(lambda row: convert_time_format(row['Start Time'], row['Date']), axis=1)
                    df['Response Time'] = df.apply(lambda row: convert_time_format(row['Response Time'], row['Date']), axis=1)
                    df['Close Time'] = df.apply(lambda row: convert_time_format(row['Close Time'], row['Date']), axis=1)

                    # Convert Date column to datetime for filtering
                    df['Date'] = pd.to_datetime(df['Date'], errors='coerce')

                    # Filter by date range
                    start_dt = datetime.strptime(start_date, '%Y-%m-%d')
                    end_dt = datetime.strptime(end_date, '%Y-%m-%d')

                    df = df[
                        (df['Date'] >= start_dt) &
                        (df['Date'] <= end_dt) &
                        (df['Date'].notna())
                    ]

                    # Convert back to string format for consistency (YYYY-MM-DD)
                    df['Date'] = df['Date'].dt.strftime('%Y-%m-%d')

            logging.info(f"Found {len(df)} IoT tasks in date range {start_date} to {end_date}")
            return df

        except Exception as e:
            logging.error(f"Failed to read IoT tasks from Google Sheets: {e}")
            raise