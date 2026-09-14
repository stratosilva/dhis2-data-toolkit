# locustfile.py
#
# Load test for DHIS2 Tracker "Traveler Portal" style TEI creation.
# - Creates new TEI + enrollment + travel & clinical events
# - Randomizes TEI attributes and DE values based on your mapping
# - Uses DHIS2 /system/id for UIDs (like the web portal)
# - Retries tracker POST up to N times on server/network errors
# - Collects simple per-operation stats (attempts, durations)


import os
import random
import string
import time
from datetime import datetime, timedelta
from collections import defaultdict
import logging
logger = logging.getLogger(__name__)

from locust import HttpUser, task, between, events
from dhis2_env import env

# ---------------------------------------------------------------------------
# Configuration (override via environment variables if needed)
# ---------------------------------------------------------------------------

# DHIS2 authentication
# Token for the target server. Set DHIS2_API_TOKEN in .env (staging by
# default); override it in the environment to run against production.
DHIS2_API_TOKEN = env("DHIS2_API_TOKEN", required=True)

# Program and tracker configuration
PROGRAM_ID = os.environ.get("PROGRAM_ID", "pam2gg32GIX")
TRAVEL_STAGE_ID = os.environ.get("TRAVEL_STAGE_ID", "ECqXBCJdIJW")
CLINICAL_STAGE_ID = os.environ.get("CLINICAL_STAGE_ID", "nqE0Yrh0XvW")
ORG_UNIT_ID = os.environ.get("ORG_UNIT_ID", "P9dOOh865eF")
TRACKED_ENTITY_TYPE_ID = os.environ.get("TRACKED_ENTITY_TYPE_ID", "NfwwxcCXeKS")

# TEA UIDs
TEA_SEX = "S0laL1aHf6i"
TEA_NATIONALITY = "GWQC1qQdw8Y"
TEA_FIRST_NAME = "ur1JM6CZeSb"
TEA_LAST_NAME = "vUacdogzWI6"
TEA_MIDDLE_NAME = "wS7QQnuWCtc"
TEA_DOB_AGE = "Rv8WM2mTuS5"
TEA_PHONE = "Vr0lFuBkaaV"
TEA_PASSPORT = "kDWurLVuVZw"
TEA_EMAIL = "AmEMVqSn0cP"
TEA_GUARDIAN_NAME = "ecHExEkzOGO"       # only if < 18 years
TEA_GUARDIAN_PHONE = "DnWpyjuxhnh"      # only if < 18 years

# DE UIDs – Travel stage
DE_AIRLINE = "EvJTARXbuPj"
DE_COUNTRY_OF_DEPARTURE = "BoQdGhFv7te"
DE_PURPOSE_OF_TRAVEL = "BXGTya98TLD"
DE_VISITED_1 = "AXpyzUwlcxY"
DE_VISITED_2 = "g4la792LVkV"
DE_VISITED_3 = "k9KUAc7EUvk"
DE_VISITED_4 = "f5H0rOaVBzu"
DE_VISITED_5 = "aUfY6AbcnH0"
DE_AIRLINE_OTHER = "ozBn9o48C7F"
DE_FLIGHT_NUM_NUMERIC = "R775EQee9sB"
DE_FLIGHT_NUM_FULL = "dP5GhQYdMMf"
DE_SEAT_NUM = "Q20Pk08bg5U"
DE_PORT_OF_DEPARTURE = "ebDNzAopp9K"
# DE_HOTEL_NAME is intentionally not used for the test

# DE UIDs – Clinical stage
DE_FEVER = "Flsj6UraUDl"
DE_HEADACHE = "VjTrjcfHQmt"
DE_RASH = "uTjGEBukLG0"
DE_MUCOSAL = "MH0EHcO1Cs7"
DE_BACK_PAIN = "EQ5xB6GqnJ1"
DE_MYALGIA = "NfnK4PySuYO"
DE_WEAKNESS = "jKArgxSrv30"
DE_RECTAL_PAIN = "hUEEynfhVbA"
DE_BLOODY_DIARRHEA = "AIlUk7HvRmk"
DE_HEALTH_FLAG = "cGSuTAbYhkM"  # DO NOT send; program rules calculate this
DE_EBOLA_CONTACT = "N1a72h5IKg5"
DE_MPOX_CONTACT = "xquRoJCQZHu"
#DE_MARBURG_CONTACT = "Byw2vN8k2vw" # Production
DE_MARBURG_CONTACT = "sVRXqLNmdCn" # Staging
DE_OTHER_SYMPTOMS = "NoCXFMxcfT3"

