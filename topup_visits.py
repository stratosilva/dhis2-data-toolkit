"""
Add this period's visit to patients who already exist on a target instance.

Shared by the three dummy-data generators - Nigeria (sandbox 1), Egypt
(sandbox 3) and the HTN/DM demo - because the operation is identical on all
three. It works on patients already in the target rather than on a source
instance, and the only thing that differs between them is which data elements
a visit carries, which each caller supplies as a `build_values` function.

A patient is a candidate when both are true:

  * they have an open scheduled visit due on or before the end of the period;
  * they have no completed visit inside the period.

Their scheduled visit is RECYCLED: that same event becomes COMPLETED on a date
inside the period. Booking a fresh event and leaving the old one scheduled
would give the patient two events for one appointment - the kept visit and a
due date that was never honoured - and the overdue indicators read exactly that
pair. A new appointment is then booked `next_interval_days` out, so the patient
is ready for the next run.

Run it monthly and the cohort keeps moving: each run turns last month's
appointments into visits and books next month's.
"""

import random
from datetime import date, datetime, timedelta

from dhis2 import RequestException as DHIS2RequestException
from logzero import logger
from requests.exceptions import RequestException as RequestsRequestException

from tracker_common import import_error_reports

DEFAULT_NEXT_INTERVAL_DAYS = 28

# How far back to look for each candidate's most recent completed visit. The
# visit builder needs it to continue the patient's treatment rather than start
# afresh, and bounding the scan keeps a long-running instance from being paged
# in its entirety.
DEFAULT_LOOKBACK_DAYS = 400

PAGE_SIZE = 500


def _events(api, program, stage, org_units, extra, fields, page_size=PAGE_SIZE,
            limit=None):
    """Page one tracker/events query, newest first where the caller asked."""
    out = []
    page = 1
    while True:
        params = {
            'program': program,
            'programStage': stage,
            # The plural form is the one this API version accepts; `orgUnit`
            # with several UIDs is rejected, and with one it quietly returns
            # nothing.
            'orgUnits': ';'.join(org_units),
            'ouMode': 'DESCENDANTS',
            'fields': fields,
            'pageSize': page_size,
            'page': page,
            'totalPages': 'false',
            **extra,
        }
        response = api.get('tracker/events', params=params).json()
        batch = response.get('instances') or response.get('events') or []
        out.extend(batch)

        if limit is not None and len(out) >= limit:
            return out[:limit]
        if len(batch) < page_size:
            return out
        page += 1


def period_bounds(period_start, period_end):
    """Default the period to the calendar month we are in."""
    today = date.today()
    start = period_start or today.replace(day=1)
    end = period_end or today
    if end > today:
        end = today
    if start > end:
        raise ValueError(f"period start {start} is after period end {end}")
    return start, end


