"""
What each HTN/DM instance calls things, and how its treatment protocol works.

The three instances - the HTN/DM demo, Nigeria (sandbox 1) and Egypt
(sandbox 3) - run the same programme, the same two stages and the same tracked
entity type. Everything else is negotiable:

  * Names. Nigeria merges them into one "Patient name"; demo splits them into
    given and family; Egypt adds a middle name.
  * Fields that exist at all. Demo has no Height, Weight or BMI data element on
    the visit stage and no protocol-step fields; Egypt has lipids, insulin and
    a statin; Nigeria has LGA, a supporter and a QR code.
  * UIDs. Two instances can carry the same field under different UIDs - the
    health insurance attribute is vFaX4Whc3oq on demo and vZzcL1xQJa1 on
    Nigeria - so a field is addressed by what it MEANS and resolved to a UID
    per instance.
  * The treatment ladder. Nigeria and demo follow the Nigerian protocol
    (amlodipine, then an ARB, then a thiazide); Egypt uses a combined
    amlodipine/valsartan pill.
  * Option codes. Even where the data element is shared, the codes are not:
    Nigeria's LOSARTAN_50MG is LOSARTAN_50MG_OD on demo. Those are matched to
    the target at run time by molecule and dose rather than listed here.

A field absent from an instance's map is simply not sent, which is what lets
one generator write to all three.
"""

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set

# Shared by all three, and the reason this unification is possible at all.
PROGRAM = 'pMIglSEqPGS'
VISIT_STAGE = 'anb2cjLx3WM'
CALL_STAGE = 'W7BCOaSquMd'
TRACKED_ENTITY_TYPE = 'MCPQUTHX1Ze'


# ---------------------------------------------------------------------------
# Treatment protocols
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Protocol:
    """
    A treatment ladder, described by what each step prescribes.

    `steps` maps a step number to {medication key: option code}. The medication
    keys are semantic and resolve through the instance's `de` map, so a ladder
    written in terms of 'amlodipine' and 'arb' works on any instance that
    carries those data elements.

    `dose_steps` is the reverse lookup the transition classifier needs: a tuple
    of doses back to the step it represents. It is derived rather than written
    out, so the two cannot drift apart.
    """
    name: str
    steps: Dict[int, Dict[str, str]]
    dose_keys: tuple                 # which medication doses identify a step
    max_step: int
    # Blood pressure at or above which the patient is hypertensive, and the
    # readings that start someone higher up the ladder.
    threshold: tuple
    step_2_entry: Optional[tuple] = None
    step_3_entry: Optional[tuple] = None
    # Step that means "refer out" rather than "prescribe something new".
    referral_step: Optional[int] = None
    # What to prescribe when an ARB is contraindicated and the ladder's own
    # regimen cannot be used. Nigeria falls back to amlodipine at the step's
    # own dose, so it needs nothing here; Egypt's ladder is a combined pill
    # containing valsartan, so it has to drop to plain amlodipine.
    monotherapy: Optional[Dict[str, str]] = None
    # Combinations that are recorded in practice but match no step. Checked
    # against the ladder at startup so one cannot quietly become valid.
    non_protocol: List[Dict[str, str]] = field(default_factory=list)
    # The blood pressure a patient reads in each control state, as
    # ((systolic low, high), (diastolic low, high)). These belong to the
    # protocol because "at target" means whatever that protocol's target is:
    # Egypt treats 130/80 as raised, so a reading of 135/85 is controlled
    # under Nigeria's protocol and uncontrolled under Egypt's. Sharing one set
    # of bands between them made every Egyptian patient read above target
    # however well treated, and the ladder ran away as a result.
    bp_normal: tuple = ((105, 125), (65, 78))
    bp_at_target: tuple = ((120, 138), (72, 88))
    bp_above_target: tuple = ((140, 159), (90, 99))
    bp_uncontrolled: tuple = ((160, 199), (100, 119))
    # How strongly being on enough treatment shows in the reading. A shorter
    # ladder needs it higher: Egypt has four steps and no blood-pressure entry
    # point, so every patient starts at step 1 and, if treatment does not bite,
    # reaches the top within a year of monthly visits - where no titration is
    # possible and every raised reading counts against the titration rate.
    response_strength: float = 0.5

    def entry_step(self, systolic, diastolic):
        """Where a newly diagnosed patient starts, from their first reading."""
        if self.step_3_entry and (systolic >= self.step_3_entry[0]
                                  or diastolic >= self.step_3_entry[1]):
            return 3
        if self.step_2_entry and (systolic >= self.step_2_entry[0]
                                  or diastolic >= self.step_2_entry[1]):
            return 2
        return 1


