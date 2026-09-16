"""
Delete the aggregate data values written into the Egypt HTN/DM programme by the
ADEx data exchanges.

These are the data elements the program indicators are mapped onto. The two
aggregateDataExchange jobs ("ADEX - HTN DM - Current period" and "... Previous
periods") run the indicators over facility level and write the results here, so
the values are derived: deleting them costs nothing that re-running the
exchange cannot rebuild.

The eight Monthly Summary Form data elements in the same set are NOT derived -
they are typed in by hand - and this script will never delete them. There is no
flag to override that.

The test is dataset membership, checked on the server every run: a data element
fed by the exchange is deliberately assigned to no dataset, so anything that
DOES sit on a data entry form is hand-entered and is skipped. That way the
protection follows the metadata rather than this hard-coded list, and a data
element added to a form later is protected automatically.

    python purge_aggregate_values.py                     # list what exists
    python purge_aggregate_values.py --confirm DELETE     # delete it
    python purge_aggregate_values.py --from 2026-01-01 --confirm DELETE

Deletion is permanent, though the exchange can regenerate these values.
"""

import argparse
import sys
from collections import Counter, defaultdict
from datetime import date, timedelta

from dhis2 import RequestException as DHIS2RequestException
from logzero import logger

from tracker_common import guard_destructive, load_api

# Sandbox 3, not production. These scripts purge and rewrite data.
TARGET_ENV_PREFIX = 'EGYPT_SANDBOX3'
CONFIRM_PHRASE = 'DELETE'

# ---------------------------------------------------------------------------
# The data elements the program indicators write into.
# ---------------------------------------------------------------------------

HTN_MONTHLY = {
    'MGWnzMBSVAC': 'HTN - Cumulative registrations',
    'UCzcgZfL9Pw': 'HTN - Monthly registrations',
    'lEReGEQfhtn': 'HTN - Patients under care',
    'GL4tv58GtWB': 'HTN - Patients under care registered before the past 3 months',
    'ChIb461qMBC': 'HTN - Patients under care, controlled BP (<130/80)',
    'zX4UHf3HHZ6': 'HTN - Patients under care, controlled BP (<140/90)',
    'mJv7u3C8w46': 'HTN - Patients under care, no visit in past 3 months',
    'N69Wu2mzoFk': 'HTN - Patients under care, uncontrolled BP (>=130/80)',
    'QmuglZ0Wk1C': 'HTN - Patients under care, uncontrolled BP (>=140/90)',
    'OFpr4x21rwc': 'HTN - Patients with no visit in past 12 months',
    'WOYSAaAC0G3': 'INV - # Facilities reporting working BP monitors',
    'FdrD4tIIOkV': 'INV - # Facilities with <30 patient days Amlodipine',
    'WwfON64Nvnr': 'INV - # Facilities with <30 patient days Hydrochlorothiazide',
    'opfSvWXu3OI': 'INV - # Facilities with <30 patient days Losartan',
    'wR81jslyioI': 'INV - # Facilities with drug stock entry',
    'HxjPNusFsOS': 'INV - # Facilities without a working BP monitor',
    'sr6YU93Hjl4': 'INV - Patient days supply - Amlodipine',
    'Ur7Ck4VMS8a': 'INV - Patient days supply - Hydrochlorothiazide',
    'ruAQ6sTSl5i': 'INV - Patient days supply - Losartan',
    # The rest of HTN monthly is hand-entered, see MANUAL_ENTRY.
    'I2NnM0BfObz': 'INV - Tablets of Amlodipine 10mg',
    'fuHs6sDnVx9': 'INV - Tablets of Amlodipine 5mg',
    'c4CWs8P5amK': 'INV - Tablets of Hydrochlorothiazide 12.5 mg',
    'hxbX9BZmrRm': 'INV - Tablets of Hydrochlorothiazide 25 mg',
    'rIPULGoGRT3': 'INV - Tablets of Hydrochlorothiazide 50 mg',
    'SVoFNNNHtk9': 'INV - Tablets of Losartan 100mg',
    'XpkHG4SfL1a': 'INV - Tablets of Losartan 50mg',
    'KgkZXNMJtQt': 'INV - Working BP monitors',
}

HTN_QUARTERLY = {
    'vY1DeFo6AcP': 'HTN - Patients with controlled BP at latest visit this quarter',
    'wiUtvIfdc6r': 'HTN - Patients with no visit in this quarter',
    'PNZzRdOuXeN': 'HTN - Patients with uncontrolled BP this quarter',
    'gSDfAwhzLxm': 'HTN - Total patient registrations in previous quarter',
}

