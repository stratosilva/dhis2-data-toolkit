"""
Run the aggregate data exchanges as the instance itself defines them.

This calls aggregateDataExchanges/{uid}/exchange, the server's own job, rather
than reading the indicators and posting the data values from here the way
run_adex_staging.py does. It is the first choice in the monthly run, and the
point of it is partly to find out how long it keeps working: when the cohorts
grow past what the job can finish inside the proxy's patience, this is where
that will show.

Read the exit code, not the log: 0 means every exchange reported success and
the data bears that out, 1 means it did not.

The response alone is not enough to go on. The proxy closes the connection at
about 60 seconds and the job behind it may have finished, half finished or
died, and a 504 cannot tell those apart. Import counts cannot either: a value
re-posted by a different writer is reported as updated even when the number is
unchanged, because storedBy moves. So the run is checked by reading the data
back and asking whether each request wrote anything at all - which is the
shape the sandbox 1 failure took, where the DM requests had written nothing
for any period at any facility.

    python run_exchange.py --target SANDBOX1
"""

import argparse
import sys
import time
from datetime import date
from pathlib import Path

ADEX_DIR = Path(r'C:\Users\User\Documents\HTN & DM ADEx')
sys.path.insert(0, str(ADEX_DIR))

import replace_adex  # noqa: E402

from dhis2 import RequestException as DHIS2RequestException  # noqa: E402
from logzero import logger  # noqa: E402

import adex_config  # noqa: E402
from tracker_common import load_api  # noqa: E402

EXCHANGE_NAME_PREFIX = 'ADEX - HTN DM'
# A one-indicator patch exchange on sandbox 1, not part of the HTN/DM set.
EXCLUDE_IF_NAME_CONTAINS = ('Workaround',)


def find_exchanges(api, name_prefix=EXCHANGE_NAME_PREFIX):
    """
    The HTN/DM exchanges on this instance, backfill before current period.

    Discovered by name rather than hardcoded: the UIDs differ between
    instances, and running the wrong one writes nothing and says nothing.
    """
    found = api.get('aggregateDataExchanges', params={
        'fields': 'id,name', 'paging': 'false'}).json()['aggregateDataExchanges']
    wanted = [(e['id'], e['name']) for e in found
              if e.get('name', '').startswith(name_prefix)
              and not any(skip in e['name'] for skip in EXCLUDE_IF_NAME_CONTAINS)]
    # Previous periods before current, so the backfill lands first.
    wanted.sort(key=lambda item: 'Current' in item[1])
    return wanted


def run_one(api, uid, name):
    """Run one exchange. Returns (ok, seconds, verdict)."""
    started = time.monotonic()
    try:
        response = api.post(f'aggregateDataExchanges/{uid}/exchange', json={}).json()
    except DHIS2RequestException as e:
        elapsed = time.monotonic() - started
        code = getattr(e, 'code', None)
        if code in (502, 504):
            return False, elapsed, (
                f"gateway {code} after {elapsed:.0f}s - the proxy stopped "
                f"waiting. Whether the job finished is not knowable from here.")
        return False, elapsed, f"failed with {code}: {str(e)[:160]}"

    elapsed = time.monotonic() - started

    # One exchange can carry several requests, each with its own summary.
    summaries = response.get('responses') or [response]
    imported = updated = ignored = 0
    conflicts = []
    for summary in summaries:
        body = summary.get('response', summary)
        counts = body.get('importCount') or body
        imported += counts.get('imported') or 0
        updated += counts.get('updated') or 0
        ignored += counts.get('ignored') or 0
        conflicts.extend(body.get('conflicts') or [])

    status = response.get('status') or ''
    verdict = (f"{status or 'done'} in {elapsed:.0f}s - imported {imported}, "
               f"updated {updated}, ignored {ignored}")
    if conflicts:
        verdict += f", {len(conflicts)} conflict(s)"
        for conflict in conflicts[:3]:
            verdict += f"\n      {str(conflict)[:140]}"

    ok = status.upper() not in ('ERROR', 'WARNING') and not conflicts
    return ok, elapsed, verdict