# Federal Ministry of Health, Hypertension Treatment Protocol for Primary
# Health Care level. Used by Nigeria and, as agreed, by the demo instance.
#
#   1  amlodipine 5
#   2  amlodipine 5  + ARB (losartan 50 / telmisartan 40)
#   3  amlodipine 10 + ARB (losartan 100 / telmisartan 80)
#   4  amlodipine 10 + ARB + HCTZ 25
#   5  refer for specialist management - the step 4 regimen continues
NIGERIA_PROTOCOL = Protocol(
    name='Nigeria FMoH primary care',
    steps={
        1: {'amlodipine': 'AMLODIPINE_5MG_OD'},
        2: {'amlodipine': 'AMLODIPINE_5MG_OD',
            'losartan': 'LOSARTAN_50MG', 'telmisartan': 'TELMISARTAN_40MG_OD'},
        3: {'amlodipine': 'AMLODIPINE_10MG_OD',
            'losartan': 'LOSARTAN_100MG', 'telmisartan': 'TELMISARTAN_80MG_OD'},
        4: {'amlodipine': 'AMLODIPINE_10MG_OD',
            'losartan': 'LOSARTAN_100MG', 'telmisartan': 'TELMISARTAN_80MG_OD',
            'hctz': 'HYDROCHLOROTHIAZIDE_25MG_OD'},
        5: {'amlodipine': 'AMLODIPINE_10MG_OD',
            'losartan': 'LOSARTAN_100MG', 'telmisartan': 'TELMISARTAN_80MG_OD',
            'hctz': 'HYDROCHLOROTHIAZIDE_25MG_OD'},
    },
    dose_keys=('amlodipine', 'arb', 'hctz'),
    max_step=5,
    threshold=(140, 90),
    step_2_entry=(160, 100),
    step_3_entry=(180, 110),
    referral_step=5,
    non_protocol=[
        # Amlodipine pushed to 10 without adding an ARB - the commonest real
        # deviation, and the only move open to a patient who cannot take one.
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
    ],
)

# Egypt's ladder is the combined pill plus a thiazide at two strengths.
EGYPT_PROTOCOL = Protocol(
    name='Egypt HTN protocol',
    steps={
        1: {'amlo_valsartan': 'AMLO_5_VAL_80'},
        2: {'amlo_valsartan': 'AMLO_10_VAL_160'},
        3: {'amlo_valsartan': 'AMLO_10_VAL_160', 'hctz': 'HCTZ_25MG'},
        4: {'amlo_valsartan': 'AMLO_10_VAL_160', 'hctz': 'HCTZ_50MG'},
    },
    dose_keys=('amlo_valsartan', 'hctz'),
    max_step=4,
    # Egypt treats 130/80 as the target, which is why its bands differ from
    # Nigeria's throughout.
    threshold=(130, 80),
    # Below the Egyptian target of 130/80, then the two raised bands either
    # side of 140/90.
    bp_at_target=((112, 128), (70, 79)),
    bp_above_target=((130, 139), (80, 89)),
    bp_uncontrolled=((140, 179), (90, 109)),
    response_strength=0.5,
    # The combined pill carries valsartan, so a patient who cannot have an ARB
    # comes off it entirely rather than staying on a lower dose of it.
    monotherapy={'amlodipine': 'AMLODIPINE_5MG'},
    non_protocol=[
        # A thiazide added before the ladder reaches step 3.
        {'amlo_valsartan': 'AMLO_5_VAL_80', 'hctz': 'HCTZ_25MG'},
        {'amlo_valsartan': 'AMLO_5_VAL_80', 'hctz': 'HCTZ_50MG'},
        # A thiazide on its own, with nothing behind it.
        {'hctz': 'HCTZ_25MG'},
        {'hctz': 'HCTZ_50MG'},
    ],
)