DM_MONTHLY = {
    'KXgYGZRacnQ': 'DM - Blood sugar controlled (FBS <126 / HbA1c <7%)',
    'CyhqB3L1Vo8': 'DM - Blood sugar uncontrolled (FBS >=126 / HbA1c >=7%)',
    'zpLOGIEKRgU': 'DM - Blood sugar uncontrolled lower (FBS 126-199 / HbA1c 7-8.9%)',
    'OROSFgktKAF': 'DM - Blood sugar uncontrolled upper (FBS >=200 / HbA1c >=9%)',
    'KhPN2jLt52I': 'DM - Cumulative registrations',
    'ersfDTtRmwa': 'DM - Monthly registrations',
    'HXqTCEzwwWz': 'DM - Patients under care',
    'joYVieLjxKQ': 'DM - Patients under care registered before the past 3 months',
    'hCi4Zy2fpAE': 'DM - Patients under care, controlled BP (<130/80)',
    'nqiYyvhiDhV': 'DM - Patients under care, controlled BP (<140/90)',
    'tYPFFIxHwj4': 'DM - Patients under care, no visit in past 3 months',
    'ou5CJSlfg0G': 'DM - Patients under care >=40yr',
    'fnTOIE4nQSn': 'DM - Patients under care >=40yr prescribed statins',
    'fbRyJBZGHTI': 'DM - Patients with no visit in past 12 months',
}

DM_QUARTERLY = {
    'VWEpiY9pWia': 'DM - Patients with controlled blood sugar at latest visit this quarter',
    'PWfoNfjcmhY': 'DM - Patients with no visit in this quarter',
    'WqkLLu6UwLy': 'DM - Patients with uncontrolled blood sugar this quarter',
    'aRo6Gr17jXJ': 'DM - Total patient registrations in previous quarter',
}

# Hand-entered on the Monthly Summary Form. Not written by the exchange, so
# deleting them destroys data no job can rebuild.
MANUAL_ENTRY = {
    'I2NnM0BfObz', 'fuHs6sDnVx9', 'c4CWs8P5amK', 'hxbX9BZmrRm',
    'rIPULGoGRT3', 'SVoFNNNHtk9', 'XpkHG4SfL1a', 'KgkZXNMJtQt',
}

ALL_DATA_ELEMENTS = {}
for group in (HTN_MONTHLY, HTN_QUARTERLY, DM_MONTHLY, DM_QUARTERLY):
    ALL_DATA_ELEMENTS.update(group)

DEFAULT_WINDOW_MONTHS = 15

# DHIS2 only returns periods wholly inside startDate..endDate, so an end date
# of "today" silently skips the current month and quarter - exactly the periods
# the "Current period" exchange writes. Reach past the end of both.
FORWARD_MARGIN_DAYS = 120
READ_CHUNK = 10        # data elements per read request, to keep the URL sane
DELETE_CHUNK = 1000    # data values per delete request


def resolve_targets(api, args):
    """
    Work out which data elements to clear, always excluding hand-entered ones.

    Dataset membership is re-checked on the server rather than trusted from the
    list above: a data element fed by the exchange is assigned to no dataset, so
    one that DOES sit on a data entry form is hand-entered now, whatever the
    list says, and is protected.
    """
    if args.data_element:
        unknown = [d for d in args.data_element if d not in ALL_DATA_ELEMENTS]
        if unknown:
            logger.warning(f"Not in the known mapping, clearing anyway: {unknown}")
        targets = {d: ALL_DATA_ELEMENTS.get(d, '(unknown)') for d in args.data_element}
    else:
        targets = dict(ALL_DATA_ELEMENTS)

    try:
        on_server = api.get('dataElements', params={
            'filter': f"id:in:[{','.join(sorted(targets))}]", 'paging': 'false',
            'fields': 'id,name,dataSetElements[dataSet[id,name]]'}).json()['dataElements']
    except DHIS2RequestException as e:
        logger.error(f"Could not read the data elements: {e}")
        sys.exit(2)

    found = {d['id'] for d in on_server}
    missing = sorted(set(targets) - found)
    if missing:
        logger.warning(f"{len(missing)} data element(s) not on this server, skipping: {missing}")
        for uid in missing:
            targets.pop(uid, None)

    in_a_dataset = {d['id']: [e['dataSet']['name'] for e in d.get('dataSetElements', [])]
                    for d in on_server if d.get('dataSetElements')}

    protected = set(in_a_dataset) | (MANUAL_ENTRY & set(targets))
    if protected:
        logger.info(
            f"Protecting {len(protected)} hand-entered data element(s): they sit on a "
            f"data entry form, so nothing would rebuild them."
        )
        for uid in sorted(protected):
            forms = in_a_dataset.get(uid)
            where = f" (on {', '.join(forms)})" if forms else " (Monthly Summary Form)"
            logger.info(f"    kept: {uid}  {targets[uid][:52]}{where}")
            targets.pop(uid, None)

    return targets


def read_values(api, uids, org_unit, start_date, end_date):
    """Every stored value for these data elements in the window."""
    values = []
    ordered = sorted(uids)

    for i in range(0, len(ordered), READ_CHUNK):
        chunk = ordered[i:i + READ_CHUNK]
        try:
            r = api.get('dataValueSets', params={
                'dataElement': chunk,
                'orgUnit': org_unit,
                'children': 'true',
                'startDate': start_date.isoformat(),
                'endDate': end_date.isoformat(),
            }).json()
        except DHIS2RequestException as e:
            logger.error(f"Read failed for {chunk}: {e}")
            continue
        values.extend(r.get('dataValues') or [])

    return values


