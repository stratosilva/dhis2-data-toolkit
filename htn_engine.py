"""
Generate dummy patients for an HTN/DM instance, or top one up with this
period's visits.

One generator for all three instances - the HTN/DM demo, Nigeria (sandbox 1)
and Egypt (sandbox 3) - selected with --instance. What differs between them is
configuration, not logic, and lives in htn_instances.py: which attributes and
data elements each one carries and under which UIDs, how it splits a patient's
name, where its geography comes from, and which treatment ladder it follows.
A field an instance does not carry is simply never produced.

    python htn_engine.py --instance nigeria --patients 1000
    python htn_engine.py --instance egypt   --patients 500 --enrol-from 2026-10-01
    python htn_engine.py --instance demo    --visits-only 1000
    python htn_engine.py --instance nigeria --patients 50 --validate-only

Two modes:

  * Enrolment. Patients are enrolled across the last year by default, each
    with their first visit on the enrolment date and monthly follow-ups from
    there, so the number of visits falls out of how long they have been
    enrolled. --enrol-from / --enrol-to narrow that to one month, which is how
    a monthly run adds new arrivals without touching anyone already there.

  * Top-up (--visits-only). Patients who already exist and whose appointment
    has come due get this period's visit: the scheduled event is recycled into
    a completed one and a fresh appointment is booked 28 days out. Shared with
    every instance through topup_visits.py.

What the server works out for itself: the protocol step ladder, the treatment
recommendation and the TEI-header age are assigned by program rules on import,
so they are not sent - a value that disagrees with the rule's own calculation
is rejected with E1307/E1309 and loses the whole patient.

Two attributes cannot be set by the creating payload at all. NCD Patient
Status is forced to ACTIVE by a program rule; the follow-up status is forced
to UPCOMING_VISIT, and on a live instance a nightly cron moves overdue
patients to OVERDUE_PENDING_CALL afterwards. Both are written in a second pass
once the records exist - see apply_patient_state.

The call centre is modelled rather than hinted at, and the generated
population covers every treatment-transition category the reporting side
classifies, which the run report prints and warns about when one comes out
empty.
"""

import argparse
import calendar
import hashlib
import json
import random
import re
import string
import sys
import uuid
from collections import Counter
from decimal import Decimal, ROUND_HALF_UP
from datetime import date, datetime, timedelta

from dhis2 import RequestException as DHIS2RequestException
from faker import Faker
from logzero import logfile, logger
from requests.exceptions import RequestException as RequestsRequestException

import htn_instances
from topup_visits import run_topup
from tracker_common import (
    age_from_dob,
    guard_destructive,
    delete_resume_file,
    dob_from_age,
    import_error_reports,
    load_api,
    load_resume_uid,
    parse_dob,
    post_chunked_tracked_entities,
    remove_created_updated,
    save_last_uid,
)

# Per-instance so three targets can be worked on without their logs, resume
# files and payload previews overwriting one another. Rebound by configure().
RESUME_FILE = 'resume_uid_nigeria.txt'
FAILED_TEIS_FILE = 'failed_teis_nigeria.jsonl'
LOG_FILE = './htn_generate.log'
PAYLOAD_PREVIEW_FILE = 'payload_preview.json'

SOURCE_ENV_PREFIX = 'NIGERIA_NCD'
# Sandbox 1 carries the Nigeria configuration. Not production: this script
# creates and overwrites patient records.
TARGET_ENV_PREFIX = 'SANDBOX1'

SOURCE_PROGRAM = 'pMIglSEqPGS'
TARGET_PROGRAM = 'pMIglSEqPGS'
VISIT_STAGE = 'anb2cjLx3WM'          # Hypertension and Diabetes visit
CALL_STAGE = 'W7BCOaSquMd'           # Calling report
TARGET_TRACKED_ENTITY_TYPE = 'MCPQUTHX1Ze'  # Person

# The pilot states. Patients are only created at facilities underneath these.
PILOT_STATES = {
    'fBInDsbaQHO': 'Ogun',
    'cTIw3RXOLCQ': 'Kano',
}
FACILITY_LEVEL = 5

# Added to every per-patient seed so --seed varies the whole run while keeping
# each patient's generated data reproducible across runs.
RUN_SALT = 0

# Whether generated patients and events carry UIDs derived from the run salt.
# Off by default, for the same reason as in the Egypt script: a purge
# soft-deletes, and DHIS2 refuses to reuse a soft-deleted UID (E1082/E1114), so
# a deterministic re-run after a purge fails on every patient.
DETERMINISTIC_UIDS = False

fake = Faker()


# ---------------------------------------------------------------------------
# Target metadata: tracked entity attributes
# ---------------------------------------------------------------------------

# Populated by configure() from the instance config: a semantic field name to
# the UID this instance uses for it. A field absent here is not sent at all,
# which is how one engine writes to instances that carry different fields.
TEA = {}
DE = {}
CALL_DE = {}
INSTANCE = None
PROTOCOL_STEPS = {}
MAX_PROTOCOL_STEP = 5
PILOT_STATES = {}
FACILITY_LEVEL = 5
HTN_THRESHOLD = (140, 90)
STEP_2_ENTRY = (160, 100)
STEP_3_ENTRY = (180, 110)


def configure(instance):
    """
    Point the engine at one instance.

    Everything instance-specific is rebound here rather than threaded through
    every function: the field maps, the treatment ladder and its blood
    pressure thresholds, and the part of the hierarchy to work in.
    """
    global TEA, DE, CALL_DE, INSTANCE, PROTOCOL_STEPS, MAX_PROTOCOL_STEP
    global PILOT_STATES, FACILITY_LEVEL
    global HTN_THRESHOLD, STEP_2_ENTRY, STEP_3_ENTRY
    global TARGET_PROGRAM, VISIT_STAGE, CALL_STAGE, TARGET_TRACKED_ENTITY_TYPE
    global RULE_OWNED_FIELDS

    global RESUME_FILE, FAILED_TEIS_FILE, LOG_FILE, PAYLOAD_PREVIEW_FILE
    RESUME_FILE = f'resume_uid_{instance.key}.txt'
    FAILED_TEIS_FILE = f'failed_teis_{instance.key}.jsonl'
    LOG_FILE = f'./htn_generate_{instance.key}.log'
    PAYLOAD_PREVIEW_FILE = f'payload_preview_{instance.key}.json'
    global PENDING_STATE_FILE
    PENDING_STATE_FILE = f'pending_state_{instance.key}.json'

    INSTANCE = instance
    TEA = dict(instance.tea)
    DE = dict(instance.de)
    CALL_DE = dict(instance.call_de)
    # The calling-report fields are addressed through DE like everything else;
    # CALL_DE stays separate only to document which stage they belong to.
    DE.update(CALL_DE)

    protocol = instance.protocol
    global MED_KEYS, PROTOCOL_DOSE_KEYS, NON_PROTOCOL_REGIMENS, MONOTHERAPY
    PROTOCOL_STEPS = protocol.steps
    MAX_PROTOCOL_STEP = protocol.max_step
    PROTOCOL_DOSE_KEYS = protocol.dose_keys

    global BP_NORMAL, BP_HTN_AT_TARGET, BP_HTN_ABOVE_TARGET, BP_HTN_UNCONTROLLED
    BP_NORMAL = protocol.bp_normal
    BP_HTN_AT_TARGET = protocol.bp_at_target
    BP_HTN_ABOVE_TARGET = protocol.bp_above_target
    BP_HTN_UNCONTROLLED = protocol.bp_uncontrolled

    global RESPONSE_STRENGTH
    RESPONSE_STRENGTH = protocol.response_strength
    MONOTHERAPY = protocol.monotherapy
    global REFERRAL_STEP
    REFERRAL_STEP = protocol.referral_step
    NON_PROTOCOL_REGIMENS = list(protocol.non_protocol)
    # Every medication field this ladder can touch, plus plain amlodipine,
    # which is the fallback when an ARB is contraindicated.
    MED_KEYS = tuple(sorted(
        {key for plan in protocol.steps.values() for key in plan}
        | {key for regimen in protocol.non_protocol for key in regimen}
        | set((protocol.monotherapy or {}))
        | {'amlodipine'}))
    HTN_THRESHOLD = protocol.threshold
    # Left as None when the protocol has no such rule. Defaulting them to the
    # treatment threshold made every raised reading on a ladder without entry
    # points - Egypt's - satisfy the "jump to step 3" test, so no patient ever
    # stayed on step 1 or 2.
    STEP_2_ENTRY = protocol.step_2_entry
    STEP_3_ENTRY = protocol.step_3_entry

    PILOT_STATES = dict(instance.pilot_states)
    FACILITY_LEVEL = instance.facility_level

    TARGET_PROGRAM = instance.program
    VISIT_STAGE = instance.visit_stage
    CALL_STAGE = instance.call_stage
    TARGET_TRACKED_ENTITY_TYPE = instance.tracked_entity_type

    RULE_OWNED_FIELDS = {uid: uid for uid in instance.rule_owned}

    _rebuild_protocol_tables()


def _configure_default():
    """Nigeria unless a caller says otherwise, which keeps the module usable
    on its own and gives the tables below something coherent to hold."""
    configure(htn_instances.NIGERIA)


def _rebuild_protocol_tables():
    """
    Derive the dose-to-step lookup from the ladder actually in force.

    Writing it out by hand alongside the ladder let the two drift; deriving it
    means a protocol change cannot silently stop classifying.
    """
    PROTOCOL_DOSE_STEPS.clear()
    for step, plan in PROTOCOL_STEPS.items():
        regimen = {key: code for key, code in plan.items()}
        # A ladder that names both an ARB brand and its alternative describes
        # one prescription with two spellings, so each is recorded separately.
        arbs = [k for k in ('losartan', 'telmisartan') if k in plan]
        variants = [regimen] if not arbs else [
            {k: v for k, v in regimen.items()
             if k not in ('losartan', 'telmisartan') or k == arb}
            for arb in arbs]
        for variant in variants:
            PROTOCOL_DOSE_STEPS.setdefault(regimen_doses(variant), step)


# Deliberately not populated:
#   ea5w3mkinZo Age (For TEI header) - rule XsC1SpJLyvZ derives it from the
#     date of birth. Unlike Egypt, no other Nigerian rule reads age, so there
#     is no reason to send our own copy.
#
# NCD Patient Status is a special case. Egypt omits it and lets rule
# GYmydeVoxDI fill it in, but Nigeria marks the attribute MANDATORY on the
# programme, so an enrollment without it is rejected outright:
#   "Attribute `fI1P3Mg1zOZ` is mandatory in Program `pMIglSEqPGS`".
# It is therefore sent as ACTIVE - the one value the rule itself computes, so
# the two cannot disagree - and every other status is written in a second pass
# (apply_patient_outcomes) once the rule has stopped asserting itself.
#
# On the live instance a SQL trigger moves the status to DIED or TRANSFER when
# the call centre removes a patient from the overdue list, because a program
# rule cannot write an attribute from an event's scope. The second pass is
# this script's stand-in for that trigger.

OPTION_SET = {
    'state':       'WquA76yjfsK',
    'lga':         'gA0wDJbH9Ll',
    'amlodipine':  'MZu1fOYzMVD',
    'losartan':    'Us48SsBDKT9',
    'telmisartan': 'cxTSbKLE9Uh',
    'hctz':        'uK9CK10LvRW',
    'metformin':   'XLQ9UwCSC17',
    'glibenclamide': 'VFWOz6IQTkp',
    'atorvastatin':  'IJxaRThwLnk',
    'dm_measure':  'jfub0TH87cT',
    'sugar_unit':  'fXhlavNYuUx',
    'days_of_meds': 'Ng6HKWbtTbX',
    'relationship': 'oh1zLZ9VJLF',
    'insurance':   'j2PA0iop5te',
    'defaulting':  'FE47US3QiQx',
    'call_result': 'VzsqD3ziF6c',
}


# ---------------------------------------------------------------------------
# Target metadata: data elements
# ---------------------------------------------------------------------------

DE = {
    'systolic':          'IxEwYiq1FTq',
    'diastolic':         'yNhtHKtKkO1',
    'amlodipine':        'eGY7e5Ttbys',
    'losartan':          'yUYbcCXo9Mv',
    'telmisartan':       'lWmOLo5hFYQ',
    'hctz':              'D8FWXAtCuL2',
    'other_htn_med':     'drY9ZQ0PEEw',
    'days_of_meds':      'XA4IyHW1Dt4',
    'dm_measure_type':   'cE3KOFmoYbZ',
    'blood_sugar_unit':  'Xjrw5pbxBJw',
    'fbs_mgdl':          'oOHbZP28H2l',
    'fbs_mmol':          'v7nC8vSTzwh',
    'rbs_mgdl':          'ywITVJvILBR',
    'rbs_mmol':          'n6k7wJQ1daN',
    'ppbs_mgdl':         'LktTflPfk1z',
    'ppbs_mmol':         'zgxvIQneRRX',
    'hba1c':             'JZlJfx3oihw',
    'recommended_step':  'wEVBblImCko',
    'metformin':         'UXiaSOqzFts',
    'glibenclamide':     'WW5l5hS2JvI',
    'atorvastatin':      'Y72gWULmJGT',
    'height_cm':         'BCMObXpMEzk',
    'weight_kg':         'uQwZbz0kUkZ',
    'bmi':               'J2YWUFBEyvZ',
    # Calling report stage
    'call_result':       'q362A7evMYt',
    'defaulting_reason': 'yc3PVWG7ilA',
    'remove_reason':     'MZkqsWH2KSe',
    'home_visit':        'mOMHtDNHmDA',
}

# Deliberately not populated, because a program rule owns them. Sending a value
# that differs from the rule's own calculation is rejected with E1307.
RULE_OWNED_FIELDS = {
    'BSCvNvN52sg': 'HTN - Current Step',
    'X9CTJkBSdGZ': 'HTN - Prev Step',
    'Yu0kEfd0aeX': 'HTN - Treatment recommendation',
    'gepG9GHO5zA': 'HTN - Previous BP',
    'DkRNomXUMNr': 'HTN - Previous Meds',
    'ea5w3mkinZo': 'Age (For TEI header)',
    'ifRI6qw96v4': 'HTN - Patient phone number (calling report)',
    'D9pz1vAbGPK': 'Enabler for autofilling fields',
    # Not rule-owned but plugin-owned: there is nothing to derive a risk score
    # from on this instance, and a fabricated one would be read as real.
    'qwCWN0aLHAi': 'HTN - Risk Score (number)',
    'dzlxGoRUDt9': 'HTN - Risk Score (text)',
    'm5WImperUYv': 'HTN - Risk Final Classification',
}

# Two TRUE_ONLY data elements that must never be sent as true, because each
# hands a field this script fills over to a rule:
#   wN6UhBAWFxU HTN - Auto-fill step meds  -> rules mEA5KMDVVAt / FfTnLFf4XRj /
#     EoiHm8ocrRO / NL18Cj5I40c overwrite the prescription with the step's
#     standard one, and disagree with what we sent.
#   BPeeHZn2YYi HTN - Use previous visit weight -> rule dSaIv76VJIR copies the
#     previous visit's weight over ours.
RULE_TRIGGER_FIELDS = {
    'wN6UhBAWFxU': 'HTN - Auto-fill step meds',
    'BPeeHZn2YYi': 'HTN - Use previous visit weight',
    'Q3o5XWj9dZ7': 'HTN - Recommend meds',
}

# Assigned by a rule, but sent anyway because our value is the one the rule
# computes:
#   BCMObXpMEzk Height (CM)   <- rule zoyvkFzuZIC copies TEA hTARCBirVLi, and
#                                we send the same number in both places.
#   J2YWUFBEyvZ BMI           <- rule CWH1IPHFXov uses
#                                round(weight / ((height*height)/10000), 1),
#                                which is exactly how build_visit derives it.
#   jCRIT4GMMOS has hypertension, Xjrw5pbxBJw blood sugar unit,
#   q362A7evMYt result of call, MZkqsWH2KSe remove-from-overdue
#                              <- their rules are gated on conditions this
#                                 script never satisfies (see build_call_report).
#   eGY7e5Ttbys / yUYbcCXo9Mv / lWmOLo5hFYQ / D8FWXAtCuL2 the four
#                                antihypertensives, and uQwZbz0kUkZ weight
#                              <- the rules that assign them are gated on one of
#                                 the RULE_TRIGGER_FIELDS below, or on
#                                 !d2:hasValue(#{systolicBP}), which is only
#                                 true on the scheduled visit where no
#                                 medication is sent. Neither gate is ever open
#                                 on an event this script fills in.
RULE_COMPATIBLE_FIELDS = {
    'BCMObXpMEzk', 'J2YWUFBEyvZ', 'jCRIT4GMMOS', 'Xjrw5pbxBJw',
    'q362A7evMYt', 'MZkqsWH2KSe', 'NI0QRzJvQ0k', 'oindugucx72',
    'eGY7e5Ttbys', 'yUYbcCXo9Mv', 'lWmOLo5hFYQ', 'D8FWXAtCuL2',
    'uQwZbz0kUkZ',
    # Mandatory on the programme, so it has to be sent; ACTIVE is what rule
    # GYmydeVoxDI assigns anyway.
    'fI1P3Mg1zOZ',
    # The recommendation rules cannot fire on import - see recommended_step -
    # so the field is ours to fill or it stays empty on every historical visit.
    'wEVBblImCko',
    # Rule eYluH2PKQ1I assigns this one, but its condition contains both
    # d2:hasValue(#{prevOtherHTNMed}) and !d2:hasValue(#{prevOtherHTNMed}), so
    # it can never be true and the rule can never fire.
    'drY9ZQ0PEEw',
}

# The value the NCD status rule computes for a new enrollment. Anything else in
# a creating payload is overwritten or rejected.
NCD_STATUS_ON_ENROLMENT = 'ACTIVE'

# What a freshly created enrollment leaves in the follow-up status attribute,
# whatever the payload asked for.
#
# That field is not ours either. On the live instance a nightly cron compares
# each patient's due date with today and moves the ones that have slipped to
# OVERDUE_PENDING_CALL; creation always lands on UPCOMING_VISIT. Verified on
# the server: importing a patient whose booked visit is 170 days past due, with
# the attribute set to OVERDUE_PENDING_CALL and the rule engine switched off,
# still stores UPCOMING_VISIT - so no program rule is involved and there is
# nothing to send differently.
#
# A plain attribute update after the import does stick, so apply_patient_state
# brings the cohort to the state tonight's cron would have produced. Without
# that pass a freshly generated demo instance shows every overdue patient as
# having an upcoming visit until the cron next runs.
#
# The symbolic names live with the rest of the call-centre constants below;
# this is the literal so the module imports in file order.
FOLLOW_UP_ON_ENROLMENT = 'UPCOMING_VISIT'


# ---------------------------------------------------------------------------
# Nigerian treatment protocol
#
# Federal Ministry of Health, Hypertension Treatment Protocol for Primary
# Health Care level. High blood pressure is SBP >= 140 or DBP >= 90.
#
#   Step 1  amlodipine 5 mg
#   Step 2  amlodipine 5 mg  + losartan 50 mg
#   Step 3  amlodipine 10 mg + losartan 100 mg
#   Step 4  amlodipine 10 mg + losartan 100 mg + HCTZ 25 mg
#   Step 5  refer for specialist hypertension management
#
# Entry point depends on the first reading: >= 180/110 starts at step 3 (and
# goes to an emergency unit within the hour), >= 160/100 starts at step 2,
# anything else above 140/90 starts at step 1. The target's program rules
# encode exactly this, which is why the BP bands below are built around those
# three thresholds rather than Egypt's 130/80.
#
# The protocol notes telmisartan 40/80 mg as preferable to losartan where it is
# available, so a share of patients are put on it instead; the step-computing
# rules accept either.
# ---------------------------------------------------------------------------

PROTOCOL_STEPS = {
    1: {'amlodipine': 'AMLODIPINE_5MG_OD'},
    2: {'amlodipine': 'AMLODIPINE_5MG_OD',
        'losartan': 'LOSARTAN_50MG', 'telmisartan': 'TELMISARTAN_40MG_OD'},
    3: {'amlodipine': 'AMLODIPINE_10MG_OD',
        'losartan': 'LOSARTAN_100MG', 'telmisartan': 'TELMISARTAN_80MG_OD'},
    4: {'amlodipine': 'AMLODIPINE_10MG_OD',
        'losartan': 'LOSARTAN_100MG', 'telmisartan': 'TELMISARTAN_80MG_OD',
        'hctz': 'HYDROCHLOROTHIAZIDE_25MG_OD'},
    # Step 5 is a referral, not a new prescription: the patient stays on the
    # step 4 regimen while specialist care is arranged.
    5: {'amlodipine': 'AMLODIPINE_10MG_OD',
        'losartan': 'LOSARTAN_100MG', 'telmisartan': 'TELMISARTAN_80MG_OD',
        'hctz': 'HYDROCHLOROTHIAZIDE_25MG_OD'},
}
MAX_PROTOCOL_STEP = 5

# ---------------------------------------------------------------------------
# Regimens, steps and treatment transitions
#
# The reporting side classifies each patient by what changed between two
# consecutive visits, so what matters is the pair of regimens, not the step
# number this script happened to be tracking. These helpers reproduce that
# classification exactly, which is what lets the run report say which
# categories the generated cohort actually covers instead of assuming.
#
# A regimen is {med key: option code or None} over the four structured
# antihypertensives. The free-text "other hypertension medication" field plays
# no part: the classification ignores it.
# ---------------------------------------------------------------------------

MED_KEYS = ('amlodipine', 'losartan', 'telmisartan', 'hctz')

# Dose in mg, read as the first number in the option code:
#   AMLODIPINE_5MG_OD -> 5, LOSARTAN_100MG -> 100, TELMISARTAN_80MG_OD -> 80,
#   HYDROCHLOROTHIAZIDE_25MG_OD -> 25. A blank field is dose 0.
DOSE_PATTERN = re.compile(r'\d+')


def dose_of(code):
    """The dose a medication option code carries, or 0 when it is blank."""
    if not code or not str(code).strip():
        return 0
    found = DOSE_PATTERN.search(str(code))
    return int(found.group()) if found else 0


def regimen_doses(regimen):
    """
    The doses that identify a step on this instance's ladder, in mg.

    Which fields those are comes from the protocol: Nigeria is identified by
    (amlodipine, ARB, thiazide), Egypt by (combined pill, thiazide). Losartan
    and telmisartan are interchangeable ARBs, so 'arb' is whichever of the two
    is recorded - the larger, when both somehow are.
    """
    doses = []
    for key in PROTOCOL_DOSE_KEYS:
        if key == 'arb':
            doses.append(max(dose_of(regimen.get('losartan')),
                             dose_of(regimen.get('telmisartan'))))
        else:
            doses.append(dose_of(regimen.get(key)))
    return tuple(doses)


