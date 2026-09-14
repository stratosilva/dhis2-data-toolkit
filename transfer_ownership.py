#!/usr/bin/env python3
import os
import sys
import csv
import time
import random
import argparse
import requests
from typing import Optional, Dict, Any, List, Tuple
from http.client import RemoteDisconnected
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from requests.exceptions import ConnectionError, ReadTimeout, ChunkedEncodingError

# ---------------------------
# Defaults / constants
# ---------------------------
DEFAULT_CSV = "HypertensionTreatmen_DATA_2025-09-03_0849.csv"
DEFAULT_PROGRAM_UID = "pMIglSEqPGS"
DEFAULT_ORG_UNIT_ROOT = "uS2CuRbDyD7"   # root OU for DESCENDANTS query
ATTR_ID_NUMBER = "RAViWaXdOM8"          # attribute used as patient_id
QUERY_FIELDS = "trackedEntity,orgUnit,programOwners"
EXPECTED_BASELINE_COUNT = 22174

# ---------------------------------------
# Simple DHIS2 client with retry behavior
# ---------------------------------------
class DHIS2:
    def __init__(self, base_url: str, username: str, password: str, timeout: int = 60, verify: bool = True):
        base = base_url.rstrip("/")
        if not base.endswith("/api"):
            base = base + "/api"
        self.base_url = base
        self.s = requests.Session()
        self.s.auth = (username, password)
        self.s.headers.update({"Accept": "application/json"})
        self.s.verify = False
        self.timeout = timeout

        # Retry knobs
        self.max_attempts_get = 10
        self.max_attempts_put = 8
        self.backoff_cap_sec = 300   # 5 min cap
        self.base_backoff_sec = 10   # grows exponentially

    def _req_with_retry(self, method: str, path: str, params=None, json=None,
                        max_attempts: int = 8) -> requests.Response:
        url = f"{self.base_url}/{path.lstrip('/')}"
        attempt = 0
        while True:
            attempt += 1
            try:
                if method == "GET":
                    resp = self.s.get(url, params=params, timeout=self.timeout)
                elif method == "PUT":
                    resp = self.s.put(url, params=params, json=json, timeout=self.timeout)
                else:
                    raise ValueError(f"Unsupported method: {method}")
            except (ConnectionError, ReadTimeout, ProtocolError, RemoteDisconnected, ChunkedEncodingError) as e:
                if attempt >= max_attempts:
                    raise
                sleep_s = min(self.backoff_cap_sec, self.base_backoff_sec * (2 ** (attempt - 1)))
                jitter = random.uniform(0, sleep_s * 0.1)
                print(f"[{method}] {path} failed ({type(e).__name__}). "
                      f"Attempt {attempt}/{max_attempts}. Sleeping {int(sleep_s + jitter)}s…")
                time.sleep(sleep_s + jitter)
                continue

            # Retry on 429/502/503/504
            if resp.status_code in (429, 502, 503, 504) and attempt < max_attempts:
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

            return resp

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> requests.Response:
        return self._req_with_retry("GET", path, params=params, max_attempts=self.max_attempts_get)

    def put(self, path: str, params: Optional[Dict[str, Any]] = None, json: Optional[Dict[str, Any]] = None) -> requests.Response:
        return self._req_with_retry("PUT", path, params=params, json=json, max_attempts=self.max_attempts_put)

# ---------------------------------------
# CSV reading & patient id extraction
# ---------------------------------------
import csv
import os

def read_baseline_patient_ids(csv_path: str, expected_count: int, sep: str = ";"):
    """
    Reads CSV, keeps only rows where redcap_event_name == baseline_arm_1.
    Extracts patient IDs using:
      1) unique_id if present
      2) else first non-empty among patient_id_1..patient_id_6
    Ensures there are exactly expected_count unique ids; otherwise raises.
    Uses a custom delimiter (default ';') and trims header names.
    """
    if not os.path.exists(csv_path):
        raise FileNotFoundError(f"CSV not found: {csv_path}")

    candidate_ids = []
    missing_rows = 0

    # Read and normalize header, then feed the rest to DictReader with the same delimiter
    with open(csv_path, newline="", encoding="utf-8", errors="replace") as f:
        header_line = f.readline()
        if not header_line:
            raise RuntimeError("CSV appears empty (no header line)")
        header = [c.strip() for c in header_line.rstrip("\r\n").split(sep)]

        reader = csv.DictReader(
            f,
            fieldnames=header,
            delimiter=sep,
            skipinitialspace=True,   # ignore spaces after delimiter
        )

        # Iterate data rows
        for row in reader:
            evt = (row.get("redcap_event_name") or "").strip().lower()
            if evt != "baseline_arm_1":
                continue

            pid = (row.get("unique_id") or "").strip()
            if not pid:
                for i in range(1, 7):
                    val = (row.get(f"patient_id_{i}") or "").strip()
                    if val:
                        pid = val
                        break

            if pid:
                candidate_ids.append(pid)
            else:
                missing_rows += 1

    # Deduplicate in original order
    seen = set()
    unique_ids = []
    for pid in candidate_ids:
        if pid not in seen:
            seen.add(pid)
            unique_ids.append(pid)

    if len(unique_ids) != expected_count:
        raise RuntimeError(
            f"ERROR: Expected {expected_count} unique patient IDs at baseline, "
            f"got {len(unique_ids)} (rows with any pid={len(candidate_ids)}, "
            f"missing PID rows={missing_rows})."
        )

    return unique_ids