def find_candidates(api, program, stage, org_units, period_start, period_end,
                    limit=None):
    """
    Patients with an appointment that has come due and no visit in the period.

    Returns the scheduled events themselves, because the top-up recycles them
    rather than creating new ones.

    When a limit is given, only enough appointments are read to satisfy it with
    room to spare rather than the whole instance. On the demo instance, which
    carries 26k open appointments, reading them all took seven minutes to pick
    fifteen patients. The shortlist is biased towards whatever order the server
    returns, but that bias does not accumulate: a patient who is topped up ends
    up with an appointment in the future and so is not a candidate next month.
    """
    shortlist = None if limit is None else max(limit * 20, 2000)

    scheduled = _events(
        api, program, stage, org_units,
        # scheduledBefore turns out to be inclusive, so this asks for one day
        # past the period and the exact bound is applied below.
        {'status': 'SCHEDULE',
         'scheduledBefore': (period_end + timedelta(days=1)).isoformat()},
        'event,trackedEntity,enrollment,orgUnit,scheduledAt,status',
        limit=shortlist)

    seen = {e['trackedEntity'] for e in _events(
        api, program, stage, org_units,
        {'status': 'COMPLETED',
         'occurredAfter': period_start.isoformat(),
         'occurredBefore': (period_end + timedelta(days=1)).isoformat()},
        'event,trackedEntity')}

    # scheduledBefore turns out to be inclusive, so an appointment one day past
    # the end of the period comes back too. Filtered here rather than trusting
    # the parameter, because a patient whose appointment is not due yet has not
    # missed anything and should not be given a visit before it.
    eligible = [
        e for e in scheduled
        if e.get('trackedEntity') and e['trackedEntity'] not in seen
        and datetime.strptime(e['scheduledAt'][:10], '%Y-%m-%d').date() <= period_end
    ]

    # One open appointment per patient is the rule this data is meant to keep:
    # a patient is waiting on exactly one visit, upcoming or overdue. Real data
    # is not always tidy - the demo instance has patients carrying four open
    # appointments, some more than a year old - and a top-up cannot tidy them.
    # Recycling one of the four still leaves three open and books a fourth, so
    # these are left alone and reported instead of being quietly made worse.
    by_patient = {}
    for event in eligible:
        by_patient.setdefault(event['trackedEntity'], []).append(event)

    crowded = {te: events for te, events in by_patient.items() if len(events) > 1}
    if crowded:
        logger.warning(
            f"  {len(crowded)} patient(s) skipped: they hold more than one open "
            f"appointment ({sum(len(e) for e in crowded.values())} in total), "
            f"which a top-up cannot resolve. Clear the extras first.")

    candidates = [events[0] for te, events in by_patient.items() if len(events) == 1]

    logger.info(f"  {len(scheduled)} appointment(s) due on or before "
                f"{period_end.isoformat()}; {len(seen)} patient(s) already seen "
                f"in the period; {len(candidates)} candidate(s)")
    return candidates


# Above this many candidates, one newest-first sweep of the instance's events
# costs less than a request per patient. Below it, the sweep is nearly all
# waste: a handful of patients sampled at random sit scattered through the
# whole history, so it ends up reading almost everything to find them.
# Measured on sandbox 1: a per-patient lookup is ~0.3s, a full sweep of 37k
# events is ~75 pages.
BULK_SCAN_THRESHOLD = 300

VISIT_FIELDS = 'event,trackedEntity,occurredAt,dataValues[dataElement,value]'


def _latest_one_by_one(api, program, stage, wanted):
    """One request per patient: predictable, and cheap for a small top-up."""
    found = {}
    for tracked_entity in wanted:
        try:
            response = api.get('tracker/events', params={
                'program': program, 'programStage': stage,
                'trackedEntity': tracked_entity, 'status': 'COMPLETED',
                'order': 'occurredAt:desc', 'pageSize': 1,
                'fields': VISIT_FIELDS,
            }).json()
        except (DHIS2RequestException, RequestsRequestException):
            continue
        batch = response.get('instances') or response.get('events') or []
        if batch:
            found[tracked_entity] = batch[0]
    return found


def _latest_by_sweep(api, program, stage, org_units, wanted, lookback_days):
    """One newest-first sweep, keeping the first event seen per patient."""
    remaining = set(wanted)
    found = {}
    since = (date.today() - timedelta(days=lookback_days)).isoformat()
    page = 1

    while remaining:
        response = api.get('tracker/events', params={
            'program': program, 'programStage': stage,
            'orgUnits': ';'.join(org_units), 'ouMode': 'DESCENDANTS',
            'status': 'COMPLETED', 'occurredAfter': since,
            'order': 'occurredAt:desc', 'fields': VISIT_FIELDS,
            'pageSize': PAGE_SIZE, 'page': page, 'totalPages': 'false',
        }).json()
        batch = response.get('instances') or response.get('events') or []
        if not batch:
            break

        for event in batch:
            te = event.get('trackedEntity')
            if te in remaining:
                found[te] = event
                remaining.discard(te)

        if len(batch) < PAGE_SIZE:
            break
        page += 1

    return found


