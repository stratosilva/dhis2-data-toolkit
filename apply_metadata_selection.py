"""
Carry out the metadata moves selected in metadata-selection.json.

diff_metadata.py writes that file with every difference set to 'inherit' and
every theme set to 'skip', so a freshly generated selection does nothing until
you edit it. Set an action on a theme to cover all its differences, or on an
individual difference to override its theme:

    skip      leave both instances alone
    a_to_b    copy side A's version to side B, creating it there if absent
    b_to_a    copy side B's version to side A, creating it there if absent
    delete_a  delete the object from side A
    delete_b  delete the object from side B

Then:

    python apply_metadata_selection.py                    # validate only
    python apply_metadata_selection.py --confirm APPLY    # actually write

Without --confirm nothing is committed: each payload is sent to the destination
with importMode=VALIDATE, so the report is the destination server's own verdict
rather than a guess. Every object this script would overwrite or delete is
backed up first, as it currently stands on the destination.

Objects are handled in dependency order, so a data element is created before
the program indicator that exports to it and before the indicator that reads
it. Deletions run in the reverse order.
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime

from dhis2 import RequestException as DHIS2RequestException
from logzero import logger

from diff_metadata import (NEVER_BULK, SELECTION_FILE, SPECS, differing_fields,
                           normalise)
from tracker_common import guard_destructive, load_api

CONFIRM_PHRASE = 'APPLY'

# Dependency order for creates and updates. A referenced object has to exist on
# the destination before the object that points at it.
APPLY_ORDER = [
    'constants',
    'optionSets',
    'trackedEntityAttributes',
    'dataElements',
    'indicatorTypes',
    'programRuleVariables',
    'programStages',
    'programRules',
    'programIndicators',
    'indicators',
    'indicatorGroups',
    'aggregateDataExchanges',
    'programs',
]

# Copying the programme object itself would carry its org unit assignments,
# which legitimately differ between instances: sandbox 3 has 33 the staging
# hierarchy does not, and staging has 382 sandbox 3 does not. Overwriting one
# side's list with the other's would unassign the programme from real
# facilities.
GUARDED_TYPES = {'programs'}

# Never sent to the destination: it keeps its own ownership and sharing, and
# these say nothing about configuration.
STRIP = {'created', 'lastUpdated', 'lastUpdatedBy', 'createdBy', 'user',
         'access', 'href', 'favorites', 'favorite', 'sharing', 'publicAccess',
         'userAccesses', 'userGroupAccesses', 'displayName', 'displayShortName',
         'displayFormName', 'displayDescription'}


def strip_for_transfer(obj):
    """The object as it should be sent to the other instance."""
    return {k: v for k, v in obj.items() if k not in STRIP}


def merge_fields(source, dest, fields, obj_type):
    """
    The destination object with only the differing fields taken from the source.

    Copying a whole object across instances does not work: a program rule's
    programRuleActions, a stage's programStageDataElements and an indicator's
    analyticsPeriodBoundaries all carry UIDs minted on the instance that owns
    them, so sending side A's object wholesale makes the destination reference
    nested objects it does not have (E5002). Patching only the fields the diff
    reported keeps the destination's own nested objects intact.

    Returns the merged object, the fields applied, and the fields that need
    handling this cannot do.
    """
    merged = dict(dest)
    applied, skipped = [], []
    nested = SPECS[obj_type]['nested']

    for field in fields:
        if field in nested:
            if obj_type == 'programStages' and field == 'programStageDataElements':
                added = add_stage_data_elements(source, merged)
                if added:
                    applied.append(f"{field} (+{len(added)})")
                else:
                    skipped.append(f"{field} (nothing to add)")
                continue
            if field == 'attributeValues':
                changed = merge_attribute_values(source, merged)
                if changed:
                    applied.append(f"{field} ({', '.join(changed)})")
                else:
                    skipped.append(f"{field} (nothing to change)")
                continue
            skipped.append(field)
            continue
        if field in source:
            merged[field] = source[field]
        else:
            merged.pop(field, None)
        applied.append(field)

    return merged, applied, skipped


def add_stage_data_elements(source, merged):
    """
    Assign data elements the source stage has and the destination does not.

    This is the one nested collection worth handling, because a data element
    that is not on the stage cannot be captured or assigned by a program rule -
    which is why Risk Classification does nothing on an instance that is
    missing the assignment. Existing members are left exactly as they are, and
    the new ones are sent without a UID so the destination mints its own.
    """
    existing = {(m.get('dataElement') or {}).get('id')
                for m in merged.get('programStageDataElements', [])}
    additions = []
    for member in source.get('programStageDataElements', []):
        uid = (member.get('dataElement') or {}).get('id')
        if uid and uid not in existing:
            fresh = {k: v for k, v in member.items()
                     if k not in STRIP and k not in ('id', 'uid', 'programStage')}
            additions.append(fresh)
    if additions:
        merged['programStageDataElements'] = \
            list(merged.get('programStageDataElements', [])) + additions
    return additions


# Children that are separate metadata objects but have no independent meaning:
# creating the parent on an instance that has never seen it means creating
# these too, in the same payload. Each entry maps the parent type to the child
# endpoint and the filter that finds them.
OWNED_CHILDREN = {
    'programRules': ('programRuleActions', 'programRule.id'),
    'optionSets': ('options', 'optionSet.id'),
}


def merge_attribute_values(source, merged):
    """
    Take the source's metadata attribute values, keeping any the destination
    has of its own.

    This matters for `vudyDP7jUy5`, "Data element for aggregate data export".
    Its value on a program indicator names the aggregate data element the
    exchange writes that indicator's results into - the exchange holds no
    mapping table, it reads this attribute off each indicator via
    `outputIdScheme: ATTRIBUTE:vudyDP7jUy5`. An indicator without it exports
    nowhere, silently.

    Upserted by attribute rather than replaced wholesale, so an attribute only
    the destination sets is not dropped.
    """
    by_attribute = {(m.get('attribute') or {}).get('id'): m
                    for m in merged.get('attributeValues', [])
                    if (m.get('attribute') or {}).get('id')}
    changed = []

    for member in source.get('attributeValues', []):
        attribute = (member.get('attribute') or {}).get('id')
        if not attribute:
            continue
        existing = by_attribute.get(attribute, {}).get('value')
        if existing == member.get('value'):
            continue
        by_attribute[attribute] = {'attribute': {'id': attribute},
                                   'value': member.get('value')}
        changed.append(attribute)

    if changed:
        merged['attributeValues'] = list(by_attribute.values())
    return changed


def owned_children(api, obj_type, uid):
    """
    Objects that have to be created alongside a new object.

    A program rule is useless without its actions and an option set without its
    options, and in both cases the children are separate objects the importer
    will otherwise reject the parent for referencing (E5002).
    """
    if obj_type not in OWNED_CHILDREN:
        return {}
    endpoint, filter_field = OWNED_CHILDREN[obj_type]
    try:
        children = api.get(endpoint, params={
            'filter': f'{filter_field}:eq:{uid}', 'fields': ':owner',
            'paging': 'false'}).json().get(endpoint, [])
    except DHIS2RequestException:
        return {}
    return {endpoint: [strip_for_transfer(c) for c in children]} if children else {}


def resolve_actions(selection):
    """
    The action for each difference, with 'inherit' resolved against its theme.

    Returns the list of things to do and the reasons anything was refused.
    """
    groups = selection.get('groups', {})
    todo, refused = [], []

    for diff in selection.get('differences', []):
        action = diff.get('action', 'inherit')
        source = 'object'
        if action == 'inherit':
            group = groups.get(diff.get('group'), {})
            action = group.get('action', 'skip')
            source = f"theme {diff.get('group')}"

        if action == 'skip':
            continue

        if action not in ('a_to_b', 'b_to_a', 'delete_a', 'delete_b'):
            refused.append((diff, f"unknown action '{action}'"))
            continue

        if source.startswith('theme') and diff.get('group') in NEVER_BULK:
            refused.append((diff, f"theme {diff['group']} cannot be actioned in "
                                  f"bulk; set an action on this object itself"))
            continue

        if diff['type'] in GUARDED_TYPES:
            refused.append((diff, f"{diff['type']} is not copied wholesale; "
                                  f"see GUARDED_TYPES"))
            continue

        todo.append({**diff, 'resolved': action, 'from': source})

    return todo, refused


def fetch_one(api, endpoint, uid):
    """One object as the server owns it, or None if it is not there."""
    try:
        return api.get(f'{endpoint}/{uid}', params={'fields': ':owner'}).json()
    except DHIS2RequestException as e:
        if getattr(e, 'code', None) == 404:
            return None
        raise


UID_IN_MESSAGE = re.compile(r'\[(\w{11})\]')
MISSING_REFERENCE_CODES = ('E5002', 'E3012')


def referenced_uids(messages):
    """UIDs named in the importer's complaints."""
    found = set()
    for message in messages:
        found |= set(UID_IN_MESSAGE.findall(message))
    return found


