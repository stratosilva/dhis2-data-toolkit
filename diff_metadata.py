"""
Compare the Hypertension & Diabetes programme metadata between two instances.

The preflight checks that an instance *has* the UIDs the scripts address. It
says nothing about whether those objects are configured the same way, and that
is where the two Egypt instances have drifted: program rules whose conditions
differ, rule actions present on one side only, data elements assigned to the
stage on one side only, program indicator filters and boundaries that do not
match.

This reads both instances and reports every difference, then writes a
selection file listing each one with an action you can edit:

    skip    leave both instances as they are
    a_to_b  push side A's version to side B
    b_to_a  push side B's version to side A

apply_metadata_selection.py then carries out only the actions you chose.

    python diff_metadata.py                                  # sandbox3 vs staging
    python diff_metadata.py --a EGYPT_SANDBOX3 --b EGYPT_STAGING
    python diff_metadata.py --only programRules,programStages
    python diff_metadata.py --include-sharing

Read-only. It writes nothing to either server.

Neither side is assumed correct. Differences explained by a change recorded in
EGYPT_METADATA_CHANGES.md are annotated, because those were applied on sandbox 3
deliberately and have not yet been carried over.
"""

import argparse
import difflib
import json
import os
import re
import sys
from datetime import datetime

from dhis2 import RequestException as DHIS2RequestException
from logzero import logger

from tracker_common import load_api

DEFAULT_A = 'EGYPT_SANDBOX3'
DEFAULT_B = 'EGYPT_STAGING'
PROGRAM = 'pMIglSEqPGS'

SELECTION_FILE = 'metadata-selection.json'
CHANGE_LOG = 'EGYPT_METADATA_CHANGES.md'

# Fields that differ between any two instances for reasons that have nothing to
# do with configuration, and would bury the real differences.
VOLATILE = {
    'created', 'lastUpdated', 'lastUpdatedBy', 'createdBy', 'href', 'access',
    'displayName', 'displayShortName', 'displayFormName', 'displayDescription',
    'favorites', 'favorite', 'user',
}

# Dropped from members of an owned nested collection: their UIDs are minted
# per instance and mean nothing across instances, and the back-reference to the
# parent is noise.
NESTED_NOISE = VOLATILE | {
    'id', 'uid', 'programStage', 'programRule', 'program', 'programIndicator',
    'trackedEntityType',
}


def key_of(*paths):
    """
    Build a stable comparison key for a member of a nested collection.

    DHIS2 returns these collections in arbitrary order, so they have to be
    matched on what they mean rather than on position. Each path is a
    dotted lookup, e.g. 'dataElement.id'.
    """
    def extract(member):
        parts = []
        for path in paths:
            value = member
            for step in path.split('.'):
                value = (value or {}).get(step) if isinstance(value, dict) else None
            parts.append(str(value))
        return '|'.join(parts)
    return extract