def latest_completed_visits(api, program, stage, org_units, wanted,
                            lookback_days=DEFAULT_LOOKBACK_DAYS):
    """
    The most recent completed visit for each patient in `wanted`, so the visit
    builder can continue their treatment instead of starting over.

    Patients with nothing to find simply get None and the builder decides what
    to do with that.
    """
    if len(wanted) <= BULK_SCAN_THRESHOLD:
        logger.info(f"  reading the last visit of {len(wanted)} patient(s) "
                    f"one at a time")
        found = _latest_one_by_one(api, program, stage, wanted)
    else:
        logger.info(f"  sweeping events newest-first for {len(wanted)} "
                    f"patients' last visits")
        found = _latest_by_sweep(api, program, stage, org_units, wanted,
                                 lookback_days)

    missing = len(wanted) - len(found)
    if missing:
        logger.info(f"  {missing} candidate(s) have no completed visit on "
                    f"record; their visit starts fresh")
    return found


def build_topup_payload(candidates, previous, build_values, period_start,
                        period_end, program, stage, next_interval_days):
    """
    Turn each candidate into a completed visit plus a fresh appointment.

    The visit date is the appointment date when that falls inside the period -
    the patient kept the appointment - and otherwise a date inside the period,
    because an appointment that came due earlier was kept late.
    """
    events = []
    today = date.today()
    next_due = today + timedelta(days=next_interval_days)
    built = 0
    skipped_not_due = 0

    for candidate in candidates:
        te = candidate['trackedEntity']
        due = datetime.strptime(candidate['scheduledAt'][:10], '%Y-%m-%d').date()

        # An appointment that is not due yet has not been missed, and a visit
        # cannot be recorded against it without dating that visit in the
        # future. A period ending after today is the only way to get here.
        if due > today:
            skipped_not_due += 1
            continue

        if period_start <= due <= period_end:
            visit_date = due
        else:
            span = (period_end - period_start).days
            visit_date = period_start + timedelta(days=random.randint(0, max(span, 0)))
            visit_date = min(visit_date, today)

        values = build_values(te, previous.get(te), visit_date)
        if not values:
            continue

        events.append({
            # Same event UID: the appointment becomes the visit.
            'event': candidate['event'],
            'program': program,
            'programStage': stage,
            'orgUnit': candidate['orgUnit'],
            'enrollment': candidate['enrollment'],
            'trackedEntity': te,
            'occurredAt': visit_date.isoformat(),
            'scheduledAt': candidate['scheduledAt'][:10],
            'status': 'COMPLETED',
            'dataValues': values,
        })
        events.append({
            'program': program,
            'programStage': stage,
            'orgUnit': candidate['orgUnit'],
            'enrollment': candidate['enrollment'],
            'trackedEntity': te,
            'scheduledAt': next_due.isoformat(),
            'status': 'SCHEDULE',
        })
        built += 1

    if skipped_not_due:
        logger.info(f"  {skipped_not_due} appointment(s) skipped: not due yet")

    return events, built


def post_events(api, events, chunk_size=50, run_rules=True):
    """Send the recycled visits and their new appointments."""
    params = {'async': 'false', 'importStrategy': 'CREATE_AND_UPDATE'}
    if not run_rules:
        params['skipRuleEngine'] = 'true'

    created = updated = ignored = 0

    for start in range(0, len(events), chunk_size):
        chunk = events[start:start + chunk_size]
        chunk_no = start // chunk_size + 1
        try:
            result = api.post('tracker', params=params, json={'events': chunk}).json()
            stats = result.get('stats', {})
            created += stats.get('created', 0)
            updated += stats.get('updated', 0)
            ignored += stats.get('ignored', 0)
            for report in (result.get('validationReport', {})
                           .get('errorReports', []) or [])[:3]:
                logger.error(f"  chunk {chunk_no} {report.get('errorCode')}: "
                             f"{report.get('message')}")
        except (DHIS2RequestException, RequestsRequestException) as e:
            logger.error(f"  chunk {chunk_no} rejected:")
            for line in sorted(set(import_error_reports(e)))[:5]:
                logger.error(f"    {line}")

    logger.info(f"  events: created {created}, updated {updated}, ignored {ignored}")
    return created, updated, ignored


