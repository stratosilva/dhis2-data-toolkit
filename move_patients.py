from dhis2 import Api, RequestException as DHIS2RequestException, setup_logger
import json
import random
from faker import Faker
from datetime import date, datetime, timedelta
import re
from logzero import logger, logfile
import time
import argparse
import os
import sys
from requests.exceptions import RequestException as RequestsRequestException, ConnectionError
from http.client import RemoteDisconnected
from dhis2_env import api_from_env

RESUME_FILE = 'resume_uid.txt'
FAILED_TEIS_FILE = 'failed_teis.jsonl'

# Credentials come from .env (git-ignored). See .env.example.
SOURCE_ENV_PREFIX = 'NIGERIA_NCD'
TARGET_ENV_PREFIX = 'HTN_DM_DEMO'

def save_last_uid(uid):
    with open(RESUME_FILE, 'w') as f:
        f.write(uid)

def load_resume_uid():
    if os.path.exists(RESUME_FILE):
        with open(RESUME_FILE, 'r') as f:
            return f.read().strip()
    return None

def delete_resume_file():
    if os.path.exists(RESUME_FILE):
        os.remove(RESUME_FILE)

def load_api(prefix, label):
    """Build an Api from the .env credentials for a server prefix."""
    try:
        return api_from_env(prefix)
    except (FileNotFoundError, KeyError) as e:
        logger.error(f"ERROR: cannot load {label} credentials: {e}")
        sys.exit(2)

def record_failed_teis(chunk, reason):
    """
    Append the trackedEntity UIDs of a chunk the target refused, so a failed
    import leaves a machine-readable trail instead of only a log line.
    """
    try:
        with open(FAILED_TEIS_FILE, 'a') as f:
            for tei in chunk:
                f.write(json.dumps({
                    "trackedEntity": tei.get("trackedEntity"),
                    "orgUnit": tei.get("orgUnit"),
                    "reason": reason,
                    "recordedAt": datetime.now().isoformat(timespec='seconds'),
                }) + "\n")
    except OSError as e:
        logger.error(f"Could not write {FAILED_TEIS_FILE}: {e}")

parser = argparse.ArgumentParser(description='Fetch tracker events from DHIS2')
parser.add_argument('--after', type=str, required=True, help='Date to filter occurredAfter (format: YYYY-MM-DD)')
parser.add_argument('--resume', action='store_true', help='Resume from last processed org unit')
args = parser.parse_args()

log_file="./move_teis.log"
logfile(log_file)

# Initialize Faker
fake = Faker()

def post_chunked_tracked_entities(api, tracked_entities, chunk_size=50, max_retries=3):
    """
    Posts trackedEntities to /api/tracker in chunks using async=false.
    """
    total = len(tracked_entities)
    logger.info(f"Sending {total} trackedEntities in chunks of {chunk_size}...")

    for i in range(0, total, chunk_size):
        chunk = tracked_entities[i:i+chunk_size]
        remove_offending_data_values_from_payload(chunk)
        payload = {"trackedEntities": chunk}
        retries = 0

        while retries < max_retries:
            try:
                response = api.post(
                    "tracker",
                    params={"async": "false", "skipRuleEngine": "true"},
                    json=payload
                )
                result = response.json()

                if result.get("status") == "ERROR":
                    logger.error("Error in tracker import:")
                    messages = []
                    for report in result.get("validationReport", {}).get("errorReports", []):
                        logger.error(f"{report.get('message')} (code: {report.get('errorCode')})")
                        messages.append(f"{report.get('errorCode')}: {report.get('message')}")

                    if len(chunk) > 1:
                        # Do not lose the whole chunk because of a few bad records:
                        # re-send one by one so the valid ones still land and the
                        # failures are attributed to individual trackedEntities.
                        logger.warning(
                            f"Chunk {i // chunk_size + 1} reported import errors; "
                            "retrying its tracked entities individually."
                        )
                        post_chunked_tracked_entities(api, chunk, chunk_size=1, max_retries=max_retries)
                    else:
                        record_failed_teis(chunk, "; ".join(messages) or "import status ERROR")
                else:
                    logger.info(f"Chunk {i//chunk_size + 1}: successfully imported {len(chunk)} trackedEntities.")
                break  # break retry loop on success or tracked error
            except (DHIS2RequestException, RequestsRequestException, ConnectionError, RemoteDisconnected) as e:
                status_code = getattr(e, "code", None)
                if status_code is not None and 400 <= status_code < 500:
                    if status_code == 409 and len(chunk) > 1:
                        logger.warning(
                            f"Chunk {i // chunk_size + 1} has validation conflicts; "
                            "retrying its tracked entities individually."
                        )
                        post_chunked_tracked_entities(api, chunk, chunk_size=1, max_retries=max_retries)
                        break
                    logger.error(
                        f"Chunk {i // chunk_size + 1} was rejected by the target "
                        f"(HTTP {status_code}); not retrying a validation error."
                    )
                    record_failed_teis(chunk, f"HTTP {status_code}: {e}")
                    break
                logger.error(f"Request failed: {str(e)}. Retrying in 3s...")
                time.sleep(3)
                retries += 1
        else:
            logger.error(f"Failed to import chunk {i // chunk_size + 1} after {max_retries} attempts.")
            record_failed_teis(chunk, f"network failure after {max_retries} attempts")


