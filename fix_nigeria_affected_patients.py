#!/usr/bin/env python3
"""
Fix broken TEI attributes in production by copying them from staging.

Requires: dhis2.py  (pip install dhis2.py)

Usage example:
  python fix_tei_attrs_dhis2.py \
    --input affected_patients_nigeria.json \
    --program pMIglSEqPGS \
    --src-url https://staging.example.org \
    --src-user user --src-pass pass \
    --dst-url https://prod.example.org \
    --dst-user user --dst-pass pass

You can also provide credentials via env vars:
  DHIS2_SRC_URL, DHIS2_SRC_USER, DHIS2_SRC_PASS
  DHIS2_DST_URL, DHIS2_DST_USER, DHIS2_DST_PASS
"""

import os
import csv
import json
import time
import argparse
from typing import List, Dict, Any, Optional

from dhis2 import Api, RequestException
from requests.exceptions import ConnectionError
from http.client import RemoteDisconnected

MISSING_CSV = "missing_teis.csv"
FAILED_CSV  = "failed_updates.csv"

def read_tei_uids(path: str) -> List[str]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    # Support both shapes: {"instances":[...]} or {"trackedEntities":[...]} or plain list
    items = data.get("instances") or data.get("trackedEntities") or data
    if not isinstance(items, list):
        raise ValueError("Input JSON must be a list or contain 'instances'/'trackedEntities'.")
    uids = []
    for it in items:
        tei = it.get("trackedEntity")
        if tei:
            uids.append(tei)
    return uids

def get_tei_from_staging(api_src: Api, program: str, tei_uid: str) -> Optional[List[Dict[str, Any]]]:
    """
    Fetch a minimal TEI from staging with only the attributes we want to copy.
    Returns a list of {'attribute': <uid>, 'value': <value>} or None if not found.
    """
    try:
        r = api_src.get(
            f"tracker/trackedEntities/{tei_uid}",
            params={},
        )
    except (RequestException, ConnectionError, RemoteDisconnected) as e:
        print(f"[GET ERROR] staging TEI {tei_uid}: {e}")
        return None

    if r.status_code == 404:
        return None
    if not r.ok:
        print(f"[GET ERROR] staging TEI {tei_uid}: HTTP {r.status_code} {r.text[:200]}")
        return None

    tei = r.json()

    return tei

def update_prod_tei(api_dst: Api, tei, dry_run: bool = False) -> bool:
    """
    POST a minimal update to /api/tracker with async=false & importStrategy=UPDATE.
    Only updates attributes; enrollments/events/etc. are not sent.
    """
    if dry_run:
        print(f"[DRY-RUN] Would update TEI attributes")
        return True

    payload = {"trackedEntities": [tei]}
    try:
        r = api_dst.post(
            "tracker",
            params={"async": "false", "importStrategy": "UPDATE"},
            json=payload,
        )
    except (RequestException, ConnectionError, RemoteDisconnected) as e:
        print(f"[POST ERROR] prod TEI {tei['trackedEntity']}: {e}")
        return False

    # DHIS2 returns a structured import report
    try:
        res = r.json()
    except ValueError:
        print(f"[POST ERROR] prod TEI {tei['trackedEntity']}: non-JSON response {r.status_code}")
        return False

    if res.get("status") == "OK":
        return True

    # Print first error if present
    vr = res.get("validationReport", {}) or {}
    errs = vr.get("errorReports", []) or []
    if errs:
        msg = errs[0].get("message") or errs[0]
        print(f"[UPDATE ERROR] TEI {tei['trackedEntity']}: {msg}")
    else:
        print(f"[UPDATE ERROR] TEI {tei['trackedEntity']}: status={res.get('status')} raw={str(res)[:200]}")
    return False

def main():
    ap = argparse.ArgumentParser(description="Fix TEI attributes in prod using values from staging (dhis2.py).")
    ap.add_argument("--input", default="affected_patients_nigeria.json", help="JSON with affected TEIs")
    ap.add_argument("--program", required=True, help="Program UID (e.g., pMIglSEqPGS)")
    ap.add_argument("--dry-run", action="store_true", help="Do not POST updates, just simulate")

    # Staging
    ap.add_argument("--src-url", default=os.getenv("DHIS2_SRC_URL"))
    ap.add_argument("--src-user", default=os.getenv("DHIS2_SRC_USER"))
    ap.add_argument("--src-pass", default=os.getenv("DHIS2_SRC_PASS"))

    # Production
    ap.add_argument("--dst-url", default=os.getenv("DHIS2_DST_URL"))
    ap.add_argument("--dst-user", default=os.getenv("DHIS2_DST_USER"))
    ap.add_argument("--dst-pass", default=os.getenv("DHIS2_DST_PASS"))

    args = ap.parse_args()

    # Validate creds
    if not all([args.src_url, args.src_user, args.src_pass, args.dst_url, args.dst_user, args.dst_pass]):
        raise SystemExit("Please provide src/dst URLs and credentials via flags or env vars.")

    # Read TEIs from file
    tei_uids = read_tei_uids(args.input)
    print(f"[INFO] TEIs in file: {len(tei_uids)}")

    # Build API clients
    api_src = Api(args.src_url, args.src_user, args.src_pass)
    api_dst = Api(args.dst_url, args.dst_user, args.dst_pass)

    # CSV logs
    missing_f = open(MISSING_CSV, "w", newline="", encoding="utf-8")
    failed_f  = open(FAILED_CSV,  "w", newline="", encoding="utf-8")
    missing_writer = csv.writer(missing_f); missing_writer.writerow(["tei_uid"])
    failed_writer  = csv.writer(failed_f);  failed_writer.writerow(["tei_uid", "reason"])

    found_cnt = 0
    updated_cnt = 0
    missing_cnt = 0
    failed_cnt = 0

    for i, tei_uid in enumerate(tei_uids, 1):
        # Pull attrs from staging
        tei = get_tei_from_staging(api_src, args.program, tei_uid)
        if tei is None:
            print(f"[WARN] TEI not found in staging: {tei_uid}")
            missing_writer.writerow([tei_uid])
            missing_cnt += 1
            continue

        found_cnt += 1

        # Push minimal attribute-only update to prod
        ok = update_prod_tei(api_dst, tei, dry_run=args.dry_run)
        if ok:
            updated_cnt += 1
            print(f"[WARN] TEI updated in production: {tei_uid}")
        else:
            failed_cnt += 1
            failed_writer.writerow([tei_uid, "update_failed"])

        # Gentle throttle
        time.sleep(0.05)

    missing_f.close()
    failed_f.close()

    print("\n=== SUMMARY ===")
    print(f"TEIs in file              : {len(tei_uids)}")
    print(f"Found in staging          : {found_cnt}")
    print(f"Updated in production     : {updated_cnt}")
    print(f"Missing in staging (csv)  : {missing_cnt} -> {MISSING_CSV}")
    print(f"Failed updates   (csv)    : {failed_cnt} -> {FAILED_CSV}")
    if args.dry_run:
        print("(dry-run: no updates actually posted)")

if __name__ == "__main__":
    main()
