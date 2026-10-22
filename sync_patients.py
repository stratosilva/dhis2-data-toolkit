#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Sync + anonymise TEIs from a source DHIS2 instance to a target DHIS2 instance.

Server URLs, usernames and passwords come from the .env file at the repository
root (see dhis2_env.py). By default this syncs Nigeria production into sandbox 1:

  python sync_patients.py --page-size 50 --updated-offset-days 7

Pass --source / --target to sync between different .env prefixes:

  python sync_patients.py --source NIGERIA_NCD --target EGYPT_SANDBOX3

The individual --source-url / --source-user / --source-pass flags (and their
--target- counterparts) still work and override .env, so a one-off server needs
no .env entry:

  python sync_patients.py \
      --source-url https://SOURCE/dhis \
      --source-user user --source-pass pass \
      --target-url https://TARGET/dhis \
      --target-user user --target-pass pass \
      --program "pMIglSEqPGS" \
      --org-unit uS2CuRbDyD7 \
      --page-size 50
      --updated-offset-days 7 (for a week)

Writes a CSV summary to: sync_results.csv
"""

import argparse
import csv
import os
import re
import sys
from typing import Dict, Any, List, Optional
import gc
import time
import random
from http.client import RemoteDisconnected
from urllib3.exceptions import ProtocolError
from requests.exceptions import ConnectionError, ReadTimeout, ChunkedEncodingError
from datetime import date, timedelta

import requests
from faker import Faker

from dhis2_env import credentials

import json
import datetime
from typing import Dict, Tuple, List
import uuid
import random

# ---- Constants (TEA UIDs) ----
PROGRAM_UID = "pMIglSEqPGS"
DEFAULT_ORG_UNIT = "uS2CuRbDyD7"

TEA_ID_NUMBER = "RAViWaXdOM8"     # "ID Number (Optional)"
TEA_QR_CODE  = "sOpP7Czrt7H"
TEA_PATIENT_NAME = "R6VkX5nsAy4"   # "Patient name"
TEA_SEX = "oindugucx72"           # "Sex" (MALE/FEMALE)
TEA_NCD_STATUS = "fI1P3Mg1zOZ"    # "HTN - NCD Patient Status"
TEA_ADDRESS = "A6Hb0Kvg4vb"       # "Address (current)"
TEA_PATIENT_PHONE = "YRDy9xy9jD0" # "Patient Phone Number"
TEA_PATIENT_DOB = "NI0QRzJvQ0k"   # "Date of birth" (AGE, expects yyyy-mm-dd)
TEA_SUPPORTER_NAME = "JO6OoiD83nH"
TEA_SUPPORTER_PHONE = "JUWgfAAGvBQ"
TEA_CONSENT = "YJGACwhN0St"       # TRUE_ONLY
# --- DataElements to drop from events (invalid / not in stage) ---
DE_DROP_SET = {"ifRI6qw96v4", "mOMHtDNHmDA", "XMtcYl6Y3Jp", "yc3PVWG7ilA", "gghaVrBpTgl",
                "CZPgB1WuEp0", "DRDE9Zug3bi"}
HTN_VISIT_STAGE = "anb2cjLx3WM"  # only strip for this stage


# The target's option set for HTN - MED - Hydrochlorothiazide only carries the
# 25mg code, so a 12.5mg value fails the whole event on import. The target has
# no option set on "Other hypertension medication", so the value moves there.
DE_HCTZ = "D8FWXAtCuL2"
DE_OTHER_HTN_MED = "drY9ZQ0PEEw"
HCTZ_UNSUPPORTED_CODE = "HYDROCHLOROTHIAZIDE_12POINT5MG"

DE_BP_DIASTOLIC = "yNhtHKtKkO1"
DE_BP_SYSTOLIC  = "IxEwYiq1FTq"
BP_REQUIRED_SET = {DE_BP_DIASTOLIC, DE_BP_SYSTOLIC}

# --- Additional TEAs used below ---
TEA_HTN_YN = "jCRIT4GMMOS"   # "HTN - Does patient have hypertension?"

TEA_DROP_VALUE_ATTR = "ea5w3mkinZo"

REQUIRED_ATTRS = {TEA_PATIENT_DOB, TEA_PATIENT_NAME, TEA_SEX}  # DoB, Patient name, Sex

# ---- Program rule guard ----
# Some target rules reject an incoming value outright: the AUTOFILL rule on
# NCD Patient Status refuses anything that is not its own calculated ACTIVE.
# Prefixing the condition with "false && " parks the rule for the duration of a
# run. The original condition is restored in a finally block, and is also
# written to disk first so a hard kill still leaves a record to restore from.
RULE_GUARD_PREFIX = "false && "
RULE_BACKUP_PATH = "program_rule_backup.json"


def disable_program_rules(api: "DHIS2", rule_uids: List[str]) -> Dict[str, str]:
    """
    Park each rule by prefixing its condition, returning {uid: original condition}.
    Raises if a rule cannot be read or written, because running on with a rule
    half-disabled would be worse than not running at all.
    """
    if not rule_uids:
        return {}

    saved: Dict[str, str] = {}

    for uid in rule_uids:
        r = api.get(f"programRules/{uid}", params={"fields": ":owner"})
        if r.status_code != 200:
            raise RuntimeError(f"Cannot read programRule {uid} (HTTP {r.status_code}): {r.text[:200]}")

        rule = r.json()
        original = rule.get("condition", "")

        if original.startswith(RULE_GUARD_PREFIX):
            print(f"  - programRule {uid} is already parked, leaving it alone")
            continue

        saved[uid] = original

        # Record the originals BEFORE touching the server, so a hard kill
        # (which skips the finally block) still leaves them recoverable.
        with open(RULE_BACKUP_PATH, "w", encoding="utf-8") as f:
            json.dump(saved, f, indent=2)

        rule["condition"] = RULE_GUARD_PREFIX + original
        w = api.put(f"programRules/{uid}", json=rule)
        if w.status_code not in (200, 201):
            del saved[uid]
            raise RuntimeError(f"Cannot park programRule {uid} (HTTP {w.status_code}): {w.text[:300]}")

        print(f"  - Parked programRule {uid}: {original!r} -> {rule['condition']!r}")

    return saved


def restore_program_rules(api: "DHIS2", saved: Dict[str, str]) -> None:
    """Put every parked rule back. Never raises: it runs from a finally block."""
    if not saved:
        return

    still_broken = {}

    for uid, original in saved.items():
        try:
            r = api.get(f"programRules/{uid}", params={"fields": ":owner"})
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code} reading it back")

            rule = r.json()
            rule["condition"] = original
            w = api.put(f"programRules/{uid}", json=rule)
            if w.status_code not in (200, 201):
                raise RuntimeError(f"HTTP {w.status_code}: {w.text[:200]}")

            print(f"  - Restored programRule {uid} to {original!r}")
        except Exception as exc:
            still_broken[uid] = original
            print(f"!! FAILED to restore programRule {uid}: {exc}", file=sys.stderr)
            print(f"!! Its condition must be set back to {original!r} by hand.", file=sys.stderr)

    if still_broken:
        with open(RULE_BACKUP_PATH, "w", encoding="utf-8") as f:
            json.dump(still_broken, f, indent=2)
        print(f"!! {len(still_broken)} rule(s) left parked; originals are in {RULE_BACKUP_PATH}",
              file=sys.stderr)
    elif os.path.exists(RULE_BACKUP_PATH):
        os.remove(RULE_BACKUP_PATH)


# ---- Selected-UID source ----
def load_uid_list(path: str) -> List[str]:
    """Read TEI UIDs from a results CSV (its 'uid' column) or a plain UID per line."""
    uids: List[str] = []
    seen = set()

    with open(path, "r", encoding="utf-8", newline="") as f:
        sample = f.read(2048)
        f.seek(0)

        if "," in sample and "uid" in sample.split("\n", 1)[0]:
            for row in csv.DictReader(f):
                uid = (row.get("uid") or "").strip()
                if uid and uid not in seen:
                    seen.add(uid)
                    uids.append(uid)
        else:
            for line in f:
                uid = line.strip()
                if uid and not uid.startswith("#") and uid not in seen:
                    seen.add(uid)
                    uids.append(uid)

    return uids


def iter_selected_teis(src: "DHIS2", uids: List[str], program_uid: str, page_size: int = 50):
    """
    Yield (page_number, page_items, pager) for an explicit list of TEI UIDs,
    matching iter_source_teis so the import loop does not care which it got.
    """
    fields = "trackedEntity,trackedEntityType,orgUnit,attributes,enrollments[*,!attributes]"
    total_pages = (len(uids) + page_size - 1) // page_size

    for index in range(0, len(uids), page_size):
        chunk = uids[index:index + page_size]
        page_no = index // page_size + 1
        items = []

        for uid in chunk:
            r = src.get(f"tracker/trackedEntities/{uid}",
                        params={"program": program_uid, "fields": fields})
            if r.status_code == 200:
                items.append(r.json())
            else:
                print(f"  - SKIP {uid}: source fetch returned HTTP {r.status_code}")

        yield page_no, items, {"page": page_no, "pageCount": total_pages}


# ---- Credentials ----
# .env prefixes this script syncs between by default: Nigeria production is the
# source, sandbox 1 the target. NIGERIA_NCD is the prefix move_patients.py
# already reads, so the production password stays in exactly one place.
DEFAULT_SOURCE_PREFIX = "NIGERIA_NCD"
DEFAULT_TARGET_PREFIX = "SANDBOX1"


def resolve_server(role, prefix, url, username, password):
    """
    Resolve one end of the sync to (url, username, password).

    Explicit flags win; whatever is left unset comes from the .env prefix.
    Supplying all three flags means .env is never read, so the script still
    runs against a one-off server on a machine that has no .env.
    """
    if url and username and password:
        return url, username, password

    try:
        env_url, env_username, env_password = credentials(prefix)
    except (FileNotFoundError, KeyError) as exc:
        detail = exc.args[0] if exc.args else exc
        print(f"ERROR: cannot resolve the {role} server: {detail}", file=sys.stderr)
        sys.exit(2)

    return url or env_url, username or env_username, password or env_password


# ---- HTTP client ----
class DHIS2:
    def __init__(self, base_url: str, username: str, password: str, timeout: int = 60, verify: bool = True):
        self.base_url = base_url.rstrip("/")
        if not self.base_url.endswith("/api"):
            self.base_url = self.base_url + "/api"
        self.s = requests.Session()
        self.s.auth = (username, password)
        self.s.headers.update({"Accept": "application/json"})
        self.timeout = timeout
        self.s.verify = verify

        # retry knobs (tweak if desired)
        self.max_attempts_get = 12     # ~ up to 1 hour worst case with capped backoff
        self.max_attempts_post = 8
        self.backoff_cap_sec = 300     # 5 minutes max sleep between tries
        self.base_backoff_sec = 10     # first sleep ~10s, then grows to cap

    def _req_with_retry(self, method: str, path: str, params=None, json=None,
                        max_attempts: int = 8) -> requests.Response:
        """
        Retry on transient network failures and server 'retryable' statuses.
        Sleeps with exponential backoff (capped at 5 minutes).
        """
        url = f"{self.base_url}/{path.lstrip('/')}"
        attempt = 0
        while True:
            attempt += 1
            try:
                if method == "GET":
                    resp = self.s.get(url, params=params, timeout=self.timeout)
                else:
                    # Dispatch on the verb: PUT must not be sent as a POST.
                    resp = self.s.request(method, url, params=params, json=json,
                                          timeout=self.timeout)
            except (ConnectionError, ReadTimeout, ProtocolError, RemoteDisconnected, ChunkedEncodingError) as e:
                if attempt >= max_attempts:
                    raise
                # exponential backoff with jitter, capped at 5 minutes
                sleep_s = min(self.backoff_cap_sec, self.base_backoff_sec * (2 ** (attempt - 1)))
                jitter = random.uniform(0, sleep_s * 0.1)
                print(f"[{method}] {path} failed ({type(e).__name__}). "
                      f"Attempt {attempt}/{max_attempts}. Sleeping {int(sleep_s + jitter)}s…")
                time.sleep(sleep_s + jitter)
                continue

            # HTTP status-based retries (e.g., 429, 502, 503, 504)
            if resp.status_code in (429, 502, 503, 504):
                if attempt >= max_attempts:
                    return resp
                retry_after = resp.headers.get("Retry-After")
                if retry_after:
                    try:
                        wait = int(retry_after)
                    except ValueError:
                        wait = self.backoff_cap_sec
                else:
                    wait = min(self.backoff_cap_sec, self.base_backoff_sec * (2 ** (attempt - 1)))
                jitter = random.uniform(0, wait * 0.1)
                print(f"[{method}] {path} got HTTP {resp.status_code}. "
                      f"Attempt {attempt}/{max_attempts}. Sleeping {int(wait + jitter)}s…")
                time.sleep(wait + jitter)
                continue

            return resp  # success (or non-retryable error)

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> requests.Response:
        return self._req_with_retry("GET", path, params=params, max_attempts=self.max_attempts_get)

    def put(self, path: str, params: Optional[Dict[str, Any]] = None, json: Optional[Dict[str, Any]] = None) -> requests.Response:
        return self._req_with_retry("PUT", path, params=params, json=json,
                                    max_attempts=self.max_attempts_post)

    def post(self, path: str, params: Optional[Dict[str, Any]] = None, json: Optional[Dict[str, Any]] = None) -> requests.Response:
        return self._req_with_retry("POST", path, params=params, json=json, max_attempts=self.max_attempts_post)


# -------------------- TEA attribute overrides --------------------
def override_test_attributes(tei: dict) -> None:
    """
    Override TEA values for:
      - sOpP7Czrt7H (QR code) -> "test-<uuid4>"
      - RAViWaXdOM8 (ID/Number) -> "test/<rand>/<rand>"

    Mutates tei['attributes'] in-place if attributes exist.
    """
    if not isinstance(tei, dict):
        return
    attrs = tei.get("attributes")
    if not isinstance(attrs, list):
        return

    qr_uid = "sOpP7Czrt7H"
    id_uid = "RAViWaXdOM8"

    # Generate values once per TEI
    qr_value = f"test-{uuid.uuid4()}"
    id_value = f"test/{random.randint(1000, 999999)}/{random.randint(1000, 999999)}"

    for a in attrs:
        if not isinstance(a, dict):
            continue
        att_uid = a.get("attribute")
        if att_uid == qr_uid:
            a["value"] = qr_value
        elif att_uid == id_uid:
            a["value"] = id_value


# -------------------- OrgUnit helpers --------------------
def org_unit_exists(api: "DHIS2", uid: str) -> bool:
    """
    Returns True if the organisationUnit UID exists on the TARGET server.
    Uses GET /api/organisationUnits/{uid}.
    """
    r = api.get(f"organisationUnits/{uid}")
    return r.status_code == 200


def remap_org_units(tei: dict, new_ou: str) -> None:
    """
    Mutates the TEI payload to set the orgUnit consistently everywhere:
      - at TEI root: tei["orgUnit"]
      - for each enrollment: enrollment["orgUnit"]
      - for each event inside those enrollments: event["orgUnit"]
    """
    if not isinstance(tei, dict):
        return
    tei["orgUnit"] = new_ou
    for enr in (tei.get("enrollments") or []):
        if isinstance(enr, dict):
            enr["orgUnit"] = new_ou
            for ev in (enr.get("events") or []):
                if isinstance(ev, dict):
                    ev["orgUnit"] = new_ou


# -------------------- ID generation via /system/id --------------------
def generate_ids(api: "DHIS2", n: int) -> List[str]:
    """
    Requests n new DHIS2 UIDs from the TARGET server using:
      GET /api/system/id?limit=n

    Returns a list of UIDs. Raises if fewer than n are returned.
    """
    if n <= 0:
        return []
    r = api.get("system/id", params={"limit": n})
    r.raise_for_status()
    data = r.json()
    # DHIS2 commonly returns {"codes": [...]} but some versions use {"uids": [...]}
    codes = data.get("codes") or data.get("uids") or []
    if len(codes) < n:
        raise RuntimeError(f"Requested {n} ids, got {len(codes)}")
    return codes[:n]


# -------------------- Mapping CSV helpers --------------------
# CSV schema: one row per (mapped_ou_id, source_tei_id)
# We store JSON maps for enrollments and events to support "many" items.
MAPPING_COLUMNS = [
    "mapped_ou_id",          # OU we mapped TO (target OU)
    "source_tei_id",         # original TEI UID from source
    "target_tei_id",         # persistent TEI UID to use on target
    "enrollment_map_json",   # JSON: {srcEnrollmentUid: tgtEnrollmentUid, ...}
    "event_map_json",        # JSON: {srcEventUid: tgtEventUid, ...}
    "last_updated",          # ISO timestamp (UTC) of the last change to this row
]


def load_mapping_csv(path: str) -> Dict[Tuple[str, str], dict]:
    """
    Loads the CSV into a dict keyed by (mapped_ou_id, source_tei_id).

    Each dict value looks like:
    {
      "target_tei_id": <str>,
      "enrollment_map": {<src_enrollment>: <tgt_enrollment>, ...},
      "event_map": {<src_event>: <tgt_event>, ...}
    }
    """
    mapping: Dict[Tuple[str, str], dict] = {}
    if not os.path.exists(path):
        return mapping

    with open(path, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Decode JSON maps safely
            try:
                enr_map = json.loads(row.get("enrollment_map_json") or "{}")
            except json.JSONDecodeError:
                enr_map = {}
            try:
                evt_map = json.loads(row.get("event_map_json") or "{}")
            except json.JSONDecodeError:
                evt_map = {}

            key = ((row.get("mapped_ou_id") or ""), (row.get("source_tei_id") or ""))
            mapping[key] = {
                "target_tei_id": row.get("target_tei_id") or "",
                "enrollment_map": enr_map,
                "event_map": evt_map,
            }
    return mapping


def upsert_mapping_row(path: str, row: dict) -> None:
    """
    Inserts or updates one mapping row identified by (mapped_ou_id, source_tei_id).
    Rewrites the whole file to keep things simple and consistent.
    """
    existing_rows = []
    found = False

    # Read existing rows if any
    if os.path.exists(path):
        with open(path, "r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for r in reader:
                if r.get("mapped_ou_id") == row["mapped_ou_id"] and r.get("source_tei_id") == row["source_tei_id"]:
                    # Replace the row with the new data
                    r = {
                        **row,
                        "last_updated": datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z",
                    }
                    found = True
                existing_rows.append(r)

    # If not found, append it
    if not found:
        existing_rows.append({
            **row,
            "last_updated": datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z",
        })

    # Write back
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=MAPPING_COLUMNS)
        writer.writeheader()
        for r in existing_rows:
            # Ensure JSON fields are serialized as strings
            out = dict(r)
            if isinstance(out.get("enrollment_map_json"), dict):
                out["enrollment_map_json"] = json.dumps(out["enrollment_map_json"], separators=(",", ":"))
            if isinstance(out.get("event_map_json"), dict):
                out["event_map_json"] = json.dumps(out["event_map_json"], separators=(",", ":"))
            writer.writerow(out)


def ensure_ids_for_tei(api: "DHIS2", tei: dict, mapped_ou_id: str,
                       csv_mapping: Dict[Tuple[str, str], dict]) -> Tuple[str, dict, dict]:
    """
    Ensures we have persistent target IDs for this TEI (and its nested enrollments/events)
    *for the given mapped OU*.

    Returns:
      (target_tei_id, enrollment_map, event_map)

    Logic:
    - Key is (mapped_ou_id, source_tei_id). If the row exists in CSV, reuse its IDs.
    - Otherwise, generate NEW IDs on the target server using /system/id and return them.
    """
    # TEI UID from source payload (DHIS2 allows 'trackedEntity' and older 'trackedEntityInstance')
    src_tei = tei.get("trackedEntity") or tei.get("trackedEntityInstance")
    key = (mapped_ou_id, src_tei or "")
    row = csv_mapping.get(key) or {"target_tei_id": "", "enrollment_map": {}, "event_map": {}}

    # Collect source enrollment and event UIDs
    src_enrs: List[str] = []
    src_evts: List[str] = []
    for enr in (tei.get("enrollments") or []):
        if isinstance(enr, dict) and enr.get("enrollment"):
            src_enrs.append(enr["enrollment"])
            for ev in (enr.get("events") or []):
                if isinstance(ev, dict) and ev.get("event"):
                    src_evts.append(ev["event"])

    # Count how many new IDs we need (TEI + missing enrollments + missing events)
    need = 0
    target_tei_id = row["target_tei_id"]
    if not target_tei_id:
        need += 1
    for e in src_enrs:
        if e not in row["enrollment_map"]:
            need += 1
    for ev in src_evts:
        if ev not in row["event_map"]:
            need += 1

    new_ids = generate_ids(api, need) if need > 0 else []
    idx = 0

    # TEI
    if not target_tei_id:
        target_tei_id = new_ids[idx]
        idx += 1 if need else 0

    # Enrollment map
    enrollment_map = dict(row["enrollment_map"])
    for e in src_enrs:
        if e not in enrollment_map:
            enrollment_map[e] = new_ids[idx]
            idx += 1

    # Event map
    event_map = dict(row["event_map"])
    for ev in src_evts:
        if ev not in event_map:
            event_map[ev] = new_ids[idx]
            idx += 1

    return target_tei_id, enrollment_map, event_map


def apply_uid_mapping_to_payload(tei: dict, target_tei_id: str,
                                 enrollment_map: dict, event_map: dict) -> None:
    """
    Mutates TEI payload to replace IDs consistently:

    Replace:
      - TEI id: tei['trackedEntity'] (and 'trackedEntityInstance' if present)
      - Enrollment id: enrollment['enrollment']
      - Event id: event['event']

    Also fix back-references:
      - enrollment['trackedEntity'] -> target_tei_id
      - event['trackedEntity'] -> target_tei_id (if present)
      - event['enrollment'] -> the mapped enrollment id
    """
    # TEI id
    if "trackedEntityInstance" in tei:  # legacy key
        tei["trackedEntityInstance"] = target_tei_id
    tei["trackedEntity"] = target_tei_id

    # Nested
    for enr in (tei.get("enrollments") or []):
        if not isinstance(enr, dict):
            continue

        src_enr = enr.get("enrollment")
        if src_enr and src_enr in enrollment_map:
            enr["enrollment"] = enrollment_map[src_enr]

        # point enrollment TEI back to the new TEI id
        enr["trackedEntity"] = target_tei_id

        for ev in (enr.get("events") or []):
            if not isinstance(ev, dict):
                continue

            src_evt = ev.get("event")
            if src_evt and src_evt in event_map:
                ev["event"] = event_map[src_evt]

            # back-references
            if "trackedEntity" in ev:
                ev["trackedEntity"] = target_tei_id
            if "enrollment" in ev and src_enr and src_enr in enrollment_map:
                ev["enrollment"] = enrollment_map[src_enr]


# ---- Small helpers on TEI attributes ----
def get_attr(attrs: List[Dict[str, Any]], uid: str) -> Optional[Dict[str, Any]]:
    for a in attrs:
        if a.get("attribute") == uid:
            return a
    return None

def set_attr(attrs: List[Dict[str, Any]], uid: str, value: Any, display_name: Optional[str] = None, value_type: Optional[str] = None):
    a = get_attr(attrs, uid)
    if a is None:
        a = {"attribute": uid, "value": value}
        if display_name:
            a["displayName"] = display_name
        if value_type:
            a["valueType"] = value_type
        attrs.append(a)
    else:
        a["value"] = value
        if display_name:
            a["displayName"] = display_name
        if value_type:
            a["valueType"] = value_type


# Formats the DoB may arrive in; the first one is what the target expects
DOB_INPUT_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%Y/%m/%d")

# Roughly a third of source records store an age ("60", "55yrs") in the date of
# birth field, and the target rejects those with `value_not_valid_age`. An age
# only pins down the year, so anchor it to 1 January: re-running the sync later
# then produces the same date, which "today minus N years" would not.
MAX_PLAUSIBLE_AGE = 120
EARLIEST_BIRTH_YEAR = 1900

AGE_PATTERN = re.compile(r"(\d{1,3})\s*(?:y|yr|yrs|year|years)?", re.IGNORECASE)
YEAR_PATTERN = re.compile(r"\d{4}")


def _tidy_dob_text(raw: str) -> str:
    """Strip the punctuation and spacing noise seen in the source data."""
    # Tolerate ISO timestamps such as 1980-05-17T00:00:00.000
    text = raw.split("T", 1)[0].strip()
    # Typos where a separator was meant: '12/07)1976'
    text = re.sub(r"[)(\\]", "/", text)
    # Spaces around separators: '04/ 06/ 1976', '19 /2/ 1966'
    text = re.sub(r"\s*([/.-])\s*", r"\1", text)
    return re.sub(r"\s+", " ", text).strip()


def _year_to_iso(year: int) -> Optional[str]:
    """A bare year of birth is only good to the year, so anchor it to 1 January."""
    if EARLIEST_BIRTH_YEAR <= year <= date.today().year:
        return "%04d-01-01" % year
    return None


def dob_to_iso(raw: str) -> Optional[str]:
    """
    Best-effort yyyy-mm-dd for one raw DoB value, or None if it is not
    recognisable. Kept separate from normalise_dob so it can be tested directly.
    """
    text = _tidy_dob_text(raw)
    if not text:
        return None

    for fmt in DOB_INPUT_FORMATS:
        try:
            return datetime.datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue

    # An age, with or without a unit suffix: '24', '55yrs', '100 yrs'
    match = AGE_PATTERN.fullmatch(text)
    if match:
        age = int(match.group(1))
        if 0 < age <= MAX_PLAUSIBLE_AGE:
            return "%04d-01-01" % (date.today().year - age)

    # A bare year of birth: '1974'
    if YEAR_PATTERN.fullmatch(text):
        return _year_to_iso(int(text))

    return None


def normalise_dob(tei: dict) -> None:
    """
    Rewrite the DoB attribute to yyyy-mm-dd when it is stored in another format
    (e.g. dd/mm/yyyy, or an age), which the target rejects with
    `value_not_valid_age`. Leaves the value untouched if it cannot be parsed.
    """
    a = get_attr(tei.get("attributes", []), TEA_PATIENT_DOB)
    if not a or not a.get("value"):
        return
    raw = str(a["value"]).strip()
    iso = dob_to_iso(raw)
    if iso is None:
        print(f"  - WARNING: unrecognised DoB '{raw}' on TEI {tei.get('trackedEntity')}, left as-is")
        return
    if iso != raw:
        a["value"] = iso
        print(f"  - Normalised DoB on TEI {tei.get('trackedEntity')}: {raw} -> {iso}")


def remap_unsupported_hctz(tei: dict) -> int:
    """
    Move HYDROCHLOROTHIAZIDE_12POINT5MG off the Hydrochlorothiazide data element
    and into the free-text "Other hypertension medication" one, which carries no
    option set. Returns the number of events changed.
    """
    changed = 0

    for enr in tei.get("enrollments", []) or []:
        for ev in enr.get("events", []) or []:
            dvs = ev.get("dataValues") or []

            hctz = next(
                (dv for dv in dvs
                 if dv.get("dataElement") == DE_HCTZ
                 and str(dv.get("value") or "").strip() == HCTZ_UNSUPPORTED_CODE),
                None,
            )
            if hctz is None:
                continue

            dvs.remove(hctz)

            other = next((dv for dv in dvs if dv.get("dataElement") == DE_OTHER_HTN_MED), None)
            if other is None:
                dvs.append({"dataElement": DE_OTHER_HTN_MED, "value": HCTZ_UNSUPPORTED_CODE})
            else:
                # Append rather than overwrite: whatever the clinician already
                # recorded as the other medication still matters.
                existing = str(other.get("value") or "").strip()
                if not existing:
                    other["value"] = HCTZ_UNSUPPORTED_CODE
                elif HCTZ_UNSUPPORTED_CODE not in existing:
                    other["value"] = f"{existing}; {HCTZ_UNSUPPORTED_CODE}"

            ev["dataValues"] = dvs
            changed += 1

    return changed


def drop_events_missing_bp(tei: dict):
    """
    For each enrollment/event, drop the event if it does not contain BOTH required BP DEs
    (diastolic yNhtHKtKkO1 AND systolic IxEwYiq1FTq) with a non-empty value.
    """
    removed_total = 0
    for enr in tei.get("enrollments", []) or []:
        events = enr.get("events", []) or []
        kept = []
        for ev in events:
            dvs = ev.get("dataValues") or []
            # Build set of DEs that are present with a non-empty value
            present = set()
            for dv in dvs:
                de = dv.get("dataElement")
                if de in BP_REQUIRED_SET and ("value" in dv):
                    v = dv["value"]
                    # accept numeric (0 unlikely here) or non-empty string
                    if v is not None and (not isinstance(v, str) or v.strip() != ""):
                        present.add(de)

            if BP_REQUIRED_SET.issubset(present):
                kept.append(ev)
            else:
                removed_total += 1

        enr["events"] = kept

    if removed_total:
        print(f"  - Dropped {removed_total} event(s) missing BP from TEI {tei.get('trackedEntity')}")


def strip_invalid_data_elements(tei: dict):
    """Remove offending DataElements only from events in the HTN Visit stage."""
    removed_total = 0
    for enr in tei.get("enrollments", []) or []:
        for ev in enr.get("events", []) or []:
            # Only apply stripping to events of the target stage
            if ev.get("programStage") != HTN_VISIT_STAGE:
                continue

            dvs = ev.get("dataValues") or []
            if not isinstance(dvs, list):
                continue

            before = len(dvs)
            ev["dataValues"] = [
                dv for dv in dvs
                if dv.get("dataElement") not in DE_DROP_SET
            ]
            removed_total += before - len(ev["dataValues"])
    if removed_total:
        print(f"  - Stripped {removed_total} invalid dataValues from TEI {tei.get('trackedEntity')}")

def anonymise_attrs(tei: Dict[str, Any], fake: Faker):
    attrs = tei.get("attributes", [])

    # Drop any present value for attribute ea5w3mkinZo
    before = len(attrs)
    attrs[:] = [
        a for a in attrs
        if not (a.get("attribute") == "ea5w3mkinZo" and "value" in a)
    ]
    if len(attrs) != before:
        print(f"  - Dropped attribute ea5w3mkinZo on TEI {tei.get('trackedEntity')}")

    # (existing) Fix NCD status UNRESPONSIVE -> INACTIVE
    for a in attrs:
        if a.get("attribute") == TEA_NCD_STATUS and a.get("value") == "UNRESPONSIVE":
            print(f"Fixing TEI {tei.get('trackedEntity')}: UNRESPONSIVE → INACTIVE")
            a["value"] = "INACTIVE"
        # if a.get("attribute") == TEA_NCD_STATUS:
        #     print("NCD Status:" + a.get("value"))

    # (new) Ensure HTN "Does patient have hypertension?" exists; default YES if missing
    if get_attr(attrs, TEA_HTN_YN) is None:
        set_attr(attrs, TEA_HTN_YN, "YES", "HTN - Does patient have hypertension?")

    # (existing) Address anonymisation
    set_attr(attrs, TEA_ADDRESS, fake.address(), "Address (current)", "TEXT")

    # (existing) Patient phone anonymisation or create
    a_phone = get_attr(attrs, TEA_PATIENT_PHONE)
    if a_phone and "value" in a_phone:
        raw = fake.msisdn()
        a_phone["value"] = f"+{raw}" if not str(raw).startswith("+") else raw
    else:
        set_attr(attrs, TEA_PATIENT_PHONE, fake.phone_number(), "Patient Phone Number", "PHONE_NUMBER")

    # (existing) Supporter name / phone anonymisation ONLY if present
    a_sup_name = get_attr(attrs, TEA_SUPPORTER_NAME)
    if a_sup_name and "value" in a_sup_name:
        a_sup_name["value"] = fake.name()
    a_sup_phone = get_attr(attrs, TEA_SUPPORTER_PHONE)
    if a_sup_phone and "value" in a_sup_phone:
        raw = fake.msisdn()
        a_sup_phone["value"] = f"+{raw}" if not str(raw).startswith("+") else raw

    # (existing) Patient name using sex
    sex_attr = get_attr(attrs, TEA_SEX)
    sex = (sex_attr.get("value") if sex_attr else "").strip().upper()
    if sex not in ("MALE", "FEMALE"):
        sex = "MALE" if fake.boolean() else "FEMALE"
    family_name = fake.last_name_male() if sex == "MALE" else fake.last_name_female()
    given_name = fake.first_name_male() if sex == "MALE" else fake.first_name_female()
    set_attr(attrs, TEA_PATIENT_NAME, f"{given_name} {family_name}")

    # (existing) Consent must be present and "true"
    a_consent = get_attr(attrs, TEA_CONSENT)
    if a_consent is None:
        set_attr(attrs, TEA_CONSENT, "true", "HTN - Consent to record data", "TRUE_ONLY")
    else:
        a_consent["value"] = "true"


def tei_has_required_attrs(tei: Dict[str, Any]) -> (bool, str):
    """Return (ok, reason). ok=False with reason when required attrs missing or attributes lack 'value'."""
    attrs = tei.get("attributes", [])
    have = {a.get("attribute") for a in attrs if "value" in a}
    if not REQUIRED_ATTRS.issubset(have):
        missing = ", ".join(sorted(REQUIRED_ATTRS - have))
        return False, f"missing required attributes: {missing}"

    # also ensure any attribute listed must have 'value'
    for a in attrs:
        if "value" not in a:
            return False, f"attribute without value: {a.get('attribute')}"
    return True, ""

def iter_source_teis(src: DHIS2, program_uid: str, org_unit: str, page_size: int = 50, updated_offset_days: int = 0):
    """
    Yield (page_number, page_items, pager_dict) for each page of TEIs.
    Robust to servers that use 'instances' (Tracker) or 'trackedEntities' (older),
    and to missing/partial 'pager'.
    """
    fields = "trackedEntity,trackedEntityType,orgUnit,attributes,enrollments[*,!attributes]"
    page = 1
    MAX_PAGES = 100000  # safety

    while True:
        params = {
            "program": program_uid,
            "orgUnit": org_unit,
            "ouMode": "DESCENDANTS",
            "fields": fields,
            "page": page,              # 1-based
            "pageSize": page_size,
            "skipPaging": "false",     # force one page
            "totalPages": "true",      # ask for pager info
        }
        if updated_offset_days and updated_offset_days > 0:
            updated_after = (date.today() - timedelta(days=updated_offset_days)).isoformat()
            params["updatedAfter"] = updated_after
        r = src.get("tracker/trackedEntities", params=params)
        if r.status_code != 200:
            raise RuntimeError(f"Source fetch failed (HTTP {r.status_code}): {r.text}")

        data = r.json() or {}
        page_items = data.get("instances")
        if page_items is None:
            page_items = data.get("trackedEntities", [])
        pager = data.get("pager") or {}

        yield page, page_items, pager

        # preferred exit via pager
        cur_page = int(pager.get("page", page) or page)
        page_count = int(pager.get("pageCount") or pager.get("totalPages") or 0)
        if page_count and cur_page >= page_count:
            break

        # fallback exit: stop on empty/short page
        if not page_items or len(page_items) < page_size:
            break

        page += 1
        if page > MAX_PAGES:
            raise RuntimeError(f"Aborting: exceeded maximum page guard ({MAX_PAGES}).")



def summarize_ok_notes(result_json: Dict[str, Any]) -> str:
    stats = result_json.get("stats", {})
    parts = []
    for k in ("created", "updated", "deleted", "ignored", "total"):
        if k in stats:
            parts.append(f"{k}={stats.get(k)}")
    tr = (result_json.get("bundleReport", {}) or {}).get("typeReportMap", {}) or {}
    if "TRACKED_ENTITY" in tr and "stats" in tr["TRACKED_ENTITY"]:
        te_s = tr["TRACKED_ENTITY"]["stats"]
        parts.append(f"TEI(c={te_s.get('created',0)},u={te_s.get('updated',0)},i={te_s.get('ignored',0)})")
    if "ENROLLMENT" in tr and "stats" in tr["ENROLLMENT"]:
        en_s = tr["ENROLLMENT"]["stats"]
        parts.append(f"ENR(c={en_s.get('created',0)},u={en_s.get('updated',0)},i={en_s.get('ignored',0)})")
    if "EVENT" in tr and "stats" in tr["EVENT"]:
        ev_s = tr["EVENT"]["stats"]
        parts.append(f"EVT(c={ev_s.get('created',0)},u={ev_s.get('updated',0)},i={ev_s.get('ignored',0)})")
    return "; ".join(parts) if parts else "OK"

def summarize_error_notes(result_json: Dict[str, Any]) -> str:
    errs = (result_json.get("validationReport", {}) or {}).get("errorReports", []) or []
    if errs:
        msgs = []
        for e in errs[:6]:
            m = e.get("message") or e.get("errorCode") or "validation error"
            uid = e.get("uid")
            if uid:
                m = f"{m} (uid={uid})"
            msgs.append(m)
        return " | ".join(msgs)
    return result_json.get("status") or "ERROR"

def main():
    ap = argparse.ArgumentParser(description="Fetch TEIs from source DHIS2, anonymise, and post to target DHIS2.")
    ap.add_argument("--source", default=DEFAULT_SOURCE_PREFIX, metavar="PREFIX",
                    help=f".env prefix for the source server (default: {DEFAULT_SOURCE_PREFIX})")
    ap.add_argument("--target", default=DEFAULT_TARGET_PREFIX, metavar="PREFIX",
                    help=f".env prefix for the target server (default: {DEFAULT_TARGET_PREFIX})")
    ap.add_argument("--source-url", help="Override the source URL from .env")
    ap.add_argument("--source-user", help="Override the source username from .env")
    ap.add_argument("--source-pass", help="Override the source password from .env")
    ap.add_argument("--target-url", help="Override the target URL from .env")
    ap.add_argument("--target-user", help="Override the target username from .env")
    ap.add_argument("--target-pass", help="Override the target password from .env")
    ap.add_argument("--program", default=PROGRAM_UID)
    ap.add_argument("--org-unit", default=DEFAULT_ORG_UNIT)
    ap.add_argument("--page-size", type=int, default=50)
    ap.add_argument("--insecure", action="store_true", help="Disable TLS verification")
    ap.add_argument("--seed", type=int, default=42, help="Faker seed for reproducible anonymisation")
    ap.add_argument("--updated-offset-days", type=int, default=0, metavar="N", help="Only sync TEIs updated in the last N days (0 = no filter).")
    ap.add_argument("--map-to-ou", dest="map_to_ou", default=None,
                    help = "If provided, remap the orgUnit everywhere in the TEI payload (root, enrollments, and events) "
                           "to this OU UID. This OU must exist on the TARGET server.")
    ap.add_argument("--only-uids", dest="only_uids", default=None, metavar="FILE",
                    help="Sync only the TEI UIDs listed in FILE (a results CSV with a 'uid' "
                         "column, or one UID per line) instead of streaming the whole program.")
    ap.add_argument("--disable-rule", dest="disable_rules", action="append", default=[],
                    metavar="UID",
                    help="Park this programRule on the TARGET for the duration of the run by "
                         "prefixing its condition with 'false && ', then restore it. Repeatable.")
    ap.add_argument("--mapping-csv", dest="mapping_csv", default="ou_uid_mapping.csv",
                    help="Path to local CSV with persistent mappings of TEI uids per OU (avoid creating duplicates when"
                         "mapping to another OU)")
    args = ap.parse_args()

    fake = Faker()
    Faker.seed(args.seed)

    args.source_url, source_user, source_pass = resolve_server(
        "source", args.source, args.source_url, args.source_user, args.source_pass)
    args.target_url, target_user, target_pass = resolve_server(
        "target", args.target, args.target_url, args.target_user, args.target_pass)

    print(f"Source: {args.source_url}")
    print(f"Target: {args.target_url}")

    src = DHIS2(args.source_url, source_user, source_pass, verify=not args.insecure)
    tgt = DHIS2(args.target_url, target_user, target_pass, verify=not args.insecure)

    # If mapping is requested, verify the OU exists on TARGET once (fail fast if not)

    if args.map_to_ou:
        if not org_unit_exists(tgt, args.map_to_ou):
            print(f"ERROR: Target orgUnit '{args.map_to_ou}' not found on {args.target_url}", file=sys.stderr)
            sys.exit(2)
        else:
            print(f"Verified target orgUnit {args.map_to_ou} exists on {args.target_url}")

    # Load csv mapping
    mapping_csv_path = args.mapping_csv
    mapping_dict = load_mapping_csv(mapping_csv_path)

    results_csv = "sync_results.csv"
    # Prepare CSV writer in append mode; write header if file doesn't exist yet
    need_header = not os.path.exists(results_csv)
    csv_f = open(results_csv, "a", newline="", encoding="utf-8")
    writer = csv.DictWriter(csv_f, fieldnames=["uid", "id number", "qr code", "result", "notes"])
    if need_header:
        writer.writeheader()
        csv_f.flush()

    total_processed = 0

    saved_rules = disable_program_rules(tgt, args.disable_rules)

    try:
        if args.only_uids:
            selected = load_uid_list(args.only_uids)
            print(f"Syncing {len(selected)} selected TEIs from {args.only_uids} "
                  f"in pages of {args.page_size}...")
            source_pages = iter_selected_teis(src, selected, args.program, page_size=args.page_size)
        else:
            print(f"Streaming TEIs from source in pages of {args.page_size}...")
            source_pages = iter_source_teis(src, args.program, args.org_unit, page_size=args.page_size,
                                            updated_offset_days=args.updated_offset_days)

        for page_no, teis, pager in source_pages:
            print(f"Page {pager.get('page', page_no)}: {len(teis)} TEIs")

            for tei in teis:
                total_processed += 1
                tei_uid = tei.get("trackedEntity")

                # Read CSV fields BEFORE anonymisation
                attrs = tei.get("attributes", [])
                id_number_val = (get_attr(attrs, TEA_ID_NUMBER) or {}).get("value", "")
                qr_code_val = (get_attr(attrs, TEA_QR_CODE) or {}).get("value", "")

                # Drop if missing required attributes (DOB, Patient name, Sex)
                ok, reason = tei_has_required_attrs(tei)
                if not ok:
                    writer.writerow({
                        "uid": tei_uid or "",
                        "id number": id_number_val,
                        "qr code": qr_code_val,
                        "result": "ERROR",
                        "notes": reason
                    })
                    csv_f.flush()
                    print(f"[{total_processed}] SKIP TEI {tei_uid}: {reason}")
                    continue

                # Anonymise / fix attributes
                anonymise_attrs(tei, fake)

                # Convert DoB from dd/mm/yyyy (and similar) to yyyy-mm-dd
                normalise_dob(tei)

                # Drop events with any BP readings missing
                # drop_events_missing_bp(tei)

                # Drop problem data elements in events
                strip_invalid_data_elements(tei)

                # Clean invalid DEs first (your existing function)
                strip_invalid_data_elements(tei)

                # Move the 12.5mg HCTZ code the target does not accept
                moved = remap_unsupported_hctz(tei)
                if moved:
                    print(f"  - Moved {HCTZ_UNSUPPORTED_CODE} to Other hypertension "
                          f"medication on {moved} event(s) of TEI {tei.get('trackedEntity')}")

                if args.map_to_ou:
                    # ---- MAPPING MODE (only when --map-to-ou is provided) ----
                    # 1) Force orgUnit everywhere to the target OU
                    remap_org_units(tei, args.map_to_ou)

                    # 2) Capture the ORIGINAL source TEI UID BEFORE changing any IDs.
                    #    We'll store this as `source_tei_id` in the mapping CSV key.
                    original_source_tei_id = tei.get("trackedEntity") or tei.get("trackedEntityInstance")

                    # 3) Ensure (or generate) persistent target IDs for this TEI/enrollments/events
                    #    keyed by (mapped_ou_id, source_tei_id). The OU is the one we are mapping TO.
                    mapped_ou_id = args.map_to_ou
                    target_tei_id, enrollment_map, event_map = ensure_ids_for_tei(
                        tgt, tei, mapped_ou_id, mapping_dict
                    )

                    # 4) Apply the ID mapping to the payload (mutates in-place)
                    apply_uid_mapping_to_payload(tei, target_tei_id, enrollment_map, event_map)

                    # 5) Upsert the mapping row so future daily runs reuse these target IDs
                    row = {
                        "mapped_ou_id": mapped_ou_id,
                        "source_tei_id": original_source_tei_id or "",
                        "target_tei_id": target_tei_id,
                        "enrollment_map_json": enrollment_map,  # dict - will be JSON-serialized by upsert_mapping_row
                        "event_map_json": event_map,  # dict - will be JSON-serialized by upsert_mapping_row
                    }
                    upsert_mapping_row(mapping_csv_path, row)

                    # Override TEA qr code and patient id before sending
                    override_test_attributes(tei)

                else:
                    # ---- NORMAL MODE (no --map-to-ou) ----
                    # No OU rewrite, no UID changes, no CSV updates.
                    # We just post the cleaned TEI as-is.
                    pass

                # 6) POST to target, one TEI at a time
                payload = {"trackedEntities": [tei]}
                try:
                    resp = tgt.post(
                        "tracker",
                        params={
                            "async": "false",
                            "importStrategy": "CREATE_AND_UPDATE",
                            "skipRuleEngine": "false",
                        },
                        json=payload,
                    )
                except requests.RequestException as e:
                    note = f"RequestException: {e}"
                    writer.writerow({
                        "uid": tei_uid or "",
                        "id number": id_number_val,
                        "qr code": qr_code_val,
                        "result": "ERROR",
                        "notes": note
                    })
                    csv_f.flush()
                    print(f"[{total_processed}] TEI {tei_uid}: ERROR - {note}")
                    continue

                # Interpret response
                result_json = {}
                try:
                    result_json = resp.json()
                except Exception:
                    pass

                if resp.status_code == 200 and (not result_json or result_json.get("status") in (None, "OK")):
                    result = "OK"
                    notes = summarize_ok_notes(result_json) if result_json else "OK"
                else:
                    result = "ERROR"
                    notes = summarize_error_notes(result_json) if result_json else f"HTTP {resp.status_code}"

                writer.writerow({
                    "uid": tei_uid or "",
                    "id number": id_number_val,
                    "qr code": qr_code_val,
                    "result": result,
                    "notes": notes
                })
                csv_f.flush()
                print(f"[{total_processed}] TEI {tei_uid}: {result} - {notes}")

            # free page memory before next fetch
            del teis
            gc.collect()

        print(f"Done. Total TEIs processed: {total_processed}")

    finally:
        csv_f.close()
        restore_program_rules(tgt, saved_rules)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted by user.", file=sys.stderr)
        sys.exit(130)
