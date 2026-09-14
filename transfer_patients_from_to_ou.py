#!/usr/bin/env python3
import argparse
import json
import random
import time
from typing import Any, Dict, List, Optional, Tuple

import requests
from http.client import RemoteDisconnected
from requests.exceptions import ConnectionError, ReadTimeout, ChunkedEncodingError
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ---------------------------
# Config
# ---------------------------
PAGE_SIZE = 200
FIELDS = "trackedEntity,trackedEntityType,orgUnit,attributes,enrollments[*,!attributes,events[*]]"

# ---------------------------------------
# Simple DHIS2 client with retry behavior
# ---------------------------------------
class DHIS2:
    def __init__(self, base_url: str, username: str, password: str, timeout: int = 60, verify: bool = False):
        base = base_url.rstrip("/")
        if not base.endswith("/api"):
            base = base + "/api"
        self.base_url = base
        self.s = requests.Session()
        self.s.auth = (username, password)
        self.s.headers.update({"Accept": "application/json", "Content-Type": "application/json"})
        self.s.verify = verify
        self.timeout = timeout

        # Retry knobs
        self.max_attempts_get = 10
        self.max_attempts_put = 8
        self.max_attempts_post = 8
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
                elif method == "POST":
                    resp = self.s.post(url, params=params, json=json, timeout=self.timeout)
                else:
                    raise ValueError(f"Unsupported method: {method}")
            except (ConnectionError, ReadTimeout, RemoteDisconnected, ChunkedEncodingError) as e:
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

    def post(self, path: str, params: Optional[Dict[str, Any]] = None, json: Optional[Dict[str, Any]] = None) -> requests.Response:
        return self._req_with_retry("POST", path, params=params, json=json, max_attempts=self.max_attempts_post)

# ---------------------------------------
# Fetch all TEIs for program + OU (with nested enrollments/events)
# ---------------------------------------
def list_teis_full(client: DHIS2, program_uid: str, source_ou_uid: str, page_size: int = PAGE_SIZE) -> List[Dict[str, Any]]:
    page = 1
    results: List[Dict[str, Any]] = []
    while True:
        params = {
            "program": program_uid,
            "orgUnit": source_ou_uid,
            "ouMode": "SELECTED",     # only TEIs whose owning OU is the source OU
            "fields": FIELDS,
            "page": page,
            "pageSize": page_size,
            "skipPaging": "false",
            "totalPages": "true",
        }
        r = client.get("tracker/trackedEntities", params=params)
        if r.status_code != 200:
            raise RuntimeError(f"GET tracker/trackedEntities failed (HTTP {r.status_code}): {r.text[:300]}")
        data = r.json() or {}
        instances = data.get("instances") or data.get("trackedEntities") or []
        results.extend(instances)

        current = data.get("page", page)
        total_pages = data.get("pageCount") or data.get("totalPages") or 1
        if current >= total_pages:
            break
        page += 1

    return results

# ---------------------------------------
# Mutate orgUnit everywhere
# ---------------------------------------
def apply_orgunit_mapping_everywhere(tei: Dict[str, Any], dest_ou_uid: str) -> None:
    """
    Mutates TEI payload to set dest_ou_uid across all levels:
      tei['orgUnit'], enr['orgUnit'], ev['orgUnit'].
    Leaves IDs untouched (we are doing UPDATE by ID).
    """
    # TEI level
    if "orgUnit" in tei:
        tei["orgUnit"] = dest_ou_uid

    # Enrollments + Events
    for enr in (tei.get("enrollments") or []):
        if isinstance(enr, dict) and "orgUnit" in enr:
            enr["orgUnit"] = dest_ou_uid
        for ev in (enr.get("events") or []):
            if isinstance(ev, dict) and "orgUnit" in ev:
                ev["orgUnit"] = dest_ou_uid

# ---------------------------------------
# Import (UPDATE) + Ownership transfer
# ---------------------------------------
def post_full_tei_update(client: DHIS2, tei: Dict[str, Any], dry_run: bool = False) -> Tuple[bool, str]:
    """
    POST /api/tracker?importStrategy=UPDATE with the single TEI (including nested enrollments/events).
    """
    payload = {"trackedEntities": [tei]}
    if dry_run:
        return True, "DRY-RUN (would POST tracker UPDATE with nested enrollments/events)"

    params = {
        "importStrategy": "UPDATE",
        # Optional knobs you may want:
        # "atomicMode": "NONE",
        # "reportMode": "FULL",
    }
    r = client.post("tracker", params=params, json=payload)
    ok = r.status_code in (200, 201)
    try:
        body = r.json()
        note = json.dumps(body)[:800]
    except Exception:
        note = r.text[:800]
    return ok, f"HTTP {r.status_code} {note}"

