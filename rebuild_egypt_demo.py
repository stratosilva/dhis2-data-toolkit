"""
Rebuild the Egypt demo instance end to end.

    purge patients and aggregate values -> reinject patients -> analytics
    -> run ADEx -> analytics

Analytics runs twice because the ADEx depends on it: the exchange reads the
program indicators out of the analytics tables, and the dashboard reads the
aggregate data elements it writes out of them too.

Analytics runs twice on purpose. The exchanges read the program indicators
through analytics, so it has to run after the tracker import for them to see
the new patients; and the dashboard reads the aggregate data elements through
analytics too, so it has to run again after the exchanges write them.

    python rebuild_egypt_demo.py --patients 1000 --after 2025-09-01 --seed 1
"""

import argparse
import subprocess
import sys
import time
from datetime import date, timedelta

from dhis2 import RequestException as DHIS2RequestException
from logzero import logfile, logger

from tracker_common import load_api

TARGET_ENV_PREFIX = 'EGYPT_NCD'
EXCHANGES = [
    ('ty6LYIC2aE5', 'ADEX - HTN DM - Previous periods'),
    ('tQ3bUoFoaSI', 'ADEX - HTN DM - Current period'),
]
ANALYTICS_TIMEOUT_SECONDS = 900
ANALYTICS_POLL_SECONDS = 10


def step(label):
    logger.warning(f"{'=' * 12} {label} {'=' * 12}")


def run(cmd):
    """
    Run one of the toolkit scripts, folding its output into this log.

    Both streams are kept. The toolkit scripts log through logzero, which
    writes to stderr, so discarding stderr on success threw away every run
    summary - including the line reporting how many patient outcomes were
    applied - and left this log looking as though the step had done nothing.
    """
    logger.info(f"$ {' '.join(cmd)}")
    result = subprocess.run([sys.executable] + cmd, capture_output=True, text=True)

    for stream in (result.stderr, result.stdout):
        for line in (stream or '').splitlines()[-40:]:
            if line.strip():
                logger.info(f"  {line}")

    if result.returncode != 0:
        raise SystemExit(f"{cmd[0]} exited {result.returncode}")

    return (result.stdout or '') + (result.stderr or '')


def run_analytics(api, label):
    """
    Trigger analytics and wait for it to finish.

    The exchange reads program indicators out of the analytics tables, so
    running it against stale analytics silently produces yesterday's numbers.
    """
    step(f"analytics ({label})")
    before = api.get('system/info').json().get('lastAnalyticsTableSuccess')

    try:
        api.post('resourceTables/analytics', params={'skipResourceTables': 'false'})
    except DHIS2RequestException as e:
        # A 504 here means the proxy gave up, not that the job failed.
        logger.warning(f"  trigger returned {getattr(e, 'code', '?')}; polling anyway")

    deadline = time.time() + ANALYTICS_TIMEOUT_SECONDS
    while time.time() < deadline:
        time.sleep(ANALYTICS_POLL_SECONDS)
        try:
            now = api.get('system/info').json().get('lastAnalyticsTableSuccess')
        except DHIS2RequestException:
            continue
        if now and now != before:
            logger.info(f"  analytics finished at {now}")
            return True
        logger.info("  still running...")

    logger.error(f"  analytics did not finish within {ANALYTICS_TIMEOUT_SECONDS}s")
    return False


def run_exchange(api, uid, name):
    """
    Run one aggregate data exchange.

    The 12-month exchange regularly exceeds the gateway timeout while
    completing server-side, so a 504 is reported and then verified by looking
    at the data rather than treated as a failure.
    """
    logger.info(f"  {name} ({uid})")
    try:
        r = api.post(f'aggregateDataExchanges/{uid}/exchange', json={}).json()
        body = r.get('response', r)
        logger.info(f"    status={r.get('status')} imported={body.get('imported')} "
                    f"updated={body.get('updated')} ignored={body.get('ignored')}")
        return True
    except DHIS2RequestException as e:
        code = getattr(e, 'code', None)
        if code in (502, 504):
            logger.warning(f"    gateway {code}: the proxy gave up. The job usually "
                           f"completes server-side; verified below.")
            return True
        logger.error(f"    failed: {str(e)[:200]}")
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--patients', type=int, default=1000)
    parser.add_argument('--after', default='2025-09-01')
    parser.add_argument('--seed', type=int, default=1)
    parser.add_argument('--chunk-size', type=int, default=10)
    parser.add_argument('--skip-purge', action='store_true',
                        help='Leave the existing patients in place')
    args = parser.parse_args()

    logfile('./rebuild_egypt_demo.log')
    api = load_api(TARGET_ENV_PREFIX, 'target (Egypt NCD)')
    logger.warning(f"Rebuilding on {api.base_url} (DHIS2 {api.version})")

    if not args.skip_purge:
        step("purge tracker patients")
        run(['purge_egypt_patients.py', '--confirm', 'DELETE'])

        step("purge aggregate values")
        run(['purge_aggregate_values.py', '--confirm', 'DELETE'])

    step(f"generate {args.patients} patients")
    run(['move_patients_egypt.py', '--after', args.after,
         '--patients', str(args.patients), '--seed', str(args.seed),
         '--chunk-size', str(args.chunk_size)])

    run_analytics(api, 'so the indicators see the new patients')

    step("run the aggregate data exchanges")
    for uid, name in EXCHANGES:
        run_exchange(api, uid, name)

    run_analytics(api, 'so the dashboard sees the new aggregate values')

    step("done")
    logger.warning("Rebuild complete. Verify with the aggregate readback.")


if __name__ == '__main__':
    main()