def _facilities(api, ou_terms, wanted=5):
    """A few org units at the level a request writes into."""
    levels = [term.split('-', 1)[1] for term in ou_terms
              if str(term).upper().startswith('LEVEL-')]
    if not levels:
        return []
    described = api.get('organisationUnitLevels', params={
        'filter': f'id:eq:{levels[0]}', 'fields': 'level', 'paging': 'false'}
    ).json()['organisationUnitLevels']
    if not described:
        return []
    found = api.get('organisationUnits', params={
        'filter': f"level:eq:{described[0]['level']}", 'fields': 'id,name',
        'paging': 'false'}).json()['organisationUnits']
    return found[:wanted]


def verify(api, today):
    """
    Did every request actually write anything?

    This asks whether values exist, not whether they match. A re-post by a
    different writer touches storedBy, so DHIS2 reports it as updated even
    when the number is unchanged - import counts cannot tell a job that ran
    from one that did not. Absence can: when sandbox 1's exchange was cut off
    by the proxy, its DM requests had written nothing at all, for any period
    or facility.

    A request is only called empty when every sampled data element is empty at
    every sampled facility, so one quiet facility cannot raise a false alarm.
    These data elements belong to no data set, which is why each value is read
    singly through /api/dataValues rather than exported.
    """
    config, _dropped = adex_config.build(api)
    requests = config['source']['requests']
    attribute = adex_config._mapping_attribute(requests)
    mapping = adex_config._target_elements(
        api, {uid for r in requests for uid in r['dx']}, attribute)

    empty_requests = []
    for request in requests:
        periods = replace_adex.resolve_period(request['pe'][0], today)
        if not periods:
            continue
        # The most recent period the request covers, which is the one a run
        # that has just finished must have written.
        period = periods[-1].output
        elements = [mapping[uid][1] for uid in request['dx']
                    if uid in mapping and mapping[uid][1]][:6]
        sites = _facilities(api, request.get('ou') or [])
        if not elements or not sites:
            continue

        found = 0
        for element in elements:
            for site in sites:
                try:
                    value = api.get('dataValues', params={
                        'de': element, 'pe': period, 'ou': site['id']}).json()
                except DHIS2RequestException:
                    continue
                if value:
                    found += 1
        if found:
            logger.info(f"    {request['name']}: {found} value(s) present "
                        f"for {period}")
        else:
            logger.warning(f"    {request['name']}: nothing written for "
                           f"{period} at any of {len(sites)} facilities")
            empty_requests.append(request['name'])

    return empty_requests


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--target', required=True,
                        help='Credential prefix in .env naming the instance')
    parser.add_argument('--skip-verify', action='store_true',
                        help='Trust what the exchange reported and do not read '
                             'the data back')
    args = parser.parse_args()

    api = load_api(args.target, f'target ({args.target})')
    logger.info(f"Exchanges on {api.base_url} as {args.target}")

    exchanges = find_exchanges(api)
    if not exchanges:
        logger.error(f"  no exchange named '{EXCHANGE_NAME_PREFIX}...' on this "
                     f"instance; nothing to run")
        return 1

    failures = 0
    for uid, name in exchanges:
        logger.info(f"  {name} ({uid})")
        ok, _elapsed, verdict = run_one(api, uid, name)
        if ok:
            logger.info(f"    {verdict}")
        else:
            logger.warning(f"    {verdict}")
            failures += 1

    if failures:
        logger.warning(f"  {failures} of {len(exchanges)} exchange(s) did not "
                       f"report success")
        return 1

    logger.info(f"  all {len(exchanges)} exchange(s) reported success")

    if args.skip_verify:
        return 0

    # A clean response is worth less than it looks: the proxy can close the
    # connection on a job that then finishes, or dies half way, and both come
    # back the same. Reading the data back is what settles it.
    logger.info("  checking that each request wrote something")
    empty = verify(api, date.today())
    if empty:
        logger.warning(f"  {len(empty)} request(s) wrote nothing: "
                       f"{', '.join(empty)}")
        return 1

    logger.info("  every request wrote values")
    return 0


if __name__ == '__main__':
    sys.exit(main())