# ---------------------------------------------------------------------------
# Instances
# ---------------------------------------------------------------------------

@dataclass
class Instance:
    """One target instance: what it is called, and what it calls things."""
    key: str
    label: str
    env_prefix: str
    protocol: Protocol
    tea: Dict[str, str]
    de: Dict[str, str]
    call_de: Dict[str, str]
    # Attributes a program rule owns on this instance, which must not be sent
    # with a value of our own.
    rule_owned: Set[str] = field(default_factory=set)
    # How the patient's name is split across attributes.
    name_style: str = 'given_family'      # or 'merged', 'given_middle_family'
    # What the insurance attribute accepts. Nigeria names the two national
    # schemes; the demo asks a plain yes/no against the shared YES/NO option
    # set, so the same field means different things on each.
    insurance_codes: List[str] = field(default_factory=list)
    # Which geography attributes to fill, and how.
    geography: str = 'none'               # 'nigeria_hierarchy', 'egypt_option_sets'
    # Which pool of names, phone formats and identifiers to draw from.
    identity: str = 'nigeria'             # or 'egypt'
    # The attribute used to find a patient again after the import, for the
    # second pass that writes the values the creating payload cannot. The
    # server mints the tracked entity UID, so this is the only handle we keep.
    # Egypt has no QR code; its National ID is unique and mandatory instead.
    lookup_attribute: str = 'qr_code'
    # Option sets the geography is drawn from, where it comes from option sets
    # rather than from the hierarchy.
    district_option_set: str = ''
    governorate_option_set: str = ''
    # Units the blood-sugar fields may be reported in. Egypt's form locks
    # RBS/FBS/PPBS to mg/dL and a rule asserts it, so offering mmol/L there
    # produces a value the rule then refuses.
    blood_sugar_units: List[str] = field(
        default_factory=lambda: ['MG_OR_DL', 'MMOL_OR_L'])
    # Default scope for a top-up and for facility selection.
    pilot_states: Dict[str, str] = field(default_factory=dict)
    facility_level: int = 5
    program: str = PROGRAM
    visit_stage: str = VISIT_STAGE
    call_stage: str = CALL_STAGE
    tracked_entity_type: str = TRACKED_ENTITY_TYPE

    def has_tea(self, name):
        return name in self.tea

    def has_de(self, name):
        return name in self.de


# Attributes every instance carries, so the engine can rely on them.
_COMMON_TEA = {
    'dob': 'NI0QRzJvQ0k',
    'sex': 'oindugucx72',
    'address': 'A6Hb0Kvg4vb',
    'phone': 'YRDy9xy9jD0',
    'consent': 'YJGACwhN0St',
    'has_hypertension': 'jCRIT4GMMOS',
    'has_diabetes': 'v4DnYfXn9Mu',
    'history_mi': 'oklF6MuAGaq',
    'history_ckd': 'bANIHEeQqEZ',
    'history_stroke': 'S4Pcuj5Eua7',
    'ncd_status': 'fI1P3Mg1zOZ',
    'follow_up_status': 'rgeuEnAI0nj',
}

