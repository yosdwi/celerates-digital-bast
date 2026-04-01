import importlib
import argparse
import sys
import traceback
import requests
import time
import threading
from datetime import datetime

from src import config

def healthcheck_ping(status, step_name, details=""):
    if not config.HEALTHCHECK_URL:
        return
    
    url = f"{config.HEALTHCHECK_URL}"
    if status: # 'start' or 'fail'
        url += f"/{status}"

    try:
        requests.post(url, timeout=10, data=details.encode('utf-8'))
    except requests.RequestException as e:
        print(f"Healthcheck ping failed for step {step_name}: {e}", file=sys.stderr)


def get_step_module(step_name):
    try:
        module_path = f"src.steps.{step_name}"
        return importlib.import_module(module_path)
    except ImportError:
        print(f"Error: Could not import step module: {step_name}", file=sys.stderr)
        raise

def run_step(step_name):
    """Run a single step and handle errors gracefully"""
    healthcheck_ping("start", step_name)
    try:
        print(f"--- [{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Running step: {step_name} ---")
        module = get_step_module(step_name)

        if not hasattr(module, 'run'):
            raise AttributeError(f"Step module {step_name} does not have a 'run' function.")

        module.run()
        print(f"--- [{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Step {step_name} completed successfully ---")
        healthcheck_ping("", step_name) # Success ping
        return True

    except Exception as e:
        print(f"--- [{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Step {step_name} failed ---", file=sys.stderr)
        error_details = traceback.format_exc()
        print(error_details, file=sys.stderr)
        healthcheck_ping("fail", step_name, details=error_details[:10000])
        return False

def scheduler_loop():
    """Simple scheduler that runs 3 steps every 2 hours"""
    steps_to_run = [
        "step_03_iot_process_redmine_tasks",
        "step_04_generate_timesheets",
        "step_05_generate_unique_keys"
    ]

    interval_hours = 2
    interval_seconds = interval_hours * 60 * 60  # 2 hours in seconds

    print(f"=== Digital BAST Scheduler Started ===")
    print(f"Will run steps every {interval_hours} hours: {', '.join(steps_to_run)}")

    while True:
        try:
            print(f"\n=== [{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Starting scheduled run ===")

            success_count = 0
            for step in steps_to_run:
                if run_step(step):
                    success_count += 1
                    # Small delay between steps to avoid overwhelming the system
                    time.sleep(30)

            print(f"=== [{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Scheduled run completed: {success_count}/{len(steps_to_run)} steps successful ===")
            print(f"Next run in {interval_hours} hours at {datetime.fromtimestamp(time.time() + interval_seconds).strftime('%Y-%m-%d %H:%M:%S')}")

            # Wait for next cycle
            time.sleep(interval_seconds)

        except KeyboardInterrupt:
            print(f"\n=== [{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Scheduler stopped by user ===")
            break
        except Exception as e:
            print(f"=== [{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Scheduler error: {e} ===", file=sys.stderr)
            print("Continuing in 60 seconds...")
            time.sleep(60)

def main():
    parser = argparse.ArgumentParser(description="ETL Digital BAST Step Runner")
    parser.add_argument("--step", help="The name of the ETL step to run (e.g., step_01_sync_holidays).")
    parser.add_argument("--scheduler", action="store_true", help="Run the scheduler that executes steps every 2 hours.")
    args = parser.parse_args()

    # Check if scheduler mode
    if args.scheduler:
        scheduler_loop()
        return

    # Single step mode (legacy behavior)
    if not args.step:
        parser.error("Either --step or --scheduler must be provided")

    step_name = args.step

    if not step_name.startswith("step_") or ".." in step_name or "/" in step_name:
        print(f"Error: Invalid step name format: {step_name}", file=sys.stderr)
        sys.exit(1)

    # Run single step
    success = run_step(step_name)
    if not success:
        sys.exit(1)

if __name__ == "__main__":
    main()
