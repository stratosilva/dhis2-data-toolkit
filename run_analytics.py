"""
Rebuild the analytics tables on a target and wait for them to finish.

The aggregate exchange reads its program indicators out of these tables, and
the dashboard reads the aggregate data elements out of them too, so a monthly
run needs analytics twice: once after the new patients land, so the indicators
can see them, and once after the exchange writes, so the dashboard can see
that. Without the second one a dashboard keeps serving the previous month.

The trigger itself often returns a gateway timeout while the job carries on
server side, so completion is decided by watching lastAnalyticsTableSuccess
change rather than by the response.

    python run_analytics.py --target SANDBOX1
    python run_analytics.py --target SANDBOX1 --timeout 7200
"""

import argparse
import sys
import time

from dhis2 import RequestException as DHIS2RequestException
from logzero import logger

from tracker_common import load_api

DEFAULT_TIMEOUT_SECONDS = 5400
POLL_SECONDS = 20


def rebuild(api, timeout=DEFAULT_TIMEOUT_SECONDS, poll=POLL_SECONDS):
    """Trigger analytics and block until the success timestamp moves."""
    before = api.get('system/info').json().get('lastAnalyticsTableSuccess')
    logger.info(f"  last success before: {before}")

    try:
        api.post('resourceTables/analytics', params={'skipResourceTables': 'false'})
    except DHIS2RequestException as e:
        # A 502/504 here is the proxy giving up on a job that is still running.
        logger.warning(f"  trigger returned {getattr(e, 'code', '?')}; polling anyway")

    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(poll)
        try:
            now = api.get('system/info').json().get('lastAnalyticsTableSuccess')
        except DHIS2RequestException:
            continue
        if now and now != before:
            logger.info(f"  analytics finished at {now}")
            return True

    logger.error(f"  analytics did not finish within {timeout}s")
    return False


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--target', required=True,
                        help='Credential prefix in .env naming the instance')
    parser.add_argument('--timeout', type=int, default=DEFAULT_TIMEOUT_SECONDS,
                        help='Seconds to wait for the rebuild (default: %(default)s)')
    args = parser.parse_args()

    api = load_api(args.target, f'target ({args.target})')
    logger.info(f"Analytics on {api.base_url} as {args.target}")
    return 0 if rebuild(api, args.timeout) else 1


if __name__ == '__main__':
    sys.exit(main())