def pending_only(messages, would_create, own_uid):
    """
    Whether every complaint is about a reference this run creates first.

    Used to keep the dry run honest: these are not failures, they are the
    consequence of validating without committing.
    """
    if not messages:
        return False
    for message in messages:
        if not any(code in message for code in MISSING_REFERENCE_CODES):
            return False
        # The message names the object being imported as well as the
        # reference it could not resolve; only the reference matters.
        named = set(UID_IN_MESSAGE.findall(message)) - {own_uid}
        # The object being imported names itself in the message too, so only
        # the references matter.
        if not named or not (named & would_create):
            return False
    return True


def error_messages(report):
    """Every error the importer reported, flattened."""
    messages = []
    for type_report in (report.get('typeReports') or []):
        for object_report in (type_report.get('objectReports') or []):
            for error in (object_report.get('errorReports') or []):
                messages.append(f"{error.get('errorCode')}: {error.get('message')}")
    return messages


def send(api, payload, commit):
    """
    Create or update objects through the metadata importer.

    atomicMode ALL keeps a partial object out of the destination, and
    importMode VALIDATE makes the dry run the server's own judgement rather
    than this script's.

    A rejected import comes back as a 409, which dhis2.py raises; the import
    report is in the exception body, so it is parsed out rather than lost.
    """
    params = {
        'importStrategy': 'CREATE_AND_UPDATE',
        'atomicMode': 'ALL',
        'importMode': 'COMMIT' if commit else 'VALIDATE',
    }
    try:
        body = api.post('metadata', params=params, json=payload).json()
    except DHIS2RequestException as e:
        raw = getattr(e, 'description', None)
        try:
            body = json.loads(raw)
        except (TypeError, ValueError):
            return 'ERROR', [f"HTTP {getattr(e, 'code', '?')}: {str(e)[:200]}"]

    report = body.get('response', body)
    status = report.get('status') or body.get('status')
    messages = error_messages(report)
    if status in ('OK', 'SUCCESS') and not messages:
        return 'OK', []
    return 'ERROR', messages or [body.get('message', 'rejected')]


