import math
import csv
import json
import re
import pandas as pd
from collections import defaultdict
from dhis2 import Api, generate_uid, RequestException
from logzero import logger, logfile
from datetime import datetime
import time
import sys
import os
from urllib.parse import quote
from dhis2_env import api_from_env

log_file="./move_teis.log"
logfile(log_file)

def post_chunked_tracked_entities(api, tracked_entities, chunk_size=50, max_retries=3):
    """
    Posts trackedEntities to /api/tracker in chunks using async=false.
    """
    total = len(tracked_entities)
    logger.info(f"Sending {total} trackedEntities in chunks of {chunk_size}...")

    for i in range(0, total, chunk_size):
        chunk = tracked_entities[i:i+chunk_size]
        payload = {"trackedEntities": chunk}
        retries = 0

        while retries <= max_retries:
            try:
                response = api.post(
                    "tracker",
                    params={"async": "false", "skipRuleEngine": "true"},
                    json=payload
                )
                result = response.json()

                if result.get("status") == "ERROR":
                    logger.error("Error in tracker import:")
                    for report in result.get("validationReport", {}).get("errorReports", []):
                        logger.error(f"{report.get('message')} (code: {report.get('errorCode')})")
                else:
                    logger.info(f"Chunk {i//chunk_size + 1}: successfully imported {len(chunk)} trackedEntities.")
                break  # break retry loop on success or tracked error
            except RequestException as e:
                logger.error(f"Request failed: {str(e)}. Retrying in 3s...")
                with open("payload.json", "w", encoding="utf-8") as f:
                    json.dump(payload, f, indent=4, ensure_ascii=False)
                exit(0)
                time.sleep(3)
                retries += 1

api_target = api_from_env('NIGERIA_NCD')

# Load mappings from CSVs
json_mapping = {}
with open("NHCI Redcap to DHIS2 mapping - json_fields.csv") as f:
    reader = csv.DictReader(f)
    for row in reader:
        if row['DHIS2 uid']:
            json_mapping[row['Redcap Field Name']] = {
                'type': row['DHIS2 metadata equivalent'].strip().lower(),
                'uid': row['DHIS2 uid'].strip(),
                'name': row['DHIS2 name'].strip(),
                'valueType': row.get('Value Type', '').strip()
            }

def _norm_code(s: str) -> str:
    # Normalize codes consistently (e.g., "fct/01/02" -> "FCT/01/02")
    return (s or "").strip().upper()

