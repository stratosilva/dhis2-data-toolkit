"""
Move patients from the Nigeria NCD instance into the Egypt Hypertension &
Diabetes programme.

Real patients are fetched from Nigeria, anonymised (name, phone, PHC ID,
National ID) and re-expressed against the Egyptian metadata model: Egyptian
National ID with embedded demographics, governorate/district option sets,
medication history at enrolment, and visits following the Egyptian treatment
protocol.

What is preserved from the source: sex, date of birth, enrolment date, the
number and dates of visits, and NCD patient status.

What is generated: identity (names, phone, PHC ID, National ID), geography,
anthropometry, and the clinical trajectory. The clinical values are generated
rather than copied because they must satisfy Egyptian thresholds and the
treatment-protocol ladder, which the Nigeria data does not follow.

Program rules run server-side on import: BMI, CVD risk score and risk
classification are calculated by DHIS2 and are deliberately not sent.

Usage:
    python move_patients_egypt.py --after 2025-01-01
    python move_patients_egypt.py --after 2025-01-01 --resume
    python move_patients_egypt.py --after 2025-01-01 --dry-run --limit 5
"""

import argparse
import calendar
import hashlib
import json
import os
import random
import string
import sys
from datetime import date, datetime, timedelta

from dhis2 import RequestException as DHIS2RequestException
from faker import Faker
from logzero import logfile, logger
from requests.exceptions import RequestException as RequestsRequestException

from tracker_common import (
    age_from_dob,
    guard_destructive,
    deterministic_mapping,
    delete_resume_file,
    dob_from_age,
    load_api,
    load_resume_uid,
    parse_dob,
    post_chunked_tracked_entities,
    reindex,
    remove_created_updated,
    save_last_uid,
)

RESUME_FILE = 'resume_uid_egypt.txt'
FAILED_TEIS_FILE = 'failed_teis_egypt.jsonl'
LOG_FILE = './move_patients_egypt.log'

SOURCE_ENV_PREFIX = 'NIGERIA_NCD'
# Sandbox 3, not production. These scripts purge and rewrite data.
TARGET_ENV_PREFIX = 'EGYPT_SANDBOX3'

SOURCE_PROGRAM = 'pMIglSEqPGS'
TARGET_PROGRAM = 'pMIglSEqPGS'
TARGET_PROGRAM_STAGE = 'anb2cjLx3WM'
TARGET_TRACKED_ENTITY_TYPE = 'MCPQUTHX1Ze'  # Person

# DHIS2 stores multi-select option set values as comma-separated codes.
MULTI_VALUE_SEPARATOR = ','

# Added to every per-patient seed so --seed varies the whole run while
# keeping each patient's generated data reproducible across runs.
RUN_SALT = 0

# Whether generated patients and events carry UIDs derived from the run salt.
#
# Off by default. Deterministic UIDs let a re-run address the same records, but
# the workflow is create-only: a purge soft-deletes, DHIS2 refuses to reuse a
# soft-deleted UID (E1082/E1114), and a deterministic run after a purge
# therefore fails on every patient. Letting the server mint UIDs sidesteps
# that; --deterministic-uids opts back in for anyone who wants idempotency on
# a server that has never had these UIDs deleted.
DETERMINISTIC_UIDS = False

fake = Faker()


# ---------------------------------------------------------------------------
# Target metadata: tracked entity attributes
# ---------------------------------------------------------------------------

TEA = {
    'national_id':        'a1N4sJukutU',
    'given_name':         'sB1IHYu2xQT',
    'middle_name':        'wS7QQnuWCtc',
    'family_name':        'ENRjVGxVL6l',
    'phone':              'YRDy9xy9jD0',
    'dob':                'NI0QRzJvQ0k',
    'sex':                'oindugucx72',
    'has_hypertension':   'jCRIT4GMMOS',
    'took_bp_meds':       'l5yxkxHgTw9',
    'has_diabetes':       'v4DnYfXn9Mu',
    'took_dm_meds':       'Xij8fyJKRX7',
    'past_dm_drugs':      'OEG2DI4G2RG',
    'past_htn_drugs':     'H35mkQTlqAY',
    'history_mi':         'oklF6MuAGaq',
    'history_ckd':        'bANIHEeQqEZ',
    'history_stroke':     'S4Pcuj5Eua7',
    'ncd_status':         'fI1P3Mg1zOZ',
    'consent':            'YJGACwhN0St',
    'follow_up_status':   'rgeuEnAI0nj',
    'smoking':            'hXU9UgIMQes',
    'district':           'InXHTXWxKR2',
    'governorate':        'mAWWEdw00l7',
    'phc_id':             'r7EHikpEWkh',
    'age_header':         'ea5w3mkinZo',
    'height':             'hTARCBirVLi',
}

# Deliberately not populated:
#   A6Hb0Kvg4vb Address, nDH0qmLk5xE Village/city - below district, not needed
#   xeeayVAQ9K5 / uTZ8FzaioDI "Other past drugs" free text - OTHER is never chosen
#   azB2Ab4o9Mm Weight at enrolment - weight is captured per visit for BMI

DISTRICT_OPTION_SET = 'zj1FfMoKhhC'
GOVERNORATE_OPTION_SET = 'CXjVw7UgWU0'


# ---------------------------------------------------------------------------
# Target metadata: visit data elements
# ---------------------------------------------------------------------------

DE = {
    'systolic':           'IxEwYiq1FTq',
    'diastolic':          'yNhtHKtKkO1',
    'amlodipine':         'eGY7e5Ttbys',   # monotherapy
    'amlo_valsartan':     'bSTmAUelYYz',   # combined pill
    'hctz':               'D8FWXAtCuL2',
    'dm_measure_type':    'cE3KOFmoYbZ',
    'blood_sugar_unit':   'Xjrw5pbxBJw',
    'fbs_mgdl':           'oOHbZP28H2l',
    'hba1c':              'JZlJfx3oihw',
    'rbs_mgdl':           'ywITVJvILBR',
    'ppbs_mgdl':          'LktTflPfk1z',
    'height_cm':          'BCMObXpMEzk',
    'weight_kg':          'uQwZbz0kUkZ',
    'total_chol_mgdl':    'eNqHpd5y3zk',
    'total_chol_mmol':    'pSqsIKFb8NX',
    'hdl':                'A0R0OtmtKeT',
    'ldl':                'wYsjlYSXaPC',
    'triglycerides':      'YEkRDSlQK5W',
    'statin_prescribed':  'CBkUvBbOIKz',
    'statin_drug':        'bkJK1Xib0bs',
    'antidiabetics':      'YQnlNXxS19B',
    'insulin_name':       'FTKIOSgJPK4',
    'insulin_dose':       'RUCjJ3cGhB6',
    'insulin_frequency':  'm3lkE1CX10X',
    'scheduling_done':    'Mj9jx9JLmyl',
    'bmi':                'J2YWUFBEyvZ',
}

# Deliberately not populated:
#   qwCWN0aLHAi Risk Score, m5WImperUYv Risk Classification - program rules
#   CEEvww5FPbm / Lh9mvB8ASaB Diagnose diabetes/hypertension - prompts
#   DKKPQKIO9qN Clinical override - internal
#   drY9ZQ0PEEw / uP4PTYGDcx1 "Other medication" free text
#   t8004lkeOhy Number of tablets, q362A7evMYt Result of call,
#   MZkqsWH2KSe Remove from overdue - call-centre workflow, not migrated
#   v7nC8vSTzwh / n6k7wJQ1daN / zgxvIQneRRX mmol/L blood sugar - the Egyptian
#     form locks RBS/FBS to mg/dL


# ---------------------------------------------------------------------------
# Egyptian treatment protocol
# ---------------------------------------------------------------------------

# Step 1 has two entry points: the combined pill, or amlodipine monotherapy
# which is reserved for the very elderly (and pregnancy, which is not modelled
# because the source carries no pregnancy status).
PROTOCOL_STEPS = {
    1: {'amlo_valsartan': 'AMLO_5_VAL_80'},
    2: {'amlo_valsartan': 'AMLO_10_VAL_160'},
    3: {'amlo_valsartan': 'AMLO_10_VAL_160', 'hctz': 'HCTZ_25MG'},
    4: {'amlo_valsartan': 'AMLO_10_VAL_160', 'hctz': 'HCTZ_50MG'},
}
AMLODIPINE_MONOTHERAPY = 'AMLODIPINE_5MG'

# Chance of stepping up when a visit reads above target.
ESCALATION_PROBABILITY = 0.45
AMLODIPINE_MONOTHERAPY_MIN_AGE = 70

# The CVD risk charts only cover 40-75, and the Egypt override rules collide
# above 75 (see eligible_dob). Patients are therefore migrated inside this
# band, apart from a small deliberate cohort below it.
CVD_RISK_MIN_AGE = 40
CVD_RISK_MAX_AGE = 75
YOUNG_COHORT_FRACTION = 0.01
YOUNG_COHORT_AGES = (30, 35)

# Egypt treats > 130/80 as raised. Hypertensive patients therefore never read
# below that: "at target" here means mildly raised rather than normal.
# Egypt treats 130/80 as the target: at or above it the patient is not
# controlled. A hypertensive patient who responds to treatment must therefore
# read BELOW 130/80, which is what the "% Controlled BP (<130/80)" dashboard
# counts. Three bands, so the population splits the way a real one does:
BP_NORMAL = ((105, 125), (65, 78))            # never had hypertension
BP_HTN_AT_TARGET = ((112, 128), (70, 79))     # on treatment, at target
BP_HTN_ABOVE_TARGET = ((130, 139), (80, 89))  # above target, not stage 2
BP_HTN_UNCONTROLLED = ((140, 179), (90, 109))  # frankly uncontrolled

STATIN_CODES = [
    'ATORVASTATIN_10MG', 'ATORVASTATIN_20MG', 'ATORVASTATIN_40MG',
    'ROSUVASTATIN_10MG', 'ROSUVASTATIN_20MG',
]

ANTIDIABETIC_CODES = [
    'METFORMIN_500MG_OD', 'METFORMIN_500MG_BD', 'METFORMIN_500MG_TDS',
    'METFORMIN_850MG_OD', 'METFORMIN_850MG_BD',
    'METFORMIN_1000MG_OD', 'METFORMIN_1000MG_BD',
    'METFORMIN_1000MG_XR_OD', 'METFORMIN_2000MG_XR_OD',
    'GLIBENCLAMIDE_2POINT5MG_OD', 'GLIBENCLAMIDE_5MG_OD', 'GLIBENCLAMIDE_10MG_OD',
    'GLIMEPIRIDE_1MG_OD', 'GLIMEPIRIDE_2MG_OD', 'GLIMEPIRIDE_3MG_OD', 'GLIMEPIRIDE_4MG_OD',
    'GLICLAZIDE_MR_30MG_OD', 'GLICLAZIDE_MR_60MG_OD',
    'VILDAGLIPTIN_50MG_OD', 'VILDAGLIPTIN_50MG_BD',
    'PIOGLITAZONE_15MG_OD', 'PIOGLITAZONE_30MG_OD',
    'DAPAGLIFLOZIN_5MG_OD', 'DAPAGLIFLOZIN_10MG_OD',
    'EMPAGLIFLOZIN_10MG_OD', 'EMPAGLIFLOZIN_25MG_OD',
]

PAST_ANTIDIABETIC_CODES = [
    'METFORMIN_500MG', 'METFORMIN_850MG', 'METFORMIN_1000MG',
    'GLIBENCLAMIDE_5MG',
    'GLIMEPIRIDE_1MG', 'GLIMEPIRIDE_2MG', 'GLIMEPIRIDE_3MG', 'GLIMEPIRIDE_4MG',
    'GLICLAZIDE_30MG', 'GLICLAZIDE_60MG', 'GLICLAZIDE_80MG',
    'VILDAGLIPTIN_50MG', 'SITAGLIPTIN_50MG', 'SITAGLIPTIN_100MG',
    'LINAGLIPTIN_5MG', 'SAXAGLIPTIN_5MG',
    'DAPAGLIFLOZIN_5MG', 'DAPAGLIFLOZIN_10MG',
    'EMPAGLIFLOZIN_10MG', 'EMPAGLIFLOZIN_25MG',
    'CANAGLIFLOZIN_100MG', 'CANAGLIFLOZIN_300MG',
    'PIOGLITAZONE_15MG', 'PIOGLITAZONE_30MG', 'PIOGLITAZONE_45MG',
    'REPAGLINIDE_1MG', 'REPAGLINIDE_2MG',
    'ACARBOSE_50MG', 'ACARBOSE_100MG',
]

PAST_ANTIHYPERTENSIVE_CODES = [
    'AMLODIPINE_5MG', 'AMLODIPINE_10MG',
    'LOSARTAN_50MG', 'LOSARTAN_100MG',
    'VALSARTAN_80MG', 'VALSARTAN_160MG',
    'LISINOPRIL_5MG', 'LISINOPRIL_10MG', 'LISINOPRIL_20MG',
    'ENALAPRIL_5MG', 'ENALAPRIL_10MG', 'ENALAPRIL_20MG',
    'RAMIPRIL_2_5MG', 'RAMIPRIL_5MG', 'RAMIPRIL_10MG',
    'BISOPROLOL_2_5MG', 'BISOPROLOL_5MG', 'BISOPROLOL_10MG',
    'CARVEDILOL_6_25MG', 'CARVEDILOL_12_5MG', 'CARVEDILOL_25MG',
    'METOPROLOL_25MG', 'METOPROLOL_50MG', 'METOPROLOL_100MG',
    'ATENOLOL_25MG', 'ATENOLOL_50MG', 'ATENOLOL_100MG',
    'HYDROCHLOROTHIAZIDE_12_5MG', 'HYDROCHLOROTHIAZIDE_25MG',
    'CHLORTHALIDONE_12_5MG', 'CHLORTHALIDONE_25MG',
    'INDAPAMIDE_1_5MG', 'INDAPAMIDE_2_5MG',
    'SPIRONOLACTONE_25MG', 'SPIRONOLACTONE_50MG',
    'DOXAZOSIN_1MG', 'DOXAZOSIN_2MG', 'DOXAZOSIN_4MG',
    'METHYLDOPA_250MG', 'METHYLDOPA_500MG',
]