# Also all these OUs have more than 50 enrollments after 1st Jan 2023
nigeria_ous_with_data = [
    "S84LuOwGZ6R",
    "QLanNpwrwqb",
    "Gciekg99hVj",
    "YsZ3eAa7HU1",
    "WqebnzGgb71",
    "b8zi5ST4dH3",
    "PFmp8iIeWRb",
    "ACgBmF2uh5h",
    "XuQ3oT7hwnc",
    "UhwUiVzHWsA",
    "rBGMr2mML23",
    "bYamdI7FHE5",
    "VzyOk9CLz5X",
    "DwV5TmasIOB",
    "uRzXjHHigm9",
    "PAxRf5zY1As",
    "S8OFPw6S1An",
    "TUzBaQ3PYDm",
    "tweJXXNQgkA",
    "MtPVEBYLvHZ",
    "lSoOduWvZUx",
    "BaGCGdYknNN",
    "XMOewxpQvsf",
    "JJL8Ukh1ZlZ",
    "z2syYAAoWvG",
    "TYdI1qB6nNd",
    "LTsuanAvTRU",
    "lMr57aLPSoT",
    "dQE9wQrW9rS",
    "tu9DZ2g4LZH",
    "bnZRTFMPLCY",
    "o5uazdFDoBc",
    "tMmRVO0UPrc",
    "yNtPl1eGwar",
    "jpPaQTbjIlU",
    "iPHdxJ2F9ve",
    "UupnludKN9T",
    "m2DR5doEjKC",
    "OjhcqZ6O5aK",
    "DwFXaxsC09L",
    "o9IDb1gMBqV",
    "WmH9sWwbDGC",
    "BqwpWi32Gus",
    "xUORgwU22YU",
    "DxfVRX9j8np",
    "aoOPscxu7Tc",
    "tcSIgkbW2jA",
    "fLSPCrGZjF5",
    "WtOaeyRb56h",
    "lYBdFTtjv8R",
    "wgbimD4TWvq",
    "nltVWcc5JRe",
    "NLbb5g68GnM",
    "ehJsDey0nF2",
    "SSx6C5WTRT0",
    "neBNBh5lyU5",
    "spFKAo4GHXy",
    "oAXeCYwch7U",
    "PKf7VHpxmBG"
]

program_TEAs = [
    "A6Hb0Kvg4vb",
    "D40A8CAZBqz",
    "Ot616hCy9j7",
    "D9pz1vAbGPK",
    "NI0QRzJvQ0k",
    "ENRjVGxVL6l",
    "sB1IHYu2xQT",
    "oindugucx72",
    "YJGACwhN0St",
    "l5yxkxHgTw9",
    "Xij8fyJKRX7",
    "v4DnYfXn9Mu",
    "jCRIT4GMMOS",
    "oklF6MuAGaq",
    "bANIHEeQqEZ",
    "S4Pcuj5Eua7",
    "fI1P3Mg1zOZ",
    "rgeuEnAI0nj",
    "YRDy9xy9jD0",
    "sOpP7Czrt7H",
    "zi0ZYBLGEZu",
    "y52SN2FJ0p5",
    "vFaX4Whc3oq" #HTN - Is patient enrolled in health insurance?
]

program_stage_DEs = [
    "ifRI6qw96v4",
    "q362A7evMYt",
    "MZkqsWH2KSe",
    "IxEwYiq1FTq",
    "yNhtHKtKkO1",
    "eGY7e5Ttbys",
    "D8FWXAtCuL2",
    "yUYbcCXo9Mv",
    "lWmOLo5hFYQ",
    "drY9ZQ0PEEw",
    "cE3KOFmoYbZ",
    "oOHbZP28H2l",
    "JZlJfx3oihw",
    "ywITVJvILBR",
    "Xjrw5pbxBJw",
    "UXiaSOqzFts",
    "WW5l5hS2JvI",
    "Y72gWULmJGT",
    "v7nC8vSTzwh",
    "LktTflPfk1z",
    "zgxvIQneRRX",
    "n6k7wJQ1daN",
    "q362A7evMYt",
    "oYNi8TgIFTm",
    "MZkqsWH2KSe",
    "WQfxibygUCL",
    "vdh990I4zq1",
    "qj1icHDw83W",
    "wRUfpNGwBQB",
    "Ar77Gso4XEb"
]

DIABETES_ATTRIBUTE = "v4DnYfXn9Mu"  # HTN - Does patient have diabetes?