# Visit data elements every instance carries.
_COMMON_DE = {
    'systolic': 'IxEwYiq1FTq',
    'diastolic': 'yNhtHKtKkO1',
    'amlodipine': 'eGY7e5Ttbys',
    'hctz': 'D8FWXAtCuL2',
    'other_htn_med': 'drY9ZQ0PEEw',
    'dm_measure_type': 'cE3KOFmoYbZ',
    'blood_sugar_unit': 'Xjrw5pbxBJw',
    'fbs_mgdl': 'oOHbZP28H2l',
    'fbs_mmol': 'v7nC8vSTzwh',
    'rbs_mgdl': 'ywITVJvILBR',
    'rbs_mmol': 'n6k7wJQ1daN',
    'ppbs_mgdl': 'LktTflPfk1z',
    'ppbs_mmol': 'zgxvIQneRRX',
    'hba1c': 'JZlJfx3oihw',
}

# Calling report. Result of call and the removal reason are shared; only
# Nigeria has a home-visit flag, and the defaulting reason is named
# "DELETE - ..." on the instances that still carry it, so it is not populated.
_COMMON_CALL_DE = {
    'call_result': 'q362A7evMYt',
    'remove_reason': 'MZkqsWH2KSe',
}


DEMO = Instance(
    key='demo',
    label='HTN/DM demo',
    env_prefix='HTN_DM_DEMO',
    protocol=NIGERIA_PROTOCOL,
    name_style='given_family',
    geography='none',
    tea={
        **_COMMON_TEA,
        'given_name': 'sB1IHYu2xQT',
        'family_name': 'ENRjVGxVL6l',
        'qr_code': 'sOpP7Czrt7H',
        'sms_consent': 'D40A8CAZBqz',
        'insurance': 'vFaX4Whc3oq',
        'took_bp_meds': 'l5yxkxHgTw9',
        'took_dm_meds': 'Xij8fyJKRX7',
    },
    insurance_codes=['YES'],
    de={
        **_COMMON_DE,
        'losartan': 'yUYbcCXo9Mv',
        'telmisartan': 'lWmOLo5hFYQ',
        'metformin': 'UXiaSOqzFts',
        'glibenclamide': 'WW5l5hS2JvI',
        'atorvastatin': 'Y72gWULmJGT',
        # Deliberately absent on this instance: height_cm, weight_kg, bmi,
        # days_of_meds and every protocol-step field. The generator skips
        # whatever is not here.
    },
    call_de=dict(_COMMON_CALL_DE),
    rule_owned={'fI1P3Mg1zOZ'},
)


NIGERIA = Instance(
    key='nigeria',
    label='Nigeria (sandbox 1)',
    env_prefix='SANDBOX1',
    protocol=NIGERIA_PROTOCOL,
    name_style='merged',
    geography='nigeria_hierarchy',
    pilot_states={'fBInDsbaQHO': 'Ogun', 'cTIw3RXOLCQ': 'Kano'},
    tea={
        **_COMMON_TEA,
        'patient_name': 'R6VkX5nsAy4',
        'qr_code': 'sOpP7Czrt7H',
        'id_number': 'RAViWaXdOM8',
        'state': 'HrHMllGSXtG',
        'lga': 'kUZs1ScFhzx',
        'insurance': 'vZzcL1xQJa1',
        'htn_treated_before': 'HpadWu1Fi39',
        'dm_treated_before': 'lk2cUwjjhXW',
        'supporter_name': 'JO6OoiD83nH',
        'supporter_phone': 'JUWgfAAGvBQ',
        'supporter_relation': 'MELevYwFa8I',
        'sms_consent': 'D40A8CAZBqz',
        'smoking': 'hXU9UgIMQes',
        'height': 'hTARCBirVLi',
    },
    insurance_codes=['INSURANCE_FEDERAL', 'INSURANCE_STATE'],
    de={
        **_COMMON_DE,
        'losartan': 'yUYbcCXo9Mv',
        'telmisartan': 'lWmOLo5hFYQ',
        'metformin': 'UXiaSOqzFts',
        'glibenclamide': 'WW5l5hS2JvI',
        'atorvastatin': 'Y72gWULmJGT',
        'days_of_meds': 'XA4IyHW1Dt4',
        'height_cm': 'BCMObXpMEzk',
        'weight_kg': 'uQwZbz0kUkZ',
        'bmi': 'J2YWUFBEyvZ',
        'recommended_step': 'wEVBblImCko',
    },
    call_de={**_COMMON_CALL_DE, 'home_visit': 'mOMHtDNHmDA'},
    rule_owned={
        'BSCvNvN52sg',   # Current Step
        'X9CTJkBSdGZ',   # Prev Step
        'Yu0kEfd0aeX',   # Treatment recommendation
        'gepG9GHO5zA',   # Previous BP
        'DkRNomXUMNr',   # Previous Meds
        'ea5w3mkinZo',   # Age (TEI header)
        'ifRI6qw96v4',   # phone on the calling report
        'qwCWN0aLHAi', 'dzlxGoRUDt9', 'm5WImperUYv',   # risk
    },
)