# (amlodipine, ARB, HCTZ) -> protocol step. Any other recorded combination is
# non-protocol: amlodipine 10 with no ARB, amlodipine 10 with ARB 50, an ARB
# on its own, and so on.
PROTOCOL_DOSE_STEPS = {
    (5, 0, 0): 1,
    (5, 40, 0): 2, (5, 50, 0): 2,
    (10, 80, 0): 3, (10, 100, 0): 3,
    (10, 80, 25): 4, (10, 100, 25): 4,
}


def regimen_is_empty(regimen):
    """True when none of the four structured medications is recorded."""
    return not any(regimen.get(key) for key in MED_KEYS)


def regimen_step(regimen):
    """The protocol step a regimen matches, or None when it matches none."""
    return PROTOCOL_DOSE_STEPS.get(regimen_doses(regimen))


# The category names below are the ones the dashboard uses, spelling included.
CAT_NO_DATA = 'No medication data'
CAT_STOPPED = 'Stopped treatment'
CAT_INITIATED_NON_PROTOCOL = 'Initiated on non-protocol treatment'
CAT_TITRATED_1 = 'Titrated by 1 step'
CAT_TITRATED_2 = 'Titrated by 2 or more steps'
CAT_SAME_STEP = 'In-protocol same step'
CAT_DE_ESCALATION = 'In-protocol de-escalation'
CAT_NP_TITRATION = 'Non-protocol titration'
CAT_NP_DE_ESCALATION = 'Non-protocol de-escalation'
# "occured" is deliberate: it matches the DHIS2 option.
CAT_NP_BOTH = 'Non-protocol titration and de-escalation (both occured)'
CAT_NP_NO_CHANGE = 'Non-protocol with no change'

TRANSITION_CATEGORIES = [
    CAT_NO_DATA, CAT_STOPPED,
    'Initiated on step 1', 'Initiated on step 2', 'Initiated on step 3',
    'Initiated on step 4', CAT_INITIATED_NON_PROTOCOL,
    CAT_TITRATED_1, CAT_TITRATED_2, CAT_SAME_STEP, CAT_DE_ESCALATION,
    CAT_NP_TITRATION, CAT_NP_DE_ESCALATION, CAT_NP_BOTH, CAT_NP_NO_CHANGE,
]


def classify_transition(previous, current, had_earlier_meds):
    """
    The treatment-transition category for one visit, given the regimen at the
    visit before it and whether any earlier visit in the enrollment recorded
    structured medication at all.

    The rules are applied in order; the first match wins. This is a direct
    transcription of the reporting definition, kept here so the generator can
    check its own output rather than guess at the coverage it produces.
    """
    if regimen_is_empty(current):
        # Nothing recorded now: either nothing ever was, or treatment stopped.
        return CAT_STOPPED if had_earlier_meds else CAT_NO_DATA

    if not had_earlier_meds:
        step = regimen_step(current)
        return f'Initiated on step {step}' if step else CAT_INITIATED_NON_PROTOCOL

    previous_step, current_step = regimen_step(previous), regimen_step(current)

    if previous_step and current_step:
        if current_step >= previous_step + 2:
            return CAT_TITRATED_2
        if current_step == previous_step + 1:
            return CAT_TITRATED_1
        if current_step == previous_step:
            return CAT_SAME_STEP
        return CAT_DE_ESCALATION

    # One side or both are non-protocol: compare the three doses directly.
    ups = downs = 0
    for before, after in zip(regimen_doses(previous), regimen_doses(current)):
        ups += after > before
        downs += after < before

    if ups and downs:
        return CAT_NP_BOTH
    if ups:
        return CAT_NP_TITRATION
    if downs:
        return CAT_NP_DE_ESCALATION
    return CAT_NP_NO_CHANGE

# The protocol's own thresholds, in mmHg.
HTN_THRESHOLD = (140, 90)
STEP_2_ENTRY = (160, 100)
STEP_3_ENTRY = (180, 110)

# Losartan is the protocol default; telmisartan is the stated preference where
# stocked. Both appear, because a real programme's stock varies by facility.
TELMISARTAN_SHARE = 0.25

# Combinations that are recorded in practice but match no step. Every one is
# checked against PROTOCOL_DOSE_STEPS at import time (see
# check_non_protocol_regimens) so a later edit cannot quietly turn one of them
# back into a protocol regimen.
NON_PROTOCOL_REGIMENS = [
    # Amlodipine pushed to 10 without adding an ARB - the commonest real
    # deviation, and the only move open to a patient who cannot take an ARB.
    {'amlodipine': 'AMLODIPINE_10MG_OD'},
    # Dose mismatch between the two halves of the combination.
    {'amlodipine': 'AMLODIPINE_10MG_OD', 'losartan': 'LOSARTAN_50MG'},
    {'amlodipine': 'AMLODIPINE_5MG_OD', 'losartan': 'LOSARTAN_100MG'},
    {'amlodipine': 'AMLODIPINE_10MG_OD', 'telmisartan': 'TELMISARTAN_40MG_OD'},
    # An ARB with no calcium channel blocker behind it.
    {'losartan': 'LOSARTAN_50MG'},
    {'losartan': 'LOSARTAN_100MG'},
    {'telmisartan': 'TELMISARTAN_80MG_OD'},
    # A thiazide brought in before the ladder calls for it.
    {'hctz': 'HYDROCHLOROTHIAZIDE_25MG_OD'},
    {'amlodipine': 'AMLODIPINE_5MG_OD', 'hctz': 'HYDROCHLOROTHIAZIDE_25MG_OD'},
    {'amlodipine': 'AMLODIPINE_10MG_OD', 'hctz': 'HYDROCHLOROTHIAZIDE_25MG_OD'},
    {'amlodipine': 'AMLODIPINE_5MG_OD', 'losartan': 'LOSARTAN_50MG',
     'hctz': 'HYDROCHLOROTHIAZIDE_25MG_OD'},
]

# How a patient's prescribing history departs from the protocol, if at all.
# Weights, not probabilities: one behaviour per patient, drawn once, so each
# category below is reliably represented rather than left to independent
# per-visit coin flips that might never coincide.
#
# Following the protocol is the norm, which is what the dashboard should show.
TITRATION_BEHAVIOURS = {
    'protocol': 68,
    # Started on a step the first reading did not call for.
    'wrong_start': 6,
    # The first regimen on record matches no step at all.
    'non_protocol_start': 4,
    # One visit with no medication recorded, then treatment resumes. Reads as
    # "Stopped treatment" at that visit and a fresh start at the next.
    'interrupted': 6,
    # Moved down the ladder while still above target. Weighted higher than the
    # share of de-escalations it produces: about 40% of these patients never
    # leave step 1, and there is nowhere below it to move, so the rest
    # contribute nothing to the category.
    'de_escalator': 7,
    # Switched onto a combination that matches no step, for a visit or two.
    'non_protocol_switch': 7,
    # One drug increased while another was reduced in the same change.
    'erratic': 2,
    # Jumped two steps at once.
    'double_jump': 2,
}

# Antihypertensives outside the protocol, recorded as free text in "Other
# hypertension medication". The classification ignores this field entirely, so
# these are realism rather than signal - but they are what a clinic actually
# writes there, including the amiloride/HCTZ substitute the protocol names for
# when plain HCTZ is out of stock.
OTHER_HTN_MEDS = [
    'Nifedipine retard 20mg BD',
    'Nifedipine 10mg TDS',
    'Methyldopa 250mg TDS',
    'Lisinopril 10mg OD',
    'Lisinopril 5mg OD',
    'Bisoprolol 5mg OD',
    'Atenolol 50mg OD',
    'Carvedilol 12.5mg BD',
    'Spironolactone 25mg OD',
    'Furosemide 40mg OD',
    'Amiloride 2.5mg/HCTZ 25mg OD',
    'Moduretic OD',
]

# Hydrochlorothiazide 12.5mg is no longer available in Nigeria. The target's
# option set carries only the 25mg code, and nothing in this script may emit a
# 12.5mg value - check_target_metadata enforces both ends of that.
HCTZ_WITHDRAWN_DOSE = 12.5

# Chance of stepping up when a visit reads above target. Escalating on every
# single raised reading marches the whole cohort to step 4, which is not what a
# real treatment-step breakdown looks like; some clinical inertia is genuine.
ESCALATION_PROBABILITY = 0.90

# Step 5 is a referral out of primary care, not another prescription, so it is
# a far bigger decision than moving from step 3 to step 4. Escalating 4 -> 5 at
# the ordinary rate left almost a third of the cohort sitting at "refer for
# specialist management", which is not what a treatment-step breakdown looks
# like in a programme that is mostly managing people successfully.
REFERRAL_PROBABILITY = 0.55

# Losartan must not be given to pregnant women or to women of childbearing age
# who are not on effective contraception. Pregnancy is not modelled - the
# programme records none - so women below this age are kept on amlodipine
# monotherapy rather than being escalated onto an ARB.
CHILDBEARING_AGE_MAX = 49

# Blood pressure bands, built around the protocol's thresholds.
BP_NORMAL = ((105, 125), (65, 78))             # never had hypertension
BP_HTN_AT_TARGET = ((120, 138), (72, 88))      # on treatment, below 140/90
BP_HTN_ABOVE_TARGET = ((140, 159), (90, 99))   # raised, below the step-2 entry
BP_HTN_UNCONTROLLED = ((160, 199), (100, 119))  # at or above the step-2 entry

# Oral antidiabetics and the statin the Nigerian form carries. Egypt's long
# list of gliptins, gliflozins and insulins has no counterpart here: these
# three option sets are the whole formulary.
METFORMIN_CODES = ['METFORMIN_250MG', 'METFORMIN_500MG', 'METFORMIN_1000MG']
GLIBENCLAMIDE_CODES = ['GLIBENCLAMIDE_2POINT5MG_OD', 'GLIBENCLAMIDE_5MG_OD',
                       'GLIBENCLAMIDE_10MG_OD']
ATORVASTATIN_CODES = ['ATORVASTATIN_10MG_OD', 'ATORVASTATIN_20MG_OD']

# Clinical detail only some instances carry. Egypt records a lipid panel, a
# statin and an insulin regimen; Nigeria and the demo have no data element for
# any of it, so the blocks that fill them never run there.
STATIN_CODES = [
    'ATORVASTATIN_10MG', 'ATORVASTATIN_20MG', 'ATORVASTATIN_40MG',
    'ROSUVASTATIN_10MG', 'ROSUVASTATIN_20MG',
]

MULTI_ANTIDIABETIC_CODES = [
    'METFORMIN_500MG_OD', 'METFORMIN_500MG_BD', 'METFORMIN_850MG_OD',
    'METFORMIN_1000MG_OD', 'METFORMIN_1000MG_BD',
    'GLIBENCLAMIDE_5MG_OD', 'GLIMEPIRIDE_2MG_OD', 'GLIMEPIRIDE_4MG_OD',
    'GLICLAZIDE_MR_30MG_OD', 'GLICLAZIDE_MR_60MG_OD',
    'VILDAGLIPTIN_50MG_BD', 'PIOGLITAZONE_30MG_OD',
    'DAPAGLIFLOZIN_10MG_OD', 'EMPAGLIFLOZIN_10MG_OD',
]

# Basal once daily, premixed twice daily, rapid-acting with meals, following
# WHO practice. Doses stay well under the 100-unit form limit.
INSULIN_REGIMENS = {
    'basal': {'names': ['INSULIN_GLARGINE_100', 'INSULIN_DETEMIR',
                        'INSULIN_DEGLUDEC'],
              'frequency': 'OD', 'dose': (10, 40)},
    'premixed': {'names': ['INSULIN_MIXTARD_70_30'],
                 'frequency': 'BD', 'dose': (10, 30)},
    'rapid': {'names': ['INSULIN_ASPART', 'INSULIN_LISPRO', 'INSULIN_REGULAR'],
              'frequency': 'TDS', 'dose': (4, 12)},
}

MULTI_VALUE_SEPARATOR = ','

DAYS_OF_MEDICATION_CODES = ['1', '2', '3']      # 30 / 60 / 90 days
INSURANCE_CODES = ['INSURANCE_FEDERAL', 'INSURANCE_STATE']
RELATIONSHIP_CODES = ['1', '2', '3', '4', '5', '6', '7', '8', '9']
TREATED_FIRST_TIME, TREATED_IN_PAST = '1', '2'

# ---------------------------------------------------------------------------
# The call centre
#
# A patient whose booked visit has come and gone is on the overdue list, which
# is what the Calling report stage works through. One call has one of three
# outcomes:
#
#   AGREE_TO_VISIT        the patient will come back; the chase stops here and
#                         the next visit follows, later than it was due
#   REMIND_TO_CALL_LATER  no answer or no commitment; another call follows
#   REMOVE_FROM_OVERDUE   the patient comes off the list, and the reason given
#                         is what decides their NCD Patient Status
#
# Result of call is COMPULSORY on the stage and a program rule's ASSIGN does
# not satisfy that check on import (E1303), so every call sends it explicitly
# rather than leaving the target's rules to derive it from the defaulting
# reason.
# ---------------------------------------------------------------------------

CALL_AGREE = 'AGREE_TO_VISIT'
CALL_REMIND = 'REMIND_TO_CALL_LATER'
CALL_REMOVE = 'REMOVE_FROM_OVERDUE'

# What each removal reason means for the patient's NCD Patient Status.
#
# On the live instance that update is made by a SQL trigger, not by a program
# rule. Program rules aauUMLP5O94 and B9aztxgueSs look as though they do it,
# but a rule evaluated in an event's scope cannot write a tracked entity
# attribute, so they never take effect from the Calling report stage. Verified
# against the server: a calling report carrying remove reason DIED still
# requires the attribute to be sent as ACTIVE, and the import is rejected with
# E1309 if it is not.
#
# apply_patient_outcomes therefore stands in for the trigger, applying the
# status in a second pass once the record exists.
REMOVAL_OUTCOME = {
    'DIED': 'DIED',
    'TRANSFERRED_TO_ANOTHER_FACILITY': 'TRANSFER',
    'MOVED_TO_PRIVATE_PRACTITIONER': 'TRANSFER',
    'MOVED': 'INACTIVE',
    'NOT_RESPONDING': 'INACTIVE',
    'NUMBER_DOES_NOT_WORK': 'INACTIVE',
    'REFUSED_TO_COME_BACK': 'INACTIVE',
    # The patient is off the list but still in care, so nothing to apply.
    'HAS_ALREADY_VISITED': 'ACTIVE',
    'OTHER': 'ACTIVE',
}

# How often the call centre reaches each conclusion when it does remove
# someone. Weighted so the three terminal statuses all appear without the
# cohort looking like it is haemorrhaging patients.
REMOVAL_REASON_WEIGHTS = {
    'NOT_RESPONDING': 25,
    'NUMBER_DOES_NOT_WORK': 15,
    'TRANSFERRED_TO_ANOTHER_FACILITY': 15,
    'MOVED': 12,
    'REFUSED_TO_COME_BACK': 10,
    'MOVED_TO_PRIVATE_PRACTITIONER': 8,
    'DIED': 10,
    'HAS_ALREADY_VISITED': 5,
    'OTHER': 3,
}

# Three defaulting reasons are wired to rules ogOxhaCpttD, KCaL3ieVdaJ and
# kVGMxyBfPQD. These ones do take effect on import, because they assign data
# elements inside the same event rather than a tracked entity attribute.
# Pairing each removal with the defaulting reason that derives it means our
# values and the rules' are identical, so the rule fires and agrees instead of
# rejecting the event with E1307.
REMOVAL_DEFAULTING_REASON = {
    'DIED': 'DEAD',
    'TRANSFERRED_TO_ANOTHER_FACILITY': 'SELF_TRANSFER',
    'MOVED': 'RELOCATED',
}

# Everything else the patient might say. DEAD, RELOCATED and SELF_TRANSFER are
# absent on purpose: they would trigger the rules above and overwrite a removal
# reason we did not intend.
PLAIN_DEFAULTING_REASONS = ['DISTANCE', 'LACK_OF_FUNDS', 'SICK', 'NO_SYMPTOMS',
                            'SIDE_EFFECT', 'BUSY', 'TAKING_HERBS', 'TRAVELLED',
                            'PREGNANT', 'OTHER']

# PREGNANT is only an answer a woman of childbearing age can give.
SEX_SPECIFIC_DEFAULTING = {'PREGNANT'}

# Every code the script can put in either calling-report option set, for
# check_target_metadata.
CALL_RESULTS = [CALL_AGREE, CALL_REMIND, CALL_REMOVE]
DEFAULTING_REASONS = sorted(set(PLAIN_DEFAULTING_REASONS)
                            | set(REMOVAL_DEFAULTING_REASON.values()))
REMOVAL_REASONS = sorted(REMOVAL_REASON_WEIGHTS)

# A visit this many days past its due date is one the call centre chased.
CALL_CHASE_MIN_DAYS = 10
MAX_CHASE_CALLS = 3
HOME_VISIT_SHARE = 0.12

FOLLOW_UP_UPCOMING = 'UPCOMING_VISIT'
FOLLOW_UP_PENDING = 'OVERDUE_PENDING_CALL'
FOLLOW_UP_CALLED = 'CALLED'

assert FOLLOW_UP_ON_ENROLMENT == FOLLOW_UP_UPCOMING

NCD_STATUS_MAP = {'UNRESPONSIVE': 'INACTIVE'}
VALID_NCD_STATUS = {'ACTIVE', 'TRANSFER', 'DIED', 'INACTIVE'}

SCHEDULED_VISIT_INTERVAL_DAYS = 28

# The protocol reviews monthly, so a patient who keeps their appointment comes
# back 28 to 31 days later: on the day it was booked for, or a day or two
# after. Nothing earlier - a visit before its own due date reads as the patient
# turning up unannounced, not as a kept appointment.
PUNCTUAL_GAP_DAYS = (28, 31)
LATE_GAP_DAYS = (45, 200)

DEFAULT_VISITS_MIN = 1
# A safety cap, not a target: the visit count falls out of how long the patient
# has been enrolled. Set it too low and a long-standing patient has their
# monthly visits truncated and then one recent one, leaving a fabricated gap.
DEFAULT_VISITS_MAX = 60

DEFAULT_WINDOW_MONTHS = 12

# Enrolments are spread over the last fifteen months by default. Not twelve:
# the indicators that matter count patients "enrolled more than three months
# ago and seen recently", so a twelve-month window leaves that denominator
# made up only of the earliest arrivals. Fifteen gives a full year of patients
# who already qualify.
#
# A monthly top-up run narrows it with --enrol-from / --enrol-to: point both
# at the current month and the run adds that month's new arrivals, leaving
# everyone already registered alone.
DEFAULT_ENROLMENT_WINDOW_DAYS = 456      # 15 months
MIN_USEFUL_WINDOW_DAYS = 90
RECENT_VISIT_WINDOW_DAYS = 90

# Nigeria screens every adult from 18, so the cohort is not pinned to a
# risk-chart window the way Egypt's is.
MIN_PATIENT_AGE = 18
MAX_PATIENT_AGE = 90


# ---------------------------------------------------------------------------
# Population mix
# ---------------------------------------------------------------------------

# Every proportion the generated population is steered by. Override any of them
# on the command line:
#
#   --mix controlled_min=0.45 --mix diabetic=0.2
#   --mix-file nigeria_mix.json
#
# Values may be given as fractions (0.45) or percentages (45).
DEFAULT_MIX = {
    # The headline rates are BANDS, not points: each calendar month picks its
    # own value inside the band, so trend lines fluctuate instead of running
    # flat. Controlled means below the Nigerian target of 140/90.
    'controlled_min': 0.40,
    'controlled_max': 0.55,
    # Uncontrolled means at or above 160/100, the step-2 entry threshold.
    # Whatever is left over sits between 140/90 and 160/100.
    'uncontrolled_min': 0.15,
    'uncontrolled_max': 0.25,
    # No visit recorded in the last three months.
    'overdue_min': 0.10,
    'overdue_max': 0.30,
    # Nigeria's programme is hypertension-led; diabetes is the smaller cohort.
    'hypertensive': 0.88,
    'diabetic': 0.22,
    'dm_controlled': 0.45,
    'female': 0.58,
    'late_at_visit_2': 0.22,
    'late_at_visit_3': 0.18,
    'late_all_visits': 0.06,
    'lost_to_followup': 0.05,
    # --- The call centre -------------------------------------------------
    # Of the patients whose booked visit is overdue RIGHT NOW, how the list
    # has been worked. The three shares below are of that group, not of the
    # whole cohort; whatever is left over has been called and is still on the
    # list waiting to come back.
    'overdue_not_yet_called': 0.35,   # no Calling report at all -> PENDING
    'overdue_removed': 0.20,          # taken off the list; reason sets status
    'overdue_agreed': 0.25,           # last call ended in AGREE_TO_VISIT
    # Patients whose status was changed at the facility rather than by the
    # call centre, so a cohort with few overdue patients still carries some of
    # each terminal status. Applied as an NCD Patient Status update after
    # import, the same second pass the removals use.
    'status_died_no_call': 0.008,
    'status_transferred_no_call': 0.010,
    'status_inactive_no_call': 0.015,
    'appointment_overdue': 0.25,
    'took_meds_before': 0.35,
    'smoker_male': 0.10,         # Nigerian adult male smoking prevalence is low
    'smoker_female': 0.01,
    'history_mi': 0.04,
    'history_stroke': 0.05,
    'history_ckd': 0.04,
    'mistitrated': 0.10,
    # Nigeria-specific coverage shares.
    'insured': 0.15,             # BHCPF or SHIA enrolment is still uncommon
    'sms_consent': 0.70,
    'has_supporter': 0.55,
    'has_phone': 0.85,
    'id_number_known': 0.60,
    'statin_if_diabetic': 0.30,
    # Share of women of childbearing age who are not on effective
    # contraception, and for whom an ARB is therefore contraindicated.
    'arb_contraindicated_if_woman': 0.35,
    # Share of patients carrying a non-protocol antihypertensive in the
    # free-text "Other" field alongside their structured prescription.
    'other_htn_med': 0.06,
    # Only used by instances that record a lipid panel, a statin or insulin.
    'high_cholesterol': 0.40,
    'statin_missed': 0.02,
    'insulin_if_diabetic': 0.25,
    'labs_at_visit': 0.55,       # a blood sugar reading taken at this visit
    # Share of LATE visits the call centre chased before the patient came
    # back. Those chases are what put Calling reports in between two visits,
    # which is where the call-centre indicators get their denominator.
    'chased_if_late': 0.80,
}

MIX = dict(DEFAULT_MIX)


def p(name):
    """A probability from the population mix."""
    return MIX[name]


def normalise_mix_value(key, raw):
    """Accept either a fraction (0.45) or a percentage (45)."""
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