# Every data element that carries a blood-sugar measurement. A patient holding
# any of these must answer YES to DIABETES_ATTRIBUTE, whether the value was
# injected by this script or already present in the source.
DIABETES_DATA_ELEMENTS = {
    "Xjrw5pbxBJw",  # Blood sugar unit
    "cE3KOFmoYbZ",  # Blood sugar type
    "oOHbZP28H2l",  # FBS mg/dL
    "v7nC8vSTzwh",  # FBS mmol/L
    "JZlJfx3oihw",  # HbA1c
    "ywITVJvILBR",  # RBS mg/dL
    "n6k7wJQ1daN",  # RBS mmol/L
    "LktTflPfk1z",  # PPBS mg/dL
    "zgxvIQneRRX",  # PPBS mmol/L
}

# Data elements that should never be sent in event dataValues.
# XMtcYl6Y3Jp is rejected by the target server because it is not a valid DE
# and/or is not assigned to the target program stage.
OFFENDING_DATA_ELEMENTS = {
    "XMtcYl6Y3Jp",
}

# Use a set for faster and safer membership checks.
PROGRAM_STAGE_DES_SET = set(program_stage_DEs) - OFFENDING_DATA_ELEMENTS


def reindex(json_object, key):
	new_json = dict()
	for elem in json_object:
		key_value = elem[key]
		# elem.pop(key)
		new_json[key_value] = elem
	return new_json

def json_extract(obj, key):
    """Recursively fetch values from nested JSON."""
    arr = []

    def extract(obj, arr, key):
        """Recursively search for values of key in JSON tree."""
        if isinstance(obj, dict):
            for k, v in obj.items():
                if isinstance(v, (dict, list)):
                    extract(v, arr, key)
                elif k == key:
                    arr.append(v)
        elif isinstance(obj, list):
            for item in obj:
                extract(item, arr, key)
        return arr

    values = extract(obj, arr, key)
    return values

def remove_created_updated(obj):
    """
    Recursively removes 'createdBy' and 'updatedBy' keys from a nested JSON-like dict/list structure.
    """
    if isinstance(obj, dict):
        return {
            key: remove_created_updated(value)
            for key, value in obj.items()
            if key not in ("createdBy", "updatedBy")
        }
    elif isinstance(obj, list):
        return [remove_created_updated(item) for item in obj]
    else:
        return obj

# Function to check hypertension and add diabetes data
def check_hypertension_and_add_data_values(event, probability):
    systole = None
    diastole = None
    has_hypertension = False
    has_diabetes = False

    is_not_controlled = random.random() < probability

    for dv in event.get('dataValues', []):
        if dv['dataElement'] == "IxEwYiq1FTq" and 'value' in dv:
            systole = int(dv['value'])
        if dv['dataElement'] == "yNhtHKtKkO1" and 'value' in dv:
            diastole = int(dv['value'])

    if systole is not None and diastole is not None:
        diabetes_type = random.choice(["FBS", "HBA1C"])# choice(["RBS", "FBS", "PPBS", "HBA1C"])

        # Event has already diabetes measurement?
        event_has_diabetes_data = False
        for dv in event.get('dataValues', []):
            if 'Xjrw5pbxBJw' in dv['dataElement']:
                event_has_diabetes_data = True
                break

        if diabetes_type and not event_has_diabetes_data:
            data_values_to_add = []
            if diabetes_type != "HBA1C":
                blood_sugar_unit = random.choice(["MG_OR_DL", "MMOL_OR_L"])
                data_values_to_add.append({
                    "dataElement": "Xjrw5pbxBJw",
                    "value": blood_sugar_unit
                })
            else:
                blood_sugar_unit = None

            if diabetes_type == "FBS":
                if blood_sugar_unit == "MG_OR_DL":
                    blood_sugar_value = random.randint(126, 220) if is_not_controlled else random.randint(70, 125)
                    data_element_id = "oOHbZP28H2l"
                else:
                    blood_sugar_value = round(random.uniform(7, 12), 1) if is_not_controlled else round(random.uniform(3.9, 6.9), 1)
                    data_element_id = "v7nC8vSTzwh"
            elif diabetes_type == "HBA1C":
                blood_sugar_value = round(random.uniform(7, 10), 1) if is_not_controlled else round(random.uniform(5.6, 6.9), 1)
                data_element_id = "JZlJfx3oihw"
            # elif diabetes_type == "PPBS":
            #     if blood_sugar_unit == "MG_OR_DL":
            #         blood_sugar_value = random.randint(140, 250) if is_not_controlled else random.randint(100, 139)
            #         data_element_id = "LktTflPfk1z"
            #     else:
            #         blood_sugar_value = random.uniform(7.8, 13.9) if is_not_controlled else random.uniform(5.6, 7.7)
            #         data_element_id = "zgxvIQneRRX"
            # elif diabetes_type == "RBS":
            #     if blood_sugar_unit == "MG_OR_DL":
            #         blood_sugar_value = random.randint(140, 250) if is_not_controlled else random.randint(70, 139)
            #         data_element_id = "ywITVJvILBR"
            #     else:
            #         blood_sugar_value = random.uniform(7.8, 13.9) if is_not_controlled else random.uniform(3.9, 7.7)
            #         data_element_id = "n6k7wJQ1daN"

            data_values_to_add.append({
                "dataElement": "cE3KOFmoYbZ",
                "value": diabetes_type
            })
            data_values_to_add.append({
                "dataElement": data_element_id,
                "value": str(blood_sugar_value)
            })

            event['dataValues'].extend(data_values_to_add)

    return is_not_controlled