# Each entry: how to fetch it, and which nested collections to match on meaning
# rather than order.
SPECS = {
    'programs': {
        'endpoint': 'programs',
        'params': {'filter': f'id:eq:{PROGRAM}'},
        'label': 'programme',
        'nested': {
            'programTrackedEntityAttributes': key_of('trackedEntityAttribute.id'),
            'programStages': key_of('id'),
            'organisationUnits': key_of('id'),
        },
    },
    'programStages': {
        'endpoint': 'programStages',
        'params': {'filter': f'program.id:eq:{PROGRAM}'},
        'label': 'program stage',
        'nested': {
            'programStageDataElements': key_of('dataElement.id'),
            'programStageSections': key_of('name'),
            'notificationTemplates': key_of('name'),
        },
    },
    'programRules': {
        'endpoint': 'programRules',
        'params': {'filter': f'program.id:eq:{PROGRAM}'},
        'label': 'program rule',
        'nested': {
            # A rule can carry several actions of the same type, so the key has
            # to include what the action targets.
            'programRuleActions': key_of(
                'programRuleActionType', 'dataElement.id', 'attribute.id',
                'programIndicator.id', 'content', 'data'),
        },
    },
    'programRuleVariables': {
        'endpoint': 'programRuleVariables',
        'params': {'filter': f'program.id:eq:{PROGRAM}'},
        'label': 'program rule variable',
        'nested': {},
    },
    'programIndicators': {
        'endpoint': 'programIndicators',
        'params': {'filter': f'program.id:eq:{PROGRAM}'},
        'label': 'program indicator',
        'nested': {
            'analyticsPeriodBoundaries': key_of(
                'boundaryTarget', 'analyticsPeriodBoundaryType',
                'offsetPeriods', 'offsetPeriodType'),
            'attributeValues': key_of('attribute.id'),
            'legendSets': key_of('id'),
            'categoryMappingIds': key_of('id'),
        },
    },
    'constants': {
        'endpoint': 'constants',
        'params': {},
        'label': 'constant',
        'nested': {},
        # The whole instance, not just this programme; filtered below to the
        # ones the cascade indicators use.
        'restrict_to': {'n1e76QJKUzt', 'AQwjmAAFrCh'},
    },
    # Aggregate indicators: the dashboard reads these, and new ones were added
    # on sandbox 3 for statins prescribed and for the two control thresholds
    # (140/90 and 130/80). Both instances carry well under a hundred, so they
    # are compared wholesale rather than scoped to a group.
    'indicators': {
        'endpoint': 'indicators',
        'params': {},
        'label': 'indicator',
        'nested': {'legendSets': key_of('id'), 'attributeValues': key_of('attribute.id')},
    },
    'indicatorTypes': {
        'endpoint': 'indicatorTypes',
        'params': {},
        'label': 'indicator type',
        'nested': {},
    },
    'indicatorGroups': {
        'endpoint': 'indicatorGroups',
        'params': {},
        'label': 'indicator group',
        'nested': {'indicators': key_of('id')},
        'restrict_to': {'trCqci4WNIg', 'Lchpd4wnQpj'},
    },
    # Every data element, not a curated list. A program rule action, a rule
    # variable or a program indicator can reference any of them, and a
    # reference to one the destination lacks fails the import (E5002). Both
    # instances carry only ~130, so comparing the lot costs nothing and cannot
    # miss a dependency.
    'dataElements': {
        'endpoint': 'dataElements',
        'params': {},
        'label': 'data element',
        'nested': {
            'dataSetElements': key_of('dataSet.id'),
            'legendSets': key_of('id'),
            'attributeValues': key_of('attribute.id'),
        },
    },
    'trackedEntityAttributes': {
        'endpoint': 'trackedEntityAttributes',
        'params': {},
        'label': 'tracked entity attribute',
        'nested': {'legendSets': key_of('id'),
                   'attributeValues': key_of('attribute.id')},
    },
    # Data elements and attributes reference option sets, so an option set the
    # destination lacks blocks the data element that uses it. The options
    # themselves are compared as members of their set, keyed on code, because
    # an option's UID is minted per instance while its code is the value the
    # generator actually writes.
    'optionSets': {
        'endpoint': 'optionSets',
        'params': {},
        'label': 'option set',
        'nested': {'options': key_of('code')},
        'expand': {'options': 'id,code,name,sortOrder'},
        # sortOrder is numbered from 0 on one instance and from 1 on the other,
        # so every option of every set reads as changed while the order is in
        # fact identical. Comparing position within the set instead still
        # catches a genuine reordering.
        'rank': {'options': 'sortOrder'},
    },
    'aggregateDataExchanges': {
        'endpoint': 'aggregateDataExchanges',
        'params': {},
        'label': 'aggregate data exchange',
        'nested': {},
        'name_prefix': 'ADEX - HTN DM',
    },
}

TYPE_ORDER = ['programs', 'programStages', 'programRules', 'programRuleVariables',
              'programIndicators', 'constants', 'indicators', 'indicatorTypes',
              'indicatorGroups', 'dataElements', 'trackedEntityAttributes',
              'optionSets']

# Compared only when asked for with --only aggregateDataExchanges. The exchange
# configuration is maintained separately (replace_adex.py and the
# agg_data_exchange-*.json files), so it is left out of the default report.
OPTIONAL_TYPES = ['aggregateDataExchanges']

WHITESPACE = re.compile(r'\s+')

# Fields whose value is an expression: compared with whitespace collapsed, so a
# reformat does not read as a change.
EXPRESSION_FIELDS = {'condition', 'filter', 'expression', 'data', 'aggregationType'}