# Retry configuration
MAX_RETRIES = int(os.environ.get("DHIS2_MAX_RETRIES", "1"))

# ---------------------------------------------------------------------------
# Categorical value lists
# ---------------------------------------------------------------------------

SEX_OPTIONS = ["FEMALE", "MALE", "OTHER"]
# SEX_OPTIONS = ["FEMALE", "MALE"]

COUNTRY_CODES = [
    "ET", "AF", "AX", "AL", "DZ", "AS", "AD", "AO", "AI", "AQ", "AG", "AR", "AM", "AW",
    "AU", "AT", "AZ", "BS", "BH", "BD", "BB", "BY", "BE", "BZ", "BJ", "BM", "BT", "BO",
    "BA", "BW", "BV", "BR", "IO", "BN", "BG", "BF", "BI", "KH", "CM", "CA", "CV", "KY",
    "CF", "TD", "CL", "CN", "CX", "CC", "CO", "KM", "CG", "CD", "CK", "CR", "CI", "HR",
    "CU", "CY", "CZ", "DK", "DJ", "DM", "DO", "EC", "EG", "SV", "GQ", "ER", "EE", "FK",
    "FO", "FJ", "FI", "FR", "GF", "PF", "TF", "GA", "GM", "GE", "DE", "GH", "GI", "GR",
    "GL", "GD", "GP", "GU", "GT", "GG", "GN", "GW", "GY", "HT", "HM", "VA", "HN", "HK",
    "HU", "IS", "IN", "ID", "IR", "IQ", "IE", "IM", "IL", "IT", "JM", "JP", "JE", "JO",
    "KZ", "KE", "KI", "KP", "KR", "XK", "KW", "KG", "LA", "LV", "LB", "LS", "LR", "LY",
    "LI", "LT", "LU", "MO", "MK", "MG", "MW", "MY", "MV", "ML", "MT", "MH", "MQ", "MR",
    "MU", "YT", "MX", "FM", "MD", "MC", "MN", "ME", "MS", "MA", "MZ", "MM", "NA", "NR",
    "NP", "NL", "AN", "NC", "NZ", "NI", "NE", "NG", "NU", "NF", "MP", "NO", "OM", "PK",
    "PW", "PS", "PA", "PG", "PY", "PE", "PH", "PN", "PL", "PT", "PR", "QA", "RE", "RO",
    "RU", "RW", "BL", "SH", "KN", "LC", "MF", "PM", "VC", "WS", "SM", "ST", "SA", "SN",
    "RS", "SC", "SL", "SG", "SK", "SI", "SB", "SO", "ZA", "GS", "SS", "ES", "LK", "SD",
    "SR", "SJ", "SZ", "SE", "CH", "SY", "TW", "TJ", "TZ", "TH", "TL", "TG", "TK", "TO",
    "TT", "TN", "TR", "TM", "TC", "TV", "UG", "UA", "AE", "GB", "US", "UY", "UZ", "VU",
    "VE", "VN", "VG", "VI", "WF", "YE", "ZM",
]

TRAVEL_COUNTRY_CODES = None

AIRLINE_CODES = [
    "A3", "AH", "G9", "UU", "BP", "AC", "CA", "AF", "AI", "AS", "AA", "KP", "J4", "BA",
    "MU", "CZ", "DL", "MS", "EK", "ET", "FZ", "XY", "GF", "IB", "6E", "AZ", "JL", "J9",
    "B6", "KQ", "KL", "KE", "KU", "LA", "LH", "9Y", "OTHER", "PK", "QF", "QR", "SV",
    "ZH", "SQ", "UL", "TK",
]

PURPOSE_OF_TRAVEL_CODES = [
    "TOURISM", "BUSINESS", "VISITING_FAMILY_FRIENDS", "RESIDENT",
    "TRANSIT_LT_8H", "LAYOVER_GT_8H", "MEDICAL", "EDUCATION",
    "OFFICIAL_DIPLOMATIC", "RELIGIOUS_PILGRIMAGE", "OTHER",
]