def transfer_ownership_for_enrolled_programs(client: DHIS2, tei: Dict[str, Any], dest_ou_uid: str, dry_run: bool = False) -> List[Tuple[str, bool, str]]:
    """
    For each enrollment present in the TEI, transfer ownership for its program to dest_ou_uid.
    """
    results: List[Tuple[str, bool, str]] = []
    progs = []
    for enr in (tei.get("enrollments") or []):
        prog = enr.get("program")
        if prog:
            progs.append(prog)
    for prog in sorted(set(progs)):
        if dry_run:
            results.append((prog, True, f"DRY-RUN (would transfer ownership to ou={dest_ou_uid})"))
            continue
        params = {"trackedEntityInstance": tei.get("trackedEntity"), "program": prog, "ou": dest_ou_uid}
        r = client.put("tracker/ownership/transfer", params=params)
        try:
            j = r.json()
        except Exception:
            j = {}
        if r.status_code == 200 and (j.get("status") in (None, "OK")):
            results.append((prog, True, "ownership transferred"))
        elif r.status_code == 200 and not j:
            results.append((prog, True, "ownership transferred (empty JSON)"))
        else:
            results.append((prog, False, f"HTTP {r.status_code} {r.text[:300]}"))
    return results

# ---------------------------------------
# Main
# ---------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Move all TEIs of a program from one OU to another (same DHIS2). Fetch TEIs with nested enrollments/events, change orgUnit everywhere, POST full payload (UPDATE), and transfer ownership.")
    ap.add_argument("--base-url", required=True, help="DHIS2 base URL (e.g., https://your.dhis2.org/dhis or with /api)")
    ap.add_argument("--user", required=True, help="DHIS2 username")
    ap.add_argument("--pass", dest="password", required=True, help="DHIS2 password")
    ap.add_argument("--program", required=True, help="Program UID to restrict TEIs")
    ap.add_argument("--source-ou", required=True, help="Source orgUnit UID")
    ap.add_argument("--dest-ou", required=True, help="Destination orgUnit UID")
    ap.add_argument("--page-size", type=int, default=PAGE_SIZE, help=f"Page size for TEI listing (default {PAGE_SIZE})")
    ap.add_argument("--verify", action="store_true", help="Verify TLS certs (off by default).")
    ap.add_argument("--dry-run", action="store_true", help="Do not modify; only print what would happen.")
    args = ap.parse_args()

    client = DHIS2(base_url=args.base_url, username=args.user, password=args.password, verify=args.verify)

    print(f"Listing TEIs in program {args.program} and OU {args.source_ou}…")
    teis = list_teis_full(client, args.program, args.source_ou, page_size=args.page_size)
    total = len(teis)
    print(f"Found {total} TEIs.")

    processed = 0
    updates_ok = 0
    updates_err = 0
    ownership_ok = 0
    ownership_err = 0

    for idx, tei in enumerate(teis, start=1):
        tei_uid = tei.get("trackedEntity")
        if not tei_uid:
            print(f"[{idx}/{total}] WARNING: TEI without trackedEntity UID; skipping")
            continue

        try:
            # Mutate orgUnit everywhere (TEI + enrollments + events)
            apply_orgunit_mapping_everywhere(tei, args.dest_ou)

            # POST full TEI (nested) as UPDATE
            ok, note = post_full_tei_update(client, tei, dry_run=args.dry_run)
            print(f"[{idx}/{total}] TEI {tei_uid}: UPDATE orgUnit → {args.dest_ou}: {'OK' if ok else 'ERROR'} - {note[:200]}")
            updates_ok += 1 if ok else 0
            updates_err += 0 if ok else 1

            # Transfer ownership for every enrolled program
            ow = transfer_ownership_for_enrolled_programs(client, tei, args.dest_ou, dry_run=args.dry_run)
            for prog, ok2, msg2 in ow:
                print(f"    ownership {prog}: {'OK' if ok2 else 'ERROR'} - {msg2}")
                if ok2:
                    ownership_ok += 1
                else:
                    ownership_err += 1

            processed += 1

        except Exception as e:
            print(f"[{idx}/{total}] TEI {tei_uid}: ERROR - {e}")
            updates_err += 1

    print("\nSummary")
    print("-------")
    print(f"TEIs processed:   {processed}/{total}")
    print(f"Tracker updates:  {updates_ok} OK, {updates_err} errors")
    print(f"Ownership:        {ownership_ok} OK, {ownership_err} errors")

if __name__ == "__main__":
    main()