# A program rule has no enabled flag in 2.42. A rule is taken out of service
# either by marking its name or by wiring a constant false into its condition so
# it can never match. Both are detected, because "enabled on one side, disabled
# on the other" is the difference that matters most here and it would otherwise
# read as an ordinary name or condition change.
DISABLED_NAME = re.compile(r'\bDISABLED?\b', re.IGNORECASE)
DISABLED_CONDITION = re.compile(r'(?:^\s*|&&\s*)false\s*&&|&&\s*false\s*(?:$|&&)',
                                re.IGNORECASE)


def looks_disabled(obj):
    """Why this rule appears to be out of service, if it does."""
    reasons = []
    if DISABLED_NAME.search(obj.get('name') or ''):
        reasons.append('name')
    condition = WHITESPACE.sub(' ', obj.get('condition') or '')
    if DISABLED_CONDITION.search(condition) or condition.strip().lower() == 'false':
        reasons.append('condition')
    return reasons


# ---------------------------------------------------------------------------
# Fetching and normalising
# ---------------------------------------------------------------------------

def fetch(api, name, spec):
    """Every object of one type, keyed by UID, as the server owns it."""
    params = dict(spec['params'])
    fields = ':owner'
    # :owner returns a referenced collection as bare UIDs. Where the members'
    # own content is what matters - an option set's option codes - ask for it
    # explicitly.
    for field, subfields in (spec.get('expand') or {}).items():
        fields += f',{field}[{subfields}]'
    params.update({'fields': fields, 'paging': 'false'})
    try:
        response = api.get(spec['endpoint'], params=params).json()
    except DHIS2RequestException as e:
        logger.error(f"  {name}: fetch failed ({str(e)[:120]})")
        return None

    objects = response.get(spec['endpoint'], [])
    restrict = spec.get('restrict_to')
    if restrict:
        objects = [o for o in objects if o.get('id') in restrict]
    prefix = spec.get('name_prefix')
    if prefix:
        objects = [o for o in objects if (o.get('name') or '').startswith(prefix)]
    return {o['id']: o for o in objects if o.get('id')}


def canonical(value):
    """A stable sort key for a list member of any shape."""
    return json.dumps(value, sort_keys=True, default=str)


def scrub(value, drop):
    """
    Recursively remove keys that cannot be compared across instances.

    Lists are sorted canonically: DHIS2 returns collection members in no
    particular order, and for everything compared here order carries no
    meaning (position is expressed by an explicit sortOrder field where it
    matters). Without this, two identically configured instances differ on
    almost every object.
    """
    if isinstance(value, dict):
        return {k: scrub(v, drop) for k, v in value.items() if k not in drop}
    if isinstance(value, list):
        return sorted((scrub(v, drop) for v in value), key=canonical)
    return value


def normalise(obj, spec, include_sharing, include_translations):
    """
    An object reduced to the parts that carry configuration meaning.

    Nested collections become dicts keyed on meaning, so two servers that
    return the same members in a different order compare equal.
    """
    drop = set(VOLATILE)
    if not include_sharing:
        drop |= {'sharing', 'publicAccess', 'userAccesses', 'userGroupAccesses'}
    if not include_translations:
        drop |= {'translations'}

    out = {}
    for field, value in obj.items():
        if field in drop:
            continue
        if field in spec['nested'] and isinstance(value, list):
            keyfunc = spec['nested'][field]
            members = {}
            for member in value:
                if not isinstance(member, dict):
                    members[str(member)] = member
                    continue
                members[keyfunc(member)] = scrub(member, NESTED_NOISE | drop)
            rank_field = (spec.get('rank') or {}).get(field)
            if rank_field:
                ordered = sorted(members,
                                 key=lambda k: (members[k].get(rank_field) is None,
                                                members[k].get(rank_field)))
                for position, key in enumerate(ordered):
                    if members[key].get(rank_field) is not None:
                        members[key][rank_field] = position
            out[field] = members
        elif field in EXPRESSION_FIELDS and isinstance(value, str):
            out[field] = WHITESPACE.sub(' ', value).strip()
        else:
            out[field] = scrub(value, drop)
    return out


