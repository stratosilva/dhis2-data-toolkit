"""
Check that an instance carries everything the Egypt scripts assume, before
anything is written to it.

The generator and the fix scripts address metadata by UID: the programme, the
visit stage, every tracked entity attribute and data element, the option sets,
the mapping attribute, the aggregate targets and the data exchanges. Those UIDs
came from sandbox 3. On another instance they may be absent or mean something
else, in which case a run would fail late, or worse, write plausible-looking
values into the wrong objects.

    python preflight_target.py --target EGYPT_STAGING

Read-only: it makes no changes. A clean report means a rebuild can be pointed
at the instance; anything reported missing has to be resolved first.
"""

import argparse
import sys

from dhis2 import RequestException as DHIS2RequestException
from logzero import logger

import move_patients_egypt as gen
import purge_aggregate_values as agg
from tracker_common import load_api

DEFAULT_TARGET = 'EGYPT_SANDBOX3'

MAPPING_ATTRIBUTE = 'vudyDP7jUy5'
EXCHANGE_NAME_PREFIX = 'ADEX - HTN DM'
CONSTANTS = {
    'n1e76QJKUzt': 'HTN estimated adults with hypertension',
    'AQwjmAAFrCh': 'DM estimated adults with diabetes',
}


def exists(api, endpoint, uids, label, extra_fields='id,name'):
    """Which of these UIDs the instance actually has."""
    if not uids:
        return [], []
    uids = sorted(set(uids))
    found = set()
    for i in range(0, len(uids), 50):
        chunk = uids[i:i + 50]
        try:
            r = api.get(endpoint, params={
                'filter': f"id:in:[{','.join(chunk)}]",
                'fields': extra_fields, 'paging': 'false'}).json()
            found |= {x['id'] for x in r[list(r.keys())[0]]}
        except DHIS2RequestException as e:
            logger.error(f"  {label}: lookup failed ({str(e)[:90]})")
            return [], uids
    missing = [u for u in uids if u not in found]
    status = 'OK' if not missing else f"MISSING {len(missing)}"
    logger.info(f"  {label:34} {len(found)}/{len(uids)}  {status}")
    if missing:
        logger.warning(f"      absent: {missing}")
    return sorted(found), missing


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--target', default=DEFAULT_TARGET,
                        help='Credential prefix in .env naming the instance to '
                             'check (default: %(default)s)')
    parser.add_argument('--ou-level', type=int, default=4,
                        help='Org unit level the scripts treat as facilities '
                             '(default: %(default)s)')
    args = parser.parse_args()

    api = load_api(args.target, f'target ({args.target})')
    logger.warning(f"Checking {api.base_url} (DHIS2 {api.version}) "
                   f"as {args.target}")

    problems = 0

    logger.info("programme structure")
    try:
        p = api.get(f'programs/{gen.TARGET_PROGRAM}', params={
            'fields': 'id,name,trackedEntityType[id],programStages[id,name]'}).json()
        stages = {s['id'] for s in p.get('programStages', [])}
        logger.info(f"  programme {gen.TARGET_PROGRAM}         '{p.get('name')}'")
        if gen.TARGET_PROGRAM_STAGE not in stages:
            logger.error(f"  visit stage {gen.TARGET_PROGRAM_STAGE} NOT on this "
                         f"programme (has {sorted(stages)})")
            problems += 1
        else:
            logger.info(f"  visit stage {gen.TARGET_PROGRAM_STAGE}       present")
        tet = (p.get('trackedEntityType') or {}).get('id')
        if tet != gen.TARGET_TRACKED_ENTITY_TYPE:
            logger.error(f"  tracked entity type is {tet}, scripts assume "
                         f"{gen.TARGET_TRACKED_ENTITY_TYPE}")
            problems += 1
        else:
            logger.info(f"  tracked entity type {tet}  matches")
    except DHIS2RequestException as e:
        logger.error(f"  programme {gen.TARGET_PROGRAM} not found ({str(e)[:80]})")
        problems += 1

    logger.info("metadata the generator writes")
    for endpoint, uids, label in (
            ('trackedEntityAttributes', gen.TEA.values(), 'tracked entity attributes'),
            ('dataElements', gen.DE.values(), 'visit data elements'),
            ('optionSets', [gen.DISTRICT_OPTION_SET, gen.GOVERNORATE_OPTION_SET],
             'geography option sets'),
    ):
        _, missing = exists(api, endpoint, list(uids), label)
        problems += bool(missing)

    logger.info("metadata the aggregate purge and ADEx depend on")
    _, missing = exists(api, 'dataElements',
                        list(set(agg.ALL_DATA_ELEMENTS) - agg.MANUAL_ENTRY),
                        'ADEx target data elements')
    problems += bool(missing)
    _, missing = exists(api, 'dataElements', sorted(agg.MANUAL_ENTRY),
                        'hand-entered data elements')
    problems += bool(missing)
    _, missing = exists(api, 'constants', list(CONSTANTS), 'cascade constants')
    problems += bool(missing)

    try:
        r = api.get('attributes', params={
            'filter': f'id:eq:{MAPPING_ATTRIBUTE}', 'fields': 'id,name',
            'paging': 'false'}).json()['attributes']
        if r:
            logger.info(f"  mapping attribute {MAPPING_ATTRIBUTE}  '{r[0]['name']}'")
        else:
            logger.error(f"  mapping attribute {MAPPING_ATTRIBUTE} absent - the ADEx "
                         f"cannot resolve program indicators to data elements")
            problems += 1
    except DHIS2RequestException as e:
        logger.error(f"  mapping attribute lookup failed ({str(e)[:80]})")
        problems += 1

    logger.info("org units")
    try:
        ous = api.get('organisationUnits', params={
            'filter': f'level:eq:{args.ou_level}', 'fields': 'id',
            'paging': 'false'}).json()['organisationUnits']
        if ous:
            logger.info(f"  level {args.ou_level} facilities              {len(ous)}")
        else:
            logger.error(f"  no org units at level {args.ou_level}; pass "
                         f"--ou-level with the facility level")
            problems += 1
    except DHIS2RequestException as e:
        logger.error(f"  org unit lookup failed ({str(e)[:80]})")
        problems += 1

    logger.info("aggregate data exchanges")
    try:
        found = api.get('aggregateDataExchanges', params={
            'fields': 'id,name', 'paging': 'false'}).json()['aggregateDataExchanges']
        matching = [e for e in found if e.get('name', '').startswith(EXCHANGE_NAME_PREFIX)]
        if matching:
            for e in matching:
                logger.info(f"  {e['id']}  {e['name']}")
        else:
            logger.error(f"  none named '{EXCHANGE_NAME_PREFIX}...'; aggregate values "
                         f"cannot be rebuilt on this instance")
            problems += 1
    except DHIS2RequestException as e:
        logger.error(f"  exchange lookup failed ({str(e)[:80]})")
        problems += 1

    logger.info("option codes the generator emits")
    try:
        gen.check_target_metadata(api)
    except SystemExit:
        logger.error("  option set check failed - see above")
        problems += 1

    logger.warning("=" * 60)
    if problems:
        logger.error(f"{problems} problem area(s). Resolve these before pointing a "
                     f"rebuild at {args.target}.")
        sys.exit(1)
    logger.warning(f"{args.target} carries everything the scripts assume.")


if __name__ == '__main__':
    main()
