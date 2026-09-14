#!/usr/bin/env python3
import csv
import time
import random
import argparse
import requests
from http.client import RemoteDisconnected
from urllib3.exceptions import ProtocolError
from requests.exceptions import ConnectionError, ReadTimeout, ChunkedEncodingError

def req_with_retry(session, url, params, payload, timeout=60, max_attempts=8, backoff_cap=300, base_backoff=10):
    attempt = 0
    while True:
        attempt += 1
        try:
            r = session.post(url, params=params, json=payload, timeout=timeout)
        except (ConnectionError, ReadTimeout, ProtocolError, RemoteDisconnected, ChunkedEncodingError) as e:
            if attempt >= max_attempts:
                raise
            sleep_s = min(backoff_cap, base_backoff * (2 ** (attempt - 1)))
            jitter = random.uniform(0, sleep_s * 0.1)
            print(f"[POST] retry {attempt}/{max_attempts} after {type(e).__name__}; sleeping {int(sleep_s+jitter)}s…")
            time.sleep(sleep_s + jitter)
            continue

        if r.status_code in (429, 502, 503, 504):
            if attempt >= max_attempts:
                return r
            retry_after = r.headers.get("Retry-After")
            if retry_after:
                try:
                    wait = int(retry_after)
                except ValueError:
                    wait = backoff_cap
            else:
                wait = min(backoff_cap, base_backoff * (2 ** (attempt - 1)))
            jitter = random.uniform(0, wait * 0.1)
            print(f"[POST] HTTP {r.status_code}; retry {attempt}/{max_attempts}; sleeping {int(wait+jitter)}s…")
            time.sleep(wait + jitter)
            continue
        return r

def main():
    ap = argparse.ArgumentParser(description="Delete TEIs listed in CSV using DHIS2 Tracker DELETE import strategy.")
    ap.add_argument("--base-url", required=True, help="e.g. https://your.dhis/server/api (or https://your.dhis/server; /api is added if missing)")
    ap.add_argument("--user", required=True)
    ap.add_argument("--pass", dest="password", required=True)
    ap.add_argument("--csv", default="teis_to_delete.csv", help="CSV with column 'tei'")
    ap.add_argument("--verify", action="store_true", help="Verify TLS certificates (default false).")
    args = ap.parse_args()

    base = args.base_url.rstrip("/")
    if not base.endswith("/api"):
        base = base + "/api"

    s = requests.Session()
    s.auth = (args.user, args.password)
    s.headers.update({"Accept": "application/json"})
    s.verify = args.verify

    endpoint = f"{base}/tracker"
    params = {"async": "false", "importStrategy": "DELETE"}

    # Read UIDs
    uids = []
    with open(args.csv, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            tei = (row.get("tei") or "").strip()
            if tei:
                uids.append(tei)

    if not uids:
        print("No TEIs found in CSV; nothing to delete.")
        return

    print(f"Deleting {len(uids)} TEI(s)…")
    for i, tei in enumerate(uids, start=1):
        payload = {"trackedEntities": [{"trackedEntity": tei}]}
        r = req_with_retry(s, endpoint, params, payload)
        ok = False
        try:
            j = r.json()
            ok = (r.status_code == 200 and (j.get("status") in (None, "OK")))
        except Exception:
            ok = (r.status_code == 200)

        if ok:
            print(f"[{i}/{len(uids)}] Deleted TEI {tei}")
        else:
            print(f"[{i}/{len(uids)}] ERROR deleting TEI {tei}: HTTP {r.status_code} {r.text[:200]}")

if __name__ == "__main__":
    main()