# Anti-hypertensive class per molecule, so a history does not combine two
# drugs of the same class (two ACE inhibitors, or an ACE inhibitor with an ARB).
ANTIHYPERTENSIVE_CLASS = {
    'AMLODIPINE': 'CCB',
    'LOSARTAN': 'RAS', 'VALSARTAN': 'RAS',
    'LISINOPRIL': 'RAS', 'ENALAPRIL': 'RAS', 'RAMIPRIL': 'RAS',
    'BISOPROLOL': 'BB', 'CARVEDILOL': 'BB', 'METOPROLOL': 'BB', 'ATENOLOL': 'BB',
    'HYDROCHLOROTHIAZIDE': 'THIAZIDE', 'CHLORTHALIDONE': 'THIAZIDE',
    'INDAPAMIDE': 'THIAZIDE',
    'SPIRONOLACTONE': 'MRA',
    'DOXAZOSIN': 'ALPHA', 'PRAZOSIN': 'ALPHA',
    'METHYLDOPA': 'CENTRAL', 'CLONIDINE': 'CENTRAL',
}

# Insulin regimens follow WHO practice: basal once daily, premixed twice daily,
# rapid-acting with meals. Doses stay well under the 100-unit form limit.
INSULIN_REGIMENS = {
    'basal': {
        'names': ['INSULIN_GLARGINE_100', 'INSULIN_GLARGINE_300',
                  'INSULIN_DETEMIR', 'INSULIN_DEGLUDEC'],
        'frequency': 'OD',
        'dose': (10, 40),
    },
    'premixed': {
        'names': ['INSULIN_MIXTARD_70_30'],
        'frequency': 'BD',
        'dose': (10, 30),
    },
    'rapid': {
        'names': ['INSULIN_ASPART', 'INSULIN_LISPRO',
                  'INSULIN_GLULISINE', 'INSULIN_REGULAR'],
        'frequency': 'TDS',
        'dose': (4, 12),
    },
}

NCD_STATUS_MAP = {'UNRESPONSIVE': 'INACTIVE'}
VALID_NCD_STATUS = {'ACTIVE', 'TRANSFER', 'DIED', 'INACTIVE'}
FOLLOW_UP_STATUSES = ['UPCOMING_VISIT', 'OVERDUE_PENDING_CALL', 'CALLED']

SCHEDULED_VISIT_INTERVAL_DAYS = 28
SCHEDULED_STATUSES = {'SCHEDULE', 'OVERDUE', 'SKIPPED'}

# A patient who keeps their appointment comes back in 25-28 days; the visit is
# booked for 28, so this reads as punctual or slightly early. A patient who
# comes back late shows up well after the due date.
PUNCTUAL_GAP_DAYS = (25, 28)
LATE_GAP_DAYS = (45, 200)

DEFAULT_VISITS_MIN = 1
# A safety cap, not a target: the visit count is meant to fall out of how long
# the patient has been enrolled. Set low enough to bind and a long-standing
# patient gets their monthly visits truncated and then a single recent one,
# leaving a fabricated year-long gap in the middle.
DEFAULT_VISITS_MAX = 60

DEFAULT_WINDOW_MONTHS = 12

# How far before the start of the window a patient may have enrolled. Every
# dashboard indicator of the form "enrolled more than three months ago and
# seen recently" needs patients whose enrolment predates the window, so the
# cohort cannot all start on the same day.
ENROLLMENT_LEAD_DAYS = 180

# A dashboard needs roughly a quarter of history before its trend lines mean
# anything.
MIN_USEFUL_WINDOW_DAYS = 90


# ---------------------------------------------------------------------------
# Population mix. Tuned to give a demo dataset that covers the scenarios the
# risk plugin and the protocol nudges need to see.
# ---------------------------------------------------------------------------

# Every proportion the generated population is steered by. Override any of
# them on the command line:
#
#   --mix controlled=0.65 --mix diabetic=0.5 --mix smoker=25
#   --mix-file egypt_mix.json
#
# Values may be given as fractions (0.65) or percentages (65).
DEFAULT_MIX = {
    # The three dashboard headline rates are BANDS, not points: each calendar
    # month picks its own value inside the band, so the trend lines fluctuate
    # instead of running flat. All three are shares of the whole cohort, which
    # is how the dashboard tiles count them.
    #
    # Controlled = BP below the Egyptian target of 130/80.
    'controlled_min': 0.55,
    'controlled_max': 0.65,
    # Uncontrolled = BP at or above 140/90. Whatever is left over between the
    # two sits above target but below stage 2 (130-139/80-89).
    'uncontrolled_min': 0.20,
    'uncontrolled_max': 0.30,
    # No visit recorded in the last three months.
    'overdue_min': 0.05,
    'overdue_max': 0.25,
    'hypertensive': 0.75,
    'diabetic': 0.55,
    # Share of diabetics whose latest glycaemic reading is in range.
    'dm_controlled': 0.55,
    # Sex split of the generated population.
    'female': 0.55,
    # Lateness, as a share of patients. This is about coming back late for a
    # booked appointment - the gap to the previous visit stretches well past
    # the 28-day interval - and is separate from `overdue`, which is the
    # dashboard's "no visit in the last three months".
    #
    # The second and third visits are called out because that is where a
    # programme actually loses people; every other visit is kept punctual.
    'late_at_visit_2': 0.20,
    'late_at_visit_3': 0.15,
    'late_all_visits': 0.05,
    # Age tails. The rest of the cohort sits inside the 40-75 window the CVD
    # risk plugin can score.
    'age_under_40': 0.04,
    'age_over_75': 0.03,
    # Lost to follow-up: enrolled before the window and last seen before it
    # too, so they appear in cumulative registrations but have no visit
    # anywhere in the reporting window.
    'lost_to_followup': 0.03,
    # Patient outcomes, applied as an NCD Patient Status update after import.
    # Program rule GYmydeVoxDI assigns ACTIVE when the enrollment is created,
    # so these cannot be set in the creating payload; they are a second pass.
    'died': 0.005,
    'transferred': 0.02,
    # Share of patients whose booked appointment has already passed, which is
    # what drives TEA rgeuEnAI0nj to OVERDUE_PENDING_CALL. Patients with no
    # visit in three months are always in this group; the rest get their next
    # appointment in the future, as a clinic that is keeping up would.
    #
    # Left to mechanics this came out at ~68%: the appointment was always the
    # last visit plus 28 days, and non-overdue patients were last seen up to 89
    # days ago, so most of them were already past due.
    'appointment_overdue': 0.20,
    # Share of patients for whom a statin was indicated but not prescribed, so
    # the "prescribed statins" indicator is not a flat 100%.
    'statin_missed': 0.02,
    'took_meds_before': 0.45,      # given the matching condition
    'smoker_male': 0.40,
    'smoker_female': 0.05,
    'history_mi': 0.08,
    'history_stroke': 0.05,
    'history_ckd': 0.07,
    'mistitrated': 0.20,           # not escalated as the protocol requires
    'labs_at_visit': 0.45,
    'insulin_if_diabetic': 0.25,
    'high_cholesterol': 0.40,
}

# Populated from DEFAULT_MIX plus any CLI override, in main().
MIX = dict(DEFAULT_MIX)


def p(name):
    """A probability from the population mix."""
    return MIX[name]


def normalise_mix_value(key, raw):
    """Accept either a fraction (0.65) or a percentage (65)."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"mix value for '{key}' is not a number: {raw!r}")

    if value < 0:
        raise ValueError(f"mix value for '{key}' cannot be negative: {value}")
    if value > 100:
        raise ValueError(f"mix value for '{key}' cannot exceed 100: {value}")

    return value / 100.0 if value > 1 else value


def apply_mix_overrides(pairs=None, mix_file=None):
    """Fold --mix-file then --mix overrides into MIX, validating every key."""
    overrides = {}

    if mix_file:
        try:
            with open(mix_file, 'r', encoding='utf-8') as f:
                overrides.update(json.load(f))
        except (OSError, json.JSONDecodeError) as e:
            raise ValueError(f"cannot read --mix-file {mix_file}: {e}")

    for pair in pairs or []:
        key, separator, raw = pair.partition('=')
        if not separator:
            raise ValueError(f"--mix expects key=value, got {pair!r}")
        overrides[key.strip()] = raw.strip()

    unknown = sorted(set(overrides) - set(DEFAULT_MIX))
    if unknown:
        raise ValueError(
            f"unknown mix key(s) {unknown}. Valid keys: {sorted(DEFAULT_MIX)}"
        )

    for key, raw in overrides.items():
        MIX[key] = normalise_mix_value(key, raw)

    return {k: MIX[k] for k in overrides}

BMI_BANDS = [
    ('normal', (18.5, 24.9), 0.25),
    ('overweight', (25.0, 29.9), 0.35),
    ('obese', (30.0, 39.5), 0.40),
]

# Egyptian adult mean stature, cm.
HEIGHT_BY_SEX = {'MALE': (170.3, 6.8), 'FEMALE': (158.9, 6.2)}

EGYPT_GIVEN_NAMES = {
    'MALE': ['Ahmed', 'Mohamed', 'Mahmoud', 'Mostafa', 'Youssef', 'Omar', 'Khaled',
             'Hassan', 'Hussein', 'Ibrahim', 'Karim', 'Tarek', 'Sherif', 'Amr',
             'Ayman', 'Sayed', 'Mamdouh', 'Ashraf', 'Waleed', 'Ramadan', 'Nasser',
             'Adel', 'Sameh', 'Hany', 'Emad', 'Gamal', 'Salah', 'Yasser'],
    'FEMALE': ['Fatma', 'Aisha', 'Mona', 'Heba', 'Nour', 'Salma', 'Yasmin',
               'Amira', 'Dalia', 'Rania', 'Hoda', 'Mervat', 'Nadia', 'Shaimaa',
               'Marwa', 'Eman', 'Sara', 'Doaa', 'Ghada', 'Asmaa', 'Nesma',
               'Samah', 'Zeinab', 'Safaa', 'Hanan', 'Nermin', 'Reham'],
}

EGYPT_FAMILY_NAMES = [
    'Abdel Rahman', 'Abdel Aziz', 'Hassan', 'Ibrahim', 'Mahmoud', 'Ali',
    'Mohamed', 'El Sayed', 'Farouk', 'Mansour', 'Shawky', 'Fahmy', 'Zaki',
    'Ramadan', 'Soliman', 'Kamel', 'Nagy', 'Riad', 'Sabry', 'Tawfik',
    'Abdallah', 'El Masry', 'El Sherbiny', 'Shokry', 'Helmy', 'Bakr',
    'Gaber', 'Hegazy', 'Lotfy', 'Nabil', 'Rashad', 'Sedky', 'Wahba',
]

EGYPT_MOBILE_PREFIXES = ['10', '11', '12', '15']


# ---------------------------------------------------------------------------
# Identity generation
# ---------------------------------------------------------------------------

def egyptian_national_id(dob_iso, sex, governorate_digits):
    """
    Build a 14-digit Egyptian National ID whose embedded demographics match the
    patient we are sending.

        C YYMMDD GG SSS X K
        C   century: 2 = 1900-1999, 3 = 2000-2099
        GG  governorate code
        SSS serial
        X   gender: odd male, even female
        K   verification digit

    The Egyptian platform checks the 14-digit length and extracts date of
    birth, governorate and sex from the positions above; it does not perform a
    real checksum, so K is computed with the conventional weighted algorithm
    purely so the number looks well formed.
    """
    born = datetime.strptime(dob_iso, "%Y-%m-%d").date()
    century_digit = '2' if born.year < 2000 else '3'
    yymmdd = born.strftime("%y%m%d")

    serial = f"{random.randint(0, 999):03d}"

    gender_digit = random.choice([1, 3, 5, 7, 9] if sex == 'MALE' else [0, 2, 4, 6, 8])

    first_13 = f"{century_digit}{yymmdd}{governorate_digits}{serial}{gender_digit}"

    weights = [2, 7, 6, 5, 4, 3, 2, 7, 6, 5, 4, 3, 2]
    total = sum(int(d) * w for d, w in zip(first_13, weights))
    check_digit = (11 - (total % 11)) % 11
    if check_digit == 10:
        check_digit = 0

    return f"{first_13}{check_digit}"


def egyptian_phone():
    """An Egyptian mobile number in international format."""
    return f"+20{random.choice(EGYPT_MOBILE_PREFIXES)}{random.randint(0, 99999999):08d}"


def phc_id():
    """An arbitrary primary health centre identifier: letters plus digits."""
    return fake.bothify(text='??-####', letters='ABCDEFGHJKLMNPRSTUVWXYZ').upper()


def sample_height(sex):
    """Adult stature in cm, with enough spread to move BMI around."""
    mean, sd = HEIGHT_BY_SEX.get(sex, HEIGHT_BY_SEX['MALE'])
    return round(min(max(random.gauss(mean, sd), 140.0), 196.0), 1)


def sample_bmi_target():
    labels, bands, weights = zip(*BMI_BANDS)
    band = random.choices(bands, weights=weights, k=1)[0]
    return round(random.uniform(*band), 1)


def weight_for(height_cm, bmi):
    """Weight in kg implied by a height and BMI, clamped to the form's 40-250."""
    weight = bmi * (height_cm / 100.0) ** 2
    return round(min(max(weight, 40.0), 250.0), 1)


# ---------------------------------------------------------------------------
# Medication helpers
# ---------------------------------------------------------------------------