def load_org_unit_map(mapping_csv_path: str) -> dict:
    """
    Reads 'NHCI Redcap to DHIS2 mapping - OUs.csv' and builds:
      org_unit_map[CODE] = {"name": <Facilities in DHIS2>, "id": <OU UIDs in DHIS2>}
    where CODE is like 'FCT/01/02'
    """
    org_unit_map = {}
    with open(mapping_csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            code = _norm_code(row.get("Code"))
            dhis2_name = (row.get("Facilities in DHIS2") or "").strip()
            dhis2_ou_id = (row.get("OU UIDs in DHIS2") or "").strip()
            if code and dhis2_ou_id:
                org_unit_map[code] = {"name": dhis2_name, "id": dhis2_ou_id}
    return org_unit_map

org_unit_map = load_org_unit_map("NHCI Redcap to DHIS2 mapping - OUs.csv")


def facility_code_from_pid(pid: str) -> str:
    """
    From PID like 'FCT/01/02/0023' return 'FCT/01/02'.
    Robust to minor format issues; falls back to first 9 chars if needed.
    """
    pid = (pid or "").strip()
    if not pid:
        return ""
    parts = pid.split("/")
    if len(parts) >= 3:
        return _norm_code("/".join(parts[:3]))
    # fallback if PID isn't slash-delimited as expected
    return _norm_code(pid[:9]) if len(pid) >= 9 else ""


# Load the medication mapping
med_mapping = []
with open("NHCI Redcap to DHIS2 mapping - Meds mapping.csv") as f:
    reader = csv.DictReader(f)
    for row in reader:
        med_mapping.append({
            "drug_code": row['drug_prescribed_numeric_code'],
            "dose_code": row['dose_prescribed_numeric_code'],
            "dataElement": row['dhis2_de_uid'],
            "value": row['dhis2_code_value']
        })

prev_results_csv = "last_exec_results.csv"  # adjust if your path differs
ok_patients_prev_run = set()

if os.path.exists(prev_results_csv):
    try:
        with open(prev_results_csv, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                # Keep patient IDs with result == OK
                res = (row.get("result") or "").strip().upper()
                pid = (row.get("patient id") or "").strip()
                if res == "OK" and pid:
                    ok_patients_prev_run.add(pid)
    except Exception as e:
        print(f"Note: could not read {prev_results_csv}: {e}")

prev_results_error_csv = "last_exec_results_errors.csv"  # adjust if your path differs
error_patients_prev_run = set()

if os.path.exists(prev_results_error_csv):
    try:
        with open(prev_results_error_csv, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                # Keep patient IDs with result == OK
                res = (row.get("result") or "").strip().upper()
                pid = (row.get("patient id") or "").strip()
                if res == "ERROR" and pid:
                    error_patients_prev_run.add(pid)
    except Exception as e:
        print(f"Note: could not read {prev_results_error_csv}: {e}")

# Get CSV file name from command-line arguments
if len(sys.argv) < 2:
    logger.error("Please provide the path to the CSV file as a command-line argument.")
    sys.exit(1)

csv_file = sys.argv[1]
if not csv_file.lower().endswith(".csv") or not os.path.exists(csv_file):
    logger.error(f"Provided file '{csv_file}' is not a valid CSV or does not exist.")
    sys.exit(1)

# Load the CSV file
records = pd.read_csv(csv_file, low_memory=False, sep=";").fillna("")

with open(csv_file, encoding="utf-8", errors="replace") as f:
    header = f.readline().rstrip("\n").split(";")
cols = [c.strip() for c in header]

# records = pd.read_csv(
#     csv_file,
#     low_memory=False,
#     sep=";",
#     engine="python",
#     usecols=cols,     # keep only header columns
#     dtype=str
# ).fillna("")

# Normalize redcap_event_name values
records['redcap_event_name'] = records['redcap_event_name'].replace({
    'baseline_arm_1': 'Baseline',
    'ad_hoc_arm_1': 'Ad_Hoc'
})

# Group by patient ID
patients = defaultdict(list)
for _, row in records.iterrows():
    if row.get("id"):
        patients[row["id"].strip()].append(row)

# Helper function to convert dates

ZERO_WIDTHS = ("\u200b", "\u200c", "\u200d", "\ufeff", "\xa0")
NIL_TOKENS = {"", "nan", "none", "nil", "n/a", "nat"}  # añade más si hace falta
DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%m-%d-%Y")

def normalize_text(x):
    if x is None:
        return ""
    s = str(x)
    for z in ZERO_WIDTHS:
        s = s.replace(z, "")
    return s.strip()

def is_nan(x):
    return isinstance(x, float) and math.isnan(x)

def parse_date_ok(s):
    # intenta varios formatos y comprueba rango razonable
    for fmt in DATE_FORMATS:
        try:
            d = datetime.strptime(s, fmt).date()
            if 1900 <= d.year <= datetime.now().year and d <= datetime.now().date():
                return d
        except ValueError:
            continue
    return None

def format_date(s):
    d = parse_date_ok(s)
    return d.isoformat() if d else ""

# def format_date(value):
#     try:
#         return datetime.strptime(value.strip(), "%m/%d/%Y").strftime("%Y-%m-%d")
#     except Exception:
#         try:
#             return datetime.strptime(value.strip(), "%d/%m/%Y").strftime("%Y-%m-%d")
#         except Exception:
#             print("Date could not be formatted : " + str(value))
#             return value  # Return as-is if not parseable

MIN_LEN = 7          # min digits for a valid phone token
MAX_LEN = 15         # max digits per E.164
ZERO_WIDTHS = ["\u200b", "\u200c", "\u200d", "\ufeff", "\xa0"]
NIL_TOKENS = {"", "nan", "none", "nil", "n/a"}

# Confusion fixes: letters commonly typed instead of digits
CONFUSION_MAP = str.maketrans({
    "O": "0", "o": "0",
    "I": "1", "l": "1", "i": "1",
})

# Find raw phone-like tokens (keeps '+' if present)
TOKEN_RE = re.compile(r"\+?\d+")

def normalize_text(s: str) -> str:
    s = s.strip()
    for z in ZERO_WIDTHS:
        s = s.replace(z, "")
    return s.translate(CONFUSION_MAP)

def digits_len(token: str) -> int:
    return sum(ch.isdigit() for ch in token)

def clean_token(token: str) -> str:
    # Keep only digits and '+'
    token = re.sub(r"[^0-9+]", "", token)

    # Collapse multiple '+' and keep it only at the front
    token = re.sub(r"\++", "+", token)
    if "+" in token and not token.startswith("+"):
        token = "+" + token.replace("+", "")
    elif token.startswith("+"):
        token = "+" + token[1:].replace("+", "")
    else:
        token = token.replace("+", "")

    # Convert leading 00 to '+'
    if token.startswith("00"):
        token = "+" + token[2:]

    return token

def extract_tokens(s: str):
    # Find separate number runs so we don’t merge multiple numbers
    raw = TOKEN_RE.findall(s)
    # Clean and length-filter
    out = []
    for t in raw:
        t_clean = clean_token(t)
        dlen = digits_len(t_clean)
        if MIN_LEN <= dlen <= MAX_LEN:
            out.append(t_clean)
    return out

def clean_phone(value, multi: str = "first"):
    """
    multi: 'first' (default) | 'longest' | 'all'
      - 'first': return the first plausible number
      - 'longest': return the longest plausible number
      - 'all': join all plausible numbers with ' | '
    """
    if pd.isna(value):
        return ""
    s = str(value)
    if s.lower().strip() in NIL_TOKENS:
        return ""

    s = normalize_text(s)

    # Special-case: if the whole string is just symbols and letters, this yields []
    tokens = extract_tokens(s)

    if not tokens:
        return ""

    if multi == "first":
        return tokens[0]
    elif multi == "longest":
        return max(tokens, key=lambda x: digits_len(x))
    elif multi == "all":
        # If you prefer commas, change the separator
        return " | ".join(tokens)
    else:
        return tokens[0]

def is_empty(value) -> bool:
    if value is None:
        return True
    # Soporta NaN de pandas/numpy si llegó como float
    if isinstance(value, float) and math.isnan(value):
        return True
    s = str(value).strip()
    return s == "" or s.lower() in {"nan", "none", "nil", "n/a"}

def _to_int(v):
    return int(float(str(v).strip()))

def append_bp_only_with_fallback(form, dataValues, json_mapping):
    """
    Adds systolic & diastolic to dataValues using *2* DEs, with value fallback from *1*.
    Returns True if BOTH BP values were appended; False if event should be skipped.
    """
    # ---- SYSTOLIC ----
    sys_v = form.get("systolic2")
    if is_empty(sys_v) or sys_v < 60 or sys_v > 260 :
        sys_v = form.get("systolic1")
    if is_empty(sys_v):
        pid = (form.get("patient_id_1") or form.get("id") or "").strip()
        # Missing systolic entirely -> skip event
        print(f"[SKIP] Missing systolic (2 & 1) for patient {pid}", file=sys.stderr)
        return False
    try:
        sys_int = _to_int(sys_v)
    except Exception as e:
        pid = (form.get("patient_id_1") or form.get("id") or "").strip()
        print(f"[SKIP] Non-numeric systolic for patient {pid}: {sys_v!r}", file=sys.stderr)
        return False

    # ---- DIASTOLIC ----
    dia_v = form.get("diastolic2")
    if is_empty(dia_v) or dia_v < 40 or dia_v > 260:
        dia_v = form.get("diastolic1")
    if is_empty(dia_v):
        pid = (form.get("patient_id_1") or form.get("id") or "").strip()
        # Missing diastolic entirely -> skip event
        print(f"[SKIP] Missing diastolic (2 & 1) for patient {pid}", file=sys.stderr)
        return False
    try:
        dia_int = _to_int(dia_v)
    except Exception as e:
        pid = (form.get("patient_id_1") or form.get("id") or "").strip()
        print(f"[SKIP] Non-numeric diastolic for patient {pid}: {dia_v!r}", file=sys.stderr)
        return False

    # Map to *2* DEs (even if value came from *1*)
    if "systolic2" in json_mapping:
        dataValues.append({
            "dataElement": json_mapping["systolic2"]["uid"],
            "value": sys_int,
        })
    if "diastolic2" in json_mapping:
        dataValues.append({
            "dataElement": json_mapping["diastolic2"]["uid"],
            "value": dia_int,
        })
    return True


PID_COL_RE = re.compile(r"^patient_id_(\d+)$")

def find_patient_id(form: dict) -> str:
    """
    Return the first non-empty value among patient_id_1..N (in numeric order),
    else fall back to common alternatives, else ''.
    """
    numbered = []
    for k in form.keys():
        m = PID_COL_RE.match(k)
        if m:
            try:
                numbered.append((int(m.group(1)), k))
            except ValueError:
                continue
    for _, k in sorted(numbered):
        v = (form.get(k) or "").strip()
        if v:
            return v

    # sensible fallbacks
    for alt in ("patient_id", "patientid", "id", "patient_number"):
        v = (form.get(alt) or "").strip()
        if v:
            return v
    return ""


def missing_mandatory_baseline_fields(form: dict) -> list[str]:
    """
    Return list of *missing* mandatory fields for Baseline: date_of_birth, sex, first_name, last_name.
    """
    missing = []
    if is_empty(form.get("date_of_birth")):
        missing.append("date_of_birth")
    if is_empty(form.get("sex")):
        missing.append("sex")
    if is_empty(form.get("first_name")):
        missing.append("first_name")
    if is_empty(form.get("last_name")):
        missing.append("last_name")
    return missing


import requests  # at top if not already imported


def set_org_unit_everywhere(tei: dict, org_unit: str) -> None:
    """
    Mutates `tei` in place: sets orgUnit on the TEI itself, all enrollments,
    and all events under those enrollments.
    """
    if not tei:
        return
    tei["orgUnit"] = org_unit
    if "programOwners" in tei and len(tei["programOwners"]) > 0:
        tei["programOwners"][0]["orgUnit"] = org_unit
    else:
        tei["programOwners"] = list()
        tei["programOwners"][0] = {
            "orgUnit": org_unit,
            "trackedEntity": tei["trackedEntity"],
            "program": "pMIglSEqPGS"
        }

    for enr in (tei.get("enrollments") or []):
        enr["orgUnit"] = org_unit
        for ev in (enr.get("events") or []):
            ev["orgUnit"] = org_unit

def pick_one_and_delete_duplicates(
    instances: list,
    org_unit: str,
    patient_id: str,
    client: "DHIS2",      # your DHIS2 HTTP wrapper
    logger=None
):
    """
    From a list of TEI instances returned by /tracker/trackedEntities,
    keep exactly ONE (the one whose orgUnit == org_unit), delete the rest.

    Returns the kept TEI dict, or None if no reliable match.
    """
    if not instances:
        msg = f"No instances for patient_id={patient_id} OU={org_unit}"
        try:
            (logger or print)(msg)
        except Exception:
            print(msg)
        return None

    if len(instances) == 1:
        tei = instances[0]
        # We’re moving this TEI to a new facility → force orgUnit everywhere
        set_org_unit_everywhere(tei, org_unit)
        return tei

    # Pick the first instance matching the desired orgUnit
    keep = None
    for tei in instances:
        if tei.get("orgUnit") == org_unit:
            keep = tei
            break

    if keep is None:
        msg = (f"Multiple instances found, but none match orgUnit={org_unit} "
               f"for patient_id={patient_id}; cannot decide which to keep.")
        try:
            (logger.warning if logger else print)(msg)
        except Exception:
            print(msg)
        return None

    # Delete all others (duplicates), leaving only the one we keep
    to_delete = [t for t in instances if t is not keep]
    if to_delete:
        del_payload = {
            "trackedEntities": [
                # Minimal object is fine for DELETE: just the UID
                {"trackedEntity": t.get("trackedEntity")}
                for t in to_delete
                if t.get("trackedEntity")
            ]
        }
        try:
            del_resp = client.post(
                "tracker",
                params={"async": "false", "importStrategy": "DELETE"},
                json=del_payload,
            )
            ok = False
            try:
                jr = del_resp.json()
                ok = (del_resp.status_code == 200 and (jr.get("status") in (None, "OK")))
            except Exception:
                ok = (del_resp.status_code == 200)

            deleted_uids = [d["trackedEntity"] for d in del_payload["trackedEntities"]]
            if ok:
                print(f"Deleted duplicate TEI(s) for patient_id={patient_id}: {deleted_uids}")
            else:
                print(f"WARNING: Failed to delete duplicates for patient_id={patient_id} "
                      f"(HTTP {del_resp.status_code}) -> {del_resp.text[:300]}")
        except requests.RequestException as e:
            print(f"WARNING: Exception deleting duplicates for patient_id={patient_id}: {e}")

    return keep



results_rows = []  # rows for CSV: uid, patient id, result, notes
payloads = []

pids_created = []
for pid, forms in patients.items():
    tracked_entity_id = generate_uid()
    enrollment_id = generate_uid()

    drop_patient = False
    drop_reason = ""
    enrollment_date = None
    org_unit_id = None
    org_unit_name = None
    attributes = []
    event_by_date = {}
    height = None  # may appear in Baseline and used later
    skip_id = None

    for form in forms:
        cur_id = (form.get("id") or "").strip()

        # keep skipping id until it changes
        if skip_id is not None:
            if cur_id == skip_id:
                continue  # skip patient row
            else:
                skip_id = None  # id changed, stop skipping

        # If row = baseseline + DoB is empty -> skip this patient
        # If row = Baseline and any mandatory is missing -> DROP THIS PATIENT
        if form.get("redcap_event_name") == "Baseline":
            # --- Skip patients that were already OK in the previous execution ---
            pid = find_patient_id(form)
            if pid and pid in ok_patients_prev_run:
                try:
                    skip_id = cur_id  # ensure subsequent rows for this patient are skipped too
                except NameError:
                    pass
                # Optional: print/debug
                print(f"Skipping patient already OK last run: {pid}")
                continue
            # --- Only do patients with ERROR
            # pid = find_patient_id(form)
            # if pid and pid not in error_patients_prev_run:
            #     try:
            #         skip_id = cur_id  # ensure subsequent rows for this patient are skipped too
            #     except NameError:
            #         pass
            #     # Optional: print/debug
            #     print(f"Skipping patient with no error in last run: {pid}")
            #     continue
            # -------------------------------------------------------------------
            missing = missing_mandatory_baseline_fields(form)
            if missing:
                pid_for_log = find_patient_id(form)
                drop_patient = True
                drop_reason = f"mandatory attribute(s) missing: {', '.join(missing)}"
                # log to CSV (uid empty because no TEI is created)
                results_rows.append({
                    "uid": "",
                    "patient id": pid_for_log,
                    "result": "ERROR",
                    "notes": drop_reason
                })
                # skip this patient's further rows
                skip_id = cur_id
                continue

        redcap_event = form.get("redcap_event_name")

        if redcap_event == "Baseline":

            # Detect orgUnit from the patient_id (PID) → facility code (FCT/xx/xx)
            org_unit_name = ""
            org_unit_id = ""

            pid_cur = find_patient_id(form)  # you already call this elsewhere; safe to reuse here
            fac_code = facility_code_from_pid(pid_cur)

            if fac_code and fac_code in org_unit_map:
                org_unit_name = org_unit_map[fac_code]["name"]
                org_unit_id = org_unit_map[fac_code]["id"]
            else:
                # keep empty so downstream guard prevents adding this TEI to payloads
                try:
                    logger.warning(f"No orgUnit match found from PID '{pid_cur}' → code '{fac_code}'")
                except NameError:
                    print(f"WARNING: No orgUnit match found from PID '{pid_cur}' → code '{fac_code}'")

            if form.get("registration_date"):
                enrollment_date = format_date(form["registration_date"])

            for k, v in form.items():
                if not v:
                    continue
                if k == "htn_timing":
                    if v == "0":
                        v = "true"
                    else:
                        v = ""
                if k == "heart_failure":
                    if v == "1":
                        v = "true"
                    else:
                        v = ""
                if k == "sex":
                    if v == 1:
                        v = 'FEMALE'
                    else:
                        v = 'MALE'
                if k == "date_of_birth":
                    s = normalize_text(v)
                    # Process nulls/NaN/“empty” and invalid dates
                    if s.lower() in NIL_TOKENS or is_nan(v) or not parse_date_ok(s):
                        print("No DoB:" + form.get("id", ""))
                        v = ""
                    else:
                        v = format_date(s)
                if k == "first_name" or k == "last_name":
                    continue  # handled below
                if k == "match" and v != "Yes":
                    continue
                if k == "phone_num" or k == "nextkin_num":
                    # Make sure the value is correct
                    v = clean_phone(v)

                if k in json_mapping:
                    if v == "":
                        continue
                    if k == "height":
                        height = v
                        continue
                    if k == "patients_complete":
                        continue
                    entry = json_mapping[k]
                    if entry['type'] == "tracked entity attribute" and entry.get('valueType') == "[Yes,No]":
                        if isinstance(v, str):
                            v = v.upper()
                        else:
                            v = "YES" if v == 1 else "NO"
                    attributes.append({
                        "attribute": entry['uid'],
                        "displayName": entry['name'],
                        "value": v.upper() if isinstance(v, str) else v
                    })

            # Add merged name
            full_name = f"{form.get('first_name', '')} {form.get('last_name', '')}".strip()
            if full_name:
                name_attr = json_mapping.get("first_name") or json_mapping.get("last_name")
                attributes.append({
                    "attribute": name_attr['uid'],
                    "displayName": name_attr['name'],
                    "value": full_name
                })

            # Always add NCD Patient Status = ACTIVE
            attributes.append({
                "attribute": "fI1P3Mg1zOZ",
                "displayName": "HTN - NCD Patient Status",
                "value": "ACTIVE"
            })

            # Add mandatory TEAs
            consent_found = False
            for attr in attributes:
                if attr['attribute'] == 'YJGACwhN0St':
                    consent_found = True
            if not consent_found:
                attributes.append({
                    "attribute": "YJGACwhN0St",
                    "displayName": "HTN - Consent to record data",
                    "valueType": "TRUE_ONLY",
                    "value": "true"
                })
            address_found = False
            for attr in attributes:
                if attr['attribute'] == 'A6Hb0Kvg4vb':
                    address_found = True
            if not address_found:
                attributes.append({
                    "attribute": "A6Hb0Kvg4vb",
                    "displayName": "Address (current)",
                    "valueType": "TEXT",
                    "value": "Unknown"
                })
            patient_has_hypertension_found = False
            for attr in attributes:
                if attr['attribute'] == 'jCRIT4GMMOS':
                    patient_has_hypertension_found = True
            if not patient_has_hypertension_found:
                attributes.append({
                    "attribute": "jCRIT4GMMOS",
                    "displayName": "HTN - Does patient have hypertension?",
                    "valueType": "TEXT",
                    "value": "YES"
                })

        elif redcap_event == "Ad_Hoc" and form.get("redcap_repeat_instrument") == "blood_pressure":
            visit_date = format_date(form.get("visit_date"))
            if not visit_date:
                continue
            scheduled = format_date(form.get("next_visit_date"))
            dataValues = []
            ok_bp = append_bp_only_with_fallback(form, dataValues, json_mapping)
            if not ok_bp:
                continue  # <-- skip this event
            if k == "weight":
                if "weight" in json_mapping:
                    w = form.get("weight")
                    if not is_empty(w):
                        try:
                            dataValues.append({
                                "dataElement": json_mapping["weight"]["uid"],
                                "value": _to_int(w),
                            })
                        except Exception as e:
                            pid = (form.get("patient_id_1") or form.get("id") or "").strip()
                            raise ValueError(f"Non-numeric weight for patient {pid}: {w!r}") from e
            if height and "height" in json_mapping:
                if not is_empty(height):
                    try:
                        dataValues.append({
                            "dataElement": json_mapping["height"]["uid"],
                            "value": _to_int(height),
                        })
                    except Exception as e:
                        pid = (form.get("patient_id_1") or form.get("id") or "").strip()
                        raise ValueError(f"Non-numeric height for patient {pid}: {height!r}") from e
            event_by_date[visit_date] = {
                "event": generate_uid(),
                "status": "COMPLETED",
                "program": "pMIglSEqPGS",
                "programStage": "anb2cjLx3WM",
                "enrollment": enrollment_id,
                "trackedEntity": tracked_entity_id,
                "orgUnit": org_unit_id,
                "orgUnitName": org_unit_name,
                "occurredAt": visit_date,
                "scheduledAt": scheduled,
                "dataValues": dataValues,
            }

        elif redcap_event == "Ad_Hoc" and form.get("redcap_repeat_instrument") == "hypertensive_medications_prescribed":
            date_prescribed = format_date(form.get("date_prescribed"))
            if not date_prescribed:
                continue
            if date_prescribed not in event_by_date:
                logger.error(f"Meds prescribed date {date_prescribed} has no matching visit date for patient {pid}")
                continue
            for i in range(1, 4):
                drug_key = f"drugs_prescribed{'' if i == 1 else '_' + str(i)}"
                dose_key = f"dose_prescribed{'' if i == 1 else '_' + str(i)}"
                drug_val = str(form.get(drug_key, "")).strip()
                dose_val = str(form.get(dose_key, "")).strip()
                if not drug_val or not dose_val:
                    continue
                else:
                    drug_val = drug_val[0]
                    dose_val = dose_val[0]
                match = next((m for m in med_mapping if m['drug_code'] == drug_val and m['dose_code'] == dose_val), None)
                if match:
                    event_by_date[date_prescribed]['dataValues'].append({
                        "dataElement": match['dataElement'],
                        "value": match['value']
                    })

    # Build payload only if not dropped and we have an orgUnit
    if (not drop_patient) and org_unit_id not in ("", None):
        events = list(event_by_date.values())

        payload = {
            "trackedEntity": tracked_entity_id,
            "trackedEntityType": "MCPQUTHX1Ze",
            "orgUnit": org_unit_id,
            "attributes": attributes,
            "enrollments": [
                {
                    "enrollment": enrollment_id,
                    "trackedEntity": tracked_entity_id,
                    "program": "pMIglSEqPGS",
                    "status": "ACTIVE",
                    "orgUnit": org_unit_id,
                    "orgUnitName": org_unit_name,
                    "enrolledAt": enrollment_date or "2020-01-01",
                    "occurredAt": enrollment_date or "2020-01-01",
                    "followUp": False,
                    "deleted": False,
                    "events": events
                }
            ]
        }
        payloads.append(payload)

# Output: payloads is a list of JSON objects ready for POST to DHIS2 tracker endpoint
# with open("payloads_output.json", "w") as out_file:
#     json.dump(payloads, out_file, indent=2)
# post_chunked_tracked_entities(api_target, payloads)

# --------------------------------------------
# REPLACE THE OLD "SEND" SECTION WITH THIS
# --------------------------------------------


PROGRAM_UID = "pMIglSEqPGS"
ATTR_PATIENT_ID = "RAViWaXdOM8"  # "ID Number (Optional)"

def _get_attr_value(te_payload, attr_uid):
    for a in te_payload.get("attributes", []):
        if a.get("attribute") == attr_uid:
            return a.get("value", "")
    return ""

def _get_first_enrollment_for_program(server_te, program_uid):
    for enr in server_te.get("enrollments", []) or []:
        if enr.get("program") == program_uid:
            return enr
    return None

def _events_key(ev):
    # Build matching key identical for client+server
    # We treat missing scheduledAt as empty string to keep key consistent
    return (
        (ev.get("occurredAt") or "").strip(),
        (ev.get("scheduledAt") or "").strip(),
    )

def fetch_server_te_by_patient_id(api, patient_id, org_unit):
    """
    Fetch exactly ONE TEI by attribute + orgUnit (+ program).
    Returns that TEI dict (with enrollments+events) or None if 0 or >1 found.
    """
    params = {
        "program": PROGRAM_UID,
        "orgUnit": "uS2CuRbDyD7",
        "ouMode": "DESCENDANTS",
        "attribute": f"{ATTR_PATIENT_ID}:eq:{patient_id}",
        # Ask for enrollments + events so we can match and reuse event UIDs
        "fields": "trackedEntity,orgUnit,enrollments[enrollment,program,orgUnit,events[event,occurredAt,scheduledAt]],programOwners",
        # Optional: limit to be safe (we’ll still validate the count)
        "pageSize": 2,
    }
    try:
        r = api.get("tracker/trackedEntities", params=params)
        r.raise_for_status()
        data = r.json() or {}
        instances = data.get("instances") or []  # <-- DHIS2 returns 'instances' here

        selected = pick_one_and_delete_duplicates(
            instances=instances,
            org_unit=org_unit,
            patient_id=patient_id,
            client=api,  # or whatever DHIS2 client you use for the target
            logger=logger if 'logger' in globals() else None
        )
        return selected

    except Exception as e:
        try:
            logger.error(f"Lookup failed for patient_id={patient_id} OU={org_unit}: {e}")
        except NameError:
            print(f"Lookup failed for patient_id={patient_id} OU={org_unit}: {e}")
        return None

DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")

def _norm_date_str(dt_str: str) -> str:
    """
    Normalize any DHIS2-like timestamp to 'YYYY-MM-DD'.
    Examples:
      '2020-02-21' -> '2020-02-21'
      '2020-02-21T00:00:00.000' -> '2020-02-21'
      '2020-02-21T00:00:00.000Z' -> '2020-02-21'
      ''/None -> ''
    """
    if not dt_str:
        return ""
    m = DATE_RE.search(str(dt_str))
    return m.group(1) if m else str(dt_str).strip()

def _events_key(ev: dict) -> tuple[str, str]:
    """
    Key used for matching client/server events:
    (normalized_occurredAt, normalized_scheduledAt)
    """
    return (
        _norm_date_str(ev.get("occurredAt")),
        _norm_date_str(ev.get("scheduledAt")),
    )

def merge_event_uids_from_server(client_te: dict, server_te: dict):
    """
    If server has events:
      - create an index keyed by (occurredAt, scheduledAt) using DATE-ONLY normalization
      - for each client event, if a normalized key match exists, reuse the server event UID
      - OPTIONAL: fallback to unique occurredAt-only match (normalized) if needed
    """
    server_enr = _get_first_enrollment_for_program(server_te, PROGRAM_UID)
    if not server_enr:
        return  # no enrollment for program on server -> nothing to map

    server_events = server_enr.get("events", []) or []

    # Primary index: (occurredAt_date, scheduledAt_date) -> event
    server_index = { _events_key(sev): sev for sev in server_events }

    # Optional fallback: occurredAt_date -> [events]
    by_occurred = defaultdict(list)
    for sev in server_events:
        by_occurred[_norm_date_str(sev.get("occurredAt"))].append(sev)

    client_enr = client_te.get("enrollments", [])[0]
    for ev in client_enr.get("events", []):
        k = _events_key(ev)
        match = server_index.get(k)

        if match:
            ev["event"] = match["event"]
            continue

        # ---- OPTIONAL FALLBACK (safe): try unique occurredAt-only match
        # If you want to enforce strict pair matching only, comment this block out.
        occ = _norm_date_str(ev.get("occurredAt"))
        candidates = by_occurred.get(occ, [])
        if len(candidates) == 1:
            ev["event"] = candidates[0]["event"]
            # Optionally log that fallback was used
            try:
                logger.debug(
                    f"Fallback occurredAt-only match used for TEI={client_te.get('trackedEntity')} "
                    f"date={occ} -> event={candidates[0]['event']}"
                )
            except NameError:
                pass
        else:
            # No match or ambiguous; leave event UID as-is so it will be created
            try:
                logger.debug(
                    f"No normalized match for TEI={client_te.get('trackedEntity')} key={k}. "
                    f"{'Ambiguous occurredAt fallback' if len(candidates) > 1 else 'No candidates'}."
                )
            except NameError:
                pass

def reuse_server_uids(client_te, server_te):
    """
    Overwrite client trackedEntity and enrollment uids with server ones.
    Also propagate TEI+enrollment UID into all client events to avoid dangling refs.
    """
    client_enr = client_te.get("enrollments", [])[0]

    te_uid = server_te["trackedEntity"]
    client_te["trackedEntity"] = te_uid

    server_enr = _get_first_enrollment_for_program(server_te, PROGRAM_UID)
    if server_enr is not None:
        enr_uid = server_enr["enrollment"]
        # reuse enrollment uid
        client_enr["enrollment"] = enr_uid
    # else: if server has no enrollment for this program, we keep our generated enrollment id

    # Ensure internal references are consistent
    for ev in client_enr.get("events", []):
        ev["trackedEntity"] = client_te["trackedEntity"]
        ev["enrollment"] = client_enr["enrollment"]

def summarize_ok_notes(result_json):
    """Build a compact stats string for notes column."""
    stats = result_json.get("stats", {})
    parts = []
    for k in ("created", "updated", "deleted", "ignored", "total"):
        if k in stats:
            parts.append(f"{k}={stats.get(k)}")
    # If available, pull event sub-stats for clarity
    tr = (result_json.get("bundleReport", {}) or {}).get("typeReportMap", {}) or {}
    if "EVENT" in tr and "stats" in tr["EVENT"]:
        evs = tr["EVENT"]["stats"]
        parts.append(f"EVENTS(c={evs.get('created',0)},u={evs.get('updated',0)},i={evs.get('ignored',0)})")
    return "; ".join(parts) if parts else "OK"

def summarize_error_notes(result_json):
    # validationReport.errorReports[] may contain messages; join the first few
    errs = (result_json.get("validationReport", {}) or {}).get("errorReports", []) or []
    if errs:
        msgs = []
        for e in errs[:5]:
            m = e.get("message") or e.get("errorCode") or "validation error"
            # Include offending UID if present
            uid = e.get("uid")
            if uid:
                m = f"{m} (uid={uid})"
            msgs.append(m)
        return " | ".join(msgs)
    # fallback: whole status/message if available
    return result_json.get("status") or "ERROR"

# Persist what we SEND for debugging
with open("payloads_output.json", "w", encoding="utf-8") as out_file:
    json.dump(payloads, out_file, indent=2, ensure_ascii=False)

total = len(payloads)
logger.info(f"Preparing CREATE_AND_UPDATE for {total} trackedEntities (one-by-one).")

for idx, te in enumerate(payloads, start=1):
    # ---- Extract keys we need from outgoing payload
    te_uid_out = te.get("trackedEntity")
    ou = te.get("orgUnit")
    patient_id = _get_attr_value(te, ATTR_PATIENT_ID) or ""

    # ---- Lookup server TEI by patient id + OU + program
    server_te = fetch_server_te_by_patient_id(api_target, patient_id, ou)

    if server_te is None:
        logger.error("TEI not found!!")

    # If a server TEI exists, reuse UIDs and merge event IDs as needed
    if server_te:
        # Always reuse server TE + enrollment UIDs
        reuse_server_uids(te, server_te)

        # If server already has events: map matches by (occurredAt, scheduledAt)
        server_enr = _get_first_enrollment_for_program(server_te, PROGRAM_UID)
        server_events = (server_enr.get("events", []) if server_enr else []) or []
        if server_events:
            merge_event_uids_from_server(te, server_te)
        # If no events on server: nothing else to do; your events will be created under server UIDs



    # ---- Send this single TEI with CREATE_AND_UPDATE
    one_payload = {"trackedEntities": [te]}
    try:
        resp = api_target.post(
            "tracker",
            params={
                "async": "false",
                "importStrategy": "CREATE_AND_UPDATE",
                "skipRuleEngine": "false",
            },
            json=one_payload,
        )
        status_ok = (resp.status_code == 200)
        result_json = {}
        try:
            result_json = resp.json()
        except Exception:
            pass

        if status_ok and result_json.get("status") in (None, "OK"):  # DHIS2 returns "OK" on success
            result = "OK"
            notes = summarize_ok_notes(result_json)
        else:
            result = "ERROR"
            notes = summarize_error_notes(result_json) if result_json else f"HTTP {resp.status_code}"

    except RequestException as e:
        result = "ERROR"
        notes = f"RequestException: {str(e)}"

    # Track CSV row (uid is the TEI UID we ended up using in the payload)
    final_uid = te.get("trackedEntity")
    results_rows.append({
        "uid": final_uid,
        "patient id": patient_id,
        "result": result,
        "notes": notes
    })

    # Progress message
    print(f"Updated patient {idx} of {total} (tei={final_uid}): {result} - {notes}")

# ---- Write CSV summary
csv_out = "update_results.csv"
with open(csv_out, "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=["uid", "patient id", "result", "notes"])
    writer.writeheader()
    writer.writerows(results_rows)

print(f"Summary written to {csv_out}")