def tei_has_diabetes_data(tracked_entity):
    """True when any event of the tracked entity carries a blood-sugar value."""
    for enrollment in tracked_entity.get('enrollments', []):
        for event in enrollment.get('events', []):
            for dv in event.get('dataValues', []):
                if dv.get('dataElement') in DIABETES_DATA_ELEMENTS and dv.get('value') not in (None, ''):
                    return True
    return False


def set_diabetes_attribute(tracked_entity, value='YES'):
    """Set 'Does patient have diabetes?', overwriting any existing answer."""
    for attr in tracked_entity.get('attributes', []):
        if attr['attribute'] == DIABETES_ATTRIBUTE:
            attr['value'] = value
            return

    tracked_entity.setdefault('attributes', []).append({
        "attribute": DIABETES_ATTRIBUTE,
        "value": value,
        "displayName": "HTN - Does patient have diabetes?",
        "valueType": "TEXT"
    })


def remove_stored_by(data):
    if isinstance(data, dict):
        if 'storedBy' in data:
            del data['storedBy']
        for key, value in data.items():
            remove_stored_by(value)
    elif isinstance(data, list):
        for item in data:
            remove_stored_by(item)


def remove_offending_data_values_from_event(event):
    """
    Remove dataValues whose dataElement should not be sent to the target server.

    This function is intentionally called after all transformations/additions, so
    any stale DE that comes from the source payload or from script logic is removed
    before POSTing to /api/tracker.
    """
    original_data_values = event.get("dataValues", [])
    cleaned_data_values = []
    removed_data_elements = []

    for dv in original_data_values:
        data_element = dv.get("dataElement")

        if data_element in OFFENDING_DATA_ELEMENTS:
            removed_data_elements.append(data_element)
            continue

        if data_element not in PROGRAM_STAGE_DES_SET:
            removed_data_elements.append(data_element)
            continue

        cleaned_data_values.append(dv)

    if removed_data_elements:
        logger.warning(
            "Event %s: removed dataValues for invalid/offending DEs: %s",
            event.get("event"),
            sorted(set(removed_data_elements)),
        )

    event["dataValues"] = cleaned_data_values
    return event


def remove_offending_data_values_from_payload(obj):
    """
    Recursively sanitize a tracker payload by cleaning every event.dataValues list.
    Useful as a final safety net immediately before sending a chunk to DHIS2.
    """
    if isinstance(obj, dict):
        if "dataValues" in obj and isinstance(obj.get("dataValues"), list):
            remove_offending_data_values_from_event(obj)

        for value in obj.values():
            remove_offending_data_values_from_payload(value)

    elif isinstance(obj, list):
        for item in obj:
            remove_offending_data_values_from_payload(item)

    return obj


# Function to get a unique random orgUnit
def deterministic_mapping(source_ous, target_ous):
    source_sorted = sorted(source_ous)
    target_sorted = sorted(target_ous)

    if len(source_sorted) > len(target_sorted):
        raise ValueError("Not enough target facilities to map 1:1 without collisions.")

    return {src: tgt for src, tgt in zip(source_sorted, target_sorted)}


DOB_ATTRIBUTE = "NI0QRzJvQ0k"

# Age band used only when a date of birth cannot be read or inferred at all.
GENERATED_DOB_MIN_AGE = 45
GENERATED_DOB_MAX_AGE = 65

# Representations seen in the source. Day-first legacy formats plus ISO.
DOB_FORMATS = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%d.%m.%Y",
    "%d %m %Y",
)

# The DOB field doubles as an age field in the source: "59", "53 years",
# "55 years ", "48 yrs old". Captured so the real age can be converted to a
# date of birth instead of being thrown away.
AGE_PATTERN = re.compile(
    r"^(\d{1,3})\s*(?:years?|yrs?|y|anos|a[nñ]os|ans)?\s*(?:old)?$",
    re.IGNORECASE,
)


def generate_dob():
    """A plausible date of birth for an adult NCD patient, as YYYY-MM-DD."""
    return fake.date_of_birth(
        minimum_age=GENERATED_DOB_MIN_AGE,
        maximum_age=GENERATED_DOB_MAX_AGE,
    ).isoformat()