def molecule_key(code):
    """
    The drug identity of an option code, ignoring dose and frequency, so that
    two strengths of the same molecule are never selected together.

        METFORMIN_1000MG_XR_OD    -> METFORMIN
        GLIBENCLAMIDE_2POINT5MG_OD-> GLIBENCLAMIDE
        INSULIN_GLARGINE_100IU_ML -> INSULIN_GLARGINE
        LIRAGLUTIDE_0_6MG         -> LIRAGLUTIDE
    """
    kept = []
    for token in code.split('_'):
        if any(ch.isdigit() for ch in token):
            break
        kept.append(token)
    return '_'.join(kept) if kept else code.split('_')[0]


def pick_distinct_drugs(codes, count, class_map=None):
    """
    Choose `count` codes that do not repeat a molecule, and - when a class map
    is supplied - do not repeat a therapeutic class either.
    """
    shuffled = random.sample(codes, len(codes))
    chosen, seen_molecules, seen_classes = [], set(), set()

    for code in shuffled:
        molecule = molecule_key(code)
        if molecule in seen_molecules:
            continue
        drug_class = class_map.get(molecule) if class_map else None
        if drug_class is not None and drug_class in seen_classes:
            continue

        chosen.append(code)
        seen_molecules.add(molecule)
        if drug_class is not None:
            seen_classes.add(drug_class)

        if len(chosen) == count:
            break

    return chosen


def multi_value(codes):
    return MULTI_VALUE_SEPARATOR.join(codes)


# ---------------------------------------------------------------------------
# Patient profile
# ---------------------------------------------------------------------------

def draw_appointment_overdue():
    """
    Whether this patient's booked appointment should already have passed.

    The mix key is the TOTAL share of patients showing OVERDUE_PENDING_CALL,
    not an extra slice on top. Patients with no visit in three months, and
    those lost to follow-up, are past due by definition, so this only has to
    top the remainder up to the target - otherwise the three sources union
    together and the figure overshoots (20% asked, 35% delivered).
    """
    target = p('appointment_overdue')
    # Use the rate this run actually drew, not the band midpoint: band_value is
    # deterministic per run, so the estimate matches what build_profile used.
    # The midpoint overestimated it and the top-up came out too small (20%
    # asked, 13% delivered).
    already = band_value('overdue', 'cohort') + p('lost_to_followup')

    if target <= already:
        return False
    return random.random() < (target - already) / max(1.0 - already, 1e-9)


def draw_ncd_status():
    """
    The patient's NCD Patient Status.

    Applied after import rather than in the creating payload, because program
    rule GYmydeVoxDI assigns ACTIVE when the enrollment is created.
    """
    roll = random.random()
    if roll < p('died'):
        return 'DIED'
    if roll < p('died') + p('transferred'):
        return 'TRANSFER'
    return 'ACTIVE'


def draw_conditions():
    """
    Draw (hypertensive, diabetic) so that both prevalences match the mix
    exactly while as few patients as possible end up with neither condition.

    Drawing the two independently and then forcing "at least one condition"
    inflated hypertension by the whole neither-bucket: 75% asked became 86%.
    Sampling the joint distribution instead keeps both marginals honest.
    """
    p_htn, p_dm = p('hypertensive'), p('diabetic')

    both = max(p_htn + p_dm - 1.0, 0.0)
    htn_only = p_htn - both
    dm_only = p_dm - both
    neither = max(1.0 - p_htn - p_dm, 0.0)

    bucket = random.choices(
        ('both', 'htn', 'dm', 'neither'),
        weights=(both, htn_only, dm_only, neither),
        k=1,
    )[0]

    return bucket in ('both', 'htn'), bucket in ('both', 'dm')


def band_value(name, key):
    """
    Pick a value inside the mix band `<name>_min`..`<name>_max`, stable for a
    given key.

    Keying on the calendar month makes every patient seen in that month share
    the same target, so the dashboard's monthly trend line moves from month to
    month instead of sitting flat, while still staying inside the band.
    """
    low, high = MIX[f'{name}_min'], MIX[f'{name}_max']
    if high < low:
        low, high = high, low

    digest = hashlib.sha256(f"{name}:{key}:{RUN_SALT}".encode('utf-8')).hexdigest()
    fraction = int(digest[:8], 16) / 0xFFFFFFFF

    return low + fraction * (high - low)


def month_of(iso_date):
    return iso_date[:7]


BP_CONTROLLED, BP_ABOVE_TARGET, BP_UNCONTROLLED = 'controlled', 'above', 'uncontrolled'


def monthly_bp_shares(month_key):
    """
    The month's target shares, applied directly to HYPERTENSIVE patients.

    The bands are NOT rescaled across the whole cohort. The HTN dashboard
    indicators count hypertensive patients under care - "HTN - Patients under
    care with controlled BP" over "HTN - Patients under care" - so a patient
    who never had hypertension is not in either the numerator or the
    denominator. Rescaling to make the whole-cohort figure hit the band pushed
    the hypertensive rate down to ~47%, and the dashboard duly reported 45%
    against a 55-65% band.
    """
    controlled_htn = band_value('controlled', month_key)
    uncontrolled_htn = band_value('uncontrolled', month_key)

    if p('hypertensive') <= 0:
        return 1.0, 0.0, 0.0

    controlled_htn = min(max(controlled_htn, 0.0), 1.0)
    uncontrolled_htn = min(max(uncontrolled_htn, 0.0), 1.0)

    if controlled_htn + uncontrolled_htn > 1.0:
        # The two bands cannot both be met at this prevalence; keep the
        # controlled figure, which is the one the dashboard leads with.
        uncontrolled_htn = 1.0 - controlled_htn

    above_htn = 1.0 - controlled_htn - uncontrolled_htn
    return controlled_htn, above_htn, uncontrolled_htn


def bp_state_for_visit(profile, month_key):
    """The control state of one visit, drawn from that month's targets."""
    if not profile['hypertensive']:
        return BP_CONTROLLED

    controlled, above, uncontrolled = monthly_bp_shares(month_key)
    return random.choices(
        (BP_CONTROLLED, BP_ABOVE_TARGET, BP_UNCONTROLLED),
        weights=(controlled, above, uncontrolled),
        k=1,
    )[0]


def check_mix_feasible():
    """Warn when the requested bands cannot be met at this prevalence."""
    p_htn = p('hypertensive')
    never_htn = 1.0 - p_htn

    if MIX['controlled_min'] < never_htn:
        logger.warning(
            f"controlled_min={MIX['controlled_min']:.0%} is below the "
            f"{never_htn:.0%} of patients who are not hypertensive and always "
            f"read under 130/80, so the controlled rate cannot fall that low. "
            f"Raise hypertensive prevalence to go lower."
        )

    worst = (MIX['controlled_max'] - never_htn) / p_htn + MIX['uncontrolled_max'] / p_htn \
        if p_htn > 0 else 0
    if worst > 1.0:
        logger.warning(
            f"controlled_max={MIX['controlled_max']:.0%} and "
            f"uncontrolled_max={MIX['uncontrolled_max']:.0%} cannot both be met "
            f"at {p_htn:.0%} hypertensive prevalence; the controlled band wins "
            f"and uncontrolled will come out lower than asked."
        )






def build_profile(sex, dob_iso, district_code):
    """
    Decide everything about this patient up front, so the enrolment attributes
    and every visit tell one consistent story.
    """
    age = age_from_dob(dob_iso)

    # The mix alone decides prevalence.
    #
    # The source diagnosis is deliberately ignored. Nigeria is itself a
    # hypertension-and-diabetes programme, so virtually every source patient
    # carries a raised reading: using that as a signal is not evidence about
    # an individual, it is a constant, and it pushed hypertension prevalence to
    # 92-98% however the mix was set. Prevalence is a property of the
    # population we are asked to produce, not of the source.
    hypertensive, diabetic = draw_conditions()

    height_cm = sample_height(sex)

    profile = {
        'age': age,
        'sex': sex,
        'dob': dob_iso,
        'height_cm': height_cm,
        'bmi_target': sample_bmi_target(),
        'district': district_code,
        'governorate': 'EG' + district_code[2:4],
        'hypertensive': hypertensive,
        'diabetic': diabetic,
        'took_bp_meds': hypertensive and random.random() < p('took_meds_before'),
        'took_dm_meds': diabetic and random.random() < p('took_meds_before'),
        'smoker': random.random() < p('smoker_male' if sex == 'MALE' else 'smoker_female'),
        'history_mi': random.random() < p('history_mi'),
        'history_stroke': random.random() < p('history_stroke'),
        'history_ckd': random.random() < p('history_ckd'),
        'mistitrated': random.random() < p('mistitrated'),
        'high_cholesterol': random.random() < p('high_cholesterol'),
        # Control state at the patient's most recent visit. Drawn here rather
        # than emerging from the titration ladder, so the dashboard percentage
        # matches the mix exactly instead of being a side effect.
        # Per-visit control is drawn from the month's band in build_events;
        # this only records whether the patient ends up controlled, for the
        # run summary.
        'controlled': False,
        'uncontrolled': False,
        'dm_controlled': diabetic and random.random() < p('dm_controlled'),
        'overdue': random.random() < band_value('overdue', 'cohort'),
        'lost_to_followup': random.random() < p('lost_to_followup'),
        'ncd_status': draw_ncd_status(),
        'appointment_overdue': draw_appointment_overdue(),
        'late_always': random.random() < p('late_all_visits'),
        'late_at_2': random.random() < p('late_at_visit_2'),
        'late_at_3': random.random() < p('late_at_visit_3'),
    }

    # Under-40s are not eligible for a CVD risk score, and a history of heart
    # attack / stroke / kidney disease at that age is not plausible in this
    # population, so the deliberate young cohort carries none.
    if age < CVD_RISK_MIN_AGE:
        profile['history_mi'] = False
        profile['history_stroke'] = False
        profile['history_ckd'] = False

    profile['national_id'] = egyptian_national_id(dob_iso, sex, district_code[2:4])

    profile['past_htn_drugs'] = (
        pick_distinct_drugs(PAST_ANTIHYPERTENSIVE_CODES, random.randint(1, 3),
                            ANTIHYPERTENSIVE_CLASS)
        if profile['took_bp_meds'] else []
    )
    profile['past_dm_drugs'] = (
        pick_distinct_drugs(PAST_ANTIDIABETIC_CODES, random.randint(1, 2))
        if profile['took_dm_meds'] else []
    )

    # A patient already on treatment starts further up the ladder.
    profile['start_step'] = 2 if profile['took_bp_meds'] else 1

    # The step at which this patient's blood pressure comes to target. None
    # means never: those patients climb to step 4 and stay uncontrolled.
    # A patient who ends up controlled got there after climbing some of the
    # ladder; one who does not never gets there.
    profile['control_at_step'] = random.choice([1, 2, 2, 3, 3, 4])

    # Amlodipine monotherapy is only defensible in the very elderly.
    profile['amlodipine_monotherapy'] = (
        profile['start_step'] == 1
        and age >= AMLODIPINE_MONOTHERAPY_MIN_AGE
        and random.random() < 0.6
    )

    # Most diabetics improve on treatment; the rest stay poorly controlled.
    profile['dm_improves'] = profile['dm_controlled']

    profile['on_insulin'] = diabetic and random.random() < p('insulin_if_diabetic')
    if profile['on_insulin']:
        regimen_name = random.choices(['basal', 'premixed', 'rapid'], weights=[6, 3, 1], k=1)[0]
        regimen = INSULIN_REGIMENS[regimen_name]
        profile['insulin'] = {
            'name': random.choice(regimen['names']),
            'frequency': regimen['frequency'],
            'dose': random.randint(*regimen['dose']),
        }

    # A statin is indicated by high cholesterol, diabetes or a CVD history.
    # A small share of those are missed, which is both realistic and keeps the
    # "prescribed statins" indicator off a flat 100%.
    statin_indicated = (profile['high_cholesterol'] or diabetic
                        or profile['history_mi'] or profile['history_stroke'])
    profile['statin_missed'] = statin_indicated and random.random() < p('statin_missed')
    profile['statin'] = (random.choice(STATIN_CODES)
                         if statin_indicated and not profile['statin_missed']
                         else None)

    profile['antidiabetics'] = (
        pick_distinct_drugs(ANTIDIABETIC_CODES, random.randint(1, 3))
        if diabetic else []
    )

    return profile


# ---------------------------------------------------------------------------
# Enrolment attributes
# ---------------------------------------------------------------------------

def yes_no(flag):
    return 'YES' if flag else 'NO'