# ---------------------------------------------------------------------------
# Comparing
# ---------------------------------------------------------------------------

def differing_fields(a, b):
    """Which top-level fields differ, and how."""
    diffs = {}
    for field in sorted(set(a) | set(b)):
        left, right = a.get(field), b.get(field)
        if left == right:
            continue
        if isinstance(left, dict) and isinstance(right, dict):
            detail = {
                'only_in_a': sorted(set(left) - set(right)),
                'only_in_b': sorted(set(right) - set(left)),
                'changed': sorted(k for k in set(left) & set(right)
                                  if left[k] != right[k]),
            }
            diffs[field] = {'kind': 'collection', 'detail': detail,
                            'a': left, 'b': right}
        else:
            diffs[field] = {'kind': 'scalar', 'a': left, 'b': right}
    return diffs


def compare(name, spec, side_a, side_b, include_sharing, include_translations):
    """Every difference between the two sides for one object type."""
    results = []

    for uid in sorted(set(side_a) | set(side_b)):
        raw_a, raw_b = side_a.get(uid), side_b.get(uid)
        label = (raw_a or raw_b).get('name') or (raw_a or raw_b).get('displayName') or uid

        if raw_a is None or raw_b is None:
            entry = {
                'type': name, 'uid': uid, 'name': label,
                'kind': 'only_in_a' if raw_b is None else 'only_in_b',
                'fields': [],
            }
            if name == 'programRules':
                entry['disabled_a'] = looks_disabled(raw_a) if raw_a else None
                entry['disabled_b'] = looks_disabled(raw_b) if raw_b else None
            results.append(entry)
            continue

        norm_a = normalise(raw_a, spec, include_sharing, include_translations)
        norm_b = normalise(raw_b, spec, include_sharing, include_translations)
        diffs = differing_fields(norm_a, norm_b)
        if diffs:
            entry = {
                'type': name, 'uid': uid, 'name': label, 'kind': 'differs',
                'fields': sorted(diffs), 'diffs': diffs,
            }
            if name == 'programRules':
                entry['disabled_a'] = looks_disabled(raw_a)
                entry['disabled_b'] = looks_disabled(raw_b)
            results.append(entry)

    return results


def service_state(side_a, side_b):
    """
    Rules that are out of service on one instance but live on the other.

    This is reported separately from the field-by-field diff because it is the
    question the reviewer actually has, and a `false &&` guard would otherwise
    appear as just another condition difference among many.
    """
    def state(obj):
        if obj is None:
            return 'absent'
        reasons = looks_disabled(obj)
        return 'disabled via ' + '+'.join(reasons) if reasons else 'live'

    rows = []
    for uid in sorted(set(side_a) | set(side_b)):
        a, b = side_a.get(uid), side_b.get(uid)
        state_a, state_b = state(a), state(b)
        if state_a == state_b:
            continue
        # Absent on one side is already reported as only_in_*; worth repeating
        # here only when the side that has it has taken it out of service.
        if 'absent' in (state_a, state_b) and 'disabled' not in state_a + state_b:
            continue
        rows.append({'uid': uid, 'name': (a or b).get('name', uid),
                     'a': state_a, 'b': state_b})
    return rows


# ---------------------------------------------------------------------------
# Annotating against the change log
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------

# Differences fall into a small number of themes. Reviewing 150 objects one by
# one is not realistic, so each difference is tagged and the selection file
# carries a per-theme action as well as a per-object one.
BMI_BOUNDARY = re.compile(r'#\{[^}]*bmi[^}]*\}\s*<=?\s*35', re.IGNORECASE)
# Blood pressure thresholds as they are written in indicator names, e.g.
# "<140/90", ">=130/80".
THRESHOLD = re.compile(r'(\d{2,3})\s*/\s*(\d{2,3})')


def thresholds(text):
    return set(THRESHOLD.findall(text or ''))


# The systolic and diastolic data elements. A program indicator filter states
# its blood pressure threshold as a comparison against these, so a change of
# threshold hides inside an ordinary-looking filter change.
SBP_DE = 'IxEwYiq1FTq'
DBP_DE = 'yNhtHKtKkO1'
BP_COMPARISON = re.compile(
    r'#\{[^}]*(?:' + SBP_DE + '|' + DBP_DE + r')\}\s*(<=|>=|<|>|==)\s*(\d+)')


