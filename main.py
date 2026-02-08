import importlib
import argparse
import sys
import traceback
import requests

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

def main():
  
    parser = argparse.ArgumentParser(description="ETL Digital BAST Step Runner")
    parser.add_argument("--step", required=True, help="The name of the ETL step to run (e.g., step_01_sync_holidays).")
    args = parser.parse_args()

    step_name = args.step
    
    if not step_name.startswith("step_") or ".." in step_name or "/" in step_name:
        print(f"Error: Invalid step name format: {step_name}", file=sys.stderr)
        sys.exit(1)

    healthcheck_ping("start", step_name)
    try:
        print(f"--- Running step: {step_name} ---")
        module = get_step_module(step_name)
        
        if not hasattr(module, 'run'):
            raise AttributeError(f"Step module {step_name} does not have a 'run' function.")

        module.run()
        print(f"--- Step {step_name} completed successfully ---")
        healthcheck_ping("", step_name) # Success ping

    except Exception as e:
        print(f"--- Step {step_name} failed ---", file=sys.stderr)
        error_details = traceback.format_exc()
        print(error_details, file=sys.stderr)
        # Truncate details to prevent exceeding payload limits
        healthcheck_ping("fail", step_name, details=error_details[:10000])
        sys.exit(1)

if __name__ == "__main__":
    main()