def build_attributes(profile, ncd_status):
    # ncd_status is accepted for traceability but no longer sent (see below).
    """Every tracked entity attribute the Egyptian enrolment expects."""
    sex = profile['sex']
    attributes = [
        {'attribute': TEA['national_id'], 'value': profile['national_id']},
        {'attribute': TEA['given_name'], 'value': random.choice(EGYPT_GIVEN_NAMES[sex])},
        {'attribute': TEA['middle_name'], 'value': random.choice(EGYPT_GIVEN_NAMES['MALE'])},
        {'attribute': TEA['family_name'], 'value': random.choice(EGYPT_FAMILY_NAMES)},
        {'attribute': TEA['phone'], 'value': egyptian_phone()},
        {'attribute': TEA['dob'], 'value': profile['dob']},
        {'attribute': TEA['sex'], 'value': sex},
        # Age is assigned by a program rule from the date of birth, but the 73
        # CVD-risk rules test A{age} in their conditions, and on import a rule's
        # ASSIGN output is not visible to another rule's condition. Leaving age
        # to the rule left risk score and classification empty on every visit;
        # sending it - the same value the rule computes - makes them populate.
        {'attribute': TEA['age_header'], 'value': str(profile['age'])},
        {'attribute': TEA['phc_id'], 'value': phc_id()},
        {'attribute': TEA['height'], 'value': str(profile['height_cm'])},
        {'attribute': TEA['governorate'], 'value': profile['governorate']},
        {'attribute': TEA['district'], 'value': profile['district']},
        {'attribute': TEA['consent'], 'value': 'true'},
        # TEA['ncd_status'] is assigned by program rule GYmydeVoxDI, which
        # computes ACTIVE for an open enrollment. Carrying the source status
        # over is rejected with E1309 whenever the source said otherwise, so
        # the rule owns this field.
        # Derived from the booked appointment, not drawn at random: the
        # overdue indicators read this attribute, so it has to agree with the
        # scheduled visit's due date.
        {'attribute': TEA['follow_up_status'],
         'value': profile.get('follow_up_status', 'UPCOMING_VISIT')},
        {'attribute': TEA['has_hypertension'], 'value': yes_no(profile['hypertensive'])},
        {'attribute': TEA['has_diabetes'], 'value': yes_no(profile['diabetic'])},
    ]

    # The medication-history questions only appear when the condition is Yes,
    # and the drug multi-select only when the medication answer is Yes.
    if profile['hypertensive']:
        attributes.append({'attribute': TEA['took_bp_meds'],
                           'value': yes_no(profile['took_bp_meds'])})
        if profile['past_htn_drugs']:
            attributes.append({'attribute': TEA['past_htn_drugs'],
                               'value': multi_value(profile['past_htn_drugs'])})

    if profile['diabetic']:
        attributes.append({'attribute': TEA['took_dm_meds'],
                           'value': yes_no(profile['took_dm_meds'])})
        if profile['past_dm_drugs']:
            attributes.append({'attribute': TEA['past_dm_drugs'],
                               'value': multi_value(profile['past_dm_drugs'])})

    # The CVD history attributes are TRUE_ONLY on the target, which accepts
    # 'true' and nothing else, so they are sent only when positive.
    for flag in ('history_mi', 'history_stroke', 'history_ckd'):
        if profile[flag]:
            attributes.append({'attribute': TEA[flag], 'value': 'true'})

    # Smoking is a genuine BOOLEAN, so record a negative answer too: the CVD
    # risk calculation needs to tell "does not smoke" from "not asked".
    attributes.append({'attribute': TEA['smoking'],
                       'value': 'true' if profile['smoker'] else 'false'})

    return attributes


# ---------------------------------------------------------------------------
# Visit construction
# ---------------------------------------------------------------------------

def bp_reading(profile, bp_state):
    """A reading consistent with this visit's control state."""
    if not profile['hypertensive']:
        band = BP_NORMAL
    elif bp_state == BP_CONTROLLED:
        band = BP_HTN_AT_TARGET
    elif bp_state == BP_UNCONTROLLED:
        band = BP_HTN_UNCONTROLLED
    else:
        band = BP_HTN_ABOVE_TARGET

    (sys_lo, sys_hi), (dia_lo, dia_hi) = band
    return random.randint(sys_lo, sys_hi), random.randint(dia_lo, dia_hi)


def antihypertensive_values(profile, step):
    """The data values prescribing the current protocol step."""
    values = {}

    if step == 1 and profile['amlodipine_monotherapy']:
        # Monotherapy and the combined pill are mutually exclusive on the form.
        values[DE['amlodipine']] = AMLODIPINE_MONOTHERAPY
        return values

    for key, code in PROTOCOL_STEPS[step].items():
        values[DE[key]] = code

    return values


def diabetes_values(profile, at_target):
    """One glycaemic measurement, in the units the Egyptian form allows."""
    values = {}
    measure = random.choices(['FBS', 'HBA1C', 'RBS', 'PPBS'], weights=[4, 4, 2, 1], k=1)[0]
    values[DE['dm_measure_type']] = measure

    if measure == 'HBA1C':
        # Percentage, no unit. Diagnostic threshold 6.5, form bounds 3-20.
        hba1c = round(random.uniform(5.6, 6.4) if at_target else random.uniform(6.5, 11.5), 1)
        values[DE['hba1c']] = str(hba1c)
        return values

    # RBS, FBS and PPBS are locked to mg/dL on the Egyptian form.
    values[DE['blood_sugar_unit']] = 'MG_OR_DL'

    if measure == 'FBS':
        # Form bounds 70-600, diagnostic threshold 126.
        reading = random.randint(80, 125) if at_target else random.randint(126, 260)
        values[DE['fbs_mgdl']] = str(reading)
    elif measure == 'RBS':
        # Diagnostic threshold 200.
        reading = random.randint(110, 199) if at_target else random.randint(200, 320)
        values[DE['rbs_mgdl']] = str(reading)
    else:
        reading = random.randint(110, 139) if at_target else random.randint(140, 280)
        values[DE['ppbs_mgdl']] = str(reading)

    return values


def lipid_values(profile):
    """
    A lipid panel in mg/dL, the unit the Egyptian form uses for every lab
    field. Total cholesterol is additionally sent in mmol/L, the same
    measurement expressed in the other unit the programme carries.
    """
    if profile['high_cholesterol']:
        total = random.randint(220, 330)
        ldl = random.randint(140, 230)
        hdl = random.randint(20, 42)
        triglycerides = random.randint(180, 480)
    else:
        total = random.randint(130, 199)
        ldl = random.randint(60, 129)
        hdl = random.randint(45, 85)
        triglycerides = random.randint(60, 160)

    # Only the mg/dL fields are entered by hand. Total cholesterol in mmol/L
    # (DE['total_chol_mmol']) is assigned by program rule dRSNL8k6vk6 from the
    # mg/dL value; supplying our own conversion is rejected with E1307 when it
    # rounds differently from the rule's.
    return {
        DE['total_chol_mgdl']: str(total),
        DE['ldl']: str(ldl),
        DE['hdl']: str(hdl),
        DE['triglycerides']: str(triglycerides),
    }


def build_visit(profile, step, bp_state, dm_at_target, with_labs):
    """The data values for one completed visit."""
    values = {}

    systolic, diastolic = bp_reading(profile, bp_state)
    values[DE['systolic']] = str(systolic)
    values[DE['diastolic']] = str(diastolic)

    # Height is sent on every visit even though rule zoyvkFzuZIC 'ASSIGN
    # Height from TEA to DE' also copies it from the height attribute. That
    # rule is enough in the Capture app, where rules re-evaluate as the form is
    # filled, but not on import: a rule's assignment is not visible to another
    # rule's condition in the same payload. Rule eSOdGrU8dC4 'Reset BMI' fires
    # on `!d2:hasValue(#{Height})`, assigns NULL over the BMI we send, and the
    # event is rejected with E1307 - which also costs the risk score, since
    # that reads BMI. Weight is captured per visit and varies a little.
    values[DE['height_cm']] = str(profile['height_cm'])
    bmi = max(profile['bmi_target'] + random.uniform(-0.8, 0.8), 16.0)
    weight = weight_for(profile['height_cm'], bmi)

    # A BMI of exactly 35.0 matches two WHO-chart risk rules at once - the
    # 30-35 band is written "#{bmi} >= 30 && #{bmi} <= 35" and the top band
    # "#{bmi} >= 35", so 35.0 satisfies both. They assign different risk
    # scores, the import is rejected with E1307, and the whole patient is
    # lost. Every other boundary in those rules is half-open and therefore
    # safe. Nudge the weight until the BMI misses that one value.
    while round(weight / (profile['height_cm'] / 100) ** 2, 1) == 35.0:
        weight = round(weight + 0.4, 1)

    values[DE['weight_kg']] = str(weight)

    # BMI is read-only in the form and assigned by rule BMI_Calculated, but the
    # CVD-risk rules test #{bmi} in their conditions and cannot see another
    # rule's assignment during the same import. Sending the same value the rule
    # derives is what lets risk score and classification populate.
    values[DE['bmi']] = str(round(weight / (profile['height_cm'] / 100) ** 2, 1))

    if profile['hypertensive']:
        values.update(antihypertensive_values(profile, step))

    if profile['diabetic']:
        values.update(diabetes_values(profile, dm_at_target))
        if profile['antidiabetics']:
            values[DE['antidiabetics']] = multi_value(profile['antidiabetics'])
        if profile['on_insulin']:
            insulin = profile['insulin']
            values[DE['insulin_name']] = insulin['name']
            values[DE['insulin_dose']] = str(insulin['dose'])
            values[DE['insulin_frequency']] = insulin['frequency']

    if with_labs:
        values.update(lipid_values(profile))

    if profile['statin']:
        values[DE['statin_drug']] = profile['statin']
        values[DE['statin_prescribed']] = 'true'

    # Every visit ends with an appointment booked.
    values[DE['scheduling_done']] = 'true'

    return [{'dataElement': de, 'value': value} for de, value in values.items()]


def next_step(profile, step, bp_state):
    """Protocol escalation, with a share of patients left wrongly titrated."""
    if not profile['hypertensive']:
        return step

    at_target = bp_state == BP_CONTROLLED
    if at_target:
        # Correct behaviour is to hold. A mistitrated patient may be pushed up
        # anyway.
        if profile['mistitrated'] and random.random() < 0.3:
            return min(step + 1, 4)
        return step

    # Above target: the protocol says escalate. A wrongly titrated patient is
    # left where they are. Everyone else escalates most but not all of the
    # time - some clinical inertia is real, and escalating on every single
    # raised reading marched almost the whole cohort to step 4, which is not
    # what a real treatment-step breakdown looks like.
    if profile['mistitrated']:
        return step
    if random.random() < ESCALATION_PROBABILITY:
        return min(step + 1, 4)
    return step


RECENT_VISIT_WINDOW_DAYS = 90


def shift_visits_for_recency(dated, overdue):
    """
    Slide the whole visit timeline so the latest visit falls inside or outside
    the three-month window the dashboard uses.

    The gaps between visits are left exactly as the source recorded them -
    that irregularity is the part of the Nigeria data worth keeping - while
    the offset decides whether this patient counts as having no recent visit.
    """
    if not dated:
        return dated

    today = date.today()
    last_visit = datetime.strptime(dated[-1][0], "%Y-%m-%d").date()

    if overdue:
        target_age = random.randint(RECENT_VISIT_WINDOW_DAYS + 1,
                                    RECENT_VISIT_WINDOW_DAYS + 150)
    else:
        target_age = random.randint(0, RECENT_VISIT_WINDOW_DAYS - 1)

    delta = (today - timedelta(days=target_age)) - last_visit
    if delta.days == 0:
        return dated

    shifted = []
    for occurred_date, source_event in dated:
        moved = datetime.strptime(occurred_date, "%Y-%m-%d").date() + delta
        shifted.append((moved.isoformat(), source_event))

    return shifted


def dated_from_source_events(source_events, enrollment_date, overdue):
    """Visit dates taken from a Nigeria source patient, slid for recency."""
    dated = []
    for event in source_events:
        occurred = event.get('occurredAt')
        if not occurred:
            continue
        dated.append((occurred[:10], event))
    dated.sort(key=lambda pair: pair[0])

    if not dated:
        # A patient whose source visits carry no date still needs one visit.
        dated = [(enrollment_date[:10], {})]

    return shift_visits_for_recency(dated, overdue)


def visit_is_late(profile, visit_number):
    """Whether the patient came back late for a given visit number."""
    if visit_number < 2:
        return False            # nothing scheduled the first visit
    if profile['late_always']:
        return True
    if visit_number == 2:
        return profile['late_at_2']
    if visit_number == 3:
        return profile['late_at_3']
    return False


def enrolment_window(window_start, enrollment_lead_days, enrol_from, enrol_to,
                     visits_min=2):
    """
    The span enrolments are spread over.

    By default it runs from `enrollment_lead_days` before the data window up to
    a month before today, so the cohort covers the whole period rather than all
    starting on the same day. --enrol-from / --enrol-to override it, which is
    how a weekly run adds new arrivals: point both at the last few days.
    """
    today = date.today()

    first = enrol_from or (window_start - timedelta(days=enrollment_lead_days))
    last = enrol_to or (today - timedelta(days=SCHEDULED_VISIT_INTERVAL_DAYS))

    # Leave room for the minimum number of visits before today. With
    # --visits-min 1 a patient enrolled today is fine: they have their
    # enrolment visit and a booked follow-up, which is exactly what a new
    # arrival looks like.
    latest_possible = today - timedelta(
        days=(max(visits_min, 1) - 1) * SCHEDULED_VISIT_INTERVAL_DAYS)
    if last > latest_possible:
        logger.info(
            f"Latest enrolment pulled back from {last.isoformat()} to "
            f"{latest_possible.isoformat()} to leave room for "
            f"{max(visits_min, 1)} visit(s). Pass --visits-min 1 to allow "
            f"enrolments right up to today."
        )
        last = latest_possible
    if first > last:
        logger.warning(
            f"Enrolment span collapsed to a single day ({last.isoformat()}): "
            f"--enrol-from is later than the latest usable enrolment date. "
            f"Every patient in this batch will enrol on the same day."
        )
        first = last

    return first, last


def spread_enrolment_date(position, batch_size, enrol_first, enrol_last):
    """
    Place one patient's enrolment inside the enrolment span by its position in
    the batch, so N patients cover the span evenly however large N is.

    Spacing comes from the batch size, which is what makes the cohort's
    enrolments - and therefore its visits - spread across the whole period
    instead of clustering.
    """
    span_days = max((enrol_last - enrol_first).days, 0)

    if span_days == 0:
        return enrol_first

    if batch_size <= 1:
        offset = span_days // 2
    else:
        offset = round(position * span_days / (batch_size - 1))

    # A few days of jitter so enrolments are not suspiciously regular.
    offset = min(max(offset + random.randint(-3, 3), 0), span_days)

    return enrol_first + timedelta(days=offset)