def bp_thresholds(expression):
    """
    The blood pressure cut-offs an expression tests, ignoring the >0 sanity
    guards that both sides carry.
    """
    return {(op, value) for op, value in BP_COMPARISON.findall(expression or '')
            if value != '0'}


DIED_FILTER = re.compile(r"!=\s*'DIED'")
ACTIVE_FILTER = re.compile(r"==\s*'ACTIVE'")
CASCADE_CONSTANTS = ('n1e76QJKUzt', 'AQwjmAAFrCh')


def classify(entry):
    """
    A theme for one difference, so related objects can be actioned together.

    Recognises the signatures of the changes applied on sandbox 3 this cycle.
    Anything unrecognised is left 'unclassified' and has to be read.
    """
    diffs = entry.get('diffs') or {}

    def both(field):
        d = diffs.get(field)
        if not d or d['kind'] != 'scalar':
            return '', ''
        return str(d.get('a') or ''), str(d.get('b') or '')

    if entry.get('disabled_a') or entry.get('disabled_b'):
        return 'rule-service-state'

    # The same UID carrying a different blood pressure threshold on each side.
    # This is not a rename: it means the two instances disagree about what the
    # object measures, and copying either way would silently redefine a figure
    # the other instance's dashboards already publish. Never auto-applied.
    name_a, name_b = both('name')
    short_a, short_b = both('shortName')
    t_a = thresholds(name_a) | thresholds(short_a)
    t_b = thresholds(name_b) | thresholds(short_b)
    if t_a and t_b and t_a != t_b:
        return 'bp-threshold'

    # A threshold change buried in a filter or formula. Checked before the
    # ACTIVE-filter and BMI themes, because a filter can carry both an
    # intended fix and a change of definition, and the change of definition
    # is the one that must not be applied in bulk.
    for field in ('filter', 'condition', 'numerator', 'denominator', 'expression'):
        left, right = both(field)
        if bp_thresholds(left) != bp_thresholds(right) and \
                (bp_thresholds(left) or bp_thresholds(right)):
            return 'bp-threshold'

    # Side A wraps a formula in isNull/if guards that side B lacks: a
    # divide-by-zero or blank-value fix rather than a change of meaning.
    formula_fields = ('numerator', 'denominator', 'expression')
    guarded_a = any('isNull(' in both(f)[0] for f in formula_fields)
    guarded_b = any('isNull(' in both(f)[1] for f in formula_fields)
    if guarded_a and not guarded_b:
        return 'null-guarded-formula'

    cond_a, cond_b = both('condition')
    if cond_a and cond_b and BMI_BOUNDARY.search(cond_b) and \
            cond_a.replace('< 35', '<= 35') == cond_b:
        return 'bmi35-boundary'

    filt_a, filt_b = both('filter')
    if DIED_FILTER.search(filt_b) and ACTIVE_FILTER.search(filt_a):
        return 'active-patient-filter'

    for field in ('expression', 'numerator', 'denominator'):
        a, b = both(field)
        if any(c in a for c in CASCADE_CONSTANTS) and \
                not any(c in b for c in CASCADE_CONSTANTS):
            return 'cascade-constants'

    if entry['type'] == 'constants' and entry['uid'] in CASCADE_CONSTANTS:
        return 'cascade-constants'

    # Objects that exist on one side only and are about a blood pressure
    # threshold belong with the threshold decision, not with general drift:
    # sandbox 3 is the reference for these, so its extras are to be created on
    # staging and staging's parallel set is superseded.
    own_name = entry.get('name') or ''
    if thresholds(own_name):
        if entry['kind'] == 'only_in_a':
            return 'bp-threshold'
        if entry['kind'] == 'only_in_b':
            return 'bp-superseded-on-b'

    if entry['kind'] == 'only_in_a':
        return f"new-on-a-{entry['type']}"
    if entry['kind'] == 'only_in_b':
        return f"only-on-b-{entry['type']}"
    return f"unclassified-{entry['type']}"


# Themes that must not be actioned in bulk, whatever the reviewer sets on the
# group: each object has to be decided on its own.
NEVER_BULK = set()

