"""
Run the HTN/DM aggregate exchange against a target, from the .env credentials.

replace_adex.py takes the password as a command-line argument, which puts it in
shell history and process listings. This calls the same code with an Api built
from .env instead, and applies the organisation unit level override so one
config can serve instances whose facility level has a different UID.

    python run_adex_staging.py --target EGYPT_STAGING --org-unit-level sVmUkeVEPyE
"""

import argparse
import sys
from datetime import date
from pathlib import Path

ADEX_DIR = Path(r'C:\Users\User\Documents\HTN & DM ADEx')
sys.path.insert(0, str(ADEX_DIR))

import replace_adex  # noqa: E402

from tracker_common import load_api  # noqa: E402

DEFAULT_CONFIG = ADEX_DIR / 'agg_data_exchange-all-sandbox3.json'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', default='EGYPT_STAGING')
    parser.add_argument('--org-unit-level', default=None)
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass

    api = load_api(args.target, f'target ({args.target})')
    print(f"Target: {api.base_url} as {args.target}", flush=True)
    print(f"Config: {args.config}", flush=True)

    replace_adex.configure_logging(Path('replace_adex.log'))
    replace_adex.process(args.config, api, date.today(), args.org_unit_level)
    print("ADEx run complete.", flush=True)


if __name__ == '__main__':
    main()