def check_mix_feasible():
    """Warn when the requested bands cannot be met at this prevalence."""
    p_htn = p('hypertensive')
    never_htn = 1.0 - p_htn

    if MIX['controlled_min'] < never_htn:
        logger.warning(
            f"controlled_min={MIX['controlled_min']:.0%} is below the "
            f"{never_htn:.0%} of patients who are not hypertensive and always "
            f"read under 140/90, so the controlled rate cannot fall that low."
        )

    if p_htn > 0:
        worst = ((MIX['controlled_max'] - never_htn) / p_htn
                 + MIX['uncontrolled_max'] / p_htn)
        if worst > 1.0:
            logger.warning(
                f"controlled_max={MIX['controlled_max']:.0%} and "
                f"uncontrolled_max={MIX['uncontrolled_max']:.0%} cannot both be "
                f"met at {p_htn:.0%} hypertensive prevalence; the controlled "
                f"band wins and uncontrolled will come out lower than asked."
            )


BMI_BANDS = [
    ('normal', (18.5, 24.9), 0.45),
    ('overweight', (25.0, 29.9), 0.33),
    ('obese', (30.0, 39.5), 0.22),
]

# Nigerian adult mean stature, cm.
HEIGHT_BY_SEX = {'MALE': (167.0, 7.0), 'FEMALE': (158.0, 6.5)}


# ---------------------------------------------------------------------------
# Identity
#
# Nigeria stores the whole name in one attribute, so these pools are combined
# into a single string rather than filling separate given / family fields.
# Keyed by pilot state: Ogun is Yoruba-speaking and Kano Hausa-speaking, so
# drawing from one national pool would produce names that read as out of place
# wherever they landed.
# ---------------------------------------------------------------------------

YORUBA_GIVEN = {
    'MALE': ['Adebayo', 'Olusegun', 'Babatunde', 'Oluwaseun', 'Ayodele',
             'Oluwole', 'Adewale', 'Kehinde', 'Taiwo', 'Olumide', 'Femi',
             'Tunde', 'Gbenga', 'Akinola', 'Bolaji', 'Yemi', 'Dare',
             'Oluwafemi', 'Rotimi', 'Segun', 'Niyi', 'Kunle', 'Dapo'],
    'FEMALE': ['Folake', 'Adunni', 'Bukola', 'Omolara', 'Yewande', 'Titilayo',
               'Abimbola', 'Funmilayo', 'Ronke', 'Temitope', 'Oluwakemi',
               'Adenike', 'Morenike', 'Shade', 'Bisola', 'Simisola',
               'Oluwatoyin', 'Damilola', 'Iyabo', 'Toyin', 'Modupe', 'Kehinde'],
}
YORUBA_FAMILY = [
    'Adeyemi', 'Ogundipe', 'Afolabi', 'Balogun', 'Ojo', 'Adebanjo', 'Sanusi',
    'Oyelaran', 'Ogunleye', 'Fashola', 'Adesina', 'Olatunji', 'Akinyemi',
    'Odunsi', 'Shobowale', 'Ogunbiyi', 'Alabi', 'Oluwadare', 'Ariyo',
    'Bankole', 'Soyinka', 'Awolowo', 'Onabanjo', 'Kuforiji', 'Odebunmi',
]

HAUSA_GIVEN = {
    'MALE': ['Abubakar', 'Musa', 'Ibrahim', 'Usman', 'Sani', 'Aliyu', 'Bello',
             'Yusuf', 'Nasiru', 'Kabiru', 'Lawal', 'Garba', 'Shehu', 'Umar',
             'Salisu', 'Abdullahi', 'Mustapha', 'Auwal', 'Haruna', 'Idris',
             'Suleiman', 'Nuhu', 'Danjuma'],
    'FEMALE': ['Aisha', 'Hauwa', 'Zainab', 'Fatima', 'Maryam', 'Hadiza',
               'Amina', 'Rukayya', 'Halima', 'Safiya', 'Bilkisu', 'Jamila',
               'Hafsat', 'Asma’u', 'Maimuna', 'Zulaihat', 'Rabi',
               'Khadija', 'Ramatu', 'Salamatu', 'Binta', 'Larai'],
}
HAUSA_FAMILY = [
    'Abdullahi', 'Mohammed', 'Danladi', 'Gwarzo', 'Rano', 'Dantata',
    'Tijjani', 'Maikano', 'Kano', 'Bichi', 'Dawakin', 'Gezawa', 'Karaye',
    'Wudil', 'Shanono', 'Takai', 'Rimi', 'Jibrin', 'Galadima', 'Madaki',
    'Sarki', 'Inuwa', 'Yakasai', 'Ringim',
]

NAME_POOLS = {
    'Ogun': (YORUBA_GIVEN, YORUBA_FAMILY),
    'Kano': (HAUSA_GIVEN, HAUSA_FAMILY),
}

# Nigerian mobile network codes, in the national 0XXX form the programme keeps.
NIGERIA_MOBILE_PREFIXES = [
    '0803', '0806', '0703', '0706', '0813', '0816', '0810', '0814', '0903',
    '0906', '0805', '0807', '0705', '0815', '0811', '0905', '0802', '0808',
    '0708', '0812', '0902', '0901', '0809', '0817', '0818', '0908', '0909',
]

STREET_TYPES = ['Street', 'Road', 'Close', 'Crescent', 'Avenue', 'Lane', 'Way']
AREA_WORDS = ['Quarters', 'Layout', 'Estate', 'Area', 'Junction', 'Market Road']


EGYPT_GIVEN_NAMES = {
    'MALE': ['Ahmed', 'Mohamed', 'Mahmoud', 'Mostafa', 'Youssef', 'Omar',
             'Khaled', 'Hassan', 'Hussein', 'Ibrahim', 'Karim', 'Tarek',
             'Sherif', 'Amr', 'Ayman', 'Sayed', 'Ashraf', 'Waleed', 'Nasser',
             'Adel', 'Sameh', 'Hany', 'Emad', 'Gamal', 'Salah', 'Yasser'],
    'FEMALE': ['Fatma', 'Aisha', 'Mona', 'Heba', 'Nour', 'Salma', 'Yasmin',
               'Amira', 'Dalia', 'Rania', 'Hoda', 'Mervat', 'Nadia', 'Shaimaa',
               'Marwa', 'Eman', 'Sara', 'Doaa', 'Ghada', 'Asmaa', 'Samah',
               'Zeinab', 'Safaa', 'Hanan', 'Reham'],
}

EGYPT_FAMILY_NAMES = [
    'Abdel Rahman', 'Abdel Aziz', 'Hassan', 'Ibrahim', 'Mahmoud', 'Ali',
    'Mohamed', 'El Sayed', 'Farouk', 'Mansour', 'Shawky', 'Fahmy', 'Zaki',
    'Ramadan', 'Soliman', 'Kamel', 'Nagy', 'Riad', 'Sabry', 'Tawfik',
    'Abdallah', 'El Masry', 'El Sherbiny', 'Shokry', 'Helmy', 'Bakr',
    'Gaber', 'Hegazy', 'Lotfy', 'Nabil', 'Rashad', 'Sedky', 'Wahba',
]

EGYPT_MOBILE_PREFIXES = ['10', '11', '12', '15']


def egyptian_phone():
    """An Egyptian mobile number in international format."""
    return f"+20{random.choice(EGYPT_MOBILE_PREFIXES)}{random.randint(0, 99999999):08d}"


def egyptian_national_id(dob_iso, sex, governorate_digits):
    """
    A 14-digit Egyptian National ID whose embedded demographics match the
    patient it belongs to.

        C YYMMDD GG SSS X K
        C   century: 2 = 1900-1999, 3 = 2000-2099
        GG  governorate code
        SSS serial
        X   gender: odd male, even female
        K   check digit

    The platform reads date of birth, governorate and sex out of those
    positions but performs no real checksum, so K is computed conventionally
    purely so the number is well formed.
    """
    year, month, day = int(dob_iso[:4]), int(dob_iso[5:7]), int(dob_iso[8:10])
    century = '2' if year < 2000 else '3'

    # Serial and gender digit are separate positions: the gender digit is odd
    # for a man and even for a woman, and the serial is free. Folding the two
    # together produced IDs the Egypt instance's own validation rule rejected.
    serial = f"{random.randint(0, 999):03d}"
    gender_digit = random.choice([1, 3, 5, 7, 9] if sex == 'MALE'
                                 else [0, 2, 4, 6, 8])

    first_13 = (f"{century}{year % 100:02d}{month:02d}{day:02d}"
                f"{governorate_digits}{serial}{gender_digit}")

    weights = [2, 7, 6, 5, 4, 3, 2, 7, 6, 5, 4, 3, 2]
    total = sum(int(d) * w for d, w in zip(first_13, weights))
    check_digit = (11 - (total % 11)) % 11
    if check_digit == 10:
        check_digit = 0

    return f"{first_13}{check_digit}"


def phc_id():
    """An arbitrary primary health centre identifier."""
    return fake.bothify(text='??-####',
                        letters='ABCDEFGHJKLMNPRSTUVWXYZ').upper()


def nigerian_phone():
    """A Nigerian mobile number in the national format the programme stores."""
    return f"{random.choice(NIGERIA_MOBILE_PREFIXES)}{random.randint(0, 9999999):07d}"


def draw_identity_name(sex, state_name):
    """A given and a family name from this instance's pool."""
    if INSTANCE and INSTANCE.identity == 'egypt':
        return (random.choice(EGYPT_GIVEN_NAMES[sex]),
                random.choice(EGYPT_FAMILY_NAMES))
    return draw_name(sex, state_name)


def draw_phone():
    """A phone number in this instance's national format."""
    if INSTANCE and INSTANCE.identity == 'egypt':
        return egyptian_phone()
    return nigerian_phone()


def draw_name(sex, state_name):
    """
    A given and a family name from the pool for the patient's region.

    Kept as two values rather than one string because the instances disagree
    about how many attributes a name occupies - see name_attributes.
    """
    given_pool, family_pool = NAME_POOLS.get(state_name, (YORUBA_GIVEN, YORUBA_FAMILY))
    return random.choice(given_pool[sex]), random.choice(family_pool)


def patient_name(sex, state_name):
    """One combined name, for the places that want a single string."""
    given, family = draw_name(sex, state_name)
    return f"{given} {family}"


def street_address(lga_name, state_name):
    """
    A plausible address inside the patient's LGA.

    The LGA option names carry a state prefix and the words "Local Government
    Area" ("og Abeokuta North Local Government Area"); the address only wants
    the place itself. Streets are named from the same pool as people, because
    that is how Nigerian streets are named.
    """
    place = lga_name
    for suffix in (' Local Government Area', ' Local Government'):
        place = place.replace(suffix, '')
    place = place[3:] if place[:3].islower() and ' ' in place[:4] else place

    _given, family_pool = NAME_POOLS.get(state_name, (YORUBA_GIVEN, YORUBA_FAMILY))

    return (f"{random.randint(1, 240)} {random.choice(family_pool)} "
            f"{random.choice(STREET_TYPES)}, {random.choice(AREA_WORDS)}, "
            f"{place.strip()}")


def test_qr_code():
    """
    A QR code value that is obviously a generated record.

    sync_patients.py already marks the anonymised records it writes into this
    sandbox with a 'test-' prefix; keeping the same convention means one filter
    finds every artificial patient on the instance.
    """
    # Drawn from the seeded generator, not uuid4: uuid4 reads os.urandom and
    # ignores random.seed(), so the same --seed produced a different QR code
    # every run - and the QR code is how apply_patient_state finds a patient
    # again after the import.
    return f"test-{uuid.UUID(int=random.getrandbits(128), version=4)}"


def test_id_number():
    """A PHC card number, marked as generated for the same reason."""
    return f"test/{random.randint(1000, 999999)}/{random.randint(1000, 999999)}"


def sample_height(sex):
    """Adult stature in cm, with enough spread to move BMI around."""
    mean, sd = HEIGHT_BY_SEX.get(sex, HEIGHT_BY_SEX['MALE'])
    return round(min(max(random.gauss(mean, sd), 140.0), 196.0), 1)


def sample_bmi_target():
    _labels, bands, weights = zip(*BMI_BANDS)
    band = random.choices(bands, weights=weights, k=1)[0]
    return round(random.uniform(*band), 1)


def round_half_up(value, places=1):
    """
    Round the way DHIS2's d2:round does, not the way Python does.

    Python rounds halves to even - round(21.25, 1) is 21.2 - while the rule
    engine rounds them up to 21.3. On an instance whose BMI rule is active,
    that one-digit disagreement is rejected with E1307 and loses the patient.
    """
    quantum = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def weight_for(height_cm, bmi):
    """Weight in kg implied by a height and BMI, clamped to the form's bounds."""
    # The lower bound is 40, not 30: the Egypt instance has a weight-range
    # rule that refuses anything under it, and an adult below 40kg is not a
    # plausible patient anyway.
    weight = bmi * (height_cm / 100.0) ** 2
    return round(min(max(weight, 40.0), 250.0), 1)


# ---------------------------------------------------------------------------
# Geography
# ---------------------------------------------------------------------------

def build_geography_index(api):
    """
    Map every pilot facility to the State and LGA option codes that describe
    where it actually is.

    The hierarchy already knows: a facility sits under a ward, under an LGA,
    under a state. The LGA option set uses the same names as the LGA org units
    ("og Abeokuta North Local Government Area"), and the State option set the
    same names as the state org units, so the two can be joined by name instead
    of being hashed into an arbitrary code. That is what keeps a patient's
    recorded LGA agreeing with the facility they were seen at.

    Returns {facility_uid: {'name', 'state_code', 'state_name', 'lga_code',
                            'lga_name'}}.
    """
    if INSTANCE and INSTANCE.geography == 'egypt_option_sets':
        return egypt_geography_index(api)

    if INSTANCE and INSTANCE.geography != 'nigeria_hierarchy':
        # This instance records no state or LGA on the patient, so there is
        # nothing to join against the hierarchy: every facility the programme
        # is assigned to is usable as it stands.
        return facilities_without_geography(api)

    state_options = {o['name']: o['code'] for o in api.get(
        f"optionSets/{OPTION_SET['state']}",
        params={'fields': 'options[code,name]'}).json()['options']}
    lga_options = {o['name']: o['code'] for o in api.get(
        f"optionSets/{OPTION_SET['lga']}",
        params={'fields': 'options[code,name]'}).json()['options']}

    index = {}
    unmatched_lgas = set()

    for state_uid, state_label in PILOT_STATES.items():
        units = api.get('organisationUnits', params={
            'filter': f'path:like:{state_uid}',
            'fields': 'id,name,level,path',
            'paging': 'false'}).json()['organisationUnits']
        by_uid = {o['id']: o for o in units}

        state_name = by_uid.get(state_uid, {}).get('name', '')
        state_code = state_options.get(state_name)
        if state_code is None:
            logger.error(
                f"State org unit '{state_name}' ({state_uid}) has no matching "
                f"option in the State option set; cannot record residence.")
            sys.exit(2)

        facilities = 0
        for unit in units:
            if unit['level'] != FACILITY_LEVEL:
                continue

            # /root/state/lga/ward/facility - the LGA is the third element.
            parts = unit['path'].strip('/').split('/')
            if len(parts) < 3:
                continue
            lga_name = by_uid.get(parts[2], {}).get('name', '')
            lga_code = lga_options.get(lga_name)
            if lga_code is None:
                unmatched_lgas.add(lga_name)
                continue

            index[unit['id']] = {
                'name': unit['name'],
                'state_code': state_code,
                'state_name': state_label,
                'lga_code': lga_code,
                'lga_name': lga_name,
                # The attribute values this facility implies, by semantic
                # name, so build_attributes needs to know nothing about which
                # scheme produced them.
                'geo': {'state': state_code, 'lga': lga_code},
            }
            facilities += 1

        logger.info(f"{state_label}: {facilities} facilities with a usable LGA code")

    if unmatched_lgas:
        logger.warning(
            f"{len(unmatched_lgas)} LGA org unit name(s) have no option in the "
            f"LGA option set; their facilities are skipped: "
            f"{sorted(unmatched_lgas)[:5]}")

    if not index:
        logger.error("No pilot facility could be matched to a State and LGA code.")
        sys.exit(2)

    return index


def egypt_geography_index(api):
    """
    Give every facility a district, and with it a governorate.

    Egypt records the catchment as two option-set codes rather than reading it
    off the hierarchy, and each district code embeds its governorate - EG1309
    sits under EG13 - so the pair is consistent by construction. The district
    is fixed per facility so patients seen at the same place share a catchment
    instead of scattering across the country.
    """
    districts = fetch_option_codes(api, INSTANCE.district_option_set,
                                   'Egypt districts', required=False)
    governorates = set(fetch_option_codes(api, INSTANCE.governorate_option_set,
                                          'Egypt governorates', required=False))
    usable = [c for c in districts
              if len(c) >= 4 and 'EG' + c[2:4] in governorates]
    if not usable:
        logger.error("No district code maps onto a governorate; cannot assign "
                     "geography.")
        sys.exit(2)
    logger.info(f"{len(usable)} usable district code(s)")

    facilities = facilities_without_geography(api)
    for uid, info in facilities.items():
        digest = hashlib.sha256(uid.encode('utf-8')).hexdigest()
        district = usable[int(digest, 16) % len(usable)]
        info['geo'] = {'district': district, 'governorate': 'EG' + district[2:4]}
        info['governorate_digits'] = district[2:4]
    return facilities


def facilities_without_geography(api):
    """
    Every facility the programme is assigned to, with no geography attached.

    Used by instances that do not record the patient's state or district on
    the enrolment, where a facility needs no catchment codes to be usable.
    """
    assigned = api.get(f'programs/{TARGET_PROGRAM}', params={
        'fields': 'organisationUnits[id,name,level]'}).json().get(
            'organisationUnits') or []

    at_level = [o for o in assigned if o.get('level') == FACILITY_LEVEL]
    if not at_level:
        levels = sorted({o.get('level') for o in assigned})
        logger.warning(f"The programme is assigned at level(s) {levels}, not "
                       f"{FACILITY_LEVEL}; using every assigned org unit")
        at_level = assigned

    logger.info(f"{len(at_level)} facility/ies available on this instance")
    return {o['id']: {'name': o['name'], 'state_code': None, 'state_name': '',
                      'lga_code': None, 'lga_name': '', 'geo': {}}
            for o in at_level}


def resolve_target_org_units(api, args, geography):
    """
    The facilities to post to: pilot-state facilities the programme is actually
    assigned to.

    An enrolment at an org unit the programme is not assigned to is rejected,
    so the programme's own assignment is the authority. Nigeria assigns the
    programme very widely, which leaves thousands of candidates in the two
    pilot states - far more than a demo cohort should be smeared across - so
    --facilities-per-state picks a stable sample.
    """
    assigned = {o['id'] for o in api.get(
        f'programs/{TARGET_PROGRAM}',
        params={'fields': 'organisationUnits[id]'}).json().get(
            'organisationUnits', [])}

    if not assigned:
        logger.warning(
            f"Programme {TARGET_PROGRAM} is assigned to no org units; "
            f"enrolments will be rejected until it is assigned.")
        candidates = dict(geography)
    else:
        candidates = {uid: info for uid, info in geography.items() if uid in assigned}

    if args.facility:
        chosen = {uid: candidates[uid] for uid in args.facility if uid in candidates}
        missing = sorted(set(args.facility) - set(chosen))
        if missing:
            logger.error(
                f"--facility given org unit(s) that are not pilot facilities "
                f"the programme is assigned to: {missing}")
            sys.exit(2)
        logger.info(f"Posting to {len(chosen)} facility/ies named on the command line")
        return chosen

    if not candidates:
        logger.error(
            "No facility in Ogun or Kano has the programme assigned. Assign it "
            "first, or pass --facility with a specific org unit.")
        sys.exit(2)

    per_state = args.facilities_per_state
    if per_state <= 0:
        logger.info(f"Posting to all {len(candidates)} pilot facilities")
        return candidates

    if not PILOT_STATES:
        # No pilot split on this instance, so the whole facility list is one
        # group and --facilities-per-state is simply how many to use.
        pool = sorted(candidates)
        picker = random.Random('facilities:all')
        take = picker.sample(pool, min(per_state, len(pool)))
        logger.info(f"{len(take)} of {len(pool)} facilities selected")
        return {uid: candidates[uid] for uid in take}

    chosen = {}
    for state_label in sorted(PILOT_STATES.values()):
        in_state = sorted(uid for uid, info in candidates.items()
                          if info['state_name'] == state_label)
        if not in_state:
            logger.warning(f"{state_label}: no assigned facility to post to")
            continue
        # Sampled from a fixed seed rather than the run's, so the same
        # facilities are used whatever --seed is, and a top-up run lands in the
        # same places as the cohort it is topping up.
        picker = random.Random(f'nigeria-facilities:{state_label}')
        take = picker.sample(in_state, min(per_state, len(in_state)))
        for uid in take:
            chosen[uid] = candidates[uid]
        logger.info(f"{state_label}: {len(take)} of {len(in_state)} facilities selected")

    return chosen


# ---------------------------------------------------------------------------
# Patient profile
# ---------------------------------------------------------------------------

def band_value(name, key):
    """
    Pick a value inside the mix band `<name>_min`..`<name>_max`, stable for a
    given key.

    Keying on the calendar month makes every patient seen in that month share
    the same target, so a monthly trend line moves from month to month instead
    of sitting flat, while staying inside the band.
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
    The month's target shares, applied to HYPERTENSIVE patients only.

    The dashboard indicators count hypertensive patients under care, so a
    patient who never had hypertension is in neither the numerator nor the
    denominator and the bands must not be rescaled across the whole cohort.
    """
    controlled = min(max(band_value('controlled', month_key), 0.0), 1.0)
    uncontrolled = min(max(band_value('uncontrolled', month_key), 0.0), 1.0)

    if p('hypertensive') <= 0:
        return 1.0, 0.0, 0.0

    if controlled + uncontrolled > 1.0:
        uncontrolled = 1.0 - controlled

    return controlled, 1.0 - controlled - uncontrolled, uncontrolled


# How strongly treatment shows in the reading. Without this the drawn control
# state is independent of the step the patient is on, which has two bad
# consequences: titrating never helps, so patients keep reading raised and keep
# being escalated until the whole cohort sits at the top of the ladder; and the
# share of raised readings that get acted on is capped, because a patient at
# the last step has nowhere to go and every raised reading there counts against
# the titration rate with no possible remedy.
#
# With it, a patient climbs until their treatment works and then stays there,
# which is both what a protocol is for and what makes the step distribution
# look like a real programme's.
RESPONSE_STRENGTH = 0.40