SYMPTOM_OPTIONS = ["YES", "NO", "UNSURE"]

# ---------------------------------------------------------------------------
# Simple per-operation stats (logical operation, not raw HTTP request)
# ---------------------------------------------------------------------------

operation_stats = defaultdict(
    lambda: {
        "count": 0,
        "success": 0,
        "failed": 0,
        "success_on_attempt_1": 0,
        "success_on_attempt_2": 0,
        "success_on_attempt_3": 0,
        "total_duration_us": 0,
        "min_duration_us": None,
        "max_duration_us": 0,
    }
)


def record_operation(name: str, attempts: int, success: bool, duration_us: int) -> None:
    stats = operation_stats[name]
    stats["count"] += 1
    if success:
        stats["success"] += 1
        if attempts == 1:
            stats["success_on_attempt_1"] += 1
        elif attempts == 2:
            stats["success_on_attempt_2"] += 1
        elif attempts >= 3:
            stats["success_on_attempt_3"] += 1
    else:
        stats["failed"] += 1

    stats["total_duration_us"] += duration_us
    if stats["min_duration_us"] is None or duration_us < stats["min_duration_us"]:
        stats["min_duration_us"] = duration_us
    if duration_us > stats["max_duration_us"]:
        stats["max_duration_us"] = duration_us


@events.quitting.add_listener
def on_quitting(environment, **kwargs):
    """
    When Locust is about to quit, print a simple summary of logical operations.
    """
    print("\n=== Custom Operation Summary ===")
    for name, s in operation_stats.items():
        if s["count"] == 0:
            continue
        avg_us = s["total_duration_us"] / s["count"]
        print(f"\nOperation: {name}")
        print(f"  Total operations:        {s['count']}")
        print(f"  Success:                 {s['success']}")
        print(f"  Failed:                  {s['failed']}")
        print(f"  Success on 1st attempt:  {s['success_on_attempt_1']}")
        print(f"  Success on 2nd attempt:  {s['success_on_attempt_2']}")
        print(f"  Success on 3rd+ attempt: {s['success_on_attempt_3']}")
        print(f"  Avg duration (µs):       {avg_us:.0f}")
        print(f"  Min duration (µs):       {s['min_duration_us']}")
        print(f"  Max duration (µs):       {s['max_duration_us']}")


# ---------------------------------------------------------------------------
# Random data generators
# ---------------------------------------------------------------------------

def random_name(min_len=3, max_len=10) -> str:
    length = random.randint(min_len, max_len)
    # Only letters, no numbers
    letters = string.ascii_letters
    return "".join(random.choice(letters) for _ in range(length)).capitalize()


def random_dob_and_age() -> tuple[str, int]:
    """
    Generate a random date of birth (YYYY-MM-DD) and the corresponding age in years.
    We sample an age between 0 and 80, then compute DOB from today's date.
    """
    today = datetime.utcnow().date()
    age = random.randint(0, 80)  # includes minors and adults
    # Approximate DOB: today - age years, plus a random offset within the year
    year = today.year - age
    # clamp year at a reasonable lower bound
    if year < 1940:
        year = 1940
        age = today.year - year
    # choose random month/day safely
    base = datetime(year, 1, 1).date()
    end = datetime(year, 12, 31).date()
    delta_days = (end - base).days
    offset = random.randint(0, delta_days)
    dob = base + timedelta(days=offset)
    return dob.strftime("%Y-%m-%d"), age


def random_phone_number() -> str:
    """
    Simple phone generator. You can adapt to Ethiopian format if needed.
    Example: +2519XXXXXXXX
    """
    prefix = "+2519"
    return prefix + "".join(random.choice(string.digits) for _ in range(8))


def random_passport_number(min_len=6, max_len=9) -> str:
    """
    Passport accepts letters and digits.
    """
    length = random.randint(min_len, max_len)
    chars = string.ascii_uppercase + string.digits
    return "".join(random.choice(chars) for _ in range(length))


def random_seat_number() -> str:
    row = random.randint(1, 60)
    seat_letter = random.choice(list("ABCDEF"))
    return f"{row}{seat_letter}"


def random_boolean_str() -> str:
    return "true" if random.random() < 0.5 else "false"