def synthetic_visit_dates(profile, enrolled, visits_min, visits_max,
                          window_start=None):
    """
    Visit dates for a generated patient, running forward from an enrolment
    date the caller has already placed inside the window.

    The most recent visit is pinned inside or outside the three-month window
    according to the mix, so the "no visit in three months" share comes out as
    asked. Visits in between run at 25-28 day intervals, stretching out when
    the patient comes back late.

    The visit COUNT therefore falls out of how long the patient has been
    enrolled rather than being drawn independently: a patient enrolled near the
    end of the window has a couple of visits, a long-standing one has many.
    That is what spreads visits across the whole period.
    """
    today = date.today()

    # The indicators ask "was there a visit in the past three months" at the
    # END of the reporting period, not today. Anchoring on today makes a
    # patient last seen 85 days ago look current now but overdue the moment
    # the month closes, which spiked the current month's no-visit rate to 32%
    # and dragged its controlled rate below the band. Anchor on the period end.
    period_end = date(today.year, today.month,
                      calendar.monthrange(today.year, today.month)[1])
    window_opens = period_end - timedelta(days=RECENT_VISIT_WINDOW_DAYS - 1)

    if profile['lost_to_followup'] and window_start is not None:
        # Stopped attending before the reporting window even opened: counted in
        # cumulative registrations, absent from every in-window indicator.
        last_visit = window_start - timedelta(days=random.randint(1, 120))
    elif profile['overdue']:
        # Before the window, so the patient counts as not recently seen.
        last_visit = window_opens - timedelta(days=random.randint(1, 150))
    else:
        # Inside the window and not in the future.
        earliest = min(window_opens, today)
        last_visit = earliest + timedelta(
            days=random.randint(0, max((today - earliest).days, 0)))

    # A patient who enrolled only recently cannot also have been last seen
    # long ago: give them the minimum follow-up instead of a nonsense history.
    minimum_span = timedelta(
        days=(max(visits_min, 1) - 1) * SCHEDULED_VISIT_INTERVAL_DAYS)
    if last_visit - enrolled < minimum_span:
        last_visit = min(enrolled + minimum_span, today)
    if last_visit < enrolled:
        last_visit = enrolled

    dates = [enrolled]

    # A patient who enrolled today has only their enrolment visit. Appending a
    # "most recent visit" as well would put two visits on the same date.
    if last_visit > enrolled:
        # Fill forward, stopping in time to place the final visit exactly on
        # last_visit with a plausible gap before it.
        while len(dates) < max(visits_max, 2) - 1:
            visit_number = len(dates) + 1
            gap = random.randint(*(LATE_GAP_DAYS if visit_is_late(profile, visit_number)
                                   else PUNCTUAL_GAP_DAYS))
            candidate = dates[-1] + timedelta(days=gap)
            if candidate >= last_visit - timedelta(days=PUNCTUAL_GAP_DAYS[0]):
                break
            dates.append(candidate)

        dates.append(last_visit)

    return [(d.isoformat(), {}) for d in dates]


def build_events(profile, dated, org_unit, patient_uid):
    """
    Build the visit history from a list of (visit date, source event) pairs:
    one completed event per visit in chronological order, plus exactly one
    scheduled visit at the end.
    """
    events = []
    step = profile['start_step']
    total_visits = len(dated)

    for index, (occurred_date, source_event) in enumerate(dated):
        is_last = index == total_visits - 1

        # Control is drawn from the band for the month this visit falls in, so
        # every monthly point on the dashboard lands inside the band and the
        # line fluctuates across months. A patient still on a low protocol
        # step has not had the chance to respond yet, so the first visit and
        # anyone below their response step reads uncontrolled.
        bp_state = bp_state_for_visit(profile, month_of(occurred_date))

        # A patient newly found to be hypertensive reads high at the visit that
        # found them. Someone who was already on BP medicine before enrolling
        # may well arrive at target, so they are not forced high - otherwise
        # the first months of the trend line, which are mostly first visits,
        # sit below the requested band.
        #
        # Gating any harder than this - for instance holding every patient
        # below their response step uncontrolled - dragged the early months
        # down to ~40%.
        newly_diagnosed = not profile['took_bp_meds']
        if (profile['hypertensive'] and bp_state == BP_CONTROLLED
                and index == 0 and newly_diagnosed):
            bp_state = BP_ABOVE_TARGET

        # Glycaemic control runs on its own track: never at the first visit,
        # then improving over follow-up for patients who respond.
        if index == 0 or not profile['dm_improves']:
            dm_at_target = False
        else:
            progress = index / max(total_visits - 1, 1)
            dm_at_target = random.random() < 0.25 + 0.55 * progress
        if is_last:
            dm_at_target = profile['dm_controlled']

        visit = {
            # A generated patient has no source event to inherit a UID from.
            # Deriving one from the patient and the visit number means a later
            # run updates this visit in place; without it DHIS2 mints a fresh
            # UID every run and the patient accumulates duplicate visits.
            'event': (source_event.get('event')
                      or (deterministic_uid(patient_uid, 'visit', str(index))
                          if DETERMINISTIC_UIDS else None)),
            'program': TARGET_PROGRAM,
            'programStage': TARGET_PROGRAM_STAGE,
            'orgUnit': org_unit,
            'occurredAt': occurred_date,
            'status': 'COMPLETED',
            'dataValues': build_visit(
                profile, step, bp_state, dm_at_target,
                with_labs=(index == 0 or random.random() < p('labs_at_visit')),
            ),
        }

        # Every visit after the first needs a due date, otherwise there is
        # nothing to measure lateness against and a patient who came back
        # after 250 days does not register as having been overdue. The first
        # visit has no due date by design: nothing scheduled it.
        #
        # The due date is always derived from the previous visit rather than
        # copied from the source. Nigeria left most visits unscheduled, and
        # where a source due date does exist it is unusable: either identical
        # to the visit date, which makes every visit look punctual however
        # late it was, or older than the preceding visit, which yields
        # nonsense lateness.
        if index > 0:
            previous_visit = datetime.strptime(dated[index - 1][0], "%Y-%m-%d").date()
            visit['scheduledAt'] = (
                previous_visit + timedelta(days=SCHEDULED_VISIT_INTERVAL_DAYS)
            ).isoformat()

        events.append(visit)

        if is_last:
            # Counted only for hypertensive patients, matching the dashboard
            # denominator. A patient who never had hypertension reads below
            # 130/80 but is in neither the numerator nor the denominator of
            # "HTN - Patients under care with controlled BP".
            profile['controlled'] = (profile['hypertensive']
                                     and bp_state == BP_CONTROLLED)
            profile['uncontrolled'] = (profile['hypertensive']
                                       and bp_state == BP_UNCONTROLLED)

        step = next_step(profile, step, bp_state)

    # Any event still without a UID lets DHIS2 assign one.
    for event in events:
        if not event.get('event'):
            event.pop('event', None)

    last_visit = datetime.strptime(events[-1]['occurredAt'], "%Y-%m-%d").date()

    # A patient who is keeping up has their next appointment ahead of them; one
    # who is behind has a due date that has already passed. Deriving this purely
    # as "last visit + 28 days" made two thirds of the cohort look overdue,
    # because a patient last seen 60 days ago is mechanically 32 days late.
    today = date.today()
    wants_past_due = (profile['overdue'] or profile['lost_to_followup']
                      or profile['appointment_overdue'])

    if wants_past_due:
        # The due date must still fall after the visit that preceded it - an
        # appointment booked before the last visit is nonsense, and the
        # validator rightly rejects it.
        earliest = last_visit + timedelta(days=1)
        latest = today - timedelta(days=1)
        if earliest > latest:
            # Seen today or yesterday, so there is no room for a past-due
            # appointment; book the next one instead.
            due = today + timedelta(days=random.randint(1, SCHEDULED_VISIT_INTERVAL_DAYS))
        else:
            due = earliest + timedelta(days=random.randint(0, (latest - earliest).days))
    else:
        due = today + timedelta(days=random.randint(1, SCHEDULED_VISIT_INTERVAL_DAYS))

    events.append({
        # A stable UID from the patient, so a re-run updates this scheduled
        # visit instead of adding a second one. The source has no event to
        # inherit a UID from, and letting DHIS2 mint one each run would leave
        # the patient with a growing pile of scheduled visits.
        'program': TARGET_PROGRAM,
        'programStage': TARGET_PROGRAM_STAGE,
        'orgUnit': org_unit,
        'scheduledAt': due.isoformat(),
        'status': 'SCHEDULE',
    })

    if DETERMINISTIC_UIDS:
        # A stable UID for the appointment, so a re-run updates it instead of
        # leaving the patient with a growing pile of scheduled visits.
        events[-1]['event'] = deterministic_uid(patient_uid, 'scheduled-visit')

    # The follow-up status has to agree with the appointment just booked: a due
    # date that has already passed means the patient is overdue for a call,
    # anything still in the future means the visit is upcoming. The overdue
    # indicators read this attribute, so a random value here would contradict
    # the patient's own scheduled visit.
    due = datetime.strptime(events[-1]['scheduledAt'], "%Y-%m-%d").date()
    profile['follow_up_status'] = ('OVERDUE_PENDING_CALL' if due < date.today()
                                   else 'UPCOMING_VISIT')

    return events


# ---------------------------------------------------------------------------
# Tracked entity assembly
# ---------------------------------------------------------------------------

def eligible_dob(source_dob_iso, policy='eligible'):
    """
    Settle the patient's date of birth inside the CVD-risk eligible band.

    Returns (dob_iso, age, is_young_cohort).

    The Egypt clinical-override rules all write to the same data element in
    priority order:

        1 q8BMOuJd4w9  age >= 40 && no cholesterol && diabetes  -> 'DIABETES'
        2 IG8uBGYVptG  age >= 40 && any CVD history             -> 'HISTORY_CVD'
        3 EyEQgk3QCHX  age < 40 || age > 75             -> 'AGE_NOT_ELIGIBLE'

    An over-75 patient matches both `age >= 40` and `age > 75`, so rule 1 or 2
    fills the field and rule 3 then tries to overwrite it, which the import
    rejects with E1307. Under-40 is safe because rules 1 and 2 require
    age >= 40, so only rule 3 fires and writes once.

    An age already inside 40-75 is always kept as it is.

    Under 40: the patient is aged up into the band, except for a
    YOUNG_COHORT_FRACTION slice of them left young on purpose. Those are safe
    because only rule 3 can fire for them.

    Over 75: rebased into the band under policy 'eligible', kept under policy
    'preserve'. Keeping them is only safe once rules 1 and 2 exclude age > 75.
    """
    age = age_from_dob(source_dob_iso)

    if age < CVD_RISK_MIN_AGE:
        if random.random() < YOUNG_COHORT_FRACTION:
            if YOUNG_COHORT_AGES[0] <= age <= YOUNG_COHORT_AGES[1]:
                return source_dob_iso, age, True
            age = random.randint(*YOUNG_COHORT_AGES)
            return dob_from_age(age), age, True
        age = random.randint(CVD_RISK_MIN_AGE, CVD_RISK_MAX_AGE)
        return dob_from_age(age), age, False

    if age > CVD_RISK_MAX_AGE and policy == 'eligible':
        age = random.randint(CVD_RISK_MIN_AGE, CVD_RISK_MAX_AGE)
        return dob_from_age(age), age, False

    return source_dob_iso, age, False


_UID_HEAD = string.ascii_letters
_UID_TAIL = string.ascii_letters + string.digits


def deterministic_uid(*parts):
    """
    A stable DHIS2 UID (one letter then ten alphanumerics) derived from the
    given parts, so a re-run addresses the same object instead of creating a
    duplicate.
    """
    digest = hashlib.sha256('|'.join(parts).encode('utf-8')).digest()
    return (_UID_HEAD[digest[0] % len(_UID_HEAD)]
            + ''.join(_UID_TAIL[b % len(_UID_TAIL)] for b in digest[1:11]))


def patient_seed(tei_uid, salt=0):
    """A stable 64-bit seed for one source patient."""
    digest = hashlib.sha256(f"{tei_uid}|{salt}".encode('utf-8')).digest()
    return int.from_bytes(digest[:8], 'big')


def synthetic_dob():
    """
    A date of birth for a generated patient. Most of the cohort sits inside
    the 40-75 window the CVD risk plugin can score, with small tails either
    side so the "no risk score available" paths are exercised too.
    """
    roll = random.random()
    under, over = p('age_under_40'), p('age_over_75')

    if roll < under:
        age = random.randint(25, CVD_RISK_MIN_AGE - 1)
    elif roll < under + over:
        age = random.randint(CVD_RISK_MAX_AGE + 1, 88)
    else:
        age = random.randint(CVD_RISK_MIN_AGE, CVD_RISK_MAX_AGE)

    return dob_from_age(age)


def make_synthetic_patient(index, org_unit, district_codes, stats,
                           enrolled, visits_min, visits_max, window_start=None):
    """
    Generate one patient outright, with no source record involved.

    The tracked entity and enrolment UIDs are derived from the run salt and
    the patient's index, so re-running with the same --seed updates the same
    patients instead of creating a second cohort.
    """
    patient_uid = deterministic_uid('egypt-synthetic', str(RUN_SALT), str(index))

    sex = 'FEMALE' if random.random() < p('female') else 'MALE'
    dob = synthetic_dob()
    age = age_from_dob(dob)

    district_code = district_for_org_unit(org_unit, district_codes)
    profile = build_profile(sex, dob, district_code)

    # A lost-to-follow-up patient has to have enrolled early enough to have a
    # visit history that ends before the window opens.
    if profile['lost_to_followup'] and window_start is not None:
        enrolled = min(enrolled, window_start - timedelta(days=random.randint(60, 200)))

    dated = synthetic_visit_dates(profile, enrolled, visits_min, visits_max,
                                  window_start)
    n_visits = len(dated)
    events = build_events(profile, dated, org_unit, patient_uid)

    enrolled_at = dated[0][0]

    record_profile_stats(profile, age, stats)
    stats['visits'] += n_visits
    stats['late_visits'] += sum(
        1 for i in range(2, n_visits + 1) if visit_is_late(profile, i)
    )
    if age < CVD_RISK_MIN_AGE:
        stats['young_cohort'] += 1

    record = {
        'trackedEntityType': TARGET_TRACKED_ENTITY_TYPE,
        'orgUnit': org_unit,
        'attributes': build_attributes(profile, random.choices(
            ('ACTIVE', 'TRANSFER', 'INACTIVE', 'DIED'),
            weights=(90, 4, 5, 1), k=1)[0]),
        'enrollments': [{
            'program': TARGET_PROGRAM,
            'orgUnit': org_unit,
            'enrolledAt': enrolled_at,
            'occurredAt': enrolled_at,
            'status': 'ACTIVE',
            'events': events,
        }],
    }

    if DETERMINISTIC_UIDS:
        record['trackedEntity'] = patient_uid
        record['enrollments'][0]['enrollment'] = deterministic_uid(
            'egypt-synthetic-enrollment', str(RUN_SALT), str(index))

    return record