def dob_from_age(age):
    """
    Convert a whole-year age to a date of birth, as YYYY-MM-DD.

    The birthday is spread over the twelve months that the age allows, so
    age-derived patients do not all end up sharing today's birthday. The age
    is still exact as of today.
    """
    today = date.today()

    def years_ago(years):
        try:
            return today.replace(year=today.year - years)
        except ValueError:
            # 29 February in a year whose counterpart is not a leap year.
            return today.replace(year=today.year - years, day=28)

    latest = years_ago(age)                              # birthday is today
    earliest = years_ago(age + 1) + timedelta(days=1)    # birthday was yesterday
    span = max((latest - earliest).days, 0)

    return (earliest + timedelta(days=random.randint(0, span))).isoformat()


def parse_dob(raw_value):
    """
    Best-effort conversion of a source DOB to YYYY-MM-DD.

    Handles real dates first, then the ages the source stores in the same
    field. Returns None only when the value carries nothing usable, so the
    caller can fall back to a generated date.
    """
    if raw_value is None:
        return None

    # Collapse whitespace runs and trim: the source holds values such as
    # "08/09/ 1952", "24 06 1946" and "55 years ".
    value = " ".join(str(raw_value).split())

    if not value:
        return None

    # A real date wins over any age reading. Try the value as written and with
    # the spaces removed, so "24 06 1946" and "08/09/ 1952" both parse.
    for candidate in (value, value.replace(" ", "")):
        for date_format in DOB_FORMATS:
            try:
                return datetime.strptime(candidate[:10], date_format).date().isoformat()
            except ValueError:
                pass

    # The field often holds an age instead: "59", "53 years", "48 yrs old".
    age_match = AGE_PATTERN.match(value)
    if age_match:
        age = int(age_match.group(1))
        if 0 < age < 120:
            return dob_from_age(age)

    return None


def normalize_dob_attributes(tracked_entity):
    """
    Normalize the Date of Birth / AGE attribute to YYYY-MM-DD.

    DOB is mandatory in the target program. A value that cannot be read as a
    date is replaced with a generated one for a GENERATED_DOB_MIN_AGE to
    GENERATED_DOB_MAX_AGE year old, rather than dropping the patient.

    Returns True when a date had to be generated.
    """
    generated = False

    for attr in tracked_entity.get("attributes", []):
        if attr.get("attribute") != DOB_ATTRIBUTE:
            continue

        raw_value = attr.get("value")
        parsed_value = parse_dob(raw_value)

        if parsed_value is None:
            parsed_value = generate_dob()
            generated = True
            logger.warning(
                f"trackedEntity {tracked_entity.get('trackedEntity')}: DOB "
                f"{raw_value!r} is not a usable date; generated {parsed_value}"
            )

        attr["value"] = parsed_value

    return generated