def run_topup(api, *, program, stage, org_units, limit, build_values,
              period_start=None, period_end=None,
              next_interval_days=DEFAULT_NEXT_INTERVAL_DAYS,
              lookback_days=DEFAULT_LOOKBACK_DAYS,
              chunk_size=50, run_rules=True, dry_run=False,
              follow_up=None, tracked_entity_type=None):
    """
    Give `limit` existing patients their visit for the period.

    `build_values(tracked_entity, previous_event, visit_date)` returns the data
    values for one visit, or an empty list to skip that patient.

    `follow_up`, when given, is (attribute uid, value) written to every patient
    who got a visit - their appointment is now in the future, so a programme
    that tracks a follow-up status needs it moved off "overdue". It requires
    `tracked_entity_type`, which the tracker API demands on any tracked entity
    update (E1121) even when only one attribute is changing.
    """
    start, end = period_bounds(period_start, period_end)
    logger.info(f"Top-up for {start.isoformat()} .. {end.isoformat()}")

    candidates = find_candidates(api, program, stage, org_units, start, end,
                                 limit=limit)
    if not candidates:
        logger.warning("  nothing to do: every patient with a due appointment "
                       "has already been seen in this period")
        return 0

    if limit is not None and len(candidates) > limit:
        # Sampled rather than taken in order, so a repeated run does not keep
        # topping up the same facilities first.
        candidates = random.sample(candidates, limit)
    logger.info(f"  topping up {len(candidates)} patient(s)")

    previous = latest_completed_visits(
        api, program, stage, org_units,
        [c['trackedEntity'] for c in candidates], lookback_days)

    events, built = build_topup_payload(
        candidates, previous, build_values, start, end, program, stage,
        next_interval_days)

    if not events:
        logger.warning("  the visit builder produced nothing for any candidate")
        return 0

    if dry_run:
        logger.info(f"  dry run: {built} visit(s) and {built} appointment(s) built, "
                    f"not posted")
        return built

    post_events(api, events, chunk_size=chunk_size, run_rules=run_rules)

    if follow_up:
        attribute, value = follow_up
        if not tracked_entity_type:
            logger.error("  follow-up status not updated: run_topup needs "
                         "tracked_entity_type alongside follow_up")
        else:
            _set_follow_up(api, candidates, attribute, value,
                           tracked_entity_type, chunk_size)

    return built


def _set_follow_up(api, candidates, attribute, value, tracked_entity_type,
                   chunk_size):
    """
    Move every topped-up patient's follow-up status on.

    They have just been seen and have an appointment in the future, so whatever
    the status said while they were overdue no longer describes them.
    """
    payload = [{'trackedEntity': c['trackedEntity'],
                'trackedEntityType': tracked_entity_type,
                'orgUnit': c['orgUnit'],
                'attributes': [{'attribute': attribute, 'value': value}]}
               for c in candidates]

    updated = 0
    for start in range(0, len(payload), chunk_size):
        chunk = payload[start:start + chunk_size]
        try:
            result = api.post('tracker', params={
                'async': 'false', 'importStrategy': 'UPDATE',
                'skipRuleEngine': 'true'},
                json={'trackedEntities': chunk}).json()
            updated += result.get('stats', {}).get('updated', 0)
        except (DHIS2RequestException, RequestsRequestException) as e:
            logger.error(f"  follow-up update rejected: {str(e)[:150]}")
            for line in sorted(set(import_error_reports(e)))[:3]:
                logger.error(f"    {line}")

    logger.info(f"  follow-up status set to {value} on {updated} patient(s)")