def record_profile_stats(profile, age, stats):
    """Accumulate the run summary counters shared by both generation paths."""
    stats['controlled'] += profile['controlled']
    stats['uncontrolled'] += profile.get('uncontrolled', False)
    stats['dm_controlled'] += profile['dm_controlled']
    stats['overdue'] += profile['overdue']
    stats['hypertensive'] += profile['hypertensive']
    stats['diabetic'] += profile['diabetic']
    stats['female'] += profile['sex'] == 'FEMALE'
    stats['smoker'] += profile['smoker']
    stats['cvd_history'] += (profile['history_mi'] or profile['history_stroke']
                             or profile['history_ckd'])
    stats['on_insulin'] += profile['on_insulin']
    stats['on_statin'] += bool(profile['statin'])
    stats['past_meds'] += bool(profile['past_htn_drugs'] or profile['past_dm_drugs'])
    stats['mistitrated'] += profile['mistitrated']
    stats['lost_to_followup'] += profile['lost_to_followup']
    stats['died'] += profile.get('ncd_status') == 'DIED'
    stats['transferred'] += profile.get('ncd_status') == 'TRANSFER'
    stats['overdue_call'] += profile.get('follow_up_status') == 'OVERDUE_PENDING_CALL'
    stats['statin_missed'] += profile.get('statin_missed', False)
    stats['risk_scoreable'] += (CVD_RISK_MIN_AGE <= age <= CVD_RISK_MAX_AGE)


def map_source_to_target_ous(source_ous, target_ous):
    """
    Give every source org unit a target facility.

    Egypt has far fewer facilities than Nigeria has source org units, so
    targets are reused in a stable round-robin instead of failing: several
    Nigerian facilities feed one Egyptian one.
    """
    source_sorted = sorted(source_ous)
    target_sorted = sorted(target_ous)

    if not target_sorted:
        raise ValueError("No target facilities to map onto.")

    return {src: target_sorted[i % len(target_sorted)]
            for i, src in enumerate(source_sorted)}


def district_for_org_unit(org_unit_uid, district_codes):
    """
    Stable district per facility, so patients seen at the same place share a
    catchment instead of scattering across Egypt.
    """
    digest = hashlib.sha256(org_unit_uid.encode('utf-8')).hexdigest()
    return district_codes[int(digest, 16) % len(district_codes)]


def source_sex(tei):
    for attr in tei.get('attributes', []):
        if attr.get('attribute') == 'oindugucx72' and attr.get('value'):
            value = str(attr['value']).strip().upper()
            if value in ('MALE', 'FEMALE'):
                return value
    return None


def source_ncd_status(tei):
    for attr in tei.get('attributes', []):
        if attr.get('attribute') == 'fI1P3Mg1zOZ' and attr.get('value'):
            value = str(attr['value']).strip().upper()
            value = NCD_STATUS_MAP.get(value, value)
            if value in VALID_NCD_STATUS:
                return value
    return 'ACTIVE'


def source_dob(tei):
    for attr in tei.get('attributes', []):
        if attr.get('attribute') == 'NI0QRzJvQ0k':
            return parse_dob(attr.get('value'))
    return None


def transform_tei(tei, org_unit, district_codes, stats, age_policy='eligible'):
    """
    Turn one Nigeria tracked entity into an Egypt one. Returns None when the
    record cannot be migrated.
    """
    enrollments = tei.get('enrollments') or []
    if not enrollments:
        stats['no_enrollment'] += 1
        return None

    main_enrollment = enrollments[0]

    source_events = list(main_enrollment.get('events') or [])
    for extra in enrollments[1:]:
        source_events.extend(extra.get('events') or [])

    # Scheduled source events carry no observations and are replaced by the
    # single appointment this script books at the end.
    source_events = [e for e in source_events
                     if e.get('status') not in SCHEDULED_STATUSES]

    # Seed from the source patient UID so a second run regenerates identical
    # data for the same person. Without this, a re-run gives the patient a new
    # age and medication history, which can flip which clinical-override rule
    # applies and makes the update collide with the stored value.
    seed = patient_seed(tei.get('trackedEntity') or '', RUN_SALT)
    random.seed(seed)
    fake.seed_instance(seed)

    sex = source_sex(tei)
    if sex is None:
        stats['no_sex'] += 1
        return None

    dob = source_dob(tei)
    if dob is None:
        dob = dob_from_age(random.randint(CVD_RISK_MIN_AGE, CVD_RISK_MAX_AGE))
        stats['generated_dob'] += 1

    dob, age, young = eligible_dob(dob, age_policy)
    if young:
        stats['young_cohort'] += 1
    elif age_from_dob(source_dob(tei) or dob) != age:
        stats['age_rebased'] += 1

    district_code = district_for_org_unit(org_unit, district_codes)
    profile = build_profile(sex, dob, district_code)

    enrolled_at = (main_enrollment.get('enrolledAt') or date.today().isoformat())[:10]

    # build_events slides the visit timeline to control how recently the
    # patient was last seen, so enrolment has to follow it: an enrolment date
    # after the first visit is rejected by the server.
    dated = dated_from_source_events(source_events, enrolled_at, profile['overdue'])
    events = build_events(profile, dated, org_unit, tei.get('trackedEntity') or '')

    first_visit = min((e['occurredAt'] for e in events if e.get('occurredAt')),
                      default=enrolled_at)
    if first_visit < enrolled_at:
        enrolled_at = first_visit

    record_profile_stats(profile, age, stats)

    return {
        'trackedEntity': tei.get('trackedEntity'),
        'trackedEntityType': TARGET_TRACKED_ENTITY_TYPE,
        'orgUnit': org_unit,
        'attributes': build_attributes(profile, source_ncd_status(tei)),
        'enrollments': [{
            'enrollment': main_enrollment.get('enrollment'),
            'program': TARGET_PROGRAM,
            'orgUnit': org_unit,
            'enrolledAt': enrolled_at,
            'occurredAt': (main_enrollment.get('occurredAt') or enrolled_at)[:10],
            'status': 'ACTIVE',
            'events': events,
        }],
    }


# ---------------------------------------------------------------------------
# Target metadata loading
# ---------------------------------------------------------------------------

def fetch_option_codes(api, option_set_uid, label):
    """Read the codes of a target option set, so the script emits real codes."""
    try:
        response = api.get(
            f'optionSets/{option_set_uid}',
            params={'fields': 'options[code]'}
        ).json()
    except (DHIS2RequestException, RequestsRequestException) as e:
        logger.error(f"Could not read the {label} option set ({option_set_uid}): {e}")
        sys.exit(2)

    codes = [o['code'] for o in response.get('options', []) if o.get('code')]
    if not codes:
        logger.error(f"The {label} option set ({option_set_uid}) has no options.")
        sys.exit(2)

    logger.info(f"{label}: {len(codes)} options")
    return codes


def verify_codes(used, available, label):
    """Warn when the script would send a code the target does not define."""
    unknown = sorted(set(used) - set(available))
    if unknown:
        logger.warning(
            f"{label}: {len(unknown)} code(s) not present in the target option "
            f"set and likely to be rejected: {unknown}"
        )
    return not unknown


# Fields the Egypt program rules assign. Anything a rule assigns must either be
# omitted from the payload or match the rule's own calculation exactly, or the
# import is rejected with E1307 (data element) / E1309 (attribute).
#
# Left entirely to the rules - we cannot reproduce their arithmetic:
RULE_OWNED_FIELDS = {
    'pSqsIKFb8NX': 'Total Cholesterol (mmol/L)',
    'ea5w3mkinZo': 'Age (For TEI header)',
    'fI1P3Mg1zOZ': 'HTN - NCD Patient Status',
    'J2YWUFBEyvZ': 'BMI',
    'qwCWN0aLHAi': 'HTN - Risk Score (number)',
    'm5WImperUYv': 'HTN - Risk Final Classification',
    'DKKPQKIO9qN': 'HTN - Risk - Clinical Override',
    'vdh990I4zq1': 'HTN - BP in one field',
    'ifRI6qw96v4': 'HTN - Patient phone (calling report)',
}

# Assigned by a rule but we send them anyway, having verified that our value
# matches what the rule computes (server-side importMode=VALIDATE, clean).
RULE_COMPATIBLE_FIELDS = {
    'NI0QRzJvQ0k', 'oindugucx72', 'InXHTXWxKR2', 'mAWWEdw00l7',
    'BCMObXpMEzk', 'Xjrw5pbxBJw', 'Mj9jx9JLmyl', 'CBkUvBbOIKz',
    'eGY7e5Ttbys', 'bSTmAUelYYz',
}


def warn_rule_assigned_fields(api):
    """
    Warn when the target has a rule assigning a field this script sends and
    that we have not already accounted for.

    A rule added to the Egypt programme after this script was written would
    otherwise show up only as an opaque E1307/E1309 rejection at import time.
    """
    try:
        rules = api.get('programRules', params={
            'filter': f'program.id:eq:{TARGET_PROGRAM}',
            'paging': 'false',
            'fields': 'id,programRuleActions[programRuleActionType,'
                      'dataElement[id,name],trackedEntityAttribute[id,name]]',
        }).json()['programRules']
    except (DHIS2RequestException, RequestsRequestException) as e:
        logger.warning(f"Could not read program rules to check assigned fields: {e}")
        return

    assigned = {}
    for rule in rules:
        for action in rule.get('programRuleActions', []):
            if action.get('programRuleActionType') != 'ASSIGN':
                continue
            for key in ('dataElement', 'trackedEntityAttribute'):
                target = action.get(key)
                if target:
                    assigned[target['id']] = (target.get('name', target['id']), rule['id'])

    we_send = (set(TEA.values()) | set(DE.values())) - set(RULE_OWNED_FIELDS)
    unexpected = {uid: info for uid, info in assigned.items()
                  if uid in we_send and uid not in RULE_COMPATIBLE_FIELDS}

    logger.info(f"Program rules assign {len(assigned)} fields; "
                f"{len(RULE_OWNED_FIELDS)} left to the rules by design")

    for uid, (name, rule_id) in sorted(unexpected.items()):
        logger.warning(
            f"Program rule {rule_id} assigns '{name}' ({uid}), which this script "
            "also sends. If the values disagree the import fails with E1307/E1309 "
            "- add it to RULE_OWNED_FIELDS or make the value match."
        )


def resolve_target_org_units(api, args):
    """
    The facilities to post to: the ones the programme is actually assigned to.

    Taking every org unit at the facility level is wrong on a real instance.
    Egypt's hierarchy has over eleven thousand facilities but the programme is
    only assigned to the handful in the pilot, and an enrolment at an org unit
    the programme is not assigned to is rejected. So the programme's own
    assignment is the list, filtered to the facility level in case the
    programme is also assigned at district level for reporting.

    --target-ou-level still applies, and --any-org-unit falls back to the old
    level-wide behaviour for an instance where the programme has no assignment
    yet.
    """
    program = api.get(f'programs/{TARGET_PROGRAM}', params={
        'fields': 'organisationUnits[id,name,level]'}).json()
    assigned = program.get('organisationUnits') or []

    if assigned and not args.any_org_unit:
        at_level = [o for o in assigned if o.get('level') == args.target_ou_level]
        if not at_level:
            levels = sorted({o.get('level') for o in assigned})
            logger.error(
                f"The programme is assigned to {len(assigned)} org unit(s), but none "
                f"at level {args.target_ou_level} (they are at level(s) {levels}). "
                f"Pass --target-ou-level with the right level.")
            sys.exit(2)
        logger.info(f"Posting to {len(at_level)} facility/ies the programme is "
                    f"assigned to (of {len(assigned)} assigned org units)")
        return reindex(at_level, 'id')

    if not assigned:
        logger.warning(
            f"The programme {TARGET_PROGRAM} is assigned to no org units on this "
            f"instance; falling back to every org unit at level "
            f"{args.target_ou_level}. Enrolments will be rejected unless the "
            f"programme is assigned first.")

    target_ous = api.get(
        'organisationUnits',
        params={'filter': f'level:eq:{args.target_ou_level}',
                'paging': 'false', 'fields': 'id,name'}
    ).json()['organisationUnits']

    if not target_ous:
        logger.error(f"No org units at level {args.target_ou_level} on the target. "
                     "Pass --target-ou-level with the facility level.")
        sys.exit(2)

    logger.info(f"Target facilities at level {args.target_ou_level}: "
                f"{len(target_ous)}")
    return reindex(target_ous, 'id')


