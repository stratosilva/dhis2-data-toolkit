"""
Fix the BMI 35 boundary in the Egypt CVD-risk program rules.

The WHO-chart cells write the 30-35 obesity band as

    #{bmi} >= 30 && #{bmi} <= 35

and the band above it as

    #{bmi} >= 35

so a BMI of exactly 35.0 satisfies both. The two rules assign different risk
scores and classifications, which DHIS2 rejects on import (E1307) and resolves
arbitrarily in the capture app. Every other boundary in the chart is half-open
(`>= 20 && < 25`), so this is the only value affected.

This replaces `#{bmi} <= 35` with `#{bmi} < 35`, making that boundary behave
like all the others.

    python fix_bmi35_boundary.py                   # show what would change
    python fix_bmi35_boundary.py --confirm APPLY    # apply it

Every original condition is written to a timestamped backup first, and each new
condition is validated by the server before being saved.
"""

import argparse
import json
import re
import sys
from datetime import datetime

from dhis2 import RequestException as DHIS2RequestException
from logzero import logger

from tracker_common import load_api

TARGET_ENV_PREFIX = 'EGYPT_NCD'
PROGRAM = 'pMIglSEqPGS'
CONFIRM_PHRASE = 'APPLY'

# The exact text to replace, and what to replace it with. Anchored on the
# closing brace so a stray "bmi <= 350" could never be caught by accident.
PATTERN = re.compile(r'#\{bmi\}\s*<=\s*35(?![0-9.])')
REPLACEMENT = '#{bmi} < 35'


def load_rules(api):
    return api.get('programRules', params={
        'filter': f'program.id:eq:{PROGRAM}', 'paging': 'false',
        'fields': 'id,name,condition',
    }).json()['programRules']


def validate_condition(api, condition):
    """
    Ask the server whether a condition expression is valid.

    This endpoint takes the raw expression as the request body, not JSON, so it
    goes through the session directly rather than api.post().
    """
    url = f'{api.base_url}/api/programRules/condition/description'
    try:
        r = api.session.post(url, params={'programId': PROGRAM},
                             data=condition.encode('utf-8'),
                             headers={'Content-Type': 'text/plain'}, timeout=60)
        body = r.json()
        return body.get('status') == 'OK', body.get('message') or body.get('description')
    except Exception as e:
        return False, str(e)[:200]


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--confirm', default=None,
                        help=f'Type {CONFIRM_PHRASE} to save the changes. Without it '
                             'the script only reports what it would do.')
    args = parser.parse_args()

    api = load_api(TARGET_ENV_PREFIX, 'target (Egypt NCD)')
    logger.info(f"Target DHIS2 {api.version}")

    rules = load_rules(api)
    affected = [r for r in rules if PATTERN.search(r['condition'] or '')]
    occurrences = sum(len(PATTERN.findall(r['condition'])) for r in affected)

    logger.info(f"{len(rules)} program rules on the programme")
    logger.info(f"{len(affected)} contain '#{{bmi}} <= 35', "
                f"{occurrences} occurrence(s) in total")

    if not affected:
        logger.info("Nothing to change.")
        return

    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    backup_path = f'bmi35-backup-{stamp}.json'
    with open(backup_path, 'w', encoding='utf-8') as f:
        json.dump([{'id': r['id'], 'name': r['name'], 'condition': r['condition']}
                   for r in affected], f, indent=2)
    logger.info(f"Original conditions backed up to {backup_path}")

    for r in affected[:5]:
        logger.info(f"  {r['id']}  {r['name']}  "
                    f"({len(PATTERN.findall(r['condition']))} occurrence(s))")
    if len(affected) > 5:
        logger.info(f"  ... and {len(affected) - 5} more")

    if args.confirm != CONFIRM_PHRASE:
        logger.warning(f"Nothing saved. Re-run with --confirm {CONFIRM_PHRASE} "
                       f"to change {occurrences} occurrence(s) "
                       f"across {len(affected)} rule(s).")
        return

    updated = failed = 0
    for r in affected:
        new_condition = PATTERN.sub(REPLACEMENT, r['condition'])

        ok, message = validate_condition(api, new_condition)
        if not ok:
            logger.error(f"  {r['id']} {r['name']}: server rejected the new "
                         f"condition ({message}); left unchanged")
            failed += 1
            continue

        # Read the full owned representation so the PUT cannot drop fields the
        # listing above did not ask for.
        try:
            full = api.get(f'programRules/{r["id"]}', params={'fields': ':owner'}).json()
            full['condition'] = new_condition
            api.put(f'programRules/{r["id"]}', json=full)
            updated += 1
            if updated % 10 == 0:
                logger.info(f"  updated {updated}/{len(affected)}")
        except DHIS2RequestException as e:
            logger.error(f"  {r['id']} {r['name']}: save failed ({str(e)[:160]})")
            failed += 1

    logger.info(f"Updated {updated} rule(s), failed {failed}")

    left = [r for r in load_rules(api) if PATTERN.search(r['condition'] or '')]
    logger.info(f"Rules still containing '#{{bmi}} <= 35': {len(left)}")
    if left:
        logger.warning(f"  {[r['id'] for r in left][:10]}")
    else:
        logger.warning("Boundary fixed. Re-run the risk rule verification to confirm "
                       "no overlapping cells remain.")


if __name__ == '__main__':
    main()