def main():
    api_source = load_api(SOURCE_ENV_PREFIX, "source (Nigeria NCD)")
    api_target = load_api(TARGET_ENV_PREFIX, "target (HTN/DM demo)")

    logger.warning("Server source running DHIS2 version {} revision {}".format(api_source.version, api_source.revision))
    logger.warning("Server target running DHIS2 version {} revision {}".format(api_target.version, api_target.revision))

    # Get org units level 5 from target (westland) - facilities
    ous_westland = api_target.get('organisationUnits', params={"filter": "level:eq:5", "paging": "false", "fields":"id,name"}).json()['organisationUnits']
    ous_westland = reindex(ous_westland, 'id')
    facility_uids = list(ous_westland.keys())
    print(f"Source OUs: {len(nigeria_ous_with_data)}")
    print(f"Target facilities: {len(facility_uids)}")
    ou_mapping = deterministic_mapping(nigeria_ous_with_data, facility_uids)
    resume_uid = load_resume_uid() if args.resume else None

    # A stale resume file would otherwise skip every org unit and exit as if
    # the migration had completed successfully.
    if resume_uid is not None and resume_uid not in nigeria_ous_with_data:
        logger.error(
            f"ERROR: resume file {RESUME_FILE} points at org unit '{resume_uid}', "
            "which is not in the list to migrate. Delete the file or fix it before resuming."
        )
        sys.exit(2)

    if resume_uid is not None:
        logger.info(f"Resuming from org unit {resume_uid}")

    skip_ou = bool(resume_uid)

    for ou_index in range(0,len(nigeria_ous_with_data)):
        nigeria_ou = nigeria_ous_with_data[ou_index]
        if resume_uid is not None and nigeria_ou == resume_uid:
            skip_ou = False
        if skip_ou:
            continue

        save_last_uid(nigeria_ou)

        today = date.today()
        first_day = today.replace(day=1)

        # Fetch enrollments within the date range
        # enrollments = api_source.get(
        #     'tracker/enrollments',
        #     params={
        #         "orgUnit": nigeria_ou,
        #         "skipPaging": "true",
        #         "fields": "trackedEntity",
        #         "enrolledAfter": first_day.strftime("%Y-%m-%d"),
        #         "program": "pMIglSEqPGS"
        #     }
        # ).json()['instances']
        # tei_uids = json_extract(enrollments, 'trackedEntity')

        # Query events that occurred after first day of month
        events = api_source.get(
            'tracker/events',
            params={
                "program": "pMIglSEqPGS",
                "orgUnit": nigeria_ou,
                "occurredAfter": args.after,
                "fields": "trackedEntity",
                "skipPaging": "true"
            }
        ).json()['events']

        # Step 3: Extract unique trackedEntity IDs
        tei_uids = list({e['trackedEntity'] for e in events if 'trackedEntity' in e})

        suitable_teis = list()

        ou_to_use = ou_mapping[nigeria_ou]
        logger.info(f'Mapping facility {nigeria_ou} to {ou_to_use}')

        for tei_uid in tei_uids:
            # Problematic TEIs!
            if tei_uid in ['fVJvL7v3oW9', 'lrJ1zLNii0G']:
                print('Skipping conflicting TEI')
                continue
            # tei = api_source.get('tracker/trackedEntities/'+tei_uid,
            #                           params={"program": 'pMIglSEqPGS', "paging": "false", "fields": "*"}).json()

            try:
                tei = api_source.get(
                    'tracker/trackedEntities/' + tei_uid,
                    params={"program": 'pMIglSEqPGS', "fields": "*"}
                ).json()
            except (DHIS2RequestException, RequestsRequestException) as e:
                if getattr(e, "code", None) == 404:
                    logger.warning(
                        f"Skipping trackedEntity {tei_uid}: it is referenced by an event "
                        "but is no longer available on the source server"
                    )
                    continue
                raise

            # # Check required attributes
            # required_attributes = {"NI0QRzJvQ0k", "R6VkX5nsAy4", "oindugucx72"}
            # attributes_with_values = {attr['attribute'] for attr in tei.get('attributes', []) if 'value' in attr}
            #
            # if not required_attributes.issubset(attributes_with_values):
            #     continue  # Skip to the next TEI if any required attribute is missing a value
            #
            # # Check enrollment date
            # enrollment_date = datetime.strptime(tei['enrollments'][0]['enrolledAt'], "%Y-%m-%dT%H:%M:%S.%f")
            # if enrollment_date < datetime(2024, 1, 1):
            #     continue  # Skip to the next TEI if the enrollment date is before or on 2024-01-01

            suitable_teis.append(dict(tei))

        logger.info('Found ' + str(len(suitable_teis)) + ' suitable TEIs')

        teis_ready_to_post = list()
        diabetic_with_data = 0
        diabetic_without_data = 0
        generated_dobs = 0
        for tei in suitable_teis:
            skip = False

            # Check required attributes
            required_attributes = {"NI0QRzJvQ0k", "R6VkX5nsAy4", "oindugucx72"}
            attributes_with_values = {attr['attribute'] for attr in tei.get('attributes', []) if 'value' in attr}

            if not required_attributes.issubset(attributes_with_values):
                continue

            # Remove unnecessary fields
            tei = remove_created_updated(tei)

            # Assign the new or mapped orgUnit to the TEI
            tei['orgUnit'] = ou_to_use
            if tei.get('programOwners'):
                tei['programOwners'][0]['orgUnit'] = ou_to_use
                tei['programOwners'][0]['ownerOrgUnit'] = ou_to_use
            else:
                logger.warning(
                    f"trackedEntity {tei.get('trackedEntity')} has no programOwners; "
                    "migrating it without rewriting program ownership"
                )

            # Filter out attributes
            tei['attributes'] = [attr for attr in tei.get('attributes', []) if attr['attribute'] in program_TEAs]

            # Only the attributes actually being migrated need a value. Checking
            # this before the filter above dropped patients because of an
            # attribute that never reaches the target instance anyway.
            if any('value' not in attr for attr in tei['attributes']):
                logger.warning(
                    f"Skipping trackedEntity {tei.get('trackedEntity')}: "
                    "a migrated attribute has no value"
                )
                continue

            # Make sure values for NCD Status are ACTIVE, TRANSFER, DIED, INACTIVE
            for attr in tei.get('attributes', []):
                if attr.get('attribute') == 'fI1P3Mg1zOZ' and attr.get('value') == 'UNRESPONSIVE':
                    print(f"Fixing TEI {tei.get('trackedEntity')}: UNRESPONSIVE → INACTIVE")
                    attr['value'] = 'INACTIVE'

            # Update address for the attribute with uid A6Hb0Kvg4vb
            address_found = False
            for attr in tei['attributes']:
                if attr['attribute'] == 'A6Hb0Kvg4vb':
                    attr['value'] = fake.address()
                    address_found = True
            if not address_found:
                tei['attributes'].append({
                    "attribute": "A6Hb0Kvg4vb",
                    "value": fake.address(),
                    "displayName": "Address (current)",
                    "valueType": "TEXT"
                })

            # Update phone number for the attribute with uid
            phone_number_found = False
            for attr in tei['attributes']:
                if attr['attribute'] == 'YRDy9xy9jD0':
                    raw = fake.msisdn()  # genera una cadena tipo 2348012345678
                    attr['value'] =  f"+{raw}" if not raw.startswith("+") else raw
                    phone_number_found = True
            if not phone_number_found:
                tei['attributes'].append({
                    "attribute": "YRDy9xy9jD0",
                    "value": fake.phone_number(),
                    "displayName": "Patient Phone Number",
                    "valueType": "PHONE_NUMBER"
                })

            consent_found = False
            for attr in tei['attributes']:
                if attr['attribute'] == 'YJGACwhN0St':
                    consent_found = True
            if not consent_found:
                tei['attributes'].append({
                    "attribute": "YJGACwhN0St",
                    "displayName": "HTN - Consent to record data",
                    "valueType": "TRUE_ONLY",
                    "value": "true"
                })

            patient_has_hypertension_found = False
            for attr in tei['attributes']:
                if attr['attribute'] == 'jCRIT4GMMOS':
                    patient_has_hypertension_found = True
            if not patient_has_hypertension_found:
                tei['attributes'].append({
                    "attribute": "jCRIT4GMMOS",
                    "displayName": "HTN - Does patient have hypertension?",
                    "valueType": "TEXT",
                    "value": "YES"
                })


            # Determine the sex
            sex = None
            for attr in tei['attributes']:
                if attr['attribute'] == 'oindugucx72':
                    sex = attr['value'].upper()
                    break

            if sex in ['MALE', 'FEMALE']:
                # Generate and add family and given names. Both TEAs survive the
                # program_TEAs filter above, so overwrite any source value instead
                # of appending a second entry for the same attribute.
                family_name = fake.last_name_male() if sex == 'MALE' else fake.last_name_female()
                given_name = fake.first_name_male() if sex == 'MALE' else fake.first_name_female()

                generated_names = {
                    "ENRjVGxVL6l": (family_name, "GEN - Family name"),
                    "sB1IHYu2xQT": (given_name, "GEN - Given name"),
                }

                names_found = set()
                for attr in tei['attributes']:
                    if attr['attribute'] in generated_names:
                        attr['value'] = generated_names[attr['attribute']][0]
                        names_found.add(attr['attribute'])

                for attribute_uid, (value, display_name) in generated_names.items():
                    if attribute_uid in names_found:
                        continue
                    tei['attributes'].append({
                        "attribute": attribute_uid,
                        "value": value,
                        "displayName": display_name,
                        "valueType": "TEXT"
                    })

            # Complete Health Insurance
            insurance_found = False
            for attr in tei['attributes']:
                if attr['attribute'] == 'vFaX4Whc3oq':
                    insurance_found = True
                    break
            if not insurance_found:
                patient_enrolled_in_health_insurance = "YES" if random.random() < 0.9 else "NO"
                tei['attributes'].append({
                    "attribute": "vFaX4Whc3oq",
                    "value": patient_enrolled_in_health_insurance,
                    "displayName": "HTN - Is patient enrolled in a health insurance?",
                    "valueType": "TEXT"
                })

            # The Nigeria source has virtually no diabetic patients, so diabetes is
            # generated here on purpose: ~65% of the migrated patients are flagged
            # as diabetic and get matching blood-sugar dataValues on their
            # COMPLETED events further down (check_hypertension_and_add_data_values).
            # The attribute itself is set after the events are processed, so it
            # reflects the blood-sugar values the patient actually ends up with.
            has_diabetes = random.random() < 0.65

            if 'enrollments' not in tei or len(tei['enrollments']) == 0:
                continue

            # Keep the first enrollment
            main_enrollment = tei['enrollments'][0]

            # Make sure 'events' key exists
            if 'events' not in main_enrollment:
                main_enrollment['events'] = []

            # Merge events from the other enrollments (if any)
            for extra_enrollment in tei['enrollments'][1:]:
                extra_events = extra_enrollment.get('events', [])
                main_enrollment['events'].extend(extra_events)

            # Drop ttributes inside enrollment - attributes provided elsewhere
            if 'attributes' in main_enrollment:
                main_enrollment.pop('attributes')

            # Replace the enrollments list with just the consolidated one
            tei['enrollments'] = [main_enrollment]

            # NI0QRzJvQ0k is mandatory in the target program. Dates and ages
            # are converted; only a value with nothing usable in it gets a
            # generated date, so no patient is dropped over a bad DOB.
            if normalize_dob_attributes(tei):
                generated_dobs += 1

            for enrollment in tei.get('enrollments', []):
                enrollment['orgUnit'] = ou_to_use
                enrollment['orgUnitName'] = ous_westland[ou_to_use]['name']
                is_not_controlled_probability = 1

                cleaned_events = []
                for event in enrollment.get('events', []):
                    if 'occurredAt' not in event:
                        if 'scheduledAt' not in event or event.get('status') not in ['SCHEDULE', 'OVERDUE']:
                            skip = True

                    event['orgUnit'] = ou_to_use
                    event['orgUnitName'] = ous_westland[ou_to_use]['name']
                    event['dataValues'] = [dv for dv in event.get('dataValues', []) if dv.get('dataElement') in PROGRAM_STAGE_DES_SET]

                    # Modify specific dataValues
                    filtered_dvs = []
                    for dv in event.get('dataValues', []):
                        if not 'value' in dv:
                            continue
                        if dv['dataElement'] == 'yUYbcCXo9Mv':
                            dv['value'] = f"{dv['value']}_OD"
                        # For the specific case of D8FWXAtCuL2 (Hydrochlorothiazide)
                        elif dv['dataElement'] == 'D8FWXAtCuL2':
                            if "12" in dv['value']:
                                dv['value'] = "HYDROCHLOROTHIAZIDE_12POINT5MG_OD"
                            elif "25" in dv['value']:
                                dv['value'] = "HYDROCHLOROTHIAZIDE_25MG_OD"
                        elif dv['dataElement'] == 'lWmOLo5hFYQ':
                            if "40" in dv['value']:
                                dv['value'] = "TELMISARTAN_40MG_OD"
                            elif "80" in dv['value']:
                                dv['value'] = "TELMISARTAN_80MG_OD"
                        elif dv['dataElement'] == 'UXiaSOqzFts':
                            if "OD" not in dv['value'] and "BD" not in dv['value']:
                                dv['value'] = f"{dv['value']}_OD"

                        if dv['dataElement'] == 'ifRI6qw96v4' and event.get('programStage') == 'anb2cjLx3WM':
                            # "DataElement `ifRI6qw96v4` is not part of `anb2cjLx3WM` program stage"
                            continue  # Skip phone number in wrong stage
                        else:
                            filtered_dvs.append(dv)

                    event['dataValues'] = filtered_dvs

                    # Add diabetes data
                    if has_diabetes and event.get('status') == 'COMPLETED':
                        check_hypertension_and_add_data_values(event, is_not_controlled_probability)
                        is_not_controlled_probability = random.choice([round(x, 1) for x in [i * 0.1 for i in range(3, 10)]])

                    remove_offending_data_values_from_event(event)
                    cleaned_events.append(event)

                enrollment['events'] = cleaned_events

            # Keep the diabetes attribute consistent with the event data. Any
            # patient carrying a blood-sugar measurement - injected above or
            # already present in the source - answers YES, regardless of the
            # random roll. Without this, a patient whose roll came out False but
            # whose source events already held blood-sugar values would be
            # imported with diabetes data and no answer to the question.
            patient_has_diabetes_data = tei_has_diabetes_data(tei)
            if has_diabetes or patient_has_diabetes_data:
                set_diabetes_attribute(tei, 'YES')
                if patient_has_diabetes_data:
                    diabetic_with_data += 1
                else:
                    diabetic_without_data += 1

            def clean_payload(obj):
                if isinstance(obj, dict):
                    return {
                        k: clean_payload(v)
                        for k, v in obj.items()
                        if k not in (
                        "programOwners", "assignedUser", "createdAt", "createdAtClient", "updatedAt", "updatedAtClient")
                    }
                elif isinstance(obj, list):
                    return [clean_payload(item) for item in obj]
                return obj

            #tei = clean_payload(tei)

            if not skip:
                teis_ready_to_post.append(tei)

        #Send the TEI to target instance
        if len(teis_ready_to_post) > 0:
            logger.info(
                f"Diabetes: {diabetic_with_data + diabetic_without_data} of "
                f"{len(teis_ready_to_post)} patients flagged YES "
                f"({diabetic_with_data} with blood-sugar data, "
                f"{diabetic_without_data} without)"
            )
            if generated_dobs:
                logger.info(
                    f"DOB: {generated_dobs} of {len(teis_ready_to_post)} patients "
                    f"needed a generated date of birth"
                )
            logger.info('Posting TEIs to org unit ' + ous_westland[ou_to_use]['name'])
            # with open("payloads_output.json", "w") as out_file:
            #     json.dump(teis_ready_to_post, out_file, indent=2)
            post_chunked_tracked_entities(api_target, teis_ready_to_post)
            #print(json.dumps(tei, indent=4))

    delete_resume_file()

    # datasets = api_source.get('dataSets', params={"filter": "name:like:SIA", "paging": "false", "fields":"id,name"}).json()[
    #     'dataSets']
    # c = True
    # for ds in datasets:
    #     for ou in ous:
    #         logger.info('Processing data for dataSet ' + ds['name'] + ' from ' + ou['name'])
    #         params = {
    #             #'period': row['period'],
    #             'startDate': startDate,
    #             'endDate': endDate,
    #             'orgUnit': ou['id'],
    #             'dataSet': ds['id'],
    #             'children': 'true'
    #         }
    #
    #         # Get data from source server
    #         data = api_source.get('dataValueSets', params=params).json()
    #         if 'dataValues' in data and len(data['dataValues']) > 0:
    #             post_chunked_data(api_target, data['dataValues'], 'dataValueSets', 1000)
    #         else:
    #             logger.info("No data for '{}' '{}' ".format(ou['name'], ds['name']))
    #

if __name__ == '__main__':
    main()