def bp_state_for_visit(profile, month_key, step=None):
    """
    The control state of one visit.

    Drawn from the month's targets, then pulled towards or away from control
    depending on whether this patient's current step is enough to control
    them. The two adjustments are symmetric, so the cohort still lands inside
    the requested band while individual patients respond to treatment.
    """
    if not profile['hypertensive']:
        return BP_CONTROLLED

    controlled, above, uncontrolled = monthly_bp_shares(month_key)
    state = random.choices(
        (BP_CONTROLLED, BP_ABOVE_TARGET, BP_UNCONTROLLED),
        weights=(controlled, above, uncontrolled),
        k=1,
    )[0]

    responds_at = profile.get('control_at_step')
    on_enough = (step is not None and responds_at is not None
                 and step >= responds_at)

    if on_enough:
        # A frankly uncontrolled reading is less readily explained away by
        # treatment working than one merely above target, and pulling both
        # across at the same rate emptied the uncontrolled band.
        strength = (RESPONSE_STRENGTH if state == BP_ABOVE_TARGET
                    else RESPONSE_STRENGTH * 0.45)
        if state != BP_CONTROLLED and random.random() < strength:
            return BP_CONTROLLED
    elif state == BP_CONTROLLED and random.random() < RESPONSE_STRENGTH:
        # Not yet on enough treatment to be at target. Split between the two
        # raised bands in the month's own proportion: sending every one of
        # them to "above target" drained the uncontrolled share below its band.
        raised = above + uncontrolled
        if raised > 0 and random.random() < uncontrolled / raised:
            return BP_UNCONTROLLED
        return BP_ABOVE_TARGET

    return state


def draw_conditions():
    """
    Draw (hypertensive, diabetic) so both prevalences match the mix exactly
    while as few patients as possible end up with neither condition.
    """
    p_htn, p_dm = p('hypertensive'), p('diabetic')

    both = max(p_htn + p_dm - 1.0, 0.0)
    bucket = random.choices(
        ('both', 'htn', 'dm', 'neither'),
        weights=(both, p_htn - both, p_dm - both,
                 max(1.0 - p_htn - p_dm, 0.0)),
        k=1,
    )[0]

    return bucket in ('both', 'htn'), bucket in ('both', 'dm')


def draw_status_without_call():
    """
    A status change recorded at the facility rather than by the call centre.

    Most non-ACTIVE statuses come out of the overdue list, where the removal
    reason explains them. This is the smaller path: a relative tells the clinic
    the patient has died, or the register is tidied up. It exists so that a
    cohort with few overdue patients still carries some of each status, and it
    is drawn per patient rather than sampled from the import, so the status a
    patient ends up with is the one their own record argued for.

    Whichever path sets it, the value is applied after import: program rule
    GYmydeVoxDI assigns ACTIVE whenever an enrollment is created, and anything
    else in the creating payload is rejected with E1309.
    """
    roll = random.random()
    died = p('status_died_no_call')
    transferred = died + p('status_transferred_no_call')
    inactive = transferred + p('status_inactive_no_call')

    if roll < died:
        return 'DIED'
    if roll < transferred:
        return 'TRANSFER'
    if roll < inactive:
        return 'INACTIVE'
    return 'ACTIVE'


def draw_appointment_overdue():
    """
    Whether this patient's booked appointment should already have passed.

    The mix key is the TOTAL share showing OVERDUE_PENDING_CALL, not an extra
    slice on top: patients with no visit in three months and those lost to
    follow-up are past due by definition, so this only tops the remainder up.
    """
    target = p('appointment_overdue')
    already = band_value('overdue', 'cohort') + p('lost_to_followup')

    if target <= already:
        return False
    return random.random() < (target - already) / max(1.0 - already, 1e-9)


def entry_step(systolic, diastolic):
    """
    Where the protocol starts a newly diagnosed patient, from their first
    reading.

    Nigeria starts higher for a higher reading - >= 180/110 at step 3 plus an
    emergency referral, >= 160/100 at step 2. A protocol that names no such
    thresholds, like Egypt's, starts everyone at step 1 and works up.
    """
    if STEP_3_ENTRY and (systolic >= STEP_3_ENTRY[0]
                         or diastolic >= STEP_3_ENTRY[1]):
        return 3
    if STEP_2_ENTRY and (systolic >= STEP_2_ENTRY[0]
                         or diastolic >= STEP_2_ENTRY[1]):
        return 2
    return 1


def molecule_key(code):
    """
    The drug identity of an option code, ignoring dose and frequency, so two
    strengths of the same molecule are never selected together.

        METFORMIN_1000MG_XR_OD     -> METFORMIN
        GLIBENCLAMIDE_2POINT5MG_OD -> GLIBENCLAMIDE
    """
    kept = []
    for token in str(code).split('_'):
        if any(ch.isdigit() for ch in token):
            break
        kept.append(token)
    return '_'.join(kept) if kept else str(code).split('_')[0]


# Amlodipine monotherapy is only defensible in the very elderly, where an ARB
# is being avoided deliberately. Kept here because the payload validator reads
# it to check the cohort it is given.
AMLODIPINE_MONOTHERAPY_MIN_AGE = 70


def pick_distinct_antidiabetics(count):
    """Oral antidiabetics that do not repeat a molecule."""
    shuffled = random.sample(MULTI_ANTIDIABETIC_CODES, len(MULTI_ANTIDIABETIC_CODES))
    chosen, seen = [], set()
    for code in shuffled:
        molecule = code.split('_')[0]
        if molecule in seen:
            continue
        chosen.append(code)
        seen.add(molecule)
        if len(chosen) == count:
            break
    return chosen


def build_profile(sex, dob_iso, facility_info):
    """
    Decide everything about this patient up front, so the enrolment attributes
    and every visit tell one consistent story.
    """
    age = age_from_dob(dob_iso)
    hypertensive, diabetic = draw_conditions()
    height_cm = sample_height(sex)

    profile = {
        'age': age,
        'sex': sex,
        'dob': dob_iso,
        'height_cm': height_cm,
        'bmi_target': sample_bmi_target(),
        'state_code': facility_info['state_code'],
        'state_name': facility_info['state_name'],
        'lga_code': facility_info['lga_code'],
        'lga_name': facility_info['lga_name'],
        'hypertensive': hypertensive,
        'diabetic': diabetic,
        'took_bp_meds': hypertensive and random.random() < p('took_meds_before'),
        'took_dm_meds': diabetic and random.random() < p('took_meds_before'),
        'smoker': random.random() < p('smoker_male' if sex == 'MALE' else 'smoker_female'),
        'history_mi': random.random() < p('history_mi'),
        'history_stroke': random.random() < p('history_stroke'),
        'history_ckd': random.random() < p('history_ckd'),
        'mistitrated': random.random() < p('mistitrated'),
        'insured': random.random() < p('insured'),
        'sms_consent': random.random() < p('sms_consent'),
        'has_phone': random.random() < p('has_phone'),
        'has_supporter': random.random() < p('has_supporter'),
        'id_number_known': random.random() < p('id_number_known'),
        'controlled': False,
        'uncontrolled': False,
        'dm_controlled': diabetic and random.random() < p('dm_controlled'),
        'overdue': random.random() < band_value('overdue', 'cohort'),
        'lost_to_followup': random.random() < p('lost_to_followup'),
        # Provisional: a removal from the overdue list overrides it in
        # work_the_overdue_list, because a reason on the record beats a draw.
        'ncd_status': draw_status_without_call(),
        # How this patient's prescribing departs from the protocol, if at all.
        'titration_behaviour': random.choices(
            list(TITRATION_BEHAVIOURS),
            weights=list(TITRATION_BEHAVIOURS.values()), k=1)[0],
        'non_protocol_regimen': random.choice(NON_PROTOCOL_REGIMENS),
        'other_htn_med': (random.choice(OTHER_HTN_MEDS)
                          if random.random() < p('other_htn_med') else None),
        # Filled in by build_events, one category per visit.
        'transitions': [],
        # Counters for the titration rate: how often the protocol asked for a
        # step up, and how often it got one.
        'titration_due': 0,
        'titrated': 0,
        # The step at which this patient's blood pressure comes to target.
        # None means it never does - resistant hypertension, which is why some
        # patients climb the whole ladder and still read high.
        'control_at_step': random.choices([1, 2, 3, 4, None],
                                          weights=[40, 30, 15, 8, 7], k=1)[0],
        'removal_reason': None,
        'overdue_calls': 0,
        'chase_calls': 0,
        'appointment_overdue': draw_appointment_overdue(),
        'late_always': random.random() < p('late_all_visits'),
        'late_at_2': random.random() < p('late_at_visit_2'),
        'late_at_3': random.random() < p('late_at_visit_3'),
    }

    # A history of heart attack, stroke or kidney disease is not plausible in a
    # young adult in this population.
    if age < 35:
        profile['history_mi'] = False
        profile['history_stroke'] = False
        profile['history_ckd'] = False

    # The QR code is settled here rather than in build_attributes because it is
    # how apply_patient_outcomes finds this patient again after the import: the
    # server mints the tracked entity UID, so the QR is the only handle we have
    # on a specific generated patient.
    profile['geo'] = dict(facility_info.get('geo') or {})
    profile['qr_code'] = test_qr_code()
    given, family = draw_identity_name(sex, facility_info.get('state_name', ''))
    profile['given_name'] = given
    profile['family_name'] = family
    profile['name'] = f'{given} {family}'
    profile['phone'] = draw_phone() if profile['has_phone'] else None
    profile['address'] = street_address(facility_info.get('lga_name', ''),
                                        facility_info.get('state_name', ''))
    if 'national_id' in TEA:
        profile['national_id'] = egyptian_national_id(
            dob_iso, sex, facility_info.get('governorate_digits', '01'))
    if 'phc_id' in TEA:
        profile['phc_id'] = phc_id()
    if 'age_header' in TEA:
        # Where the risk rules read the age in their conditions, a rule's own
        # assignment is not visible to them during an import, so the value has
        # to be in the payload for the risk score to populate at all.
        profile['age_header'] = str(age)

    if profile['has_supporter']:
        profile['supporter_name'] = patient_name(
            'FEMALE' if random.random() < 0.5 else 'MALE',
            facility_info['state_name'])
        profile['supporter_phone'] = nigerian_phone()
        profile['supporter_relation'] = random.choice(RELATIONSHIP_CODES)

    # The protocol forbids losartan for women of childbearing age who are NOT on
    # effective contraception, and telmisartan is an ARB too. Those patients are
    # held on amlodipine monotherapy, which is the one step the protocol leaves
    # open to them without a specialist.
    #
    # The contraception share matters: treating every woman under 50 as
    # contraindicated put a quarter of the whole cohort on amlodipine alone,
    # and since that is a non-protocol regimen above step 1 it swamped the
    # non-protocol categories with patients who are not titration errors at all.
    profile['arb_contraindicated'] = (
        sex == 'FEMALE' and age <= CHILDBEARING_AGE_MAX
        and random.random() < p('arb_contraindicated_if_woman'))

    # Telmisartan where it is stocked, losartan otherwise. Fixed per patient:
    # a facility does not alternate between the two month to month.
    profile['arb'] = ('telmisartan'
                      if random.random() < TELMISARTAN_SHARE else 'losartan')
    profile['days_of_meds'] = random.choice(DAYS_OF_MEDICATION_CODES)

    # A patient already on treatment arrives further up the ladder.
    profile['start_step'] = 2 if profile['took_bp_meds'] else None

    # Only drawn where there is somewhere to put them. The guard comes first
    # on purpose: an unconditional draw would consume from the seeded stream
    # on instances that record no lipids, shifting every later value for them.
    records_lipids = 'ldl' in DE or 'statin_drug' in DE
    profile['high_cholesterol'] = (records_lipids
                                   and random.random() < p('high_cholesterol'))
    statin_indicated = (profile['high_cholesterol'] or diabetic
                        or profile['history_mi'] or profile['history_stroke'])
    profile['statin'] = (random.choice(STATIN_CODES)
                         if 'statin_drug' in DE and statin_indicated
                         and random.random() > p('statin_missed') else None)
    profile['antidiabetics'] = (
        pick_distinct_antidiabetics(random.randint(1, 3))
        if 'antidiabetics' in DE and diabetic else [])
    profile['on_insulin'] = ('insulin_name' in DE and diabetic
                             and random.random() < p('insulin_if_diabetic'))
    if profile['on_insulin']:
        regimen_name = random.choices(['basal', 'premixed', 'rapid'],
                                      weights=[6, 3, 1], k=1)[0]
        chosen = INSULIN_REGIMENS[regimen_name]
        profile['insulin'] = {'name': random.choice(chosen['names']),
                              'frequency': chosen['frequency'],
                              'dose': random.randint(*chosen['dose'])}

    profile['on_metformin'] = diabetic
    profile['on_glibenclamide'] = diabetic and random.random() < 0.45
    profile['metformin'] = random.choice(METFORMIN_CODES) if diabetic else None
    profile['glibenclamide'] = (random.choice(GLIBENCLAMIDE_CODES)
                                if profile['on_glibenclamide'] else None)
    profile['atorvastatin'] = (random.choice(ATORVASTATIN_CODES)
                               if diabetic and random.random() < p('statin_if_diabetic')
                               else None)

    # The blood sugar unit a facility reports in, fixed per patient so a
    # patient's readings stay comparable across visits.
    profile['sugar_unit'] = allowed_sugar_unit()
    profile['dm_improves'] = profile['dm_controlled']

    return profile


# ---------------------------------------------------------------------------
# Enrolment attributes
# ---------------------------------------------------------------------------

def yes_no(flag):
    return 'YES' if flag else 'NO'


def name_attributes(profile):
    """
    The patient's name, split the way this instance splits it.

    Nigeria merges everything into one "Patient name"; the demo instance keeps
    a given and a family name; Egypt adds a middle name. The generator always
    draws a given and a family name and the instance decides how to record it.
    """
    style = INSTANCE.name_style if INSTANCE else 'merged'
    given, family = profile['given_name'], profile['family_name']

    if style == 'merged':
        return {'patient_name': f'{given} {family}'}
    if style == 'given_middle_family':
        return {'given_name': given,
                'middle_name': profile.get('middle_name') or given,
                'family_name': family}
    return {'given_name': given, 'family_name': family}


def build_attributes(profile):
    """
    Every tracked entity attribute this instance's enrolment expects.

    Driven by what the instance actually carries rather than by a fixed list:
    a field the instance has no attribute for is simply not produced, which is
    what lets the same enrolment serve three different field sets.
    """
    wanted = {
        'dob': profile['dob'],
        'sex': profile['sex'],
        'address': profile['address'],
        'consent': 'true',
        # Mandatory where present, and owned by the AUTOFILL rule on creation:
        # real outcomes land in apply_patient_state.
        'ncd_status': NCD_STATUS_ON_ENROLMENT,
        'has_hypertension': yes_no(profile['hypertensive']),
        'has_diabetes': yes_no(profile['diabetic']),
        # Derived from the booked appointment rather than drawn at random: the
        # overdue indicators read this attribute, so it has to agree with the
        # patient's own scheduled visit (set at the end of build_events).
        'follow_up_status': profile.get('follow_up_status', FOLLOW_UP_UPCOMING),
        # A genuine BOOLEAN, so a negative answer is recorded too: "does not
        # smoke" and "not asked" are different facts.
        'smoking': 'true' if profile['smoker'] else 'false',
        'qr_code': profile['qr_code'],
        'height': str(profile['height_cm']),
    }
    wanted.update(profile.get('geo') or {})
    wanted.update(name_attributes(profile))

    for extra in ('national_id', 'phc_id', 'age_header'):
        if profile.get(extra):
            wanted[extra] = profile[extra]

    if profile['phone']:
        wanted['phone'] = profile['phone']
    if profile['id_number_known']:
        wanted['id_number'] = test_id_number()

    # The treatment-history questions only appear once the condition is Yes.
    # Nigeria asks them as a first-time/treated-before option set; demo and
    # Egypt ask a plain yes/no, so the value follows the field.
    if profile['hypertensive']:
        wanted['htn_treated_before'] = (TREATED_IN_PAST if profile['took_bp_meds']
                                        else TREATED_FIRST_TIME)
        wanted['took_bp_meds'] = yes_no(profile['took_bp_meds'])
    if profile['diabetic']:
        wanted['dm_treated_before'] = (TREATED_IN_PAST if profile['took_dm_meds']
                                       else TREATED_FIRST_TIME)
        wanted['took_dm_meds'] = yes_no(profile['took_dm_meds'])

    codes = (INSTANCE.insurance_codes if INSTANCE else None) or INSURANCE_CODES
    if profile['insured'] and codes:
        wanted['insurance'] = random.choice(codes)

    if profile['has_supporter']:
        wanted['supporter_name'] = profile['supporter_name']
        wanted['supporter_phone'] = profile['supporter_phone']
        wanted['supporter_relation'] = profile['supporter_relation']

    # TRUE_ONLY attributes accept 'true' and nothing else, so they are sent
    # only when the answer is positive.
    for flag in ('history_mi', 'history_stroke', 'history_ckd'):
        if profile[flag]:
            wanted[flag] = 'true'
    if profile['sms_consent']:
        wanted['sms_consent'] = 'true'

    return [{'attribute': TEA[name], 'value': value}
            for name, value in wanted.items()
            if name in TEA and value is not None]

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


def empty_regimen():
    return {key: None for key in MED_KEYS}


def step_regimen(profile, step):
    """
    The regimen one step prescribes, as a {med key: option code} mapping.

    A ladder that names both an ARB and its alternative is describing one
    prescription with two spellings, so only the patient's own ARB is taken.
    A patient for whom an ARB is contraindicated drops to the protocol's
    monotherapy where it defines one, and otherwise keeps the ladder's
    non-ARB components - which above step 1 is a non-protocol regimen by
    definition, and a genuine clinically driven source of one.
    """
    plan = PROTOCOL_STEPS[step]
    regimen = empty_regimen()

    arbs = [key for key in ('losartan', 'telmisartan') if key in plan]

    if profile.get('arb_contraindicated') and MONOTHERAPY:
        regimen.update({k: v for k, v in MONOTHERAPY.items() if k in regimen})
        return regimen

    for key, code in plan.items():
        if key in ('losartan', 'telmisartan'):
            continue
        regimen[key] = code

    if arbs and not profile.get('arb_contraindicated'):
        chosen = profile.get('arb') if profile.get('arb') in plan else arbs[0]
        regimen[chosen] = plan[chosen]

    return regimen


def regimen_values(regimen):
    """Data values for the four structured antihypertensives that are present."""
    return {DE[key]: code for key, code in regimen.items() if code}


def erratic_regimen(previous):
    """
    A change that raises one drug and lowers another at the same time, which
    is what "Non-protocol titration and de-escalation (both occured)"
    describes.

    Every code comes from the ladder in force rather than being written in:
    hardcoding Nigeria's hydrochlorothiazide here put a code in Egypt's
    payloads that its option set does not contain, and those patients were
    rejected outright.
    """
    strongest = {}
    for plan in PROTOCOL_STEPS.values():
        for key, code in plan.items():
            if key not in strongest or dose_of(code) > dose_of(strongest[key]):
                strongest[key] = code

    changed = dict(previous)

    # Raise the first drug that has somewhere to go - a higher dose on the
    # ladder, or a drug the patient is not yet on.
    for key, code in sorted(strongest.items()):
        if dose_of(previous.get(key)) < dose_of(code):
            changed[key] = code
            raised = key
            break
    else:
        return changed

    # And lower or stop a different one they are currently taking.
    for key in sorted(previous):
        if key == raised or not previous.get(key):
            continue
        lower = [c for plan in PROTOCOL_STEPS.values()
                 for k, c in plan.items()
                 if k == key and dose_of(c) < dose_of(previous[key])]
        changed[key] = max(lower, key=dose_of) if lower else None
        break

    return changed


def wrong_start_step(correct_step):
    """
    A starting step the first reading did not call for.

    The referral step is not a place to start anyone - Nigeria's step 5 means
    "send them to a specialist", not a prescription - so it is excluded. On a
    ladder with no referral step the top step is a legitimate wrong start,
    which is what lets "Initiated on step 4" appear at all on Egypt.
    """
    top = (REFERRAL_STEP - 1) if REFERRAL_STEP else MAX_PROTOCOL_STEP
    options = [s for s in range(1, top + 1) if s != correct_step]
    return random.choice(options) if options else correct_step


def apply_titration_deviation(profile, index, step, regimen, previous):
    """
    Bend one patient's prescribing away from the protocol.

    Returns (regimen, step). A deviation that moves the patient up or down the
    ladder also moves `step`, so the visits after it carry on from where the
    patient actually is rather than snapping back; one that only affects a
    single visit - an interruption, a switch onto a non-protocol combination -
    leaves the underlying step alone so treatment resumes afterwards.
    """
    behaviour = profile['titration_behaviour']

    if behaviour == 'wrong_start':
        # `step` arriving here is the one the first reading called for, so the
        # wrong start is chosen against it rather than at random.
        step = wrong_start_step(step)
        return step_regimen(profile, step), step

    if behaviour == 'non_protocol_start':
        return dict(profile['non_protocol_regimen']), step

    if behaviour == 'interrupted':
        return empty_regimen(), step

    if behaviour == 'de_escalator':
        step = max(step - 1, 1)
        return step_regimen(profile, step), step

    if behaviour == 'double_jump':
        step = min(step + 2, MAX_PROTOCOL_STEP)
        return step_regimen(profile, step), step

    if behaviour == 'non_protocol_switch':
        # The same combination for the whole span, so a two-visit switch reads
        # as "Non-protocol with no change" on its second visit.
        return dict(profile['non_protocol_regimen']), step

    if behaviour == 'erratic':
        return erratic_regimen(previous), step

    return regimen, step


def deviation_due(behaviour, index, deviation_at, deviation_span, step, already):
    """
    Whether this visit is the one the patient's deviation lands on.

    Most deviations fire on a fixed visit. Moving up or down the ladder needs
    room to move, though: stepping down from step 1 is not a de-escalation and
    jumping two from the top is not a jump. Those two wait instead, firing at
    the first visit from `deviation_at` onwards where the move is possible -
    otherwise a patient who was still on step 1 at the chosen visit silently
    contributed nothing, and the category came out far thinner than the
    behaviour weights implied.
    """
    if deviation_at is None or index < deviation_at:
        return False

    if behaviour == 'de_escalator':
        return not already and step is not None and step > 1
    if behaviour == 'double_jump':
        return not already and step is not None and step <= MAX_PROTOCOL_STEP - 2

    return index < deviation_at + deviation_span


