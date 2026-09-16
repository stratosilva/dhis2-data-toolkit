"""
Shared plumbing for the patient-migration scripts.

Holds the parts that are the same whatever the target programme: posting to
/api/tracker with retries, resume state, a failure trail, org-unit mapping and
the date-of-birth parsing that copes with the Nigeria source data.

Programme-specific transformation lives in the individual move_patients_*.py
scripts.
"""

import json
import os
import random
import sys
import time
from datetime import date, datetime, timedelta
from http.client import RemoteDisconnected

import re
from dhis2 import RequestException as DHIS2RequestException
from logzero import logger
from requests.exceptions import ConnectionError, RequestException as RequestsRequestException

from dhis2_env import api_from_env


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

def load_api(prefix, label):
    """Build an Api from the .env credentials for a server prefix."""
    try:
        return api_from_env(prefix)
    except (FileNotFoundError, KeyError) as e:
        logger.error(f"ERROR: cannot load {label} credentials: {e}")
        sys.exit(2)


# ---------------------------------------------------------------------------
# Resume state and failure trail
# ---------------------------------------------------------------------------

def save_last_uid(path, uid):
    with open(path, 'w') as f:
        f.write(uid)


def load_resume_uid(path):
    if os.path.exists(path):
        with open(path, 'r') as f:
            return f.read().strip()
    return None


def delete_resume_file(path):
    if os.path.exists(path):
        os.remove(path)


def record_failed_teis(path, chunk, reason):
    """
    Append the trackedEntity UIDs of a chunk the target refused, so a failed
    import leaves a machine-readable trail instead of only a log line.
    """
    try:
        with open(path, 'a') as f:
            for tei in chunk:
                f.write(json.dumps({
                    "trackedEntity": tei.get("trackedEntity"),
                    "orgUnit": tei.get("orgUnit"),
                    "reason": reason,
                    "recordedAt": datetime.now().isoformat(timespec='seconds'),
                }) + "\n")
    except OSError as e:
        logger.error(f"Could not write {path}: {e}")


# ---------------------------------------------------------------------------
# Posting
# ---------------------------------------------------------------------------

def post_chunked_tracked_entities(api, tracked_entities, chunk_size=25,
                                  max_retries=3, run_rules=True,
                                  failed_file=None, sanitise=None):
    """
    Post trackedEntities to /api/tracker in chunks, synchronously.

    run_rules=True leaves the server-side program rule engine enabled, which is
    required when the target programme derives values (risk score, BMI, derived
    demographics) from program rules. It is markedly slower than skipping the
    engine, so keep chunk_size modest.

    sanitise, when given, is called on each chunk immediately before sending.

    Returns the UIDs of the tracked entities the server created, harvested from
    the import response's bundleReport. Callers need these when a value has to
    be set in a second pass - a programme attribute owned by a program rule,
    for instance, which the creating payload cannot carry.
    """
    created_uids = []
    total = len(tracked_entities)
    params = {"async": "false"}
    if not run_rules:
        params["skipRuleEngine"] = "true"

    logger.info(
        f"Sending {total} trackedEntities in chunks of {chunk_size} "
        f"(program rules {'ON' if run_rules else 'OFF'})..."
    )

    for i in range(0, total, chunk_size):
        chunk = tracked_entities[i:i + chunk_size]
        if sanitise is not None:
            sanitise(chunk)
        payload = {"trackedEntities": chunk}
        retries = 0
        chunk_no = i // chunk_size + 1

        while retries < max_retries:
            try:
                response = api.post("tracker", params=params, json=payload)
                result = response.json()

                if result.get("status") == "ERROR":
                    logger.error(f"Chunk {chunk_no}: error in tracker import:")
                    messages = []
                    for report in result.get("validationReport", {}).get("errorReports", []):
                        logger.error(f"{report.get('message')} (code: {report.get('errorCode')})")
                        messages.append(f"{report.get('errorCode')}: {report.get('message')}")

                    if len(chunk) > 1:
                        # Do not lose a whole chunk because of a few bad records.
                        logger.warning(
                            f"Chunk {chunk_no} reported import errors; "
                            "retrying its tracked entities individually."
                        )
                        created_uids.extend(post_chunked_tracked_entities(
                            api, chunk, chunk_size=1, max_retries=max_retries,
                            run_rules=run_rules, failed_file=failed_file, sanitise=sanitise,
                        ))
                    elif failed_file:
                        record_failed_teis(failed_file, chunk,
                                           "; ".join(messages) or "import status ERROR")
                else:
                    stats = result.get("stats", {})
                    logger.info(
                        f"Chunk {chunk_no}: imported {len(chunk)} trackedEntities "
                        f"(created {stats.get('created', '?')}, updated {stats.get('updated', '?')}, "
                        f"ignored {stats.get('ignored', '?')})"
                    )
                    created_uids.extend(_created_tei_uids(result))
                break
            except (DHIS2RequestException, RequestsRequestException,
                    ConnectionError, RemoteDisconnected) as e:
                status_code = getattr(e, "code", None)
                if status_code is not None and 400 <= status_code < 500:
                    if status_code == 409 and len(chunk) > 1:
                        logger.warning(
                            f"Chunk {chunk_no} has validation conflicts; "
                            "retrying its tracked entities individually."
                        )
                        created_uids.extend(post_chunked_tracked_entities(
                            api, chunk, chunk_size=1, max_retries=max_retries,
                            run_rules=run_rules, failed_file=failed_file, sanitise=sanitise,
                        ))
                        break
                    reports = log_import_error(e, chunk_no, status_code)
                    logger.error("  not retrying a validation error.")
                    if failed_file:
                        record_failed_teis(
                            failed_file, chunk,
                            "; ".join(reports) if reports else f"HTTP {status_code}: {e}",
                        )
                    break
                logger.error(f"Request failed: {e}. Retrying in 3s...")
                time.sleep(3)
                retries += 1
        else:
            logger.error(f"Failed to import chunk {chunk_no} after {max_retries} attempts.")
            if failed_file:
                record_failed_teis(failed_file, chunk,
                                   f"network failure after {max_retries} attempts")

    return created_uids