def random_email(first_name: str, last_name: str) -> str:
    domain = random.choice(["example.com", "mail.com", "test.org"])
    return f"{first_name.lower()}.{last_name.lower()}@{domain}"


def random_port_of_departure() -> str:
    """
    Random free-text for port of departure: could be a city or airport code.
    """
    city_prefix = random.choice(["ADD", "DXB", "NBO", "FRA", "CDG", "LHR", "IST"])
    suffix = "".join(random.choice(string.ascii_uppercase) for _ in range(2))
    return f"{city_prefix}_{suffix}"


def generate_clinical_data():
    """
    Returns clinical data values with realistic probability distribution:
    - 75% Green: all symptoms NO, all booleans false
    - 15% Yellow: 1–2 symptoms YES/UNSURE, booleans false
    - 5% Red: 3–6 symptoms YES/UNSURE, booleans can be true
    """

    # Probability bucket selection
    rnd = random.random()

    if rnd < 0.75:
        risk_level = "GREEN"
    elif rnd < 0.90:
        risk_level = "YELLOW"
    else:
        risk_level = "RED"

    symptom_fields = [
        DE_FEVER,
        DE_HEADACHE,
        DE_RASH,
        DE_MUCOSAL,
        DE_BACK_PAIN,
        DE_MYALGIA,
        DE_WEAKNESS,
        DE_RECTAL_PAIN,
        DE_BLOODY_DIARRHEA,
    ]

    # Default all symptoms to NO
    symptoms = {field: "NO" for field in symptom_fields}

    # Default both exposures to false
    ebola_contact = "false"
    marburg_contact = "false"

    if risk_level == "YELLOW":
        # 1–2 symptoms become YES or UNSURE
        k = random.randint(1, 2)
        chosen = random.sample(symptom_fields, k)
        for f in chosen:
            symptoms[f] = random.choice(["YES", "UNSURE"])

    elif risk_level == "RED":
        # 3–6 symptoms become YES or UNSURE
        k = random.randint(3, 6)
        chosen = random.sample(symptom_fields, k)
        for f in chosen:
            symptoms[f] = random.choice(["YES", "UNSURE"])

        # Exposure flags (boolean) become sometimes true (10–40% chance)
        if random.random() < 0.30:
            ebola_contact = "true"
        if random.random() < 0.30:
            marburg_contact = "true"

    # Build clinical DE payload
    clinical_data_values = [
        {"dataElement": f, "value": v} for f, v in symptoms.items()
    ]

    clinical_data_values.append(
        {"dataElement": DE_EBOLA_CONTACT, "value": ebola_contact}
    )
    clinical_data_values.append(
        {"dataElement": DE_MARBURG_CONTACT, "value": marburg_contact}
    )

    clinical_data_values.append({"dataElement": DE_OTHER_SYMPTOMS, "value": ""})

    return clinical_data_values

# ---------------------------------------------------------------------------
# DHIS2 helper: get UIDs from /system/id
# ---------------------------------------------------------------------------

def get_uids(http_client, headers: dict, count: int) -> list[str]:
    """
    Request 'count' UIDs from DHIS2 /system/id, return as a list of strings.
    Raises Exception if it cannot get valid UIDs.
    """
    resp = http_client.get(
        "/system/id",
        name="GET /system/id",
        headers=headers,
        params={"limit": count},
        timeout=15,
    )

    if resp.status_code != 200:
        raise Exception(f"/system/id HTTP {resp.status_code}")

    try:
        data = resp.json()
    except ValueError as e:
        raise Exception(f"/system/id invalid JSON: {e}")

    codes = data.get("codes") or data.get("uids") or []
    if len(codes) < count:
        raise Exception(f"/system/id returned {len(codes)} codes, expected {count}")

    return codes[:count]

# ---------------------------------------------------------------------------
# DHIS2 helper: get countries visited in last 21 days from option group
# ---------------------------------------------------------------------------