def check_target_metadata(api):
    """Load the district list and validate every code the script can emit."""
    district_codes = fetch_option_codes(api, DISTRICT_OPTION_SET, 'Egypt districts')
    governorate_codes = fetch_option_codes(api, GOVERNORATE_OPTION_SET, 'Egypt governorates')

    # Every district code embeds its governorate: EG1309 sits under EG13.
    usable = [c for c in district_codes if len(c) >= 4 and 'EG' + c[2:4] in set(governorate_codes)]
    orphans = len(district_codes) - len(usable)
    if orphans:
        logger.warning(f"Ignoring {orphans} district code(s) with no matching governorate")
    if not usable:
        logger.error("No district code maps onto a governorate; cannot assign geography.")
        sys.exit(2)

    for option_set, codes, label in (
        ('gxyIp1WOS4y', PAST_ANTIDIABETIC_CODES, 'Past anti-diabetic drugs'),
        ('vmiFAvijmad', PAST_ANTIHYPERTENSIVE_CODES, 'Past anti-hypertensive drugs'),
        ('p1FsIDeuQEY', ANTIDIABETIC_CODES, 'Anti-diabetic doses+freq'),
        ('aC6dLLfOVeF', STATIN_CODES, 'Statin drugs'),
    ):
        verify_codes(codes, fetch_option_codes(api, option_set, label), label)

    insulin_names = [n for r in INSULIN_REGIMENS.values() for n in r['names']]
    verify_codes(insulin_names, fetch_option_codes(api, 'nd8w5fWDDC2', 'Insulin name'),
                 'Insulin name')

    frequencies = sorted({r['frequency'] for r in INSULIN_REGIMENS.values()})
    verify_codes(frequencies, fetch_option_codes(api, 'Nxf2OPtElzY', 'Medication frequency'),
                 'Medication frequency')

    protocol_codes = {AMLODIPINE_MONOTHERAPY}
    combined, thiazide = set(), set()
    for step in PROTOCOL_STEPS.values():
        if 'amlo_valsartan' in step:
            combined.add(step['amlo_valsartan'])
        if 'hctz' in step:
            thiazide.add(step['hctz'])
    verify_codes(sorted(combined), fetch_option_codes(api, 'iYtpGuJH9PC', 'Amlodipine/Valsartan'),
                 'Amlodipine/Valsartan')
    verify_codes(sorted(thiazide), fetch_option_codes(api, 'uK9CK10LvRW', 'Hydrochlorothiazide'),
                 'Hydrochlorothiazide')
    verify_codes(sorted(protocol_codes), fetch_option_codes(api, 'MZu1fOYzMVD', 'Amlodipine'),
                 'Amlodipine')

    return usable


# ---------------------------------------------------------------------------
# Source org units
# ---------------------------------------------------------------------------

NIGERIA_OUS_WITH_DATA = [
    "S84LuOwGZ6R", "QLanNpwrwqb", "Gciekg99hVj", "YsZ3eAa7HU1", "WqebnzGgb71",
    "b8zi5ST4dH3", "PFmp8iIeWRb", "ACgBmF2uh5h", "XuQ3oT7hwnc", "UhwUiVzHWsA",
    "rBGMr2mML23", "bYamdI7FHE5", "VzyOk9CLz5X", "DwV5TmasIOB", "uRzXjHHigm9",
    "PAxRf5zY1As", "S8OFPw6S1An", "TUzBaQ3PYDm", "tweJXXNQgkA", "MtPVEBYLvHZ",
    "lSoOduWvZUx", "BaGCGdYknNN", "XMOewxpQvsf", "JJL8Ukh1ZlZ", "z2syYAAoWvG",
    "TYdI1qB6nNd", "LTsuanAvTRU", "lMr57aLPSoT", "dQE9wQrW9rS", "tu9DZ2g4LZH",
    "bnZRTFMPLCY", "o5uazdFDoBc", "tMmRVO0UPrc", "yNtPl1eGwar", "jpPaQTbjIlU",
    "iPHdxJ2F9ve", "UupnludKN9T", "m2DR5doEjKC", "OjhcqZ6O5aK", "DwFXaxsC09L",
    "o9IDb1gMBqV", "WmH9sWwbDGC", "BqwpWi32Gus", "xUORgwU22YU", "DxfVRX9j8np",
    "aoOPscxu7Tc", "tcSIgkbW2jA", "fLSPCrGZjF5", "WtOaeyRb56h", "lYBdFTtjv8R",
    "wgbimD4TWvq", "nltVWcc5JRe", "NLbb5g68GnM", "ehJsDey0nF2", "SSx6C5WTRT0",
    "neBNBh5lyU5", "spFKAo4GHXy", "oAXeCYwch7U", "PKf7VHpxmBG",
]

PROBLEM_TEIS = {'fVJvL7v3oW9', 'lrJ1zLNii0G'}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_optional_date(value, label):
    """Parse an optional YYYY-MM-DD CLI date."""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        logger.error(f"{label} must be YYYY-MM-DD, got '{value}'")
        sys.exit(2)


def resolve_window_start(args):
    """
    The date visits start from.

    In synthetic mode --after is the beginning of the data window: visits are
    spread from it up to today. It is not a filter, which is what it means
    with --from-nigeria.
    """
    today = date.today()

    if args.after:
        try:
            window_start = datetime.strptime(args.after, "%Y-%m-%d").date()
        except ValueError:
            logger.error(f"--after must be YYYY-MM-DD, got '{args.after}'")
            sys.exit(2)
    else:
        if args.from_nigeria:
            logger.error("--from-nigeria needs --after YYYY-MM-DD")
            sys.exit(2)
        window_start = today - timedelta(days=DEFAULT_WINDOW_MONTHS * 30)
        logger.info(f"No --after given; generating visits from "
                    f"{window_start.isoformat()} "
                    f"({DEFAULT_WINDOW_MONTHS} months back)")

    if window_start >= today:
        logger.error(f"--after {window_start.isoformat()} is not in the past; "
                     "there would be no window to put visits in.")
        sys.exit(2)

    span = (today - window_start).days
    if not args.from_nigeria and span < MIN_USEFUL_WINDOW_DAYS:
        logger.warning(
            f"--after {window_start.isoformat()} leaves only {span} days of "
            f"history. The dashboards summarise the last three months, so a "
            f"window shorter than {MIN_USEFUL_WINDOW_DAYS} days will leave the "
            f"trend lines nearly empty. Consider an earlier --after."
        )

    return window_start


def apply_patient_outcomes(api, created, stats):
    """
    Set NCD Patient Status on the share of patients who died or transferred.

    This has to be a second pass. Program rule GYmydeVoxDI assigns ACTIVE when
    an enrollment is created, so a status supplied in the creating payload is
    overwritten (or rejected with E1309). On a later update the rule does not
    re-assert, so the value sticks - verified on the server before this was
    written.

    `created` is a list of (tracked entity uid, org unit) pairs from the import.
    Which specific patients get which outcome does not matter, so they are
    drawn at random rather than threaded through from the generator.
    """
    want_died = stats['died']
    want_transfer = stats['transferred']
    if not (want_died or want_transfer):
        return

    if len(created) < want_died + want_transfer:
        logger.warning(
            f"Only {len(created)} patient UIDs came back from the import but "
            f"{want_died + want_transfer} outcomes were drawn; applying what fits."
        )

    pool = random.sample(created, min(len(created), want_died + want_transfer))
    assignment = ([('DIED', uid, ou) for uid, ou in pool[:want_died]]
                  + [('TRANSFER', uid, ou) for uid, ou in pool[want_died:]])

    logger.info(f"Applying outcomes to {len(assignment)} patients "
                f"({want_died} died, {len(assignment) - want_died} transferred)")

    payload = {'trackedEntities': [
        {'trackedEntity': uid,
         'trackedEntityType': TARGET_TRACKED_ENTITY_TYPE,
         'orgUnit': ou,
         'attributes': [{'attribute': TEA['ncd_status'], 'value': status}]}
        for status, uid, ou in assignment]}

    try:
        r = api.post('tracker', params={'async': 'false', 'importStrategy': 'UPDATE',
                                        'skipRuleEngine': 'true'}, json=payload).json()
        s = r.get('stats', {})
        logger.info(f"  outcomes applied: {r.get('status')} "
                    f"updated {s.get('updated')}, ignored {s.get('ignored')}")
        for e in r.get('validationReport', {}).get('errorReports', [])[:3]:
            logger.error(f"  {e.get('errorCode')}: {e.get('message')}")
    except DHIS2RequestException as e:
        logger.error(f"  could not apply outcomes: {str(e)[:200]}")