def choose_deviation(profile, total_visits):
    """
    Which visit this patient's deviation lands on, and how long it lasts.

    Returns (index, span), or (None, 0) for a patient who follows the protocol
    or has too short a history for the deviation to be visible as a transition.
    """
    behaviour = profile['titration_behaviour']

    if behaviour == 'protocol' or total_visits == 0:
        return None, 0

    if behaviour in ('wrong_start', 'non_protocol_start'):
        return 0, 1

    if total_visits < 2:
        # Everything else is a change BETWEEN two visits, so it needs two.
        return None, 0

    if behaviour in ('de_escalator', 'double_jump'):
        # Both need room on the ladder: stepping down from step 1 is not a
        # de-escalation, and jumping two from step 4 only reaches the top.
        # Landing these in the second half of the history gives the patient
        # time to have climbed somewhere worth moving from.
        index = random.randint(max(total_visits // 2, 1), total_visits - 1)
    else:
        index = random.randint(1, total_visits - 1)

    span = 1
    if (behaviour == 'non_protocol_switch' and index + 1 < total_visits
            and random.random() < 0.45):
        span = 2

    return index, span


def diabetes_values(profile, at_target):
    """
    One glycaemic measurement, in the unit this patient's facility reports in.

    The Nigerian form carries both mg/dL and mmol/L data elements for FBS, RBS
    and PPBS, so the unit decides which one is filled. HbA1c is a percentage
    and carries no unit.
    """
    values = {}
    measure = random.choices(['FBS', 'RBS', 'PPBS', 'HBA1C'],
                             weights=[5, 4, 1, 2], k=1)[0]
    values[DE['dm_measure_type']] = measure

    if measure == 'HBA1C':
        # Diagnostic threshold 6.5%.
        hba1c = round(random.uniform(5.6, 6.4) if at_target
                      else random.uniform(6.5, 11.5), 1)
        values[DE['hba1c']] = str(hba1c)
        return values

    unit = profile['sugar_unit']
    values[DE['blood_sugar_unit']] = unit
    mgdl = unit == 'MG_OR_DL'

    if measure == 'FBS':
        # Diagnostic threshold 126 mg/dL = 7.0 mmol/L.
        reading = random.randint(80, 125) if at_target else random.randint(126, 260)
    elif measure == 'RBS':
        # Diagnostic threshold 200 mg/dL = 11.1 mmol/L.
        reading = random.randint(110, 199) if at_target else random.randint(200, 320)
    else:
        # PPBS threshold 140 mg/dL = 7.8 mmol/L.
        reading = random.randint(110, 139) if at_target else random.randint(140, 280)

    key = measure.lower() + ('_mgdl' if mgdl else '_mmol')
    values[DE[key]] = str(reading) if mgdl else str(round(reading / 18.0, 1))

    return values


def lipid_values(profile):
    """
    A lipid panel in mg/dL, for the instances that record one.

    Total cholesterol in mmol/L is left out deliberately: where it exists it is
    assigned by a rule from the mg/dL value, and supplying our own conversion
    is rejected when it rounds differently.
    """
    if profile.get('high_cholesterol'):
        return {'total_chol_mgdl': str(random.randint(220, 330)),
                'ldl': str(random.randint(140, 230)),
                'hdl': str(random.randint(20, 42)),
                'triglycerides': str(random.randint(180, 480))}
    return {'total_chol_mgdl': str(random.randint(130, 199)),
            'ldl': str(random.randint(60, 129)),
            'hdl': str(random.randint(45, 85)),
            'triglycerides': str(random.randint(60, 160))}


def build_visit(profile, regimen, reading, recommended, dm_at_target, with_labs):
    """
    The data values for one completed visit.

    Keyed by semantic field name and filtered at the end against what this
    instance actually carries, so a visit on an instance with no height,
    weight or BMI data element simply does not mention them.
    """
    wanted = {}

    systolic, diastolic = reading
    wanted['systolic'] = str(systolic)
    wanted['diastolic'] = str(diastolic)

    # Height is sent on every visit as well as on the enrolment. The rule that
    # copies it from the attribute is not visible to another rule's condition
    # during an import, so the value has to be in the payload for anything
    # downstream to see it. It is the same number in both places, so the rule
    # and the payload agree.
    wanted['height_cm'] = str(profile['height_cm'])

    bmi = max(profile['bmi_target'] + random.uniform(-0.8, 0.8), 16.0)
    weight = weight_for(profile['height_cm'], bmi)
    wanted['weight_kg'] = str(weight)
    # The same arithmetic the BMI rule uses, so the two cannot disagree
    # whether or not that rule is active.
    wanted['bmi'] = str(round_half_up(
        weight / ((profile['height_cm'] ** 2) / 10000), 1))

    if profile['hypertensive']:
        for key, code in regimen.items():
            if code:
                wanted[key] = code

        if not regimen_is_empty(regimen):
            # A facility dispenses the same interval to the same patient most
            # of the time; drawing it fresh every visit made the quantity flip
            # between 30, 60 and 90 days month after month, which no clinic
            # does. A visit that records no medication dispenses none either.
            wanted['days_of_meds'] = (profile['days_of_meds']
                                      if random.random() < 0.85
                                      else random.choice(DAYS_OF_MEDICATION_CODES))

        # The recommendation describes what the protocol asks for, so it is
        # sent whatever was actually prescribed - that gap is the point.
        wanted['recommended_step'] = recommended

        # Free text, and deliberately outside the structured fields: the
        # transition classification ignores this one entirely.
        if profile['other_htn_med']:
            wanted['other_htn_med'] = profile['other_htn_med']

    if profile['diabetic']:
        if with_labs:
            for uid, value in diabetes_values(profile, dm_at_target).items():
                wanted[uid] = value
        for drug in ('metformin', 'glibenclamide', 'atorvastatin'):
            if profile.get(drug):
                wanted[drug] = profile[drug]

        # Instances that record the whole regimen in one multi-select field
        # rather than one field per drug.
        if profile.get('antidiabetics'):
            wanted['antidiabetics'] = MULTI_VALUE_SEPARATOR.join(
                profile['antidiabetics'])
        if profile.get('on_insulin'):
            insulin = profile['insulin']
            wanted['insulin_name'] = insulin['name']
            wanted['insulin_dose'] = str(insulin['dose'])
            wanted['insulin_frequency'] = insulin['frequency']

    if with_labs and 'ldl' in DE:
        wanted.update(lipid_values(profile))

    if profile.get('statin'):
        wanted['statin_drug'] = profile['statin']
        wanted['statin_prescribed'] = 'true'

    # Every visit ends with the next one booked, where the form records it.
    if 'scheduling_done' in DE:
        wanted['scheduling_done'] = 'true'

    # Semantic names resolve through the field map; anything already a UID -
    # diabetes_values works in UIDs - passes through untouched.
    values = {}
    for name, value in wanted.items():
        uid = DE.get(name, name if name not in DE and len(name) == 11 else None)
        if uid:
            values[uid] = value

    return translate_values(
        [{'dataElement': de, 'value': value} for de, value in values.items()])


def recommended_step(profile, step, reading):
    """
    What the protocol says should happen next, given this visit's reading and
    the step the patient is currently on. Returns a Protocol steps option code.

    This is the nudge the Capture app shows the clinician, and it is sent
    rather than left to the rules. The target's own recommendation rules test
    `#{prevStepComputed}`, which is itself assigned by another rule; on import
    a rule's assignment is not visible to another rule's condition, so those
    rules never fire and the field comes back empty on every historical visit.
    Verified on the server: the ones that do fire never disagree with this.
    """
    if not profile['hypertensive']:
        return 'NONE'

    systolic, diastolic = reading

    if step is None:
        return f'STEP_{entry_step(systolic, diastolic)}'

    # Below 140/90 is at target, and the protocol's instruction is to continue
    # the current treatment rather than change it.
    if systolic < HTN_THRESHOLD[0] and diastolic < HTN_THRESHOLD[1]:
        return f'STEP_{step}'

    # A reading this high is an emergency at any visit, not a one-step move -
    # on the ladders that say so. A protocol without entry thresholds steps up
    # one at a time whatever the reading.
    if STEP_3_ENTRY and (systolic >= STEP_3_ENTRY[0]
                         or diastolic >= STEP_3_ENTRY[1]):
        return f'STEP_{min(max(step + 1, 3), MAX_PROTOCOL_STEP)}'

    return f'STEP_{min(step + 1, MAX_PROTOCOL_STEP)}'


def first_visit_recommendation(reading, step):
    """
    The recommendation at the visit that starts treatment, which is the
    starting step rather than a move up from it.

    A first reading below 140/90 is the one case where a rule beats us to it.
    Rule wqxZDArVExh 'HTN First Visit - Start Step 0 (BP controlled)' is the
    only first-visit rule whose condition has an `|| !d2:hasValue(prevStep)`
    escape, so it is the only one that fires during an import, and it assigns
    NONE. Sending anything else is rejected with E1307; NONE is also the right
    answer, because a reading under the threshold indicates no treatment
    change at the visit that found the patient.
    """
    systolic, diastolic = reading
    if systolic < HTN_THRESHOLD[0] and diastolic < HTN_THRESHOLD[1]:
        return 'NONE'
    return f'STEP_{step}'


def step_number(code):
    """The step number inside a Protocol steps code, or None for NONE."""
    return int(code.split('_')[1]) if code.startswith('STEP_') else None


def next_step(profile, step, recommended):
    """
    Where the patient actually ends up next month.

    The protocol says to escalate a month after a reading still at or above
    140/90. Most clinicians follow that, some do not: a `mistitrated` patient
    is left where they are, and a share of the rest are held back by ordinary
    clinical inertia. Escalating on every single raised reading would march the
    whole cohort to the top of the ladder.
    """
    if not profile['hypertensive'] or step is None:
        return step

    target = step_number(recommended)
    if target is None or target <= step:
        # The recommendation is to hold. A wrongly titrated patient may be
        # pushed up anyway.
        if profile['mistitrated'] and random.random() < 0.3:
            return min(step + 1, MAX_PROTOCOL_STEP)
        return step

    if profile['mistitrated']:
        return step

    chance = (REFERRAL_PROBABILITY if target >= MAX_PROTOCOL_STEP
              else ESCALATION_PROBABILITY)
    return target if random.random() < chance else step


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


def plain_defaulting_reason(profile):
    """Why the patient says they missed the appointment."""
    reasons = [r for r in PLAIN_DEFAULTING_REASONS
               if r not in SEX_SPECIFIC_DEFAULTING
               or (profile['sex'] == 'FEMALE'
                   and profile['age'] <= CHILDBEARING_AGE_MAX)]
    return random.choice(reasons)


def build_call_report(profile, org_unit, called_on, result, remove_reason=None):
    """
    One Calling report event.

    The removal reason is only ever sent with REMOVE_FROM_OVERDUE: rule
    K7MIf6LoLPj hides that field for the other two outcomes, so a value there
    would be describing a field the form never showed.
    """
    values = {DE['call_result']: result}

    if remove_reason:
        values[DE['remove_reason']] = remove_reason

    # The reason for defaulting is marked "DELETE - ..." on the instances that
    # still carry it and is gone from the demo altogether, so it is only
    # populated where it exists - and then only to feed the rules that derive
    # a removal from it, which is the one thing it is still wired to.
    if 'defaulting_reason' in DE:
        if remove_reason:
            values[DE['defaulting_reason']] = (
                REMOVAL_DEFAULTING_REASON.get(remove_reason)
                or plain_defaulting_reason(profile))
        else:
            values[DE['defaulting_reason']] = plain_defaulting_reason(profile)

    if 'home_visit' in DE and random.random() < HOME_VISIT_SHARE:
        values[DE['home_visit']] = 'true'

    return {
        'program': TARGET_PROGRAM,
        'programStage': CALL_STAGE,
        'orgUnit': org_unit,
        'occurredAt': called_on.isoformat(),
        'status': 'COMPLETED',
        'dataValues': [{'dataElement': de, 'value': v} for de, v in values.items()],
    }


def chase_calls(profile, org_unit, first_day, last_day, final_result,
                remove_reason=None, max_calls=MAX_CHASE_CALLS):
    """
    A run of calls inside [first_day, last_day], ending in `final_result`.

    Everything before the last call is REMIND_TO_CALL_LATER, which is what a
    chase looks like: the centre rings, gets nowhere, rings again, and the last
    call is the one that settles it - the patient agrees to come, or comes off
    the list.
    """
    if last_day < first_day:
        return []

    span = (last_day - first_day).days
    # One call per day at most, so a narrow window means fewer calls rather
    # than two calls stacked on the same date.
    count = min(random.randint(1, max_calls), span + 1)
    offsets = sorted(random.sample(range(span + 1), count))

    calls = []
    for position, offset in enumerate(offsets):
        last = position == count - 1
        calls.append(build_call_report(
            profile, org_unit, first_day + timedelta(days=offset),
            final_result if last else CALL_REMIND,
            remove_reason=remove_reason if last else None))

    return calls


def draw_removal_reason():
    """Why the call centre took this patient off the overdue list."""
    reasons = list(REMOVAL_REASON_WEIGHTS)
    return random.choices(reasons,
                          weights=[REMOVAL_REASON_WEIGHTS[r] for r in reasons],
                          k=1)[0]


def build_events(profile, dated, org_unit, patient_uid):
    """
    Build the visit history from a list of (visit date, source event) pairs:
    one completed event per visit in chronological order, one scheduled visit
    at the end, and a Calling report for an overdue patient the centre rang.
    """
    events = []
    step = profile['start_step']
    total_visits = len(dated)

    deviation_at, deviation_span = choose_deviation(profile, total_visits)
    deviation_applied = False
    # Whether the previous visit read at or above the treatment threshold, and
    # the step the patient was on then: together they say whether the protocol
    # asked for a change and whether one was made.
    previous_raised = False
    previous_step = None
    previous_regimen = empty_regimen()
    had_earlier_meds = False

    for index, (occurred_date, source_event) in enumerate(dated):
        is_last = index == total_visits - 1

        bp_state = bp_state_for_visit(profile, month_of(occurred_date), step)

        # A patient newly found to be hypertensive reads high at the visit that
        # found them - that reading is why they were enrolled. Someone already
        # on BP medicine before enrolling may well arrive at target.
        if (profile['hypertensive'] and bp_state == BP_CONTROLLED
                and index == 0 and not profile['took_bp_meds']):
            bp_state = BP_ABOVE_TARGET

        # The reading is drawn once and then used both to settle the protocol
        # entry point and to fill the form, so the step a newly diagnosed
        # patient starts on is the one their own recorded BP calls for.
        #
        # A patient already on treatment arrives at step 2, but the protocol's
        # emergency rule does not care about history: a first reading at or
        # above 180/110 gets step 3 drugs and a referral to the nearest general
        # hospital within the hour, so the entry step is a floor, not an
        # alternative.
        reading = bp_reading(profile, bp_state)
        if profile['hypertensive'] and index == 0:
            step = max(step or 0, entry_step(*reading))
            recommended = first_visit_recommendation(reading, step)
        else:
            recommended = recommended_step(profile, step, reading)

        # What was actually prescribed. The protocol regimen for the step the
        # patient is on, unless this is the visit their deviation lands on.
        if profile['hypertensive'] and step is not None:
            regimen = step_regimen(profile, step)
            if deviation_due(profile['titration_behaviour'], index, deviation_at,
                             deviation_span, step, deviation_applied):
                regimen, step = apply_titration_deviation(
                    profile, index, step, regimen, previous_regimen)
                deviation_applied = True
        else:
            regimen = empty_regimen()

        # The protocol's own test: a reading at or above the threshold a month
        # after the last one calls for the next step up.
        #
        # The enrolment visit is not one of those occasions. A patient found
        # to be hypertensive is STARTED on the step their reading calls for -
        # that is the protocol's instruction - and reviewed a month later.
        # Counting it as a missed titration put one unanswerable obligation on
        # every hypertensive patient and more than halved the measured rate.
        # Measured from the regimen actually recorded, not from the step this
        # generator is tracking internally. On a ladder whose top step is a
        # referral - Nigeria's step 5 prescribes the same drugs as step 4 -
        # the two disagree: moving 4 to 5 changes no medication, so the
        # reporting side cannot see it and neither should this. Counting it
        # reported 63% where the dashboard would show 52%.
        recorded_step = regimen_step(regimen)
        if index > 1 and previous_raised and previous_step is not None                 and recorded_step is not None:
            profile['titration_due'] += 1
            if recorded_step > previous_step:
                profile['titrated'] += 1
        previous_raised = (reading[0] >= HTN_THRESHOLD[0]
                           or reading[1] >= HTN_THRESHOLD[1])
        previous_step = recorded_step

        category = classify_transition(previous_regimen, regimen, had_earlier_meds)
        profile['transitions'].append(category)
        previous_regimen = regimen
        had_earlier_meds = had_earlier_meds or not regimen_is_empty(regimen)

        # Glycaemic control runs on its own track: never at the first visit,
        # then improving over follow-up for the patients who respond.
        if index == 0 or not profile['dm_improves']:
            dm_at_target = False
        else:
            progress = index / max(total_visits - 1, 1)
            dm_at_target = random.random() < 0.25 + 0.55 * progress
        if is_last:
            dm_at_target = profile['dm_controlled']

        visit = {
            'event': (source_event.get('event')
                      or (deterministic_uid(patient_uid, 'visit', str(index))
                          if DETERMINISTIC_UIDS else None)),
            'program': TARGET_PROGRAM,
            'programStage': VISIT_STAGE,
            'orgUnit': org_unit,
            'occurredAt': occurred_date,
            'status': 'COMPLETED',
            'dataValues': build_visit(
                profile, regimen, reading, recommended, dm_at_target,
                with_labs=(index == 0 or random.random() < p('labs_at_visit')),
            ),
        }

        # Every visit after the first needs a due date, otherwise there is
        # nothing to measure lateness against and a patient who came back after
        # 250 days does not register as having been overdue. The first visit has
        # no due date by design: nothing scheduled it.
        if index > 0:
            previous_visit = datetime.strptime(dated[index - 1][0], "%Y-%m-%d").date()
            came = datetime.strptime(occurred_date, "%Y-%m-%d").date()
            # Booked at the previous visit, but never later than the day the
            # patient actually turned up: a visit cannot still be due after it
            # has happened. Taking previous + 28 unconditionally gave the
            # visits that came sooner than that a due date in the future, and
            # a finished visit carrying a future due date cannot be told apart
            # from a pending appointment - the patient appeared to have two.
            visit['scheduledAt'] = min(
                previous_visit + timedelta(days=SCHEDULED_VISIT_INTERVAL_DAYS), came
            ).isoformat()

        events.append(visit)

        # A visit that happened well after it was due is one the patient had
        # to be chased for. The chase sits between the due date and the day
        # they actually turned up, and ends with the call where they agreed to
        # come - which is what the next event is.
        if visit.get('scheduledAt'):
            due = datetime.strptime(visit['scheduledAt'], "%Y-%m-%d").date()
            came = datetime.strptime(occurred_date, "%Y-%m-%d").date()
            if ((came - due).days > CALL_CHASE_MIN_DAYS
                    and random.random() < p('chased_if_late')):
                chase = chase_calls(profile, org_unit, due + timedelta(days=1),
                                    came - timedelta(days=1), CALL_AGREE)
                events.extend(chase)
                profile['chase_calls'] += len(chase)

        if is_last:
            profile['controlled'] = (profile['hypertensive']
                                     and bp_state == BP_CONTROLLED)
            profile['uncontrolled'] = (profile['hypertensive']
                                       and bp_state == BP_UNCONTROLLED)
            profile['final_step'] = step

        step = next_step(profile, step, recommended)

    # Any event still without a UID lets DHIS2 assign one.
    for event in events:
        if not event.get('event'):
            event.pop('event', None)

    last_visit = max(datetime.strptime(day, "%Y-%m-%d").date()
                     for day, _source in dated)

    # A patient who is keeping up has their next appointment ahead of them; one
    # who is behind has a due date that has already passed. Deriving this purely
    # as "last visit + 28 days" would make most of the cohort look overdue,
    # because a patient last seen 60 days ago is mechanically 32 days late.
    today = date.today()
    wants_past_due = (profile['overdue'] or profile['lost_to_followup']
                      or profile['appointment_overdue'])

    if wants_past_due:
        # The due date must still fall after the visit that preceded it: an
        # appointment booked before the last visit is nonsense, and the
        # validator rightly rejects it.
        earliest = last_visit + timedelta(days=1)
        latest = today - timedelta(days=1)
        if earliest > latest:
            due = today + timedelta(days=random.randint(1, SCHEDULED_VISIT_INTERVAL_DAYS))
        else:
            due = earliest + timedelta(days=random.randint(0, (latest - earliest).days))
    else:
        due = today + timedelta(days=random.randint(1, SCHEDULED_VISIT_INTERVAL_DAYS))

    scheduled = {
        'program': TARGET_PROGRAM,
        'programStage': VISIT_STAGE,
        'orgUnit': org_unit,
        'scheduledAt': due.isoformat(),
        'status': 'SCHEDULE',
    }
    if DETERMINISTIC_UIDS:
        scheduled['event'] = deterministic_uid(patient_uid, 'scheduled-visit')
    events.append(scheduled)

    # That scheduled visit is the patient's current state: an event with a due
    # date, no occurredAt and no data. If its due date has passed the patient
    # is on the overdue list right now, and the follow-up status attribute has
    # to say so - the overdue indicators read that attribute, so leaving an
    # overdue patient on UPCOMING_VISIT would contradict their own record.
    if due >= today:
        profile['follow_up_status'] = FOLLOW_UP_UPCOMING
        return events

    events.extend(work_the_overdue_list(profile, org_unit, last_visit, due, today))
    return events


def work_the_overdue_list(profile, org_unit, last_visit, due, today):
    """
    What the call centre has done about an appointment that is already past
    due, and what that leaves on the patient's record.

    Four states, drawn from the mix:
      * not yet called      no Calling report; status stays OVERDUE_PENDING_CALL
      * removed             the last call takes them off the list, and its
                            reason decides their NCD Patient Status
      * agreed to visit     the last call got a commitment; they have not come
                            back yet, which is why the visit is still open
      * still being chased  reminders only, no conclusion yet

    Everything except the first leaves the patient on CALLED, which is the
    terminal value this option set offers - there is no "removed" follow-up
    status, and rule gBYFbwsM7y4 treats PENDING and CALLED alike as "on the
    list".
    """
    # The chase can only start after the appointment was missed, and cannot
    # run past today.
    first_day = max(due + timedelta(days=1), last_visit + timedelta(days=1))
    last_day = today

    roll = random.random()
    not_called = p('overdue_not_yet_called')
    removed = not_called + p('overdue_removed')
    agreed = removed + p('overdue_agreed')

    if roll < not_called or last_day < first_day:
        # Nobody has rung yet - either by the draw, or because the appointment
        # was missed so recently that there has been no time to.
        profile['follow_up_status'] = FOLLOW_UP_PENDING
        return []

    if roll < removed:
        reason = draw_removal_reason()
        calls = chase_calls(profile, org_unit, first_day, last_day, CALL_REMOVE,
                            remove_reason=reason)
        profile['removal_reason'] = reason
        # ACTIVE for HAS_ALREADY_VISITED and OTHER: off the list, still in care.
        profile['ncd_status'] = REMOVAL_OUTCOME[reason]
    elif roll < agreed:
        calls = chase_calls(profile, org_unit, first_day, last_day, CALL_AGREE)
    else:
        calls = chase_calls(profile, org_unit, first_day, last_day, CALL_REMIND)

    profile['follow_up_status'] = FOLLOW_UP_CALLED
    profile['overdue_calls'] = len(calls)
    return calls


# ---------------------------------------------------------------------------
# Visit timeline
# ---------------------------------------------------------------------------

def shift_visits_for_recency(dated, overdue):
    """
    Slide the whole visit timeline so the latest visit falls inside or outside
    the three-month window the dashboard uses, leaving the gaps between visits
    exactly as recorded.
    """
    if not dated:
        return dated

    today = date.today()
    last_visit = datetime.strptime(dated[-1][0], "%Y-%m-%d").date()
    window_opens = today - timedelta(days=RECENT_VISIT_WINDOW_DAYS - 1)

    if overdue:
        target = window_opens - timedelta(days=random.randint(1, 150))
    else:
        target = window_opens + timedelta(
            days=random.randint(0, RECENT_VISIT_WINDOW_DAYS - 1))

    delta = target - last_visit
    if delta.days == 0:
        return dated

    return [( (datetime.strptime(d, "%Y-%m-%d").date() + delta).isoformat(), ev)
            for d, ev in dated]


def dated_from_source_events(source_events, enrollment_date, overdue):
    """Visit dates taken from a source patient, slid for recency."""
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


def enrolment_window(enrol_window_days, enrol_from, enrol_to, visits_min=2):
    """
    The span enrolments are spread over: a year back to today by default.

    --enrol-from / --enrol-to override either end, which is how a monthly run
    adds that month's arrivals: point both at the current month. A patient
    enrolled today is a legitimate new arrival - they have their enrolment
    visit and a booked follow-up - so the window runs right up to today rather
    than stopping short to leave room for visits.
    """
    today = date.today()

    first = enrol_from or (today - timedelta(days=enrol_window_days))
    last = enrol_to or today

    latest_possible = today - timedelta(
        days=(max(visits_min, 1) - 1) * SCHEDULED_VISIT_INTERVAL_DAYS)
    if last > latest_possible:
        logger.info(
            f"Latest enrolment pulled back from {last.isoformat()} to "
            f"{latest_possible.isoformat()} to leave room for "
            f"{max(visits_min, 1)} visit(s).")
        last = latest_possible
    if first > last:
        logger.warning(
            f"Enrolment span collapsed to a single day ({last.isoformat()}): "
            f"every patient in this batch will enrol on the same day.")
        first = last

    return first, last


def spread_enrolment_date(position, batch_size, enrol_first, enrol_last):
    """
    Place one patient's enrolment inside the enrolment span by its position in
    the batch, so N patients cover the span evenly however large N is.
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
    Visit dates for a generated patient, running forward from an enrolment date
    the caller has already placed inside the window.

    The most recent visit is pinned inside or outside the three-month window
    according to the mix. Visits in between run at 25-28 day intervals - the
    protocol reviews monthly - stretching out when the patient comes back late.
    The visit COUNT therefore falls out of how long the patient has been
    enrolled rather than being drawn independently.
    """
    today = date.today()

    # The indicators ask "was there a visit in the past three months" at the END
    # of the reporting period, not today. Anchoring on today makes a patient
    # last seen 85 days ago look current now but overdue the moment the month
    # closes.
    period_end = date(today.year, today.month,
                      calendar.monthrange(today.year, today.month)[1])
    window_opens = period_end - timedelta(days=RECENT_VISIT_WINDOW_DAYS - 1)

    if profile['lost_to_followup']:
        # Attended once or twice after enrolling and then stopped. Expressed
        # as a short follow-up from the enrolment date rather than a visit
        # before the reporting window, because enrolments are now confined to
        # the enrolment window and a patient cannot have stopped attending
        # before they registered.
        last_visit = min(enrolled + timedelta(days=random.randint(0, 84)), today)
    elif profile['overdue']:
        last_visit = window_opens - timedelta(days=random.randint(1, 150))
    else:
        earliest = min(window_opens, today)
        last_visit = earliest + timedelta(
            days=random.randint(0, max((today - earliest).days, 0)))

    # A patient who enrolled only recently cannot also have been last seen long
    # ago: give them the minimum follow-up instead of a nonsense history.
    minimum_span = timedelta(
        days=(max(visits_min, 1) - 1) * SCHEDULED_VISIT_INTERVAL_DAYS)
    if last_visit - enrolled < minimum_span:
        last_visit = min(enrolled + minimum_span, today)
    if last_visit < enrolled:
        last_visit = enrolled

    dates = [enrolled]

    if last_visit > enrolled:
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


# ---------------------------------------------------------------------------
# Tracked entity assembly
# ---------------------------------------------------------------------------

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


def patient_seed(key, salt=0):
    """A stable 64-bit seed for one patient."""
    digest = hashlib.sha256(f"{key}|{salt}".encode('utf-8')).digest()
    return int.from_bytes(digest[:8], 'big')


def synthetic_dob():
    """
    A date of birth for a generated patient. Nigeria measures the blood
    pressure of every adult from 18, so the cohort is weighted towards the ages
    where hypertension is found rather than pinned to a risk-chart window.
    """
    age = int(min(max(random.gauss(52, 14), MIN_PATIENT_AGE), MAX_PATIENT_AGE))
    return dob_from_age(age)


def assemble_patient(profile, org_unit, patient_uid, dated, index_key):
    """Wrap one profile and its visit timeline into a tracker payload."""
    events = build_events(profile, dated, org_unit, patient_uid)
    enrolled_at = dated[0][0]

    record = {
        'trackedEntityType': TARGET_TRACKED_ENTITY_TYPE,
        'orgUnit': org_unit,
        # build_attributes is called after build_events because the follow-up
        # status attribute has to agree with the appointment build_events booked.
        'attributes': build_attributes(profile),
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
            'nigeria-enrollment', str(RUN_SALT), index_key)

    return record


def make_synthetic_patient(index, org_unit, facility_info, stats, enrolled,
                           visits_min, visits_max, window_start=None):
    """Generate one patient outright, with no source record involved."""
    patient_uid = deterministic_uid('nigeria-synthetic', str(RUN_SALT), str(index))

    sex = 'FEMALE' if random.random() < p('female') else 'MALE'
    dob = synthetic_dob()
    profile = build_profile(sex, dob, facility_info)

    dated = synthetic_visit_dates(profile, enrolled, visits_min, visits_max,
                                  window_start)
    record = assemble_patient(profile, org_unit, patient_uid, dated, str(index))

    record_profile_stats(profile, stats, len(dated))
    return record, pending_state(profile)


def _root_org_units(api):
    """The instance's roots, for instances with no pilot states to scope to."""
    roots = api.get('organisationUnits', params={
        'filter': 'level:eq:1', 'fields': 'id', 'paging': 'false'}
    ).json()['organisationUnits']
    return ','.join(o['id'] for o in roots)


def pending_state(profile):
    """
    The attributes this patient needs written after the import, keyed by their
    QR code, or None when creation already leaves them correct.

    Two attributes are not settled by the creating payload: NCD Patient Status,
    which program rule GYmydeVoxDI forces to ACTIVE, and the follow-up status,
    which creation forces to UPCOMING_VISIT and a nightly cron maintains
    thereafter. Both stick on a later update.
    """
    attributes = {}

    if profile['ncd_status'] != NCD_STATUS_ON_ENROLMENT:
        attributes[TEA['ncd_status']] = profile['ncd_status']

    follow_up = profile.get('follow_up_status', FOLLOW_UP_UPCOMING)
    if follow_up != FOLLOW_UP_ON_ENROLMENT:
        attributes[TEA['follow_up_status']] = follow_up

    key = profile.get(INSTANCE.lookup_attribute) if INSTANCE else profile.get('qr_code')
    return (key, attributes) if attributes and key else None


def record_profile_stats(profile, stats, n_visits):
    """Accumulate the run summary counters shared by both generation paths."""
    stats['controlled'] += profile['controlled']
    stats['uncontrolled'] += profile.get('uncontrolled', False)
    stats['dm_controlled'] += profile['dm_controlled']
    stats['overdue'] += profile['overdue']
    stats['hypertensive'] += profile['hypertensive']
    stats['diabetic'] += profile['diabetic']
    stats['female'] += profile['sex'] == 'FEMALE'
    stats['smoker'] += profile['smoker']
    stats['insured'] += profile['insured']
    stats['with_supporter'] += profile['has_supporter']
    stats['cvd_history'] += (profile['history_mi'] or profile['history_stroke']
                             or profile['history_ckd'])
    stats['mistitrated'] += profile['mistitrated']
    stats['lost_to_followup'] += profile['lost_to_followup']
    stats['overdue_call'] += profile.get('follow_up_status') in (
        FOLLOW_UP_PENDING, FOLLOW_UP_CALLED)
    stats['called'] += profile.get('follow_up_status') == FOLLOW_UP_CALLED
    stats['pending_call'] += profile.get('follow_up_status') == FOLLOW_UP_PENDING
    stats['calls'] += profile['overdue_calls'] + profile['chase_calls']
    stats['chase_calls'] += profile['chase_calls']
    if profile.get('removal_reason'):
        stats['removed_from_overdue'] += 1
    if profile['ncd_status'] != 'ACTIVE':
        stats['outcome_drawn'] += 1
    stats['arb_contraindicated'] += profile['arb_contraindicated']
    stats['visits'] += n_visits
    stats['late_visits'] += sum(
        1 for i in range(2, n_visits + 1) if visit_is_late(profile, i))

    stats['titration_due'] += profile.get('titration_due', 0)
    stats['titrated'] += profile.get('titrated', 0)
    stats[f"behaviour::{profile['titration_behaviour']}"] += 1
    stats['other_htn_med'] += bool(profile['other_htn_med'])
    for category in profile['transitions']:
        stats[f'cat::{category}'] += 1

    final_step = profile.get('final_step')
    if final_step:
        stats[f'step_{final_step}'] += 1


# ---------------------------------------------------------------------------
# Source transformation (--from-nigeria)
# ---------------------------------------------------------------------------

def source_attribute(tei, uid):
    for attr in tei.get('attributes', []):
        if attr.get('attribute') == uid and attr.get('value') not in (None, ''):
            return str(attr['value']).strip()
    return None


def source_sex(tei):
    value = (source_attribute(tei, TEA['sex']) or '').upper()
    return value if value in ('MALE', 'FEMALE') else None


def source_ncd_status(tei):
    value = (source_attribute(tei, 'fI1P3Mg1zOZ') or '').upper()
    value = NCD_STATUS_MAP.get(value, value)
    return value if value in VALID_NCD_STATUS else 'ACTIVE'


def source_dob(tei):
    for attr in tei.get('attributes', []):
        if attr.get('attribute') == TEA['dob']:
            return parse_dob(attr.get('value'))
    return None


def transform_tei(tei, org_unit, facility_info, stats):
    """
    Re-express one real source patient against the pilot configuration.

    What survives the source record: sex, date of birth, the enrolment date,
    and the number and spacing of visits. Everything else is generated, because
    identity must not cross into a sandbox and because the clinical values have
    to follow the Nigerian protocol ladder, which the free-text source data
    does not reliably do.
    """
    enrollments = tei.get('enrollments') or []
    if not enrollments:
        stats['no_enrollment'] += 1
        return None

    enrollment = enrollments[0]

    sex = source_sex(tei)
    if sex is None:
        stats['no_sex'] += 1
        sex = 'FEMALE' if random.random() < p('female') else 'MALE'

    dob = source_dob(tei)
    if not dob:
        dob = synthetic_dob()
        stats['generated_dob'] += 1
    elif not (MIN_PATIENT_AGE <= age_from_dob(dob) <= MAX_PATIENT_AGE):
        dob = synthetic_dob()
        stats['age_rebased'] += 1

    profile = build_profile(sex, dob, facility_info)
    profile['ncd_status'] = source_ncd_status(tei)

    enrolled_at = (enrollment.get('enrolledAt')
                   or enrollment.get('occurredAt')
                   or date.today().isoformat())
    dated = dated_from_source_events(enrollment.get('events') or [],
                                     enrolled_at, profile['overdue'])

    source_uid = tei.get('trackedEntity') or tei.get('trackedEntityInstance') or ''
    patient_uid = deterministic_uid('nigeria-from-source', str(RUN_SALT), source_uid)

    # Source event UIDs are not reused: they belong to the production instance,
    # and a sandbox record that shares a UID with a real one invites a later
    # sync to overwrite the wrong record.
    dated = [(day, {}) for day, _event in dated]

    record = assemble_patient(profile, org_unit, patient_uid, dated, source_uid)
    record_profile_stats(profile, stats, len(dated))
    return record, pending_state(profile)


# ---------------------------------------------------------------------------
# Target metadata checks
# ---------------------------------------------------------------------------

def fetch_option_codes(api, option_set_uid, label, required=True):
    """
    Read the codes of a target option set, so the script emits real codes.

    `required=False` turns a missing set into a warning and an empty list,
    which is what the dialect resolution wants: an instance that simply does
    not carry one of these fields is a difference to work around, not a reason
    to refuse to run.
    """
    try:
        response = api.get(f'optionSets/{option_set_uid}',
                           params={'fields': 'options[code]'}).json()
    except (DHIS2RequestException, RequestsRequestException) as e:
        message = f"Could not read the {label} option set ({option_set_uid}): {e}"
        if required:
            logger.error(message)
            sys.exit(2)
        logger.warning(message)
        return []

    codes = [o['code'] for o in response.get('options', []) if o.get('code')]
    if not codes and required:
        logger.error(f"The {label} option set ({option_set_uid}) has no options.")
        sys.exit(2)

    return codes


def verify_codes(used, available, label):
    """Warn when the script would send a code the target does not define."""
    unknown = sorted(set(used) - set(available))
    if unknown:
        logger.warning(
            f"{label}: {len(unknown)} code(s) not present in the target option "
            f"set and likely to be rejected: {unknown}")
    return not unknown


# ---------------------------------------------------------------------------
# Speaking the target's dialect
#
# The three instances this script can write to share the programme, the stages
# and the data element UIDs, but not their option sets. Nigeria's losartan is
# LOSARTAN_50MG; the HTN/DM demo calls the same thing LOSARTAN_50MG_OD and
# offers doses Nigeria does not. The demo has no "Days of Medication" or
# "Recommended Step" data element at all.
#
# Rather than carry a hardcoded table per instance, the codes this script
# speaks are matched onto the target's own by molecule and dose, and any data
# element the target does not have is dropped. The protocol ladder stays
# written in one dialect; only what goes over the wire is translated.
# ---------------------------------------------------------------------------

CODE_PARTS = re.compile(
    r'^([A-Z]+)_([0-9]+(?:(?:POINT|\.)[0-9]+)?)MG(?:_(OD|BD|TDS))?$')

# canonical code -> the target's code, or None when it has no equivalent.
MED_CODE_MAP = {}
# Data elements this script sends that the target actually has.
TARGET_DE_PRESENT = set()


def code_parts(code):
    """(molecule, dose, frequency) for a medication option code."""
    found = CODE_PARTS.match(str(code or '').strip().upper())
    if not found:
        return None
    molecule, dose, frequency = found.groups()
    return molecule, dose.replace('POINT', '.'), frequency


def _best_match(canonical, available):
    """
    The target's code for the same molecule at the same dose.

    Frequency is the only thing allowed to differ, and then only by falling
    back to once daily and finally to whatever exists - a dose is clinical, a
    dosing frequency the target happens to spell differently is not.
    """
    if canonical in available:
        return canonical

    parts = code_parts(canonical)
    if not parts:
        return None
    molecule, dose, frequency = parts

    same_dose = [c for c in available
                 if (code_parts(c) or (None, None, None))[:2] == (molecule, dose)]
    if not same_dose:
        return None

    for want in (frequency, 'OD', None):
        for code in sorted(same_dose):
            if want is None or (code_parts(code) or (None, None, None))[2] == want:
                return code
    return None


def resolve_target_dialect(api):
    """
    Work out which data elements the target has and what it calls each drug.

    Called once at startup; everything the script emits goes through
    translate_values afterwards.
    """
    MED_CODE_MAP.clear()
    TARGET_DE_PRESENT.clear()

    for uid in set(DE.values()):
        try:
            api.get(f'dataElements/{uid}', params={'fields': 'id'})
            TARGET_DE_PRESENT.add(uid)
        except (DHIS2RequestException, RequestsRequestException):
            pass

    missing_de = sorted(set(DE) - {k for k, v in DE.items() if v in TARGET_DE_PRESENT})
    if missing_de:
        logger.warning(f"The target has no data element for {missing_de}; "
                       f"those values will not be sent")

    # Every code this instance's ladder, its non-protocol regimens and its
    # oral drug lists can emit, grouped by the field it goes in. Derived from
    # the protocol rather than listed, so a different ladder needs no edit here.
    canonical = {}
    for plan in list(PROTOCOL_STEPS.values()) + list(NON_PROTOCOL_REGIMENS) +             ([MONOTHERAPY] if MONOTHERAPY else []):
        for key, code in plan.items():
            canonical.setdefault(key, set()).add(code)

    for key, codes in (('metformin', METFORMIN_CODES),
                       ('glibenclamide', GLIBENCLAMIDE_CODES),
                       ('atorvastatin', ATORVASTATIN_CODES),
                       ('statin_drug', STATIN_CODES),
                       ('antidiabetics', MULTI_ANTIDIABETIC_CODES)):
        if key in DE:
            canonical.setdefault(key, set()).update(codes)

    translated = unmatched = 0
    for key, codes in canonical.items():
        uid = DE.get(key)
        if uid not in TARGET_DE_PRESENT:
            for code in codes:
                MED_CODE_MAP[code] = None
            continue
        try:
            available = fetch_option_codes(api, _option_set_for(api, uid), key,
                                           required=False)
        except SystemExit:
            available = []
        for code in codes:
            match = _best_match(code, available)
            MED_CODE_MAP[code] = match
            if match is None:
                unmatched += 1
            elif match != code:
                translated += 1

    if translated or unmatched:
        logger.warning(f"Target dialect: {translated} medication code(s) "
                       f"translated, {unmatched} with no equivalent (those "
                       f"values are dropped)")
    else:
        logger.info("Target dialect: every medication code matches exactly")


def _option_set_for(api, data_element_uid):
    option_set = api.get(f'dataElements/{data_element_uid}', params={
        'fields': 'optionSet[id]'}).json().get('optionSet') or {}
    return option_set.get('id') or ''


def translate_values(values):
    """
    Put one visit's data values into the target's dialect.

    Drops anything the target has no data element for, and swaps each
    medication code for the target's own. A drug with no equivalent is dropped
    rather than sent and rejected.
    """
    if not TARGET_DE_PRESENT:
        return values            # dialect never resolved: send as written

    out = []
    for value in values:
        if value['dataElement'] not in TARGET_DE_PRESENT:
            continue
        code = value.get('value')
        if code in MED_CODE_MAP:
            replacement = MED_CODE_MAP[code]
            if replacement is None:
                continue
            value = {**value, 'value': replacement}
        out.append(value)
    return out


def check_non_protocol_regimens():
    """
    Every combination in NON_PROTOCOL_REGIMENS must really match no step.

    A later edit to PROTOCOL_DOSE_STEPS or to the list itself could quietly
    turn one of them into a protocol regimen, and the seeded "errors" would
    then be classified as correct treatment with nothing to show for it.
    """
    mistaken = [r for r in NON_PROTOCOL_REGIMENS if regimen_step(r) is not None]
    if mistaken:
        logger.error(f"{len(mistaken)} entry/ies in NON_PROTOCOL_REGIMENS match a "
                     f"protocol step and would not be classified as non-protocol: "
                     f"{mistaken}")
        sys.exit(2)


def check_hydrochlorothiazide(api):
    """
    Hydrochlorothiazide 12.5mg is no longer available in Nigeria.

    Two things to be sure of: that this script cannot emit a 12.5mg value, and
    that the target's option set no longer offers one. sync_patients.py carries
    a workaround that moves a 12.5mg value off the field when copying real
    production records, which is how the code is still around to worry about.
    """
    emitted = {code for regimen in NON_PROTOCOL_REGIMENS
               for key, code in regimen.items() if key == 'hctz' and code}
    emitted |= {plan['hctz'] for plan in PROTOCOL_STEPS.values() if 'hctz' in plan}
    # Only Nigeria withdrew the 12.5mg strength; elsewhere the dose set is
    # whatever that programme uses, so there is nothing to police.
    if INSTANCE and INSTANCE.key != 'nigeria':
        return

    withdrawn = sorted(c for c in emitted
                       if dose_of(c) != 25 or 'POINT' in str(c).upper())
    if withdrawn:
        logger.error(f"This script would send a withdrawn hydrochlorothiazide "
                     f"strength: {withdrawn}. Only the 25mg code is available.")
        sys.exit(2)

    available = fetch_option_codes(api, OPTION_SET['hctz'], 'Hydrochlorothiazide')
    stale = sorted(c for c in available if dose_of(c) != 25 or 'POINT' in c.upper())
    if stale:
        logger.warning(
            f"The target's Hydrochlorothiazide option set still offers "
            f"{stale}, which is no longer available in Nigeria. This script "
            f"never sends it, but the Capture form will still show it.")
    else:
        logger.info("Hydrochlorothiazide: 25mg only, as expected")


def _option_set_for_field(api, kind, field_name):
    """The option set this instance hangs off one field, or None."""
    uid = (DE if kind == 'de' else TEA).get(field_name)
    if not uid:
        return None
    endpoint = 'dataElements' if kind == 'de' else 'trackedEntityAttributes'
    try:
        found = api.get(f'{endpoint}/{uid}', params={'fields': 'optionSet[id]'}).json()
    except (DHIS2RequestException, RequestsRequestException):
        return None
    return (found.get('optionSet') or {}).get('id')


def check_target_metadata(api):
    """Validate every option code this script can emit against the target."""
    check_non_protocol_regimens()
    check_hydrochlorothiazide(api)
    protocol_codes = {}
    for plan in list(PROTOCOL_STEPS.values()) + list(NON_PROTOCOL_REGIMENS):
        for key, code in plan.items():
            protocol_codes.setdefault(key, set()).add(code)

    checks = [
        ('de', 'amlodipine', OPTION_SET['amlodipine'],
         sorted(protocol_codes.get('amlodipine', ())), 'Amlodipine'),
        ('de', 'amlo_valsartan', '', 
         sorted(protocol_codes.get('amlo_valsartan', ())), 'Amlodipine/Valsartan'),
        ('de', 'losartan', OPTION_SET['losartan'],
         sorted(protocol_codes.get('losartan', ())), 'Losartan'),
        ('de', 'telmisartan', OPTION_SET['telmisartan'],
         sorted(protocol_codes.get('telmisartan', ())), 'Telmisartan'),
        ('de', 'hctz', OPTION_SET['hctz'],
         sorted(protocol_codes.get('hctz', ())), 'Hydrochlorothiazide'),
        ('de', 'metformin', OPTION_SET['metformin'], METFORMIN_CODES, 'Metformin'),
        ('de', 'glibenclamide', OPTION_SET['glibenclamide'],
         GLIBENCLAMIDE_CODES, 'Glibenclamide'),
        ('de', 'atorvastatin', OPTION_SET['atorvastatin'],
         ATORVASTATIN_CODES, 'Atorvastatin'),
        ('de', 'days_of_meds', OPTION_SET['days_of_meds'],
         DAYS_OF_MEDICATION_CODES, 'Days of Medication'),
        ('tea', 'insurance', OPTION_SET['insurance'],
         (INSTANCE.insurance_codes if INSTANCE else INSURANCE_CODES),
         'Health Insurance'),
        ('tea', 'supporter_relation', OPTION_SET['relationship'],
         RELATIONSHIP_CODES, 'Relationship'),
        ('de', 'defaulting_reason', OPTION_SET['defaulting'],
         DEFAULTING_REASONS, 'Reason for Defaulting'),
        ('de', 'call_result', OPTION_SET['call_result'], CALL_RESULTS,
         'Result of call'),
    ]

    ok = True
    checked = 0
    for kind, field_name, option_set, codes, label in checks:
        present = (field_name in DE) if kind == 'de' else (field_name in TEA)
        if not present or not codes:
            continue
        # The option set this instance actually attaches to the field, rather
        # than the one Nigeria happens to use for it.
        option_set = _option_set_for_field(api, kind, field_name) or option_set
        # After translation: a code with no equivalent on this target has
        # already been dropped, so warning about it twice helps nobody.
        sending = sorted({MED_CODE_MAP.get(c, c) for c in codes} - {None})
        available = fetch_option_codes(api, option_set, label, required=False)
        if not available:
            continue
        checked += 1
        ok &= verify_codes(sending, available, label)

    if ok:
        logger.info(f"All {checked} option sets match the codes this script sends")


def warn_rule_assigned_fields(api):
    """
    Warn when the target has a rule assigning a field this script sends and
    that we have not already accounted for.

    A rule added to the programme after this script was written would otherwise
    show up only as an opaque E1307/E1309 rejection at import time.
    """
    try:
        rules = api.get('programRules', params={
            'filter': f'program.id:eq:{TARGET_PROGRAM}',
            'paging': 'false',
            'fields': 'id,name,condition,programRuleActions[programRuleActionType,'
                      'dataElement[id,name],trackedEntityAttribute[id,name]]',
        }).json()['programRules']
    except (DHIS2RequestException, RequestsRequestException) as e:
        logger.warning(f"Could not read program rules to check assigned fields: {e}")
        return

    assigned = {}
    parked = []
    for rule in rules:
        if rule.get('condition', '').strip().startswith('false &&'):
            parked.append((rule['id'], rule.get('name', '')))
        for action in rule.get('programRuleActions', []):
            if action.get('programRuleActionType') != 'ASSIGN':
                continue
            for key in ('dataElement', 'trackedEntityAttribute'):
                target = action.get(key)
                if target:
                    assigned[target['id']] = (target.get('name', target['id']), rule['id'])

    we_send = ((set(TEA.values()) | set(DE.values()))
               - set(RULE_OWNED_FIELDS) - set(RULE_TRIGGER_FIELDS))
    unexpected = {uid: info for uid, info in assigned.items()
                  if uid in we_send and uid not in RULE_COMPATIBLE_FIELDS}

    logger.info(f"Program rules assign {len(assigned)} fields; "
                f"{len(RULE_OWNED_FIELDS)} left to the rules by design")

    for uid, (name, rule_id) in sorted(unexpected.items()):
        logger.warning(
            f"Program rule {rule_id} assigns '{name}' ({uid}), which this script "
            "also sends. If the values disagree the import fails with E1307/E1309 "
            "- add it to RULE_OWNED_FIELDS or make the value match.")

    for rule_id, name in parked:
        logger.warning(
            f"Program rule {rule_id} '{name}' is parked with a 'false &&' guard, "
            "which is how sync_patients.py disables a rule for the duration of a "
            "run. If no run is in progress it was left behind and should be "
            "restored on the server.")


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

def validate_only(api, patients, chunk_size):
    """
    Send the cohort with importMode=VALIDATE, which runs the whole import
    including the rule engine but writes nothing.

    This is the cheap way to find a rule collision: an E1307 only shows up once
    the engine has run, so a dry run to a file cannot catch it.
    """
    logger.info(f"Validating {len(patients)} patients against the server "
                f"(importMode=VALIDATE, nothing is written)")
    errors = 0

    for i in range(0, len(patients), chunk_size):
        chunk = patients[i:i + chunk_size]
        try:
            result = api.post('tracker', params={
                'async': 'false', 'importMode': 'VALIDATE',
                'importStrategy': 'CREATE_AND_UPDATE'}, json={
                'trackedEntities': chunk}).json()
        except (DHIS2RequestException, RequestsRequestException) as e:
            # A rejected import comes back as a 409 whose body carries the
            # validation report. Printing the exception alone truncates it to
            # the first error and hides which field the server objected to.
            reports = import_error_reports(e)
            logger.error(f"Chunk {i // chunk_size + 1} rejected:")
            for line in sorted(set(reports))[:8]:
                logger.error(f"  {line}")
            errors += max(len(reports), 1)
            continue

        reports = result.get('validationReport', {}).get('errorReports', []) or []
        if reports:
            errors += len(reports)
            for report in reports[:5]:
                logger.error(f"  {report.get('errorCode')}: "
                             f"{report.get('message')} (uid={report.get('uid')})")
        stats = result.get('stats', {})
        logger.info(f"  chunk {i // chunk_size + 1}: {result.get('status')} "
                    f"created={stats.get('created')} ignored={stats.get('ignored')}")

    if errors:
        logger.error(f"Validation found {errors} error(s); nothing was written.")
    else:
        logger.info("Validation clean: the cohort imports without rule collisions.")
    return errors == 0


# QR codes per lookup request. Forty of them make a URL of roughly 1.7 kB,
# which is close enough to the usual 2 kB limit to be worth staying under on a
# long run where one 414 would silently cost a whole batch its status.
QR_LOOKUP_BATCH = 25

# Where the second pass's work is written before it is attempted, so a run that
# dies after posting the patients but before (or during) the status update can
# be finished off without regenerating the cohort.
PENDING_STATE_FILE = 'pending_state.json'


def resolve_by_lookup_attribute(api, values):
    """
    Map each generated patient's lookup value to the tracked entity UID and
    org unit the server gave it.

    The server mints the UID, so this attribute - the QR code on Nigeria and
    the demo, the National ID on Egypt, settled in build_profile - is the only
    handle the script keeps on a specific patient after the import.
    """
    found = {}

    lookup_uid = TEA.get(INSTANCE.lookup_attribute) if INSTANCE else TEA.get('qr_code')
    if not lookup_uid:
        logger.error(f"This instance has no "
                     f"'{INSTANCE.lookup_attribute if INSTANCE else 'qr_code'}' "
                     f"attribute, so generated patients cannot be found again; "
                     f"the second pass is skipped.")
        return {}

    for start in range(0, len(values), QR_LOOKUP_BATCH):
        batch = values[start:start + QR_LOOKUP_BATCH]
        try:
            response = api.get('tracker/trackedEntities', params={
                'program': TARGET_PROGRAM,
                'orgUnit': ','.join(PILOT_STATES) or _root_org_units(api),
                'ouMode': 'DESCENDANTS',
                'filter': f"{lookup_uid}:in:" + ';'.join(batch),
                'fields': f"trackedEntity,orgUnit,attributes[attribute,value]",
                'pageSize': len(batch) * 2,
            }).json()
        except (DHIS2RequestException, RequestsRequestException) as e:
            logger.error(f"  could not look up {len(batch)} patient(s) by QR "
                         f"code: {str(e)[:200]}")
            continue

        for te in (response.get('instances') or response.get('trackedEntities') or []):
            for attribute in te.get('attributes', []):
                if attribute.get('attribute') == lookup_uid:
                    found[attribute.get('value')] = (te['trackedEntity'],
                                                     te['orgUnit'])

    return found


def apply_patient_state(api, pending, stats):
    """
    Write the two attributes the creating import does not let us set.

    NCD Patient Status is forced to ACTIVE by program rule GYmydeVoxDI, and the
    follow-up status is forced to UPCOMING_VISIT regardless of what the payload
    says - the live instance has a nightly cron that moves overdue patients to
    OVERDUE_PENDING_CALL, and creation does not anticipate it. Neither is
    re-asserted on a later update, so a second pass with the rule engine off
    makes both stick.

    `pending` is a list of (qr code, {attribute: value}). Which patient gets
    which value was decided when their record was built - by the reason the
    call centre gave for removing them from the overdue list, by
    draw_status_without_call, or by whether their booked visit has slipped.
    Resolving them by QR code rather than sampling the import keeps each value
    on the patient whose own record argues for it.
    """
    if not pending:
        return

    wanted = dict(pending)

    # Recorded before the lookup, not after: the point is to survive a failure
    # in the pass itself. The file is keyed by QR code, so replaying it later
    # finds the same patients.
    try:
        with open(PENDING_STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(wanted, f, indent=1)
        logger.info(f"Second-pass work written to {PENDING_STATE_FILE} "
                    f"({len(wanted)} patients) in case this pass has to be redone")
    except OSError as e:
        logger.warning(f"Could not write {PENDING_STATE_FILE}: {e}")
    located = resolve_by_lookup_attribute(api, sorted(wanted))

    missing = sorted(set(wanted) - set(located))
    if missing:
        logger.warning(
            f"{len(missing)} patient(s) needing an attribute update could not "
            f"be found by QR code; they keep the values the import left.")

    if not located:
        return

    payload = {'trackedEntities': [
        {'trackedEntity': uid,
         'trackedEntityType': TARGET_TRACKED_ENTITY_TYPE,
         'orgUnit': org_unit,
         'attributes': [{'attribute': attribute, 'value': value}
                        for attribute, value in sorted(wanted[qr].items())]}
        for qr, (uid, org_unit) in sorted(located.items())]}

    status_counts = Counter(wanted[qr].get(TEA['ncd_status'])
                            for qr in located if TEA['ncd_status'] in wanted[qr])
    follow_counts = Counter(wanted[qr].get(TEA['follow_up_status'])
                            for qr in located if TEA['follow_up_status'] in wanted[qr])

    logger.info(f"Second pass over {len(located)} patients: "
                f"NCD status {dict(status_counts) or 'unchanged'}, "
                f"follow-up {dict(follow_counts) or 'unchanged'}")

    try:
        result = api.post('tracker', params={
            'async': 'false', 'importStrategy': 'UPDATE',
            'skipRuleEngine': 'true'}, json=payload).json()
        s = result.get('stats', {})
        logger.info(f"  applied: {result.get('status')} "
                    f"updated {s.get('updated')}, ignored {s.get('ignored')}")
        for e in result.get('validationReport', {}).get('errorReports', [])[:3]:
            logger.error(f"  {e.get('errorCode')}: {e.get('message')}")
        for status, count in status_counts.items():
            stats[f'status_{status}'] += count
        for value, count in follow_counts.items():
            stats[f'applied_{value}'] += count
    except (DHIS2RequestException, RequestsRequestException) as e:
        logger.error(f"  second pass failed: {str(e)[:200]}")
        for line in import_error_reports(e)[:5]:
            logger.error(f"  {line}")


def write_preview(patients):
    with open(PAYLOAD_PREVIEW_FILE, 'w', encoding='utf-8') as f:
        json.dump({'trackedEntities': patients}, f, indent=2)
    logger.info(f"Dry run: wrote {len(patients)} patients to {PAYLOAD_PREVIEW_FILE}")


# ---------------------------------------------------------------------------
# Run modes
# ---------------------------------------------------------------------------

def run_synthetic(args, api_target, target_ous, stats, window_start):
    """
    Generate the cohort outright and post it, facility by facility.

    Each facility's batch is posted before the next is built, so an interrupted
    run leaves whole facilities done rather than a half-written cohort.
    """
    if args.visits_min < 1 or args.visits_max < args.visits_min:
        logger.error(f"--visits-min/--visits-max must satisfy 1 <= min <= max, "
                     f"got {args.visits_min}..{args.visits_max}")
        sys.exit(2)

    facilities = sorted(target_ous)
    total = args.patients
    if args.limit is not None:
        total = min(total, args.limit)
    if total <= 0:
        logger.error("Nothing to generate: --patients must be positive.")
        sys.exit(2)

    enrol_first, enrol_last = enrolment_window(
        args.enrol_window_days,
        parse_optional_date(args.enrol_from, '--enrol-from'),
        parse_optional_date(args.enrol_to, '--enrol-to'),
        args.visits_min)

    implied = (date.today() - enrol_first).days // SCHEDULED_VISIT_INTERVAL_DAYS + 1
    if implied > args.visits_max:
        logger.warning(
            f"The enrolment span implies up to {implied} monthly visits for the "
            f"earliest patients but --visits-max is {args.visits_max}. Raise it "
            f"to at least {implied} to avoid a fabricated gap in their history.")

    logger.info(
        f"Generating {total} patients (indices {args.start_index}-"
        f"{args.start_index + total - 1}) across {len(facilities)} facilities "
        f"{'in ' + ', '.join(sorted(PILOT_STATES.values())) if PILOT_STATES else ''}")
    logger.info(
        f"  enrolments spread evenly {enrol_first.isoformat()} .. "
        f"{enrol_last.isoformat()}; visits run from there to "
        f"{date.today().isoformat()}")

    # Even spread: the first few facilities absorb the remainder.
    per_facility = [total // len(facilities)] * len(facilities)
    for i in range(total % len(facilities)):
        per_facility[i] += 1

    held_back = []          # --dry-run / --validate-only build without posting
    outcomes = []           # (qr code, status) for the second pass
    index = args.start_index

    # Patients are built one facility at a time, so that an interrupted run
    # leaves whole facilities done. Taking the enrolment date straight from the
    # running index would then hand each facility a contiguous slice of the
    # enrolment window: the first facility would register its whole cohort in
    # the opening weeks and never again, the last one only at the very end.
    # Measured on a 5000-patient run before this was fixed, the median
    # enrolment date climbed steadily from the first facility to the last, 494
    # days apart, and a per-facility registration chart showed one spike each.
    #
    # Shuffling the positions decouples the two: every facility still gets its
    # patients in one batch, but their enrolment dates are drawn from across
    # the whole window. Seeded from the run salt, so a given --seed still
    # reproduces the same cohort.
    enrolment_order = list(range(total))
    random.Random(f'enrolment-spread:{RUN_SALT}').shuffle(enrolment_order)

    for facility, count in zip(facilities, per_facility):
        if count == 0:
            continue

        batch = []
        for _ in range(count):
            position = index - args.start_index
            # Seed per patient from the run salt, so the same --seed regenerates
            # an identical cohort.
            seed = patient_seed(f'synthetic-{index}', RUN_SALT)
            random.seed(seed)
            fake.seed_instance(seed)

            enrolled = spread_enrolment_date(enrolment_order[position], total,
                                             enrol_first, enrol_last)
            record, outcome = make_synthetic_patient(
                index, facility, target_ous[facility], stats, enrolled,
                args.visits_min, args.visits_max, window_start)
            batch.append(record)
            if outcome:
                outcomes.append(outcome)
            stats['migrated'] += 1
            index += 1

        logger.info(f"{target_ous[facility]['name']} "
                    f"({target_ous[facility]['state_name']}): {len(batch)} patients")

        if args.dry_run or args.validate_only:
            held_back.extend(batch)
        else:
            post_chunked_tracked_entities(
                api_target, batch, chunk_size=args.chunk_size,
                run_rules=not args.skip_rules, failed_file=FAILED_TEIS_FILE)

    if args.dry_run:
        write_preview(held_back)
    elif args.validate_only:
        validate_only(api_target, held_back, args.chunk_size)
    else:
        apply_patient_state(api_target, outcomes, stats)


def run_from_source(args, api_source, api_target, target_ous, stats):
    """
    Re-express real source patients against the pilot configuration.

    Each source facility is mapped onto one pilot facility, so patients seen at
    the same place in production stay together in the sandbox.
    """
    source_ous = sorted(args.source_ou or NIGERIA_OUS_WITH_DATA)
    facilities = sorted(target_ous)
    logger.info(f"Source org units: {len(source_ous)}")

    mapping = {src: facilities[i % len(facilities)]
               for i, src in enumerate(source_ous)}

    resume_uid = load_resume_uid(RESUME_FILE) if args.resume else None
    if resume_uid is not None and resume_uid not in source_ous:
        logger.error(f"Resume file {RESUME_FILE} points at '{resume_uid}', which "
                     "is not in the migration list. Delete or fix it first.")
        sys.exit(2)
    skip_ou = bool(resume_uid)
    held_back = []
    outcomes = []

    for source_ou in source_ous:
        if resume_uid is not None and source_ou == resume_uid:
            skip_ou = False
        if skip_ou:
            continue
        if args.limit is not None and stats['migrated'] >= args.limit:
            break

        save_last_uid(RESUME_FILE, source_ou)
        org_unit = mapping[source_ou]
        logger.info(f"Mapping source facility {source_ou} -> "
                    f"{target_ous[org_unit]['name']} "
                    f"({target_ous[org_unit]['state_name']})")

        events = api_source.get('tracker/events', params={
            'program': SOURCE_PROGRAM,
            'orgUnit': source_ou,
            'occurredAfter': args.after,
            'fields': 'trackedEntity',
            'skipPaging': 'true',
        }).json()['events']

        tei_uids = sorted({e['trackedEntity'] for e in events if e.get('trackedEntity')})
        logger.info(f"{len(tei_uids)} candidate patients")

        ready = []
        for tei_uid in tei_uids:
            if args.limit is not None and stats['migrated'] >= args.limit:
                break

            random.seed(patient_seed(tei_uid, RUN_SALT))
            fake.seed_instance(patient_seed(tei_uid, RUN_SALT))

            try:
                tei = api_source.get(f'tracker/trackedEntities/{tei_uid}', params={
                    'program': SOURCE_PROGRAM, 'fields': '*'}).json()
            except (DHIS2RequestException, RequestsRequestException) as e:
                if getattr(e, 'code', None) == 404:
                    logger.warning(f"Skipping {tei_uid}: no longer on the source")
                    continue
                raise

            transformed = transform_tei(remove_created_updated(tei), org_unit,
                                        target_ous[org_unit], stats)
            if transformed is None:
                continue

            record, outcome = transformed
            ready.append(record)
            if outcome:
                outcomes.append(outcome)
            stats['migrated'] += 1

        if not ready:
            continue

        if args.dry_run or args.validate_only:
            held_back.extend(ready)
        else:
            post_chunked_tracked_entities(
                api_target, ready, chunk_size=args.chunk_size,
                run_rules=not args.skip_rules, failed_file=FAILED_TEIS_FILE)

    if args.dry_run:
        write_preview(held_back)
    elif args.validate_only:
        validate_only(api_target, held_back, args.chunk_size)
    else:
        apply_patient_state(api_target, outcomes, stats)
        delete_resume_file(RESUME_FILE)


# Source facilities on the Nigeria NCD instance known to hold patients. Used as
# the default list for --from-nigeria so a run does not have to sweep the whole
# hierarchy looking for data.
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
    spread from it up to today. With --from-nigeria it keeps its other meaning,
    a filter on which source visits to read.
    """
    today = date.today()

    if args.after:
        window_start = parse_optional_date(args.after, '--after')
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
            f"trend lines nearly empty.")

    return window_start


# ---------------------------------------------------------------------------
# Visits-only top-up
# ---------------------------------------------------------------------------

def tea_value(attributes, name, default=None):
    """One attribute by semantic name, tolerating an instance that lacks it."""
    uid = TEA.get(name)
    return attributes.get(uid, default) if uid else default


def de_value(values, name, default=None):
    """One data value by semantic name, tolerating an instance that lacks it."""
    uid = DE.get(name)
    return values.get(uid, default) if uid else default


def regimen_from_values(values):
    """The four structured antihypertensives recorded on an existing visit."""
    return {key: (values.get(DE[key]) if key in DE else None) or None
            for key in MED_KEYS}


def allowed_sugar_unit():
    """A blood-sugar unit this instance's form actually accepts."""
    units = INSTANCE.blood_sugar_units if INSTANCE else ['MG_OR_DL', 'MMOL_OR_L']
    if len(units) == 1:
        return units[0]
    return 'MG_OR_DL' if random.random() < 0.75 else 'MMOL_OR_L'


def profile_from_existing(attributes, previous_values):
    """
    Enough of a profile to build one more visit for a patient who already
    exists, reconstructed from their attributes and their last visit.

    A top-up does not know the story the generator told when it created this
    patient, and does not need to: what the next visit looks like follows from
    who they are and what they were last prescribed. Anything the record does
    not carry - whether this clinician titrates correctly, which blood sugar
    unit the lab reports in - is drawn fresh, exactly as it would be for a new
    patient.
    """
    sex = (tea_value(attributes, 'sex') or 'FEMALE').upper()
    if sex not in ('MALE', 'FEMALE'):
        sex = 'FEMALE'

    dob = tea_value(attributes, 'dob') or ''
    try:
        age = age_from_dob(dob[:10])
    except (ValueError, TypeError):
        age = 55

    try:
        height = float(tea_value(attributes, 'height') or 0) or sample_height(sex)
    except (TypeError, ValueError):
        height = sample_height(sex)

    previous = previous_values or {}

    # Keep the patient's build: a weight recorded last visit implies a BMI, and
    # a patient does not change shape between appointments.
    bmi_target = sample_bmi_target()
    try:
        weight = float(de_value(previous, 'weight_kg') or 0)
        if weight and height:
            bmi_target = round(weight / ((height ** 2) / 10000), 1)
    except (TypeError, ValueError):
        pass

    hypertensive = (tea_value(attributes, 'has_hypertension') or 'YES').upper() == 'YES'
    diabetic = (tea_value(attributes, 'has_diabetes') or 'NO').upper() == 'YES'
    previous_regimen = regimen_from_values(previous)

    profile = {
        'sex': sex, 'age': age, 'dob': dob[:10],
        'height_cm': height, 'bmi_target': bmi_target,
        'hypertensive': hypertensive, 'diabetic': diabetic,
        'mistitrated': random.random() < p('mistitrated'),
        'dm_controlled': diabetic and random.random() < p('dm_controlled'),
        'days_of_meds': (de_value(previous, 'days_of_meds')
                         or random.choice(DAYS_OF_MEDICATION_CODES)),
        'other_htn_med': de_value(previous, 'other_htn_med'),
        # Honours the instance's permitted units: Egypt's form locks these
        # fields to mg/dL and a rule asserts it, so anything else is refused.
        'sugar_unit': de_value(previous, 'blood_sugar_unit') or allowed_sugar_unit(),
        'metformin': de_value(previous, 'metformin'),
        'glibenclamide': de_value(previous, 'glibenclamide'),
        'atorvastatin': de_value(previous, 'atorvastatin'),
        'titration_behaviour': random.choices(
            list(TITRATION_BEHAVIOURS),
            weights=list(TITRATION_BEHAVIOURS.values()), k=1)[0],
        'non_protocol_regimen': random.choice(NON_PROTOCOL_REGIMENS),
        'transitions': [],
    }

    # Which ARB they are already on, so a top-up does not switch them between
    # losartan and telmisartan for no reason.
    if previous_regimen.get('telmisartan'):
        profile['arb'] = 'telmisartan'
    elif previous_regimen.get('losartan'):
        profile['arb'] = 'losartan'
    else:
        profile['arb'] = ('telmisartan' if random.random() < TELMISARTAN_SHARE
                          else 'losartan')

    # If they have been on amlodipine alone above step 1, an ARB is being
    # withheld deliberately; honour that rather than starting one now.
    amlodipine_only = (dose_of(previous_regimen.get('amlodipine')) >= 10
                       and not previous_regimen.get('losartan')
                       and not previous_regimen.get('telmisartan'))
    profile['arb_contraindicated'] = amlodipine_only or (
        sex == 'FEMALE' and age <= CHILDBEARING_AGE_MAX
        and random.random() < p('arb_contraindicated_if_woman'))

    return profile, previous_regimen


def nigeria_visit_builder(api, stats):
    """
    A build_values function for topup_visits, closed over the attribute lookup
    it needs.

    Each patient's attributes are fetched once, when their turn comes.
    """
    cache = {}

    def attributes_for(tracked_entity):
        if tracked_entity not in cache:
            try:
                te = api.get(f'tracker/trackedEntities/{tracked_entity}', params={
                    'program': TARGET_PROGRAM,
                    'fields': 'attributes[attribute,value]'}).json()
                cache[tracked_entity] = {a['attribute']: a.get('value')
                                         for a in te.get('attributes', [])}
            except (DHIS2RequestException, RequestsRequestException):
                cache[tracked_entity] = {}
        return cache[tracked_entity]

    def build(tracked_entity, previous_event, visit_date):
        previous_values = {dv['dataElement']: dv.get('value')
                           for dv in (previous_event or {}).get('dataValues', [])}
        profile, previous_regimen = profile_from_existing(
            attributes_for(tracked_entity), previous_values)

        previous_step_now = regimen_step(previous_regimen)
        bp_state = bp_state_for_visit(profile, month_of(visit_date.isoformat()),
                                      previous_step_now)
        reading = bp_reading(profile, bp_state)

        if not profile['hypertensive']:
            regimen = empty_regimen()
            recommended = 'NONE'
        else:
            previous_step = regimen_step(previous_regimen)
            if previous_step is None:
                # Never on a protocol regimen, or never treated at all: the
                # first reading decides where they start, the same way it does
                # for a new patient.
                previous_step = (entry_step(*reading)
                                 if not regimen_is_empty(previous_regimen) else None)

            if previous_step is None:
                step = entry_step(*reading)
                recommended = first_visit_recommendation(reading, step)
            else:
                recommended = recommended_step(profile, previous_step, reading)
                # The titration decision shows at the visit it is made, so the
                # transition from last visit to this one carries it.
                step = next_step(profile, previous_step, recommended)

            regimen = step_regimen(profile, step)
            if (profile['titration_behaviour'] != 'protocol'
                    and random.random() < 0.5):
                regimen, step = apply_titration_deviation(
                    profile, 1, step, regimen, previous_regimen)

        category = classify_transition(
            previous_regimen, regimen, not regimen_is_empty(previous_regimen))
        stats[f'cat::{category}'] += 1
        stats['visits'] += 1

        return build_visit(
            profile, regimen, reading, recommended,
            dm_at_target=profile['dm_controlled'],
            with_labs=profile['diabetic'] and random.random() < p('labs_at_visit'))

    return build


def topup_org_units(api, args):
    """
    The part of the hierarchy a top-up works over.

    Nigeria's pilot is Ogun and Kano, so those are the default. The HTN/DM demo
    instance runs the same programme and the same metadata but a different
    hierarchy entirely, so when the pilot states are not there the scope falls
    back to the instance's roots. --topup-org-unit overrides both.
    """
    if args.topup_org_unit:
        return sorted(args.topup_org_unit)

    present = []
    for uid in sorted(PILOT_STATES):
        try:
            api.get(f'organisationUnits/{uid}', params={'fields': 'id'})
            present.append(uid)
        except (DHIS2RequestException, RequestsRequestException):
            pass

    if present:
        return present

    roots = api.get('organisationUnits', params={
        'filter': 'level:eq:1', 'fields': 'id,name', 'paging': 'false'}
    ).json()['organisationUnits']
    if not roots:
        logger.error("No root org unit on the target to scope the top-up to.")
        sys.exit(2)

    logger.info(f"The pilot states are not on this instance; scoping the top-up "
                f"to its root(s): {', '.join(o['name'] for o in roots)}")
    return [o['id'] for o in roots]


def run_visits_only(args, api, target_ous, stats):
    """Give existing patients their visit for the period."""
    built = run_topup(
        api,
        program=TARGET_PROGRAM,
        stage=VISIT_STAGE,
        org_units=topup_org_units(api, args),
        limit=args.visits_only,
        build_values=nigeria_visit_builder(api, stats),
        period_start=parse_optional_date(args.period_from, '--period-from'),
        period_end=parse_optional_date(args.period_to, '--period-to'),
        next_interval_days=SCHEDULED_VISIT_INTERVAL_DAYS,
        chunk_size=args.chunk_size,
        run_rules=not args.skip_rules,
        dry_run=args.dry_run,
        # They have just been seen and their next appointment is in the
        # future, so they are no longer on the overdue list.
        follow_up=(TEA['follow_up_status'], FOLLOW_UP_UPCOMING),
        tracked_entity_type=TARGET_TRACKED_ENTITY_TYPE,
    )
    stats['migrated'] = built


def parse_args():
    parser = argparse.ArgumentParser(
        description='Generate a population for the Nigeria HTN/DM programme in '
                    'the Ogun and Kano pilot states, or migrate one from the '
                    'Nigeria NCD instance.')
    parser.add_argument('--instance', default='nigeria',
                        choices=sorted(htn_instances.REGISTRY),
                        help='Which instance to generate for (default: '
                             '%(default)s). Decides the field map, the '
                             'treatment protocol and the default credentials')
    parser.add_argument('--target', default=None,
                        help='Credential prefix in .env naming the instance to '
                             'act on (default: %(default)s)')
    parser.add_argument('--allow-production', action='store_true',
                        help='Permit running against a target that looks like '
                             'production. Off by default; this script creates '
                             'and overwrites patient records.')
    parser.add_argument('--patients', type=int, default=500,
                        help='How many patients to generate, spread evenly over '
                             'the target facilities (default: %(default)s). '
                             'Ignored with --from-nigeria')
    parser.add_argument('--visits-min', type=int, default=DEFAULT_VISITS_MIN,
                        help=f'Fewest visits per generated patient '
                             f'(default: {DEFAULT_VISITS_MIN})')
    parser.add_argument('--visits-max', type=int, default=DEFAULT_VISITS_MAX,
                        help=f'Most visits per generated patient '
                             f'(default: {DEFAULT_VISITS_MAX})')
    parser.add_argument('--facilities-per-state', type=int, default=15,
                        help='How many facilities per pilot state to use '
                             '(default: %(default)s). 0 uses every assigned '
                             'facility in Ogun and Kano, which spreads a demo '
                             'cohort far too thin')
    parser.add_argument('--facility', action='append', default=[], metavar='UID',
                        help='Post to this facility only. Repeatable; overrides '
                             '--facilities-per-state')
    parser.add_argument('--visits-only', type=int, default=None, metavar='N',
                        help='Do not enrol anyone. Instead give N patients who '
                             'already exist their visit for the period: their '
                             'open appointment becomes a completed visit and a '
                             'fresh one is booked 28 days out. Only patients '
                             'with no visit in the period are eligible. This is '
                             'the monthly top-up')
    parser.add_argument('--topup-org-unit', action='append', default=[],
                        metavar='UID',
                        help='Limit --visits-only to this part of the hierarchy. '
                             'Repeatable. Defaults to the pilot states, or to '
                             "the instance's roots when those are not present - "
                             'which is what lets this run against the HTN/DM '
                             'demo, whose hierarchy differs but whose metadata '
                             'is the same')
    parser.add_argument('--period-from', default=None,
                        help='Start of the top-up period (YYYY-MM-DD). '
                             'Defaults to the first of the current month')
    parser.add_argument('--period-to', default=None,
                        help='End of the top-up period (YYYY-MM-DD). '
                             'Defaults to today')
    parser.add_argument('--from-nigeria', action='store_true',
                        help='Re-express real patients from the Nigeria NCD '
                             'instance instead of generating them. Needs --after '
                             'and the NIGERIA_NCD credentials. Only sex, date of '
                             'birth and the visit timeline survive')
    parser.add_argument('--source-ou', action='append', default=[], metavar='UID',
                        help='Source org unit to read patients from with '
                             '--from-nigeria. Repeatable; defaults to the known '
                             'facilities with data')
    parser.add_argument('--after', default=None,
                        help='Start of the data window (YYYY-MM-DD). Generated '
                             'visits are spread from this date up to today, and '
                             'enrolments are spread over the year up to today. '
                             'Defaults to '
                             f'{DEFAULT_WINDOW_MONTHS} months back. With '
                             '--from-nigeria it is a filter on source visits')
    parser.add_argument('--start-index', type=int, default=0,
                        help='Index of the first patient to generate (default: 0). '
                             'Run again with it set past the existing cohort to '
                             'add new arrivals without touching the earlier ones')
    parser.add_argument('--enrol-from', default=None,
                        help='Earliest enrolment date (YYYY-MM-DD). Set both '
                             '--enrol-from and --enrol-to to the last few days '
                             'to generate new arrivals for a weekly top-up')
    parser.add_argument('--enrol-to', default=None,
                        help='Latest enrolment date (YYYY-MM-DD). Defaults to a '
                             'month before today, leaving room for two visits')
    parser.add_argument('--enrol-window-days', type=int,
                        default=DEFAULT_ENROLMENT_WINDOW_DAYS,
                        help='How far back enrolments start, in days before '
                             f'today (default: {DEFAULT_ENROLMENT_WINDOW_DAYS}). '
                             'Ignored when --enrol-from is given')
    parser.add_argument('--resume', action='store_true',
                        help='Resume from the last processed source org unit')
    parser.add_argument('--dry-run', action='store_true',
                        help='Build the payload and write it to a file without '
                             'posting')
    parser.add_argument('--validate-only', action='store_true',
                        help='Send the cohort with importMode=VALIDATE: the '
                             'server runs the whole import, program rules '
                             'included, and writes nothing. This is what catches '
                             'a rule collision, which --dry-run cannot')
    parser.add_argument('--limit', type=int, default=None,
                        help='Stop after this many patients')
    parser.add_argument('--chunk-size', type=int, default=10,
                        help='Tracked entities per POST. Program rules make '
                             'imports slow, so keep this small (default: 10)')
    parser.add_argument('--skip-rules', action='store_true',
                        help='Disable the server-side program rule engine. Off by '
                             'default: the programme needs rules to derive the '
                             'protocol step ladder and the treatment '
                             'recommendation')
    parser.add_argument('--deterministic-uids', action='store_true',
                        help='Derive patient and event UIDs from --seed so a '
                             're-run addresses the same records. Off by default: '
                             'after a purge DHIS2 refuses to reuse a soft-deleted '
                             'UID (E1082)')
    parser.add_argument('--seed', type=int, default=None,
                        help='Seed the random generator for reproducible output')
    parser.add_argument('--mix', action='append', metavar='KEY=VALUE', default=[],
                        help='Override one population proportion, as a fraction '
                             'or a percentage. Repeatable. Keys: '
                             + ', '.join(sorted(DEFAULT_MIX)))
    parser.add_argument('--mix-file', metavar='PATH', default=None,
                        help='JSON file of population proportions, applied before --mix')
    parser.add_argument('--show-mix', action='store_true',
                        help='Print the population mix that would be used and exit')
    return parser.parse_args()


def main():
    args = parse_args()

    # Everything instance-specific is bound before any other work, because the
    # field maps and the protocol ladder decide what the rest of the run even
    # means.
    instance = htn_instances.get(args.instance)
    configure(instance)
    args.target = args.target or instance.env_prefix
    logfile(LOG_FILE)
    logger.info(f"Instance: {instance.label}  "
                f"(protocol: {instance.protocol.name}, credentials: "
                f"{args.target})")

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
            print(f"  {marker} {key:24} {MIX[key]:6.1%}")
        print("\n  * overridden from the default")
        return

    if args.dry_run and args.validate_only:
        logger.error("--dry-run and --validate-only do different things; "
                     "pass one or the other.")
        sys.exit(2)

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

    window_start = None if args.visits_only is not None else resolve_window_start(args)

    api_target = load_api(args.target, f'target ({args.target})')
    guard_destructive(args.target, api_target, args.allow_production,
                      'create and overwrite patient records')
    logger.warning(f"Target DHIS2 {api_target.version} rev {api_target.revision}")

    api_source = None
    if args.from_nigeria:
        api_source = load_api(SOURCE_ENV_PREFIX, 'source (Nigeria NCD)')
        logger.warning(f"Source DHIS2 {api_source.version} rev {api_source.revision}")

    resolve_target_dialect(api_target)
    check_target_metadata(api_target)
    warn_rule_assigned_fields(api_target)

    # A top-up writes only events, at the org units its patients are already
    # registered in, so it needs neither the geography index nor a facility
    # selection - both of which cost a sweep of several thousand org units.
    target_ous = {}
    if args.visits_only is None:
        geography = build_geography_index(api_target)
        target_ous = resolve_target_org_units(api_target, args, geography)

    stats = {k: 0 for k in (
        'no_enrollment', 'no_sex', 'generated_dob', 'age_rebased', 'migrated',
        'hypertensive', 'diabetic', 'smoker', 'cvd_history', 'mistitrated',
        'female', 'controlled', 'uncontrolled', 'dm_controlled', 'overdue',
        'visits', 'late_visits', 'lost_to_followup',
        'overdue_call', 'called', 'pending_call', 'calls', 'chase_calls',
        'removed_from_overdue', 'outcome_drawn',
        'status_DIED', 'status_TRANSFER', 'status_INACTIVE',
        'applied_OVERDUE_PENDING_CALL', 'applied_CALLED',
        'insured', 'with_supporter', 'arb_contraindicated', 'other_htn_med',
        'titration_due', 'titrated',
        'step_1', 'step_2', 'step_3', 'step_4', 'step_5',
        *(f'cat::{c}' for c in TRANSITION_CATEGORIES),
        *(f'behaviour::{b}' for b in TITRATION_BEHAVIOURS))}

    if args.visits_only is not None:
        run_visits_only(args, api_target, target_ous, stats)
    elif args.from_nigeria:
        run_from_source(args, api_source, api_target, target_ous, stats)
    else:
        run_synthetic(args, api_target, target_ous, stats, window_start)

    if args.visits_only is not None:
        report_topup(stats)
    else:
        report(stats)


def report_topup(stats):
    """A top-up writes visits, not patients, so the population mix does not
    apply; what matters is how those visits classified."""
    logger.info("=" * 62)
    logger.info(f"Visits added                   {stats['visits']}")
    total = sum(stats[f'cat::{c}'] for c in TRANSITION_CATEGORIES) or 1
    for category in TRANSITION_CATEGORIES:
        count = stats[f'cat::{category}']
        if count:
            logger.info(f"  {category:52} {count:6}  {count / total:5.1%}")


def report(stats):
    """Print achieved against requested, so a mix that did not land is obvious
    without waiting for the dashboard to refresh."""
    migrated = stats['migrated'] or 1
    logger.info("=" * 62)
    logger.info(f"Patients generated             {stats['migrated']}")

    def achieved(label, count, denominator, asked):
        share = count / denominator if denominator else 0
        logger.info(f"  {label:28} {count:5}  {share:5.1%}  (asked {asked})")

    def point(mix_key):
        return f"{MIX[mix_key]:.0%}"

    def band(name):
        return f"{MIX[f'{name}_min']:.0%}-{MIX[f'{name}_max']:.0%}"

    achieved('hypertensive', stats['hypertensive'], migrated, point('hypertensive'))
    # Over hypertensive patients, which is the denominator the HTN dashboard
    # indicators use: a patient who never had hypertension is in neither the
    # numerator nor the denominator.
    htn = stats['hypertensive'] or 1
    target = f"<{HTN_THRESHOLD[0]}/{HTN_THRESHOLD[1]}"
    high = f">={BP_HTN_UNCONTROLLED[0][0]}/{BP_HTN_UNCONTROLLED[1][0]}"
    achieved(f'BP controlled {target}', stats['controlled'], htn,
             band('controlled'))
    achieved(f'BP uncontrolled {high}', stats['uncontrolled'], htn,
             band('uncontrolled'))
    achieved('diabetic', stats['diabetic'], migrated, point('diabetic'))
    achieved('  glycaemia in range', stats['dm_controlled'],
             stats['diabetic'] or 1, point('dm_controlled'))
    achieved('no visit in 3 months', stats['overdue'], migrated, band('overdue'))
    achieved('lost to follow-up', stats['lost_to_followup'], migrated,
             point('lost_to_followup'))
    achieved('on the overdue list now', stats['overdue_call'], migrated,
             point('appointment_overdue'))
    overdue_now = stats['overdue_call'] or 1
    achieved('  still pending a call', stats['pending_call'], overdue_now,
             point('overdue_not_yet_called'))
    achieved('  rung by the call centre', stats['called'], overdue_now,
             f"{1 - MIX['overdue_not_yet_called']:.0%}")
    achieved('  removed from the list', stats['removed_from_overdue'],
             overdue_now, point('overdue_removed'))
    achieved('female', stats['female'], migrated, point('female'))
    achieved('smoker', stats['smoker'], migrated,
             f"{MIX['smoker_male']:.0%}/{MIX['smoker_female']:.0%} m/f")
    achieved('insured (BHCPF or SHIA)', stats['insured'], migrated, point('insured'))
    achieved('with a treatment supporter', stats['with_supporter'], migrated,
             point('has_supporter'))
    achieved('CVD history', stats['cvd_history'], migrated, 'MI+stroke+CKD')
    achieved('wrongly titrated', stats['mistitrated'], migrated, point('mistitrated'))
    achieved('ARB contraindicated', stats['arb_contraindicated'], migrated,
             'women <=49')

    logger.info(f"  {'visits written':28} {stats['visits']:5}  "
                f"({stats['visits'] / migrated:.1f} per patient, "
                f"{stats['late_visits']} late)")

    steps = [stats[f'step_{i}'] for i in range(1, MAX_PROTOCOL_STEP + 1)]
    if any(steps):
        logger.info("  protocol step at last visit  " + "  ".join(
            f"S{i}={n}" for i, n in enumerate(steps, start=1)))

    achieved('non-protocol med in Other', stats['other_htn_med'], migrated,
             point('other_htn_med'))

    # The headline treatment-quality number: of the visits where the previous
    # reading was at or above the threshold, how many moved the patient up a
    # step, which is what the protocol asks for after a month.
    due = stats['titration_due']
    logger.info(f"  {'titrated after a raised BP':28} {stats['titrated']:5}  "
                f"{stats['titrated'] / due if due else 0:5.1%}  "
                f"(of {due} occasions the protocol called for it)")

    # Treatment transitions, classified exactly as the reporting side does it.
    # Printed in full, including the empty ones, because a category with no
    # patients behind it is the thing worth noticing.
    total_transitions = sum(stats[f'cat::{c}'] for c in TRANSITION_CATEGORIES) or 1
    logger.info("-" * 62)
    logger.info("Treatment transitions (one per visit, classified as the "
                "dashboard does)")
    empty = []
    for category in TRANSITION_CATEGORIES:
        count = stats[f'cat::{category}']
        if not count:
            empty.append(category)
        logger.info(f"  {category:52} {count:6}  {count / total_transitions:5.1%}")

    if empty:
        logger.warning(f"{len(empty)} transition category/ies have no patients: "
                       f"{empty}. Raise the matching weight in "
                       f"TITRATION_BEHAVIOURS, or generate more patients.")
    else:
        logger.info("  every transition category is represented")

    logger.info("  prescribing behaviour drawn  " + "  ".join(
        f"{b}={stats[f'behaviour::{b}']}" for b in TITRATION_BEHAVIOURS))

    logger.info(f"  {'calling reports written':28} {stats['calls']:5}  "
                f"({stats['chase_calls']} chasing a late visit, "
                f"{stats['calls'] - stats['chase_calls']} on the open appointment)")

    applied = (stats['status_DIED'] + stats['status_TRANSFER']
               + stats['status_INACTIVE'])
    logger.info(f"  {'NCD status != ACTIVE':28} {stats['outcome_drawn']:5}  "
                f"drawn; {applied} applied after import "
                f"(DIED={stats['status_DIED']} TRANSFER={stats['status_TRANSFER']} "
                f"INACTIVE={stats['status_INACTIVE']})")
    logger.info(f"  {'follow-up set after import':28} "
                f"{stats['applied_OVERDUE_PENDING_CALL'] + stats['applied_CALLED']:5}  "
                f"(PENDING={stats['applied_OVERDUE_PENDING_CALL']} "
                f"CALLED={stats['applied_CALLED']}); creation forces "
                f"UPCOMING_VISIT and a nightly cron owns it on the live instance")

    if stats['no_enrollment'] or stats['no_sex'] or stats['generated_dob']:
        logger.info(f"  source gaps                  no enrollment="
                    f"{stats['no_enrollment']} no sex={stats['no_sex']} "
                    f"dob generated={stats['generated_dob']} "
                    f"age rebased={stats['age_rebased']}")


# Everything above is definitions; bind the default instance now that the
# tables configure() rebuilds all exist.
_configure_default()


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        logger.error("Interrupted by user.")
        sys.exit(130)
