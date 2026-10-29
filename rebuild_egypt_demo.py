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
import re
import subprocess
import sys
import time
from datetime import date, timedelta

from dhis2 import RequestException as DHIS2RequestException
from logzero import logfile, logger

from tracker_common import load_api

# Sandbox 3, not production. These scripts purge and rewrite data.
TARGET_ENV_PREFIX = 'EGYPT_SANDBOX3'
# Discovered by name at run time rather than hardcoded: exchange UIDs differ
# between instances, and running the wrong one silently writes nothing.
EXCHANGE_NAME_PREFIX = 'ADEX - HTN DM'
# Where each program indicator records the aggregate data element it
# feeds, when a request does not name the attribute itself.
MAPPING_ATTRIBUTE = 'vudyDP7jUy5'


def find_exchanges(api):
    """The HTN/DM aggregate data exchanges on this instance."""
    found = api.get('aggregateDataExchanges', params={
        'fields': 'id,name', 'paging': 'false'}).json()['aggregateDataExchanges']
    matching = [(e['id'], e['name']) for e in found
                if e.get('name', '').startswith(EXCHANGE_NAME_PREFIX)]
    # Previous periods before current, so the 12-month backfill lands first.
    matching.sort(key=lambda x: 'Current' in x[1])
    return matching
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

    The 12-month exchange regularly exceeds the gateway timeout. A 504 means
    the proxy stopped waiting, which says nothing about whether the job
    finished, so it is reported and left for verify_aggregates to settle by
    looking at the data. On sandbox 1 a 504 did mean the job was cut short:
    every DM request after the timeout was dropped, and the aggregate values
    for them were never written.
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
            logger.warning(f"    gateway {code}: the proxy gave up. Whether the "
                           f"job finished is decided by the readback, not here.")
            return True
        logger.error(f"    failed: {str(e)[:200]}")
        return False


def verify_aggregates(api):
    """
    Read back what the exchanges were supposed to write.

    Without this the rebuild can report success while the aggregate half of
    every dashboard is empty: a gateway timeout is indistinguishable from a
    finished job until the data is looked at.

    The data elements are not the UIDs in the exchange requests. Those are
    program indicators, and each request carries an outputIdScheme of
    ATTRIBUTE:<uid> naming the metadata attribute in which every indicator
    records the aggregate data element it feeds.
    """
    exchanges = api.get('aggregateDataExchanges', params={
        'fields': 'name,source[requests[dx,outputIdScheme]]', 'paging': 'false'}
    ).json()['aggregateDataExchanges']

    indicators, attribute = set(), MAPPING_ATTRIBUTE
    for exchange in exchanges:
        if not exchange['name'].startswith(EXCHANGE_NAME_PREFIX):
            continue
        for request in exchange['source']['requests']:
            indicators.update(request['dx'])
            match = re.fullmatch(r'ATTRIBUTE:(\w{11})', request.get('outputIdScheme') or '')
            if match:
                attribute = match.group(1)

    indicators = sorted(indicators)
    elements = {}
    for start in range(0, len(indicators), 30):
        found = api.get('programIndicators', params={
            'filter': 'id:in:[' + ','.join(indicators[start:start + 30]) + ']',
            'fields': 'id,name,attributeValues[value,attribute[id]]',
            'paging': 'false'}).json()['programIndicators']
        for indicator in found:
            for value in indicator.get('attributeValues', []):
                if value['attribute']['id'] == attribute:
                    elements[value['value']] = indicator['name']

    if not elements:
        logger.error("  no indicator names its aggregate data element in "
                     f"attribute {attribute}; cannot verify")
        return False

    root = api.get('organisationUnits', params={
        'filter': 'level:eq:1', 'fields': 'id', 'paging': 'false'}
    ).json()['organisationUnits'][0]['id']

    rows = api.get('analytics', params={
        'dimension': [f"dx:{';'.join(elements)}",
                      'pe:LAST_12_MONTHS;THIS_MONTH;LAST_4_QUARTERS;THIS_QUARTER'],
        'filter': f'ou:{root}', 'skipMeta': 'true'}).json().get('rows', [])

    written = {row[0] for row in rows}
    missing = sorted(name for uid, name in elements.items() if uid not in written)

    logger.info(f"  aggregate values visible: {len(rows)} cells across "
                f"{len(written)}/{len(elements)} data elements")
    for name in missing:
        logger.warning(f"    no value anywhere: {name}")

    if not written:
        logger.error("  the exchanges wrote nothing the dashboard can read.")
        return False
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--patients', type=int, default=1000)
    parser.add_argument('--after', default='2025-09-01')
    parser.add_argument('--seed', type=int, default=1)
    parser.add_argument('--chunk-size', type=int, default=10)
    parser.add_argument('--target', default=TARGET_ENV_PREFIX,
                        help='Credential prefix in .env naming the instance to '
                             'rebuild (default: %(default)s). e.g. --target '
                             'EGYPT_STAGING')
    parser.add_argument('--skip-purge', action='store_true',
                        help='Leave the existing patients in place')
    args = parser.parse_args()

    logfile('./rebuild_egypt_demo.log')
    api = load_api(args.target, f'target ({args.target})')
    logger.warning(f"Rebuilding on {api.base_url} (DHIS2 {api.version})")

    if not args.skip_purge:
        step("purge tracker patients")
        run(['purge_egypt_patients.py', '--target', args.target, '--confirm', 'DELETE'])

        step("purge aggregate values")
        run(['purge_aggregate_values.py', '--target', args.target, '--confirm', 'DELETE'])

    step(f"generate {args.patients} patients")
    run(['htn_engine.py', '--instance', 'egypt', '--target', args.target,
         '--after', args.after,
         '--patients', str(args.patients), '--seed', str(args.seed),
         '--chunk-size', str(args.chunk_size)])

    run_analytics(api, 'so the indicators see the new patients')

    step("run the aggregate data exchanges")
    exchanges = find_exchanges(api)
    if not exchanges:
        logger.error(f"No aggregate data exchange named '{EXCHANGE_NAME_PREFIX}...' "
                     f"on this instance; the aggregate values cannot be rebuilt.")
    for uid, name in exchanges:
        run_exchange(api, uid, name)

    run_analytics(api, 'so the dashboard sees the new aggregate values')

    step("verify the aggregate values landed")
    complete = verify_aggregates(api)

    step("done")
    if complete:
        logger.warning("Rebuild complete.")
    else:
        logger.error("Rebuild finished with aggregate values missing; "
                     "rerun the exchanges before trusting a dashboard.")
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