def run_synthetic(args, api_target, target_ous, district_codes, stats, window_start):
    """
    Generate the cohort outright and post it, facility by facility.

    Patients are spread evenly over the target facilities, and each facility's
    batch is posted before the next is built, so an interrupted run leaves
    whole facilities done rather than a half-written cohort.
    """
    if args.visits_min < 1 or args.visits_max < args.visits_min:
        logger.error(f"--visits-min/--visits-max must satisfy 1 <= min <= max, "
                     f"got {args.visits_min}..{args.visits_max}")
        sys.exit(2)

    facilities = sorted(target_ous)
    total = args.patients
    if args.limit is not None:
        total = min(total, args.limit)

    enrol_first, enrol_last = enrolment_window(
        window_start, args.enrollment_lead_days,
        parse_optional_date(args.enrol_from, '--enrol-from'),
        parse_optional_date(args.enrol_to, '--enrol-to'),
        args.visits_min)

    implied = (date.today() - enrol_first).days // SCHEDULED_VISIT_INTERVAL_DAYS + 1
    if implied > args.visits_max:
        logger.warning(
            f"The enrolment span implies up to {implied} monthly visits for the "
            f"earliest patients but --visits-max is {args.visits_max}. Those "
            f"patients will have their history truncated and then a single recent "
            f"visit, leaving a gap. Raise --visits-max to at least {implied}."
        )

    today = date.today()
    logger.info(
        f"Generating {total} patients (indices {args.start_index}-"
        f"{args.start_index + total - 1}) across {len(facilities)} facilities"
    )
    logger.info(
        f"  enrolments spread evenly {enrol_first.isoformat()} .. "
        f"{enrol_last.isoformat()}; visits run from there to {today.isoformat()}"
    )
    if args.start_index:
        logger.info("  --start-index given: these are additional patients; the "
                    "existing ones are not touched")
    else:
        # Re-importing a patient who already exists only works if their data is
        # unchanged. The CVD-risk rules refuse to overwrite a stored score with
        # a different one (E1307: "the provided value must be empty or match the
        # calculated value"), and the payload merge keeps the old value, so any
        # change to age, BMI, cholesterol or smoking makes the rule and the
        # stored value disagree and the whole patient is rejected.
        logger.info("  patients are created from index 0; if this cohort already "
                    "exists, purge it first or pass --start-index to add new "
                    "arrivals instead - re-importing changed patients is refused "
                    "by the CVD-risk rules (E1307)")

    # Even spread: the first few facilities absorb the remainder.
    per_facility = [total // len(facilities)] * len(facilities)
    for i in range(total % len(facilities)):
        per_facility[i] += 1

    dry_run_payload = []
    created = []            # (tracked entity uid, org unit) for the second pass
    index = args.start_index

    for facility, count in zip(facilities, per_facility):
        if count == 0:
            continue

        batch = []
        for _ in range(count):
            position = index - args.start_index
            # Seed per patient from the run salt, so the same --seed
            # regenerates an identical cohort and re-posting updates rather
            # than duplicating.
            seed = patient_seed(f'synthetic-{index}', RUN_SALT)
            random.seed(seed)
            fake.seed_instance(seed)

            enrolled = spread_enrolment_date(position, total,
                                             enrol_first, enrol_last)
            batch.append(make_synthetic_patient(
                index, facility, district_codes, stats, enrolled,
                args.visits_min, args.visits_max, window_start))
            stats['migrated'] += 1
            index += 1

        logger.info(f"{target_ous[facility]['name']}: {len(batch)} patients")

        if args.dry_run:
            dry_run_payload.extend(batch)
        else:
            uids = post_chunked_tracked_entities(
                api_target, batch,
                chunk_size=args.chunk_size,
                run_rules=not args.skip_rules,
                failed_file=FAILED_TEIS_FILE,
            )
            created.extend((uid, facility) for uid in uids)

    if args.dry_run:
        out_file = 'egypt_payload_preview.json'
        with open(out_file, 'w', encoding='utf-8') as f:
            json.dump({'trackedEntities': dry_run_payload}, f, indent=2)
        logger.info(f"Dry run: wrote {len(dry_run_payload)} patients to {out_file}")
    else:
        apply_patient_outcomes(api_target, created, stats)


def parse_args():
    parser = argparse.ArgumentParser(
        description='Generate a population for the Egypt HTN/DM programme, or '
                    'migrate one from the Nigeria NCD instance'
    )
    parser.add_argument('--target', default=TARGET_ENV_PREFIX,
                        help='Credential prefix in .env naming the instance to act on (default: %(default)s). Use this to point the same script at a different Egypt instance, e.g. --target EGYPT_STAGING.')
    parser.add_argument('--allow-production', action='store_true',
                        help='Permit running against a target that looks like production. Off by default; these scripts destroy data.')
    parser.add_argument('--patients', type=int, default=500,
                        help='How many patients to generate, spread evenly over the '
                             'target facilities (default: 500). Ignored with '
                             '--from-nigeria')
    parser.add_argument('--visits-min', type=int, default=DEFAULT_VISITS_MIN,
                        help=f'Fewest visits per generated patient '
                             f'(default: {DEFAULT_VISITS_MIN})')
    parser.add_argument('--visits-max', type=int, default=DEFAULT_VISITS_MAX,
                        help=f'Most visits per generated patient '
                             f'(default: {DEFAULT_VISITS_MAX})')
    parser.add_argument('--from-nigeria', action='store_true',
                        help='Migrate real patients from the Nigeria NCD instance '
                             'instead of generating them. Needs --after and the '
                             'NIGERIA_NCD credentials. Only the visit count and the '
                             'gaps between visits survive the transformation')
    parser.add_argument('--after', default=None,
                        help='Start of the data window (YYYY-MM-DD). Generated '
                             'visits are spread from this date up to today, and '
                             f'enrolments start up to {ENROLLMENT_LEAD_DAYS} days '
                             'earlier so the cohort includes patients enrolled '
                             'before the window. Defaults to '
                             f'{DEFAULT_WINDOW_MONTHS} months back. With '
                             '--from-nigeria it keeps its old meaning: a filter on '
                             'which source patients to migrate')
    parser.add_argument('--start-index', type=int, default=0,
                        help='Index of the first patient to generate (default: 0). '
                             'Patient UIDs derive from the index and --seed, so '
                             'running again with --start-index set past the existing '
                             'cohort adds new patients and leaves the earlier ones '
                             'untouched. Re-running the same range updates them')
    parser.add_argument('--enrol-from', default=None,
                        help='Earliest enrolment date (YYYY-MM-DD). Defaults to '
                             '--enrollment-lead-days before the window start. Set '
                             'both --enrol-from and --enrol-to to the last few days '
                             'to generate new arrivals for a weekly top-up')
    parser.add_argument('--enrol-to', default=None,
                        help='Latest enrolment date (YYYY-MM-DD). Defaults to a '
                             'month before today, leaving room for two visits')
    parser.add_argument('--enrollment-lead-days', type=int,
                        default=ENROLLMENT_LEAD_DAYS,
                        help='How far before the window start enrolments may begin '
                             f'(default: {ENROLLMENT_LEAD_DAYS}). Keeps the '
                             '"enrolled over three months ago" indicators populated')
    parser.add_argument('--resume', action='store_true',
                        help='Resume from the last processed source org unit')
    parser.add_argument('--dry-run', action='store_true',
                        help='Build the payload and write it to a file without posting')
    parser.add_argument('--limit', type=int, default=None,
                        help='Stop after this many patients (useful with --dry-run)')
    parser.add_argument('--chunk-size', type=int, default=10,
                        help='Tracked entities per POST. Program rules make imports '
                             'slow, so keep this small (default: 10)')
    parser.add_argument('--target-ou-level', type=int, default=4,
                        help='Org unit level to use as target facilities (default: 4)')
    parser.add_argument('--any-org-unit', action='store_true',
                        help='Post to every org unit at the facility level instead '
                             'of only those the programme is assigned to. The '
                             'programme assignment is the right list on a real '
                             'instance; this is an escape hatch.')
    parser.add_argument('--skip-rules', action='store_true',
                        help='Disable the server-side program rule engine. Off by '
                             'default: the Egypt programme needs rules to derive '
                             'BMI and CVD risk')
    parser.add_argument('--age-policy', choices=('eligible', 'preserve'),
                        default='eligible',
                        help="'eligible' (default) keeps every patient inside "
                             f"{CVD_RISK_MIN_AGE}-{CVD_RISK_MAX_AGE} apart from a small "
                             "under-40 cohort, which avoids the over-75 clinical-override "
                             "rule collision. 'preserve' keeps the real source ages, "
                             "including over-75; use it once program rules q8BMOuJd4w9 "
                             "and IG8uBGYVptG exclude age > 75.")
    parser.add_argument('--deterministic-uids', action='store_true',
                        help='Derive patient and event UIDs from --seed so a re-run '
                             'addresses the same records. Off by default: after a '
                             'purge DHIS2 refuses to reuse a soft-deleted UID '
                             '(E1082), so a deterministic re-run fails on every '
                             'patient. Leave it off and the server mints UIDs')
    parser.add_argument('--seed', type=int, default=None,
                        help='Seed the random generator for reproducible output')
    parser.add_argument('--mix', action='append', metavar='KEY=VALUE', default=[],
                        help='Override one population proportion, as a fraction or '
                             'a percentage. Repeatable, e.g. --mix controlled=0.65 '
                             '--mix diabetic=50. Keys: ' + ', '.join(sorted(DEFAULT_MIX)))
    parser.add_argument('--mix-file', metavar='PATH', default=None,
                        help='JSON file of population proportions, applied before --mix')
    parser.add_argument('--show-mix', action='store_true',
                        help='Print the population mix that would be used and exit')
    return parser.parse_args()


def main():
    args = parse_args()
    logfile(LOG_FILE)

    try:
        overrides = apply_mix_overrides(args.mix, args.mix_file)
    except ValueError as e:
        logger.error(f"Bad population mix: {e}")
        sys.exit(2)

    if overrides:
        logger.info("Population mix overridden: " + ", ".join(
            f"{k}={v:.0%}" for k, v in sorted(overrides.items())))

    check_mix_feasible()

    if args.show_mix:
        print("Population mix:")
        for key in sorted(MIX):
            marker = '*' if key in overrides else ' '
            print(f"  {marker} {key:22} {MIX[key]:6.1%}")
        print("\n  * overridden from the default")
        return

    if args.deterministic_uids:
        global DETERMINISTIC_UIDS
        DETERMINISTIC_UIDS = True
        logger.warning("--deterministic-uids: a re-run will address the same "
                       "records. This fails if those UIDs were ever purged.")

    if args.seed is not None:
        global RUN_SALT
        RUN_SALT = args.seed
        logger.info(f"Run salt fixed at {args.seed}; per-patient data stays "
                    "reproducible across runs")

    window_start = resolve_window_start(args)

    api_target = load_api(args.target, f'target ({args.target})')
    guard_destructive(args.target, api_target,
                      args.allow_production, 'create and overwrite patient records')
    logger.warning(f"Target DHIS2 {api_target.version} rev {api_target.revision}")

    api_source = None
    if args.from_nigeria:
        api_source = load_api(SOURCE_ENV_PREFIX, 'source (Nigeria NCD)')
        logger.warning(f"Source DHIS2 {api_source.version} rev {api_source.revision}")

    district_codes = check_target_metadata(api_target)
    warn_rule_assigned_fields(api_target)

    target_ous = resolve_target_org_units(api_target, args)

    stats = {k: 0 for k in ('no_enrollment', 'no_sex', 'young_cohort', 'age_rebased',
                            'generated_dob', 'hypertensive', 'diabetic', 'smoker',
                            'cvd_history', 'on_insulin', 'on_statin', 'past_meds',
                            'mistitrated', 'risk_scoreable', 'migrated', 'female',
                            'controlled', 'uncontrolled', 'dm_controlled', 'overdue',
                            'visits', 'late_visits', 'lost_to_followup', 'statin_missed',
                            'died', 'transferred', 'overdue_call')}

    if not args.from_nigeria:
        run_synthetic(args, api_target, target_ous, district_codes, stats,
                      window_start)
        report(stats)
        return

    logger.info(f"Source org units: {len(NIGERIA_OUS_WITH_DATA)}")

    # The mapping is 1:1 so that patients from one source facility all land in
    # the same target facility. When the target has fewer facilities than the
    # source, migrate a subset of source facilities rather than refusing to run.
    source_ous = sorted(NIGERIA_OUS_WITH_DATA)
    if len(source_ous) > len(target_ous):
        keep = len(target_ous)
        skipped = source_ous[keep:]
        logger.warning(
            f"Org unit count differs: {len(source_ous)} source facilities vs "
            f"{keep} target facilities at level {args.target_ou_level} "
            f"(expected when the target is a pilot). Migrating patients from the "
            f"first {keep} source facilities by UID so the mapping stays 1:1; "
            f"{len(skipped)} source facilities will not be migrated."
        )
        logger.warning(f"Not migrated: {', '.join(skipped)}")
        source_ous = source_ous[:keep]

    try:
        ou_mapping = deterministic_mapping(source_ous, list(target_ous.keys()))
    except ValueError as e:
        logger.error(str(e))
        sys.exit(2)

    resume_uid = load_resume_uid(RESUME_FILE) if args.resume else None
    if resume_uid is not None and resume_uid not in source_ous:
        logger.error(f"Resume file {RESUME_FILE} points at '{resume_uid}', which is "
                     "not in the migration list. Delete or fix it before resuming.")
        sys.exit(2)
    skip_ou = bool(resume_uid)
    dry_run_payload = []

    for nigeria_ou in source_ous:
        if resume_uid is not None and nigeria_ou == resume_uid:
            skip_ou = False
        if skip_ou:
            continue
        if args.limit is not None and stats['migrated'] >= args.limit:
            break

        save_last_uid(RESUME_FILE, nigeria_ou)
        org_unit = ou_mapping[nigeria_ou]
        logger.info(f"Mapping source facility {nigeria_ou} -> {target_ous[org_unit]['name']}")

        events = api_source.get('tracker/events', params={
            'program': SOURCE_PROGRAM,
            'orgUnit': nigeria_ou,
            'occurredAfter': args.after,
            'fields': 'trackedEntity',
            'skipPaging': 'true',
        }).json()['events']

        tei_uids = sorted({e['trackedEntity'] for e in events if e.get('trackedEntity')})
        tei_uids = [uid for uid in tei_uids if uid not in PROBLEM_TEIS]
        logger.info(f"{len(tei_uids)} candidate patients")

        ready = []
        for tei_uid in tei_uids:
            if args.limit is not None and stats['migrated'] >= args.limit:
                break
            try:
                tei = api_source.get(
                    f'tracker/trackedEntities/{tei_uid}',
                    params={'program': SOURCE_PROGRAM, 'fields': '*'}
                ).json()
            except (DHIS2RequestException, RequestsRequestException) as e:
                if getattr(e, 'code', None) == 404:
                    logger.warning(f"Skipping {tei_uid}: no longer on the source server")
                    continue
                raise

            transformed = transform_tei(remove_created_updated(tei), org_unit,
                                        district_codes, stats, args.age_policy)
            if transformed is None:
                continue

            ready.append(transformed)
            stats['migrated'] += 1

        if not ready:
            continue

        if args.dry_run:
            dry_run_payload.extend(ready)
            logger.info(f"Dry run: built {len(ready)} patients (not posted)")
        else:
            post_chunked_tracked_entities(
                api_target, ready,
                chunk_size=args.chunk_size,
                run_rules=not args.skip_rules,
                failed_file=FAILED_TEIS_FILE,
            )

    if args.dry_run:
        out_file = 'egypt_payload_preview.json'
        with open(out_file, 'w', encoding='utf-8') as f:
            json.dump({'trackedEntities': dry_run_payload}, f, indent=2)
        logger.info(f"Dry run: wrote {len(dry_run_payload)} patients to {out_file}")
    else:
        delete_resume_file(RESUME_FILE)

    report(stats)


def report(stats):
    """Print achieved against requested, so a mix that did not land is
    obvious without waiting for the dashboard to refresh."""
    migrated = stats['migrated'] or 1
    logger.info("=" * 62)
    logger.info(f"Patients migrated              {stats['migrated']}")

    # Achieved against requested, so a mix that did not land is obvious
    # without waiting for the dashboard to refresh.
    def achieved(label, count, denominator, asked):
        share = count / denominator if denominator else 0
        logger.info(f"  {label:28} {count:5}  {share:5.1%}  (asked {asked})")

    def point(mix_key):
        return f"{MIX[mix_key]:.0%}"

    def band(name):
        return f"{MIX[f'{name}_min']:.0%}-{MIX[f'{name}_max']:.0%}"

    achieved('hypertensive', stats['hypertensive'], migrated, point('hypertensive'))
    # Over hypertensive patients, which is the denominator the HTN dashboard
    # indicators use ("HTN - Patients under care with controlled BP" over
    # "HTN - Patients under care"). Non-hypertensive patients are in neither.
    htn = stats['hypertensive'] or 1
    achieved('BP controlled <130/80', stats['controlled'], htn, band('controlled'))
    achieved('BP uncontrolled >=140/90', stats['uncontrolled'], htn,
             band('uncontrolled'))
    achieved('diabetic', stats['diabetic'], migrated, point('diabetic'))
    achieved('  glycaemia in range', stats['dm_controlled'],
             stats['diabetic'] or 1, point('dm_controlled'))
    achieved('no visit in 3 months', stats['overdue'], migrated, band('overdue'))
    achieved('lost to follow-up', stats['lost_to_followup'], migrated,
             point('lost_to_followup'))
    achieved('statin indicated, missed', stats['statin_missed'], migrated,
             point('statin_missed'))
    achieved('died', stats['died'], migrated, point('died'))
    achieved('transferred', stats['transferred'], migrated, point('transferred'))
    achieved('overdue, pending call', stats['overdue_call'], migrated,
             point('appointment_overdue'))
    achieved('female', stats['female'], migrated, point('female'))
    if stats['visits']:
        logger.info(f"  visits generated             {stats['visits']:5}  "
                    f"({stats['visits'] / migrated:.1f} per patient)")
        logger.info(f"  late visits                  {stats['late_visits']:5}  "
                    f"{stats['late_visits'] / stats['visits']:5.1%} of visits "
                    f"(asked v2 {MIX['late_at_visit_2']:.0%}, "
                    f"v3 {MIX['late_at_visit_3']:.0%}, "
                    f"all {MIX['late_all_visits']:.0%})")
    logger.info(f"  smokers                      {stats['smoker']}")
    logger.info(f"  history of CVD               {stats['cvd_history']}")
    logger.info(f"  already on medication        {stats['past_meds']}")
    logger.info(f"  on insulin                   {stats['on_insulin']}")
    logger.info(f"  on a statin                  {stats['on_statin']}")
    logger.info(f"  wrongly titrated             {stats['mistitrated']}")
    logger.info(f"  aged {CVD_RISK_MIN_AGE}-{CVD_RISK_MAX_AGE} (CVD scoreable)   {stats['risk_scoreable']}")
    logger.info(f"  under-40 cohort              {stats['young_cohort']}")
    logger.info(f"  age rebased into {CVD_RISK_MIN_AGE}-{CVD_RISK_MAX_AGE}      {stats['age_rebased']}")
    logger.info(f"Skipped: no enrollment {stats['no_enrollment']}, "
                f"no sex {stats['no_sex']}")
    logger.info(f"Generated a date of birth for {stats['generated_dob']} patients")
    logger.info("=" * 62)



if __name__ == '__main__':
    main()