def delete_values(api, values):
    """Delete via the aggregate import endpoint with importStrategy=DELETE."""
    deleted = failed = 0

    for i in range(0, len(values), DELETE_CHUNK):
        chunk = values[i:i + DELETE_CHUNK]
        payload = {'dataValues': [
            {k: v[k] for k in ('dataElement', 'period', 'orgUnit',
                               'categoryOptionCombo', 'attributeOptionCombo')
             if v.get(k)}
            for v in chunk]}
        try:
            r = api.post('dataValueSets',
                         params={'importStrategy': 'DELETE', 'async': 'false'},
                         json=payload).json()
            counts = r.get('response', r).get('importCount', {}) or {}
            got = counts.get('deleted', 0)
            deleted += got
            failed += counts.get('ignored', 0)
            logger.info(f"  deleted {deleted}/{len(values)}"
                        + (f" (ignored {counts.get('ignored')})" if counts.get('ignored') else ""))
        except DHIS2RequestException as e:
            failed += len(chunk)
            logger.error(f"  delete chunk failed: {str(e)[:200]}")

    return deleted, failed


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--target', default=TARGET_ENV_PREFIX,
                        help='Credential prefix in .env naming the instance to act on (default: %(default)s). Use this to point the same script at a different Egypt instance, e.g. --target EGYPT_STAGING.')
    parser.add_argument('--allow-production', action='store_true',
                        help='Permit running against a target that looks like production. Off by default; these scripts destroy data.')
    parser.add_argument('--confirm', default=None,
                        help=f"Type {CONFIRM_PHRASE} to actually delete. Without it the "
                             "script only reports what it found.")
    parser.add_argument('--from', dest='date_from', default=None,
                        help='Start of the period window (YYYY-MM-DD). Default: '
                             f'{DEFAULT_WINDOW_MONTHS} months back')
    parser.add_argument('--to', dest='date_to', default=None,
                        help=f'End of the period window (YYYY-MM-DD). Default: '
                             f'{FORWARD_MARGIN_DAYS} days ahead, because DHIS2 only '
                             f'matches periods wholly inside the range and an end of '
                             f'today would skip the current month and quarter')
    parser.add_argument('--org-unit', default=None,
                        help='Root org unit to clear under (default: hierarchy root)')
    parser.add_argument('--data-element', action='append', default=[],
                        help='Clear only this data element. Repeatable')
    args = parser.parse_args()

    today = date.today()
    start = (date.fromisoformat(args.date_from) if args.date_from
             else today - timedelta(days=DEFAULT_WINDOW_MONTHS * 31))
    end = (date.fromisoformat(args.date_to) if args.date_to
           else today + timedelta(days=FORWARD_MARGIN_DAYS))
    if start > end:
        logger.error(f"--from {start} is after --to {end}")
        sys.exit(2)

    api = load_api(args.target, f'target ({args.target})')
    guard_destructive(args.target, api,
                      args.allow_production, 'delete aggregate data values')
    logger.info(f"Target DHIS2 {api.version}")

    org_unit = args.org_unit
    if org_unit is None:
        roots = api.get('organisationUnits', params={
            'filter': 'level:eq:1', 'fields': 'id,name', 'paging': 'false',
        }).json()['organisationUnits']
        if not roots:
            logger.error("Could not find the root org unit; pass --org-unit.")
            sys.exit(2)
        org_unit = roots[0]['id']
        logger.info(f"Clearing under {roots[0]['name']} ({org_unit})")

    targets = resolve_targets(api, args)
    if not targets:
        logger.error("No data elements left to clear.")
        sys.exit(2)

    logger.info(f"{len(targets)} data element(s), periods {start} .. {end}")
    values = read_values(api, targets, org_unit, start, end)

    if not values:
        logger.info("No stored values in that window. Nothing to do.")
        return

    by_de = Counter(v['dataElement'] for v in values)
    periods = defaultdict(set)
    for v in values:
        periods[v['dataElement']].add(v['period'])

    logger.info(f"Found {len(values)} stored values:")
    for uid, count in by_de.most_common():
        logger.info(f"  {count:6}  {len(periods[uid]):3} periods  "
                    f"{uid}  {targets.get(uid, '')[:48]}")

    if args.confirm != CONFIRM_PHRASE:
        logger.warning(
            f"Nothing deleted. {len(values)} values across "
            f"{len(by_de)} data elements would be removed. Re-run with "
            f"--confirm {CONFIRM_PHRASE} to proceed; re-run the ADEx exchanges "
            f"afterwards to rebuild them."
        )
        return

    logger.warning(f"Deleting {len(values)} aggregate values.")
    deleted, failed = delete_values(api, values)
    logger.info(f"Deleted {deleted}, not deleted {failed}")

    left = read_values(api, targets, org_unit, start, end)
    logger.info(f"Remaining in the window: {len(left)}")


if __name__ == '__main__':
    main()