def remove(api, endpoint, uid, commit):
    if not commit:
        return 'OK', ['would delete']
    try:
        api.delete(f'{endpoint}/{uid}')
        return 'OK', []
    except DHIS2RequestException as e:
        return 'ERROR', [str(e)[:220]]


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--selection', default=SELECTION_FILE,
                        help='Selection file to read (default: %(default)s)')
    parser.add_argument('--confirm', default=None,
                        help=f'Type {CONFIRM_PHRASE} to commit. Without it every '
                             f'change is validated against the destination and '
                             f'discarded.')
    parser.add_argument('--allow-production', action='store_true',
                        help='Permit writing to a destination that looks like '
                             'production.')
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass

    if not os.path.exists(args.selection):
        logger.error(f"{args.selection} not found. Run diff_metadata.py first.")
        sys.exit(2)

    with open(args.selection, 'r', encoding='utf-8') as f:
        selection = json.load(f)

    prefix_a = selection['sideA']['prefix']
    prefix_b = selection['sideB']['prefix']
    commit = args.confirm == CONFIRM_PHRASE

    todo, refused = resolve_actions(selection)

    if refused:
        logger.warning(f"{len(refused)} selection(s) refused:")
        for diff, reason in refused:
            logger.warning(f"  {diff['type']}/{diff['uid']}: {reason}")

    if not todo:
        logger.warning("Nothing selected. Set an action on a theme or an object "
                       f"in {args.selection}, then re-run.")
        return

    api_a = load_api(prefix_a, f'side A ({prefix_a})')
    api_b = load_api(prefix_b, f'side B ({prefix_b})')

    # Only the instances actually written to need guarding.
    writes_to_b = any(t['resolved'] in ('a_to_b', 'delete_b') for t in todo)
    writes_to_a = any(t['resolved'] in ('b_to_a', 'delete_a') for t in todo)
    if writes_to_b:
        guard_destructive(prefix_b, api_b, args.allow_production,
                          'overwrite or delete metadata')
    if writes_to_a:
        guard_destructive(prefix_a, api_a, args.allow_production,
                          'overwrite or delete metadata')

    logger.warning(f"A = {prefix_a:16} {api_a.base_url}")
    logger.warning(f"B = {prefix_b:16} {api_b.base_url}")
    logger.warning(f"{len(todo)} object(s) selected; mode = "
                   f"{'COMMIT' if commit else 'VALIDATE ONLY'}")

    # Deletes last, and in reverse dependency order, so nothing is removed
    # while something still points at it.
    def sort_key(item):
        deleting = item['resolved'].startswith('delete')
        try:
            position = APPLY_ORDER.index(item['type'])
        except ValueError:
            position = len(APPLY_ORDER)
        return (1 if deleting else 0,
                -position if deleting else position,
                item['uid'])

    todo.sort(key=sort_key)

    # In VALIDATE mode nothing is committed, so an object that references one
    # created earlier in the same run is rejected for a reference that will in
    # fact exist by the time it is written. Knowing which UIDs this run would
    # create lets those be reported as pending rather than as failures.
    would_create = {prefix_a: set(), prefix_b: set()}
    for item in todo:
        if item['resolved'] == 'a_to_b':
            would_create[prefix_b].add(item['uid'])
        elif item['resolved'] == 'b_to_a':
            would_create[prefix_a].add(item['uid'])

    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    backup_path = f'metadata-backup-{stamp}.json'
    backups = []
    results = {'ok': 0, 'error': 0, 'pending': 0}

    for item in todo:
        endpoint = SPECS[item['type']]['endpoint']
        action = item['resolved']
        source_api, dest_api, dest_prefix = (
            (api_a, api_b, prefix_b) if action in ('a_to_b', 'delete_b')
            else (api_b, api_a, prefix_a))

        label = f"{item['type']}/{item['uid']}  {item['name'][:44]}"

        try:
            current = fetch_one(dest_api, endpoint, item['uid'])
        except DHIS2RequestException as e:
            logger.error(f"  {action:9} {label}: could not read destination "
                         f"({str(e)[:100]})")
            results['error'] += 1
            continue

        # Record what is being displaced, whether or not this run commits, so a
        # validate pass still leaves a record of what would have been lost.
        if current is not None:
            backups.append({'instance': dest_prefix, 'type': item['type'],
                            'uid': item['uid'], 'action': action,
                            'object': current})

        if action.startswith('delete'):
            if current is None:
                logger.info(f"  {action:9} {label}: already absent")
                continue
            status, messages = remove(dest_api, endpoint, item['uid'], commit)
            if status == 'OK':
                logger.info(f"  {action:9} {label}: "
                            f"{'deleted' if commit else 'would delete'}")
                results['ok'] += 1
            else:
                logger.error(f"  {action:9} {label}: {'; '.join(messages)[:200]}")
                results['error'] += 1
            continue

        source = fetch_one(source_api, endpoint, item['uid'])
        if source is None:
            logger.error(f"  {action:9} {label}: absent on the source; the "
                         f"selection file may be out of date")
            results['error'] += 1
            continue

        skipped = []
        if current is None:
            # Nothing there to preserve, so the whole object goes, together
            # with the children it cannot work without.
            payload = {endpoint: [strip_for_transfer(source)]}
            payload.update(owned_children(source_api, item['type'], item['uid']))
            verb = 'created'
        else:
            # The differing fields are recomputed against what the two
            # instances hold right now, not taken from the selection file. The
            # file says which objects to act on; between generating it and
            # running this, an object recorded as missing may have been created
            # by hand, and its stale (empty) field list would otherwise make
            # the script give up on an object it could patch.
            live = differing_fields(
                normalise(source, SPECS[item['type']], False, False),
                normalise(current, SPECS[item['type']], False, False))
            if not live:
                logger.info(f"  {action:9} {label}: already identical")
                results['ok'] += 1
                continue
            merged, applied, skipped = merge_fields(
                source, current, sorted(live), item['type'])
            if not applied:
                logger.warning(f"  {action:9} {label}: nothing this script can "
                               f"patch (differs in {', '.join(skipped)})")
                results['error'] += 1
                continue
            payload = {endpoint: [strip_for_transfer(merged)]}
            verb = f"updated [{', '.join(applied)}]"

        status, messages = send(dest_api, payload, commit)

        if status == 'OK':
            logger.info(f"  {action:9} {label}: "
                        f"{verb if commit else 'would be ' + verb}")
            if skipped:
                logger.warning(f"      not patched: {', '.join(skipped)}")
            results['ok'] += 1
        elif not commit and pending_only(messages, would_create[dest_prefix],
                                         item['uid']):
            logger.warning(f"  {action:9} {label}: pending - references "
                           f"{', '.join(sorted((referenced_uids(messages) - {item['uid']}) & would_create[dest_prefix]))}, "
                           f"created earlier in this run")
            results['pending'] += 1
        else:
            logger.error(f"  {action:9} {label}: {'; '.join(messages)[:240]}")
            results['error'] += 1

    if backups:
        with open(backup_path, 'w', encoding='utf-8') as f:
            json.dump({'generated': datetime.now().isoformat(timespec='seconds'),
                       'committed': commit, 'objects': backups}, f, indent=2,
                      ensure_ascii=False)
        logger.info(f"{len(backups)} displaced object(s) backed up to {backup_path}")

    logger.warning('=' * 60)
    logger.warning(f"{results['ok']} succeeded, {results['pending']} pending a "
                   f"dependency, {results['error']} failed")
    if not commit:
        logger.warning(f"Nothing was written. Re-run with --confirm "
                       f"{CONFIRM_PHRASE} to commit.")
    elif results['error']:
        logger.error("Some objects failed. Fix the causes and re-run; "
                     "succeeded objects are already in place.")


if __name__ == '__main__':
    main()