def ensure_travel_country_codes(http_client, headers: dict) -> None:
    """
    Ensure TRAVEL_COUNTRY_CODES is populated by calling the DHIS2 optionGroup:
    /optionGroups/m0EgeLz1Jzc?fields=options[code]

    Uses global cache so we only fetch once per Locust process.
    """
    global TRAVEL_COUNTRY_CODES
    if TRAVEL_COUNTRY_CODES is not None:
        return

    resp = http_client.get(
        "/optionGroups/m0EgeLz1Jzc",
        name="GET /optionGroups/m0EgeLz1Jzc",
        headers=headers,
        params={"fields": "options[code]"},
        timeout=15,
    )

    if resp.status_code != 200:
        raise Exception(
            f"/optionGroups/m0EgeLz1Jzc HTTP {resp.status_code} body={resp.text[:500]}"
        )

    try:
        data = resp.json()
    except ValueError as e:
        raise Exception(f"/optionGroups/m0EgeLz1Jzc invalid JSON: {e}")

    options = data.get("options") or []
    codes = [opt.get("code") for opt in options if opt.get("code")]

    if not codes:
        raise Exception("No option codes returned from optionGroup m0EgeLz1Jzc")

    TRAVEL_COUNTRY_CODES = codes
    logger.info(
        "Loaded %d travel country codes from optionGroup m0EgeLz1Jzc",
        len(TRAVEL_COUNTRY_CODES),
    )



# ---------------------------------------------------------------------------
# Payload builder
# ---------------------------------------------------------------------------

def build_random_traveler_payload(
    tei_uid: str,
    enrollment_uid: str,
    travel_event_uid: str,
    clinical_event_uid: str,
    flight_info: dict,
) -> tuple[dict, str, str]:
    """
    Build a full tracker payload for a new traveler:
    - TEI with attributes
    - 1 enrollment in PROGRAM_ID
    - 1 travel event in TRAVEL_STAGE_ID
    - 1 clinical event in CLINICAL_STAGE_ID

    Arrival date (travel_event.occurredAt) is today or up to 7 days in the future.
    All other dates (completedAt, clinical occurredAt) are today's date.
    """
    # Today (for completedAt and clinical occurredAt)
    now = datetime.utcnow()
    today_iso = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    enrolled_at = now.strftime("%Y-%m-%d")  # enrollment date (YYYY-MM-DD)

    # Arrival date: today or within the next 7 days
    days_ahead = random.randint(0, 7)
    arrival_dt = now + timedelta(days=days_ahead)
    arrival_iso = arrival_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    # TEI attributes
    first_name = random_name()
    middle_name = random_name()
    last_name = random_name()
    dob, age_years = random_dob_and_age()
    nationality = random.choice(COUNTRY_CODES)

    attributes = [
        {"attribute": TEA_FIRST_NAME, "value": first_name},
        {"attribute": TEA_MIDDLE_NAME, "value": middle_name},
        {"attribute": TEA_LAST_NAME, "value": last_name},
        {"attribute": TEA_SEX, "value": random.choice(SEX_OPTIONS)},
        # AGE type – sending DOB as YYYY-MM-DD
        {"attribute": TEA_DOB_AGE, "value": dob},
        {"attribute": TEA_NATIONALITY, "value": nationality},
        {"attribute": TEA_PHONE, "value": random_phone_number()},
        {"attribute": TEA_PASSPORT, "value": random_passport_number()},
        {"attribute": TEA_EMAIL, "value": random_email(first_name, last_name)},
    ]

    # Guardian info only if minor (< 18 years)
    if age_years < 18:
        attributes.append(
            {"attribute": TEA_GUARDIAN_NAME, "value": random_name() + " " + random_name()}
        )
        attributes.append(
            {"attribute": TEA_GUARDIAN_PHONE, "value": random_phone_number()}
        )

    # Travel event data values
    airline_code = flight_info["airline_code"]
    flight_number_numeric = flight_info["flight_number_numeric"]
    departure_country = flight_info["departure_country"]
    port_of_departure = flight_info["port_of_departure"]
    # Full flight number is calculated on server side → do not send as DE
    # flight_number_full = airline_code + flight_number_numeric

    if not TRAVEL_COUNTRY_CODES:
        # Safety fallback: avoid crash, but this should not happen if on_start loaded them
        visit_codes = ["ET"]
    else:
        # If fewer than 5 codes exist, sample as many as possible
        k = min(5, len(TRAVEL_COUNTRY_CODES))
        visit_codes = random.sample(TRAVEL_COUNTRY_CODES, k=k)
        # Pad with the first code to always have 5 entries
        while len(visit_codes) < 5:
            visit_codes.append(visit_codes[0])

    travel_data_values = [
        {"dataElement": DE_AIRLINE, "value": airline_code},
        {"dataElement": DE_COUNTRY_OF_DEPARTURE, "value": departure_country},
        {"dataElement": DE_PURPOSE_OF_TRAVEL, "value": random.choice(PURPOSE_OF_TRAVEL_CODES)},
        {"dataElement": DE_VISITED_1, "value": visit_codes[0]},
        {"dataElement": DE_VISITED_2, "value": visit_codes[1]},
        {"dataElement": DE_VISITED_3, "value": visit_codes[2]},
        {"dataElement": DE_VISITED_4, "value": visit_codes[3]},
        {"dataElement": DE_VISITED_5, "value": visit_codes[4]},
        {"dataElement": DE_AIRLINE_OTHER, "value": ""},  # only meaningful when airline == OTHER
        {"dataElement": DE_FLIGHT_NUM_NUMERIC, "value": flight_number_numeric},
        # DO NOT send DE_FLIGHT_NUM_FULL – program rules calculate this on the server
        {"dataElement": DE_SEAT_NUM, "value": random_seat_number()},
        {"dataElement": DE_PORT_OF_DEPARTURE, "value": port_of_departure},
        # DE_HOTEL_NAME is intentionally not sent
    ]

    travel_event = {
        "status": "COMPLETED",
        # Arrival can be now or up to +7 days
        "occurredAt": arrival_iso,
        # CompletedAt is always today's date
        "completedAt": today_iso,
        "programStage": TRAVEL_STAGE_ID,
        "program": PROGRAM_ID,
        "orgUnit": ORG_UNIT_ID,
        "event": travel_event_uid,
        "dataValues": travel_data_values,
    }

    clinical_data_values = generate_clinical_data()

    # Clinical event: all dates = today
    clinical_event = {
        "status": "COMPLETED",
        "occurredAt": today_iso,
        "completedAt": today_iso,
        "programStage": CLINICAL_STAGE_ID,
        "program": PROGRAM_ID,
        "orgUnit": ORG_UNIT_ID,
        "event": clinical_event_uid,
        "dataValues": clinical_data_values,
    }

    enrollment_obj = {
        "program": PROGRAM_ID,
        "status": "ACTIVE",
        "orgUnit": ORG_UNIT_ID,
        "enrolledAt": enrolled_at,  # YYYY-MM-DD
        "enrollment": enrollment_uid,
        "events": [travel_event, clinical_event],
    }

    tei_obj = {
        "trackedEntity": tei_uid,
        "orgUnit": ORG_UNIT_ID,
        "trackedEntityType": TRACKED_ENTITY_TYPE_ID,
        "attributes": attributes,
        "enrollments": [enrollment_obj],
    }

    payload = {"trackedEntities": [tei_obj]}
    return payload, tei_uid, enrollment_uid