def _created_tei_uids(result):
    """The tracked entity UIDs a successful import created."""
    reports = (result.get('bundleReport', {})
                     .get('typeReportMap', {})
                     .get('TRACKED_ENTITY', {})
                     .get('objectReports', []))
    return [r['uid'] for r in reports if r.get('uid')]


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def import_error_reports(exception):
    """
    Pull the validation reports out of a failed /api/tracker call.

    dhis2.py raises RequestException with the raw response body in
    .description; for a 409 that body holds the validationReport explaining
    exactly which field the server rejected. Without this the caller only sees
    the status code, which says nothing actionable.
    """
    body = getattr(exception, 'description', None)
    if not body:
        return []

    try:
        parsed = json.loads(body)
    except (TypeError, ValueError):
        return [str(body)[:500]]

    reports = []
    validation = parsed.get('validationReport') or {}
    for report in validation.get('errorReports') or []:
        reports.append(
            f"[{report.get('errorCode')}] {report.get('trackerType')} "
            f"{report.get('uid')}: {report.get('message')}"
        )

    if not reports:
        message = parsed.get('message') or parsed.get('httpStatus')
        if message:
            reports.append(str(message))

    return reports


def log_import_error(exception, chunk_no, status_code):
    """Log a rejected chunk with the server's reasons, de-duplicated."""
    logger.error(f"Chunk {chunk_no} was rejected by the target (HTTP {status_code}):")
    reports = import_error_reports(exception)

    if not reports:
        logger.error(f"  (no validation report in the response) {exception}")
        return reports

    seen = set()
    for report in reports:
        if report in seen:
            continue
        seen.add(report)
        logger.error(f"  {report}")

    return reports


def reindex(json_object, key):
    return {elem[key]: elem for elem in json_object}


def remove_created_updated(obj):
    """Recursively drop 'createdBy' / 'updatedBy' from a nested structure."""
    if isinstance(obj, dict):
        return {k: remove_created_updated(v) for k, v in obj.items()
                if k not in ("createdBy", "updatedBy")}
    if isinstance(obj, list):
        return [remove_created_updated(item) for item in obj]
    return obj


def deterministic_mapping(source_ous, target_ous):
    """Map each source org unit to a distinct target org unit, stably."""
    source_sorted = sorted(source_ous)
    target_sorted = sorted(target_ous)

    if len(source_sorted) > len(target_sorted):
        raise ValueError(
            f"Not enough target facilities ({len(target_sorted)}) to map "
            f"{len(source_sorted)} source org units 1:1 without collisions."
        )

    return {src: tgt for src, tgt in zip(source_sorted, target_sorted)}


# ---------------------------------------------------------------------------
# Date of birth
# ---------------------------------------------------------------------------

DOB_FORMATS = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%d.%m.%Y",
    "%d %m %Y",
)

# The source stores ages in the DOB field: "59", "53 years", "48 yrs old".
AGE_PATTERN = re.compile(
    r"^(\d{1,3})\s*(?:years?|yrs?|y|anos|a[nñ]os|ans)?\s*(?:old)?$",
    re.IGNORECASE,
)


def dob_from_age(age, today=None):
    """
    Convert a whole-year age to a date of birth, as YYYY-MM-DD.

    The birthday is spread over the twelve months the age allows, so
    age-derived patients do not all share today's birthday.
    """
    today = today or date.today()

    def years_ago(years):
        try:
            return today.replace(year=today.year - years)
        except ValueError:
            return today.replace(year=today.year - years, day=28)

    latest = years_ago(age)
    earliest = years_ago(age + 1) + timedelta(days=1)
    span = max((latest - earliest).days, 0)

    return (earliest + timedelta(days=random.randint(0, span))).isoformat()


def parse_dob(raw_value):
    """
    Best-effort conversion of a source DOB to YYYY-MM-DD.

    Real dates win; then ages stored in the same field. Returns None only when
    the value holds nothing usable.
    """
    if raw_value is None:
        return None

    value = " ".join(str(raw_value).split())
    if not value:
        return None

    for candidate in (value, value.replace(" ", "")):
        for date_format in DOB_FORMATS:
            try:
                return datetime.strptime(candidate[:10], date_format).date().isoformat()
            except ValueError:
                pass

    age_match = AGE_PATTERN.match(value)
    if age_match:
        age = int(age_match.group(1))
        if 0 < age < 120:
            return dob_from_age(age)

    return None


def age_from_dob(dob_iso, on=None):
    """Whole-year age implied by an ISO date of birth."""
    on = on or date.today()
    born = datetime.strptime(dob_iso, "%Y-%m-%d").date()
    return on.year - born.year - ((on.month, on.day) < (born.month, born.day))