EGYPT = Instance(
    key='egypt',
    label='Egypt (sandbox 3)',
    env_prefix='EGYPT_SANDBOX3',
    protocol=EGYPT_PROTOCOL,
    name_style='given_middle_family',
    geography='egypt_option_sets',
    identity='egypt',
    lookup_attribute='national_id',
    district_option_set='zj1FfMoKhhC',
    blood_sugar_units=['MG_OR_DL'],
    governorate_option_set='CXjVw7UgWU0',
    facility_level=4,
    tea={
        **_COMMON_TEA,
        'given_name': 'sB1IHYu2xQT',
        'middle_name': 'wS7QQnuWCtc',
        'family_name': 'ENRjVGxVL6l',
        'national_id': 'a1N4sJukutU',
        'phc_id': 'r7EHikpEWkh',
        'governorate': 'mAWWEdw00l7',
        'district': 'InXHTXWxKR2',
        'took_bp_meds': 'l5yxkxHgTw9',
        'took_dm_meds': 'Xij8fyJKRX7',
        'past_htn_drugs': 'H35mkQTlqAY',
        'past_dm_drugs': 'OEG2DI4G2RG',
        'smoking': 'hXU9UgIMQes',
        'height': 'hTARCBirVLi',
        'age_header': 'ea5w3mkinZo',
    },
    de={
        **_COMMON_DE,
        'amlo_valsartan': 'bSTmAUelYYz',
        'height_cm': 'BCMObXpMEzk',
        'weight_kg': 'uQwZbz0kUkZ',
        'bmi': 'J2YWUFBEyvZ',
        'total_chol_mgdl': 'eNqHpd5y3zk',
        'hdl': 'A0R0OtmtKeT',
        'ldl': 'wYsjlYSXaPC',
        'triglycerides': 'YEkRDSlQK5W',
        'statin_prescribed': 'CBkUvBbOIKz',
        'statin_drug': 'bkJK1Xib0bs',
        'antidiabetics': 'YQnlNXxS19B',
        'insulin_name': 'FTKIOSgJPK4',
        'insulin_dose': 'RUCjJ3cGhB6',
        'insulin_frequency': 'm3lkE1CX10X',
        'scheduling_done': 'Mj9jx9JLmyl',
    },
    call_de=dict(_COMMON_CALL_DE),
    rule_owned={
        'pSqsIKFb8NX',   # Total cholesterol mmol/L
        'fI1P3Mg1zOZ',   # NCD Patient Status
        'qwCWN0aLHAi', 'm5WImperUYv', 'DKKPQKIO9qN',   # risk
        'vdh990I4zq1',   # BP in one field
        'ifRI6qw96v4',
    },
)


REGISTRY = {i.key: i for i in (DEMO, NIGERIA, EGYPT)}


def get(key):
    """Look up an instance by name, with a usable error when it is unknown."""
    try:
        return REGISTRY[key]
    except KeyError:
        raise SystemExit(
            f"Unknown instance '{key}'. Choose one of: {', '.join(sorted(REGISTRY))}")