SUGGESTION = {
    'bp-threshold': 'a_to_b  (sandbox 3 is the reference for 140/90 and '
                    '130/80; it overrides staging)',
    'bp-superseded-on-b': "delete_b  (staging's parallel 'Egypt target' set is "
                          "superseded by sandbox 3's)",
    'null-guarded-formula': 'a_to_b  (side A guards against null/zero '
                            'denominators)',
    'bmi35-boundary': 'a_to_b  (sandbox 3 carries the fix; staging still has '
                      'the <= 35 overlap bug)',
    'active-patient-filter': "a_to_b  (== 'ACTIVE' is the intended filter)",
    'cascade-constants': 'a_to_b  (constants replace the hardcoded denominator)',
    'rule-service-state': 'REVIEW  (Egypt is the reference for which rules '
                          'should be live)',
}


def load_logged_uids():
    """
    UIDs mentioned in EGYPT_METADATA_CHANGES.md.

    A difference on one of these is very likely a change applied deliberately
    on sandbox 3 that has not been carried over, rather than drift. Annotating
    them stops the reviewer having to remember which was which.
    """
    if not os.path.exists(CHANGE_LOG):
        return set()
    with open(CHANGE_LOG, 'r', encoding='utf-8') as f:
        text = f.read()
    return set(re.findall(r'\b[A-Za-z][A-Za-z0-9]{10}\b', text))


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def show_scalar(field, left, right, indent='        '):
    """A readable before/after for one field."""
    ls, rs = json.dumps(left, default=str), json.dumps(right, default=str)
    if isinstance(left, str) and isinstance(right, str) and \
            (len(left) > 70 or len(right) > 70):
        print(f"{indent}{field}:")
        a_words, b_words = left.split(), right.split()
        matcher = difflib.SequenceMatcher(None, a_words, b_words)
        pairs = []
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == 'equal':
                continue
            pairs.append((' '.join(a_words[i1:i2]) or '(nothing)',
                          ' '.join(b_words[j1:j2]) or '(nothing)'))
        # The same token swapped several times in one expression is one change
        # repeated, not several changes; say so instead of printing it N times.
        if len(pairs) > 1 and len(set(pairs)) == 1:
            a_part, b_part = pairs[0]
            print(f"{indent}  {len(pairs)}x  A: {a_part[:140]}")
            print(f"{indent}      B: {b_part[:140]}")
            return
        for a_part, b_part in pairs[:4]:
            print(f"{indent}  A: {a_part[:140]}")
            print(f"{indent}  B: {b_part[:140]}")
        if len(pairs) > 4:
            print(f"{indent}  ... and {len(pairs) - 4} more change(s)")
        return
    print(f"{indent}{field}: A={ls[:110]}  B={rs[:110]}")


