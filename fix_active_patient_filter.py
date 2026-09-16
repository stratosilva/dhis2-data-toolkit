"""
Make the Egypt program indicators select only ACTIVE patients.

31 program indicators filter on

    A{fI1P3Mg1zOZ} != 'DIED'

which was equivalent to "still in care" when DIED was the only non-active
status. The NCD Patient Status option set now carries ACTIVE, TRANSFER, DIED
and INACTIVE, so `!= 'DIED'` also counts transferred and inactive patients.
This replaces it with

    A{fI1P3Mg1zOZ} == 'ACTIVE'

which is what the three indicators that were already written that way use
(fhDVOoY7gHf, bLiMzdg6ZHz, ApG3MF1VmOL).

`EWpkCPCuJf3` (HTN - Cumulative dead patients) tests `== 'DIED'` and is
deliberately left alone.

    python fix_active_patient_filter.py                  # show what would change
    python fix_active_patient_filter.py --confirm APPLY   # apply it

Originals are backed up first and every new filter is validated by the server
before it is saved.
"""

import argparse
import json
import re
import sys
from datetime import datetime

from dhis2 import RequestException as DHIS2RequestException
from logzero import logger

from tracker_common import load_api

# Sandbox 3, not production. These scripts purge and rewrite data.
TARGET_ENV_PREFIX = 'EGYPT_SANDBOX3'
PROGRAM = 'pMIglSEqPGS'
CONFIRM_PHRASE = 'APPLY'
STATUS_TEA = 'fI1P3Mg1zOZ'

PATTERN = re.compile(r"A\{" + STATUS_TEA + r"\}\s*!=\s*'DIED'")
REPLACEMENT = "A{" + STATUS_TEA + "} == 'ACTIVE'"

# Correct as it stands: this one counts the dead on purpose.
LEAVE_ALONE = {'EWpkCPCuJf3'}


def load_pis(api):
    return api.get('programIndicators', params={
        'filter': f'program.id:eq:{PROGRAM}', 'paging': 'false',
        'fields': 'id,name,filter',
    }).json()['programIndicators']


def validate_filter(api, expression):
    """
    Ask the server whether a program indicator filter is valid. The endpoint
    takes the raw expression as the body, so it bypasses api.post().
    """
    url = f'{api.base_url}/api/programIndicators/filter/description'
    try:
        r = api.session.post(url, params={'programIndicatorId': ''},
                             data=expression.encode('utf-8'),
                             headers={'Content-Type': 'text/plain'}, timeout=60)
        body = r.json()
        return body.get('status') == 'OK', body.get('message') or body.get('description')
    except Exception as e:
        return False, str(e)[:200]


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--target', default=TARGET_ENV_PREFIX,
                        help='Credential prefix in .env naming the instance to act on (default: %(default)s). Use this to point the same script at a different Egypt instance, e.g. --target EGYPT_STAGING.')
    parser.add_argument('--confirm', default=None,
                        help=f'Type {CONFIRM_PHRASE} to save the changes.')
    args = parser.parse_args()

    api = load_api(args.target, f'target ({args.target})')
    logger.info(f"Target DHIS2 {api.version}")

    pis = load_pis(api)
    affected = [p for p in pis
                if PATTERN.search(p.get('filter') or '') and p['id'] not in LEAVE_ALONE]
    occurrences = sum(len(PATTERN.findall(p['filter'])) for p in affected)

    logger.info(f"{len(pis)} program indicators on the programme")
    logger.info(f"{len(affected)} filter on != 'DIED', {occurrences} occurrence(s)")
    for uid in sorted(LEAVE_ALONE):
        match = next((p for p in pis if p['id'] == uid), None)
        if match:
            logger.info(f"  leaving alone: {uid} {match['name'][:56]}")

    if not affected:
        logger.info("Nothing to change.")
        return

    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    backup = f'active-filter-backup-{stamp}.json'
    with open(backup, 'w', encoding='utf-8') as f:
        json.dump([{'id': p['id'], 'name': p['name'], 'filter': p['filter']}
                   for p in affected], f, indent=2)
    logger.info(f"Original filters backed up to {backup}")

    if args.confirm != CONFIRM_PHRASE:
        logger.warning(f"Nothing saved. Re-run with --confirm {CONFIRM_PHRASE} to "
                       f"change {len(affected)} program indicator(s).")
        return

    updated = failed = 0
    for p in affected:
        new_filter = PATTERN.sub(REPLACEMENT, p['filter'])

        ok, message = validate_filter(api, new_filter)
        if not ok:
            logger.error(f"  {p['id']} {p['name'][:40]}: server rejected the new "
                         f"filter ({message}); left unchanged")
            failed += 1
            continue

        try:
            full = api.get(f'programIndicators/{p["id"]}',
                           params={'fields': ':owner'}).json()
            full['filter'] = new_filter
            api.put(f'programIndicators/{p["id"]}', json=full)
            updated += 1
            if updated % 10 == 0:
                logger.info(f"  updated {updated}/{len(affected)}")
        except DHIS2RequestException as e:
            logger.error(f"  {p['id']} {p['name'][:40]}: save failed ({str(e)[:140]})")
            failed += 1

    logger.info(f"Updated {updated}, failed {failed}")

    after = load_pis(api)
    left = [p for p in after
            if PATTERN.search(p.get('filter') or '') and p['id'] not in LEAVE_ALONE]
    now_active = [p for p in after
                  if re.search(r"A\{" + STATUS_TEA + r"\}\s*==\s*'ACTIVE'", p.get('filter') or '')]
    logger.info(f"Still filtering on != 'DIED': {len(left)}")
    logger.info(f"Now filtering on == 'ACTIVE': {len(now_active)}")


if __name__ == '__main__':
    main()