# ---------------------------------------
# Tracker query & ownership transfer
# ---------------------------------------
def fetch_te_by_id(client: DHIS2, patient_id: str, program_uid: str, org_unit_root: str) -> Dict[str, Any]:
    """
    Query tracker/trackedEntities for a specific patient_id attribute.
    Returns a dict with key 'instances'.
    """
    params = {
        "program": program_uid,
        "orgUnit": org_unit_root,
        "ouMode": "DESCENDANTS",
        "attribute": f"{ATTR_ID_NUMBER}:eq:{patient_id}",
        "fields": QUERY_FIELDS,
        "page": 1,
        "pageSize": 50,
        "skipPaging": "false",
        "totalPages": "true",
    }
    r = client.get("tracker/trackedEntities", params=params)
    if r.status_code != 200:
        raise RuntimeError(f"GET tracker/trackedEntities failed for {patient_id} (HTTP {r.status_code}): {r.text[:300]}")
    data = r.json() or {}
    instances = data.get("instances")
    if instances is None:
        instances = data.get("trackedEntities", [])
    return {"instances": instances}

def transfer_ownership(client: DHIS2, tei_uid: str, program_uid: str, ou_uid: str, dry_run: bool = False) -> Tuple[bool, str]:
    """
    PUT /api/tracker/ownership/transfer?trackedEntityInstance=...&program=...&ou=...
    Returns (ok, note).
    """
    if dry_run:
        return True, f"DRY-RUN (would transfer ownership to ou={ou_uid})"

    params = {
        "trackedEntityInstance": tei_uid,
        "program": program_uid,
        "ou": ou_uid,
    }
    r = client.put("tracker/ownership/transfer", params=params)
    try:
        j = r.json()
    except Exception:
        j = {}

    if r.status_code == 200 and (j.get("status") in (None, "OK")):
        return True, "ownership transferred"
    if r.status_code == 200 and not j:
        return True, "ownership transferred (empty JSON)"
    return False, f"HTTP {r.status_code} {r.text[:300]}"

# ---------------------------------------
# Main
# ---------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Transfer program ownership to each TEI's orgUnit for all baseline patient IDs.")
    ap.add_argument("--csv", default=DEFAULT_CSV, help=f"Source CSV (default: {DEFAULT_CSV})")
    ap.add_argument("--base-url", required=True, help="DHIS2 base URL (e.g., https://your.dhis2.org/dhis or with /api)")
    ap.add_argument("--user", required=True, help="DHIS2 username")
    ap.add_argument("--pass", dest="password", required=True, help="DHIS2 password")
    ap.add_argument("--program", default=DEFAULT_PROGRAM_UID, help=f"Program UID (default: {DEFAULT_PROGRAM_UID})")
    ap.add_argument("--org-unit-root", default=DEFAULT_ORG_UNIT_ROOT, help=f"Root OU for the query (default: {DEFAULT_ORG_UNIT_ROOT})")
    ap.add_argument("--expected-count", type=int, default=EXPECTED_BASELINE_COUNT, help=f"Expected unique baseline count (default: {EXPECTED_BASELINE_COUNT})")
    ap.add_argument("--verify", action="store_true", help="Verify TLS certs (off by default).")
    ap.add_argument("--dry-run", action="store_true", help="Do not perform PUT; just print what would happen.")
    args = ap.parse_args()

    client = DHIS2(base_url=args.base_url, username=args.user, password=args.password, verify=args.verify)

    # 1) Read CSV & extract baseline patient IDs
    patient_ids = read_baseline_patient_ids(args.csv, expected_count=args.expected_count)
    total = len(patient_ids)  # should be expected-count (22174)
    print(f"Found {total} unique baseline patient IDs.")

    # 2) For each patient ID, fetch TEI and transfer ownership
    processed = 0
    missing = 0
    errors = 0
    for idx, pid in enumerate(patient_ids, start=1):
        try:
            q = fetch_te_by_id(client, pid, args.program, args.org_unit_root)
            instances = q.get("instances") or []
            if len(instances) == 0:
                print(f"[{idx}/{total}] {pid}: WARNING - no TEI found; skipping")
                missing += 1
                continue
            if len(instances) > 1:
                uids = [i.get("trackedEntity") for i in instances if i.get("trackedEntity")]
                raise RuntimeError(f"{pid}: duplicate TEIs found: {uids}")

            tei = instances[0]
            tei_uid = tei.get("trackedEntity")
            ou_uid = tei.get("orgUnit")
            if not tei_uid or not ou_uid:
                raise RuntimeError(f"{pid}: TEI missing orgUnit or trackedEntity in response")

            ok, note = transfer_ownership(client, tei_uid, args.program, ou_uid, dry_run=args.dry_run)
            print(f"[{idx}/{total}] {pid} → TEI {tei_uid}: {('OK' if ok else 'ERROR')} - {note}")
            processed += 1 if ok else 0
            errors += 0 if ok else 1

        except Exception as e:
            print(f"[{idx}/{total}] {pid}: ERROR - {e}")
            errors += 1

    print(f"\nDone. Processed: {processed}, Missing: {missing}, Errors: {errors}, Total: {total}")

if __name__ == "__main__":
    main()