# ---------------------------------------------------------------------------
# Locust User
# ---------------------------------------------------------------------------

class TravelerUser(HttpUser):
    """
    Simulates a traveler declaration being submitted through the portal.
    Main task: create a new TEI + enrollment + events via /tracker.
    """

    wait_time = between(0.1, 1.0)  # adjust or override via environment if needed

    def on_start(self):
        # Prepare default headers with authentication
        self.headers = {
            "Authorization": f"ApiToken {DHIS2_API_TOKEN}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        self.created_travelers = []

        # Load travel country codes (Countries visited last 21 days)
        try:
            ensure_travel_country_codes(self.client, self.headers)
        except Exception as e:
            logger.error("Failed to load travel country codes: %s", e)
            # it's OK to just let requests fail; you'll see it in Locust

        # Per-user "current flight" state
        self.current_flight = None
        self.remaining_in_flight = 0

    def _get_or_create_flight(self) -> dict:
        """
        Returns a flight definition that will be shared by a batch of passengers.
        For each batch, all travelers have the same:
        - airline
        - flight number (numeric)
        - country of departure
        - port of departure
        """
        if self.current_flight is None or self.remaining_in_flight <= 0:
            # New batch size: e.g. 60, 70 or 100 passengers
            batch_size = random.choice([60, 70, 100])

            airline_code = random.choice(AIRLINE_CODES)
            flight_number_numeric = "".join(
                random.choice(string.digits) for _ in range(random.randint(3, 4))
            )
            departure_country = random.choice(COUNTRY_CODES)
            port_of_departure = random_port_of_departure()

            self.current_flight = {
                "airline_code": airline_code,
                "flight_number_numeric": flight_number_numeric,
                "departure_country": departure_country,
                "port_of_departure": port_of_departure,
                "batch_size": batch_size,
            }
            self.remaining_in_flight = batch_size

        # Consume one seat in this flight batch
        self.remaining_in_flight -= 1
        return self.current_flight

    @task(10)
    def create_traveler(self):
        """
        Logical operation: create a full traveler (TEI + enrollment + events)
        and then fetch the enrollment to read the server-calculated health flag.
        """
        op_name = "create_traveler"
        attempts = 0
        success = False
        op_start_ns = time.perf_counter_ns()

        # Get 4 UIDs (tei, enrollment, travel event, clinical event) from /system/id
        try:
            tei_uid, enrollment_uid, travel_event_uid, clinical_event_uid = get_uids(
                self.client, self.headers, 4
            )
        except Exception as e:
            # If we cannot get UIDs, record as failed operation and return
            op_duration_us = (time.perf_counter_ns() - op_start_ns) // 1000
            record_operation(op_name, 1, False, op_duration_us)
            # Let Locust see this as a request failure as well
            events.request.fire(
                request_type="GET",
                name="GET /system/id (logical failure)",
                response_time=op_duration_us / 1000.0,
                response_length=0,
                exception=e,
            )
            return

        # Get current flight (shared attributes for a batch of passengers)
        flight_info = self._get_or_create_flight()

        payload, tei_uid, enrollment_uid = build_random_traveler_payload(
            tei_uid,
            enrollment_uid,
            travel_event_uid,
            clinical_event_uid,
            flight_info,
        )

        while attempts < MAX_RETRIES and not success:
            attempts += 1

            # Use catch_response so we can mark success/failure based on import status
            with self.client.post(
                    "/tracker?async=false",
                    name="POST /tracker (create traveler)",
                    json=payload,
                    headers=self.headers,
                    timeout=30,
                    catch_response=True,
            ) as response:
                try:
                    body = response.json()
                except ValueError:
                    body = response.text

                if response.status_code not in (200, 201):
                    msg = f"HTTP {response.status_code}: {str(body)[:1500]}"
                    logger.error("[TRACKER ERROR] %s", msg)
                    response.failure(msg)
                else:
                    if isinstance(body, dict):
                        status = body.get("status")
                        if status and status != "OK":
                            msg = f"Tracker status not OK: {status} | body={str(body)[:1500]}"
                            logger.error("[TRACKER IMPORT NOT OK] %s", msg)
                            response.failure(msg)
                        else:
                            response.success()
                            success = True
                    else:
                        msg = f"Invalid JSON response: {str(body)[:500]}"
                        logger.error("[TRACKER INVALID JSON] %s", msg)
                        response.failure(msg)

            if not success and attempts < MAX_RETRIES:
                # Simple linear backoff; you can tune this
                time.sleep(0.5 * attempts)

        # Optional: fetch enrollment to read health flag (GREEN/YELLOW/RED)
        if success:
            self.client.get(
                f"/tracker/enrollments/{enrollment_uid}"
                "?fields=trackedEntity,program,enrolledAt,"
                "events[dataValues[dataElement,value]]",
                name="GET /tracker/enrollments (health flag)",
                headers=self.headers,
                timeout=30,
            )
            self.created_travelers.append((tei_uid, enrollment_uid))
            logger.info(
                "Checked health status flag for tei uid = " + tei_uid
            )

        op_duration_us = (time.perf_counter_ns() - op_start_ns) // 1000
        record_operation(op_name, attempts, success, op_duration_us)

    # You can later add another @task for updates, e.g.:
    # @task(2)
    # def update_traveler(self):
    #     """
    #     Example: send a new clinical event or update attributes
    #     for a TEI created earlier in this session.
    #     """
    #     if not self.created_travelers:
    #         return
    #     tei_uid, enrollment_uid = random.choice(self.created_travelers)
    #     # Build an updated payload for this TEI/enrollment and POST /tracker again.
    #     # The same retry + record_operation pattern can be reused here.