def describe(entry, prefix_a, prefix_b, logged):
    """Print one difference."""
    flag = '  [in change log]' if entry['uid'] in logged else ''
    if entry['kind'] == 'only_in_a':
        print(f"    {entry['uid']}  {entry['name'][:62]}")
        print(f"        only on {prefix_a}{flag}")
        return
    if entry['kind'] == 'only_in_b':
        print(f"    {entry['uid']}  {entry['name'][:62]}")
        print(f"        only on {prefix_b}{flag}")
        return

    print(f"    {entry['uid']}  {entry['name'][:62]}{flag}")
    if entry.get('disabled_a') or entry.get('disabled_b'):
        da = '+'.join(entry['disabled_a']) if entry.get('disabled_a') else 'live'
        db = '+'.join(entry['disabled_b']) if entry.get('disabled_b') else 'live'
        print(f"        SERVICE STATE  A: {da}   B: {db}")
    for field in entry['fields']:
        d = entry['diffs'][field]
        if d['kind'] == 'scalar':
            show_scalar(field, d['a'], d['b'])
            continue
        detail = d['detail']
        bits = []
        if detail['only_in_a']:
            bits.append(f"{len(detail['only_in_a'])} only on A")
        if detail['only_in_b']:
            bits.append(f"{len(detail['only_in_b'])} only on B")
        if detail['changed']:
            bits.append(f"{len(detail['changed'])} changed")
        print(f"        {field}: {', '.join(bits)}")
        for member in detail['only_in_a'][:6]:
            print(f"          + A only: {member[:100]}")
        for member in detail['only_in_b'][:6]:
            print(f"          + B only: {member[:100]}")
        for member in detail['changed'][:6]:
            left, right = d['a'][member], d['b'][member]
            # Only a member that is itself a mapping of fields can be broken
            # down further; an ADEx 'source', for instance, holds lists.
            if isinstance(left, dict) and isinstance(right, dict):
                sub = ', '.join(sorted(differing_fields(left, right)))
            else:
                sub = 'value differs'
            print(f"          ~ changed: {member[:70]} ({sub})")


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--a', default=DEFAULT_A,
                        help='Credential prefix for side A (default: %(default)s)')
    parser.add_argument('--b', default=DEFAULT_B,
                        help='Credential prefix for side B (default: %(default)s)')
    parser.add_argument('--only', default=None,
                        help='Comma-separated object types to compare, from: '
                             + ','.join(TYPE_ORDER + OPTIONAL_TYPES)
                             + f". {','.join(OPTIONAL_TYPES)} are compared only "
                               f"when named here.")
    parser.add_argument('--include-sharing', action='store_true',
                        help='Compare sharing settings too. Off by default: they '
                             'legitimately differ per instance and swamp the output.')
    parser.add_argument('--include-translations', action='store_true',
                        help='Compare translations too. Off by default.')
    parser.add_argument('--out', default=SELECTION_FILE,
                        help='Selection file to write (default: %(default)s)')
    args = parser.parse_args()

    # Indicator names carry characters like the >= sign, which a cp1252 console
    # cannot encode; without this the report dies partway through printing.
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass

    types = TYPE_ORDER
    if args.only:
        requested = [t.strip() for t in args.only.split(',') if t.strip()]
        unknown = [t for t in requested if t not in SPECS]
        if unknown:
            logger.error(f"Unknown object type(s): {unknown}. "
                         f"Choose from {TYPE_ORDER + OPTIONAL_TYPES}")
            sys.exit(2)
        types = [t for t in TYPE_ORDER + OPTIONAL_TYPES if t in requested]

    api_a = load_api(args.a, f'side A ({args.a})')
    api_b = load_api(args.b, f'side B ({args.b})')

    logger.warning(f"A = {args.a:16} {api_a.base_url}  (DHIS2 {api_a.version})")
    logger.warning(f"B = {args.b:16} {api_b.base_url}  (DHIS2 {api_b.version})")

    logged = load_logged_uids()
    if logged:
        logger.info(f"{len(logged)} UID(s) mentioned in {CHANGE_LOG} will be flagged")

    all_diffs = []
    counts = {}
    rule_states = []

    for name in types:
        spec = SPECS[name]
        side_a = fetch(api_a, name, spec)
        side_b = fetch(api_b, name, spec)
        if side_a is None or side_b is None:
            logger.error(f"Skipping {name}: could not read both sides")
            continue

        diffs = compare(name, spec, side_a, side_b,
                        args.include_sharing, args.include_translations)
        all_diffs.extend(diffs)
        counts[name] = (len(side_a), len(side_b), len(diffs))
        if name == 'programRules':
            rule_states = service_state(side_a, side_b)

    if rule_states:
        print()
        logger.warning('=' * 72)
        logger.warning(f"PROGRAM RULES OUT OF SERVICE ON ONE SIDE ONLY "
                       f"({len(rule_states)})")
        print(f"    {'uid':12} {'A':24} {'B':24} name")
        for row in rule_states:
            print(f"    {row['uid']:12} {row['a']:24} {row['b']:24} "
                  f"{row['name'][:44]}")

    for entry in all_diffs:
        entry['group'] = classify(entry)

    print()
    logger.warning('=' * 72)
    logger.warning('SUMMARY')
    print(f"    {'type':24} {'on A':>6} {'on B':>6} {'differ':>7}")
    for name in types:
        if name not in counts:
            continue
        a_n, b_n, d_n = counts[name]
        print(f"    {name:24} {a_n:6} {b_n:6} {d_n:7}")

    groups = {}
    for entry in all_diffs:
        groups.setdefault(entry['group'], []).append(entry)

    threshold_items = [e for e in all_diffs
                       if e['group'] in ('bp-threshold', 'bp-superseded-on-b')]
    if threshold_items:
        print()
        logger.warning('=' * 72)
        logger.warning(f"BLOOD PRESSURE THRESHOLD OBJECTS ({len(threshold_items)})")
        logger.warning(f"{args.a} is the reference for 140/90 and 130/80. Its "
                       f"versions override")
        logger.warning(f"{args.b}, and {args.b}'s parallel set is superseded. "
                       f"Listed here because these")
        logger.warning("change what a published figure means, so they should be "
                       "read before applying.")
        for entry in threshold_items:
            d = entry.get('diffs', {})
            if entry['kind'] != 'differs':
                side = args.a if entry['kind'] == 'only_in_a' else args.b
                print(f"    {entry['uid']}  {entry['type']}  "
                      f"[only on {side}]  {entry['name'][:48]}")
                continue
            print(f"    {entry['uid']}  {entry['type']}")
            for field in ('name', 'shortName'):
                if field in d and d[field]['kind'] == 'scalar':
                    print(f"        {field:10} A: {d[field]['a']}")
                    print(f"        {'':10} B: {d[field]['b']}")
            formula = [f for f in ('numerator', 'denominator', 'expression',
                                   'filter') if f in d]
            print(f"        formula    {'differs: ' + ', '.join(formula)
                                       if formula else 'IDENTICAL on both sides'}")

    if groups:
        print()
        logger.warning('BY THEME')
        for group in sorted(groups, key=lambda g: -len(groups[g])):
            note = SUGGESTION.get(group, '')
            print(f"    {group:34} {len(groups[group]):4}"
                  + (f"   suggested: {note}" if note else ''))

    for name in types:
        entries = [d for d in all_diffs if d['type'] == name]
        if not entries:
            continue
        print()
        logger.warning('=' * 72)
        logger.warning(f"{name}: {len(entries)} difference(s)")
        for kind, heading in (('only_in_a', f'only on {args.a}'),
                              ('only_in_b', f'only on {args.b}'),
                              ('differs', 'present on both, configured differently')):
            group = [e for e in entries if e['kind'] == kind]
            if not group:
                continue
            print(f"\n  {heading} ({len(group)}):")
            for entry in group:
                describe(entry, args.a, args.b, logged)

    selection = {
        'generated': datetime.now().isoformat(timespec='seconds'),
        'sideA': {'prefix': args.a, 'url': api_a.base_url},
        'sideB': {'prefix': args.b, 'url': api_b.base_url},
        'actions': {
            'skip': 'leave both instances alone',
            'a_to_b': f'copy the {args.a} version to {args.b} '
                      f'(creating it there if absent)',
            'b_to_a': f'copy the {args.b} version to {args.a} '
                      f'(creating it there if absent)',
            'delete_a': f'delete the object from {args.a}',
            'delete_b': f'delete the object from {args.b}',
            'inherit': "follow the action set on this difference's theme",
        },
        # Set a theme's action once to cover every difference in it. A
        # difference whose own action is left as 'inherit' follows its theme.
        'groups': {group: {'count': len(entries), 'action': 'skip',
                           'suggested': SUGGESTION.get(group, 'review')}
                   for group, entries in sorted(groups.items())},
        'differences': [{
            'type': e['type'],
            'uid': e['uid'],
            'name': e['name'],
            'kind': e['kind'],
            'group': e['group'],
            'fields': e['fields'],
            'in_change_log': e['uid'] in logged,
            'action': 'inherit',
        } for e in all_diffs],
    }

    with open(args.out, 'w', encoding='utf-8') as f:
        json.dump(selection, f, indent=2, ensure_ascii=False)

    print()
    logger.warning('=' * 72)
    if not all_diffs:
        logger.warning("No differences. The two instances agree on everything compared.")
        return
    logger.warning(f"{len(all_diffs)} difference(s) written to {args.out}, all set to "
                   f"'skip'.")
    logger.warning(f"Edit the 'action' on the ones you want moved, then run "
                   f"apply_metadata_selection.py")


if __name__ == '__main__':
    main()
