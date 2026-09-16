"""
Check a generated Egypt payload against the clinical and metadata rules the
programme expects. Run after move_patients_egypt.py --dry-run.

    python move_patients_egypt.py --after 2025-01-01 --dry-run --limit 400
    python validate_egypt_payload.py egypt_payload_preview.json
"""

import json
import sys
from collections import Counter
from datetime import date, datetime

import move_patients_egypt as mp

TEA, DE = mp.TEA, mp.DE


def dv_map(event):
    return {d['dataElement']: d['value'] for d in event.get('dataValues', [])}


def attr_map(tei):
    return {a['attribute']: a['value'] for a in tei.get('attributes', [])}


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else 'egypt_payload_preview.json'
    with open(path, encoding='utf-8') as f:
        teis = json.load(f)['trackedEntities']

    failures = Counter()
    notes = Counter()
    today = date.today()

    def fail(check, detail=''):
        failures[check] += 1
        if failures[check] <= 2 and detail:
            print(f"    e.g. {check}: {detail}")

    controlled_last = 0
    hypertensive = 0
    overdue = 0
    steps_seen = Counter()
    bp_bands = Counter()

    for tei in teis:
        attrs = attr_map(tei)
        uid = tei.get('trackedEntity', '?')
        enrollment = tei['enrollments'][0]
        events = enrollment['events']

        visits = [e for e in events if e.get('status') == 'COMPLETED']
        scheduled = [e for e in events if e.get('status') == 'SCHEDULE']

        # --- structure -----------------------------------------------------
        if len(scheduled) != 1:
            fail('not exactly one scheduled visit', f"{uid} has {len(scheduled)}")
        if not visits:
            fail('no completed visit', uid)
            continue
        if enrollment['enrolledAt'] > visits[0]['occurredAt']:
            fail('enrolled after first visit',
                 f"{uid} {enrollment['enrolledAt']} > {visits[0]['occurredAt']}")
        if scheduled and scheduled[0].get('scheduledAt', '') <= visits[-1]['occurredAt']:
            fail('scheduled visit not after last visit', uid)

        dates = [e['occurredAt'] for e in visits]
        if dates != sorted(dates):
            fail('visits not chronological', uid)

        # --- National ID consistency ---------------------------------------
        nid = attrs.get(TEA['national_id'], '')
        dob = attrs.get(TEA['dob'], '')
        sex = attrs.get(TEA['sex'], '')
        if len(nid) != 14 or not nid.isdigit():
            fail('national ID not 14 digits', f"{uid} {nid!r}")
        else:
            century = {'2': 1900, '3': 2000}.get(nid[0])
            if century is None:
                fail('national ID century digit', nid)
            else:
                yy, mm, dd = int(nid[1:3]), int(nid[3:5]), int(nid[5:7])
                try:
                    embedded = date(century + yy, mm, dd).isoformat()
                except ValueError:
                    embedded = None
                if embedded != dob:
                    fail('national ID DOB disagrees with DOB attribute',
                         f"{uid} id={embedded} attr={dob}")
            expected_parity = 1 if sex == 'MALE' else 0
            if int(nid[12]) % 2 != expected_parity:
                fail('national ID gender parity disagrees with sex',
                     f"{uid} digit13={nid[12]} sex={sex}")
            gov = attrs.get(TEA['governorate'], '')
            if gov[2:4] != nid[7:9]:
                fail('national ID governorate disagrees with attribute',
                     f"{uid} id={nid[7:9]} attr={gov}")

        # --- geography ------------------------------------------------------
        district = attrs.get(TEA['district'], '')
        gov = attrs.get(TEA['governorate'], '')
        if district and gov and 'EG' + district[2:4] != gov:
            fail('district not inside governorate', f"{uid} {district} vs {gov}")

        # --- age ------------------------------------------------------------
        if dob:
            age = mp.age_from_dob(dob)
            # Sent explicitly so the CVD-risk rule conditions can see it.
            if TEA['age_header'] not in attrs:
                fail('age attribute missing; the CVD-risk rules need it', uid)
            elif attrs[TEA['age_header']] != str(age):
                fail('age attribute disagrees with DOB', f"{uid} {age}")

        # --- conditional attribute cascade ---------------------------------
        htn = attrs.get(TEA['has_hypertension'])
        dm = attrs.get(TEA['has_diabetes'])
        if htn not in ('YES', 'NO'):
            fail('hypertension answer missing', uid)
        if dm not in ('YES', 'NO'):
            fail('diabetes answer missing', uid)
        if htn == 'NO' and TEA['took_bp_meds'] in attrs:
            fail('BP medication question asked of a non-hypertensive', uid)
        if dm == 'NO' and TEA['took_dm_meds'] in attrs:
            fail('DM medication question asked of a non-diabetic', uid)
        if TEA['past_htn_drugs'] in attrs and attrs.get(TEA['took_bp_meds']) != 'YES':
            fail('past BP drugs without a YES to recent medication', uid)
        if TEA['past_dm_drugs'] in attrs and attrs.get(TEA['took_dm_meds']) != 'YES':
            fail('past DM drugs without a YES to recent medication', uid)

        # multi-select: no repeated molecule
        for key in ('past_htn_drugs', 'past_dm_drugs'):
            raw = attrs.get(TEA[key])
            if not raw:
                continue
            codes = raw.split(',')
            molecules = [mp.molecule_key(c) for c in codes]
            if len(set(molecules)) != len(molecules):
                fail(f'repeated molecule in {key}', f"{uid} {raw}")
            if 'OTHER' in codes:
                fail(f'OTHER selected in {key}', uid)

        # --- visits ---------------------------------------------------------
        if htn == 'YES':
            hypertensive += 1
        if dates[-1] < (today - mp.timedelta(days=mp.RECENT_VISIT_WINDOW_DAYS)).isoformat():
            overdue += 1

        for index, event in enumerate(visits):
            values = dv_map(event)
            is_last = index == len(visits) - 1

            systolic = int(values.get(DE['systolic'], 0))
            diastolic = int(values.get(DE['diastolic'], 0))
            if not (60 <= systolic <= 260):
                fail('systolic outside form bounds 60-260', f"{uid} {systolic}")
            if not (30 <= diastolic <= 150):
                fail('diastolic outside form bounds 30-150', f"{uid} {diastolic}")

            if systolic < 130 and diastolic < 80:
                band = 'controlled <130/80'
            elif systolic >= 140 or diastolic >= 90:
                band = 'uncontrolled >=140/90'
            else:
                band = 'above target 130-139/80-89'
            if is_last:
                bp_bands[band] += 1
                if band == 'controlled <130/80':
                    controlled_last += 1

            # weight / height bounds from the data entry guide
            weight = float(values.get(DE['weight_kg'], 0))
            if not (40 <= weight <= 250):
                fail('weight outside form bounds 40-250', f"{uid} {weight}")
            if DE['height_cm'] not in values:
                fail('height missing from visit', uid)
            if DE['scheduling_done'] not in values:
                fail('scheduling-done flag missing', uid)
            # BMI is sent on purpose: the CVD-risk rules read it and cannot
            # see the BMI rule's own assignment during the same import. It must
            # agree with height and weight.
            if DE['bmi'] not in values:
                fail('BMI missing; the CVD-risk rules need it', uid)
            else:
                h, w = float(values[DE['height_cm']]), float(values[DE['weight_kg']])
                if abs(float(values[DE['bmi']]) - w / (h / 100) ** 2) > 0.15:
                    fail('BMI disagrees with height and weight', uid)
            for rule_de in ('qwCWN0aLHAi', 'm5WImperUYv', 'CEEvww5FPbm', 'Lh9mvB8ASaB'):
                if rule_de in values:
                    fail('program-rule field sent explicitly', f"{uid} {rule_de}")

            # antihypertensives: monotherapy and combined pill are exclusive
            mono = values.get(DE['amlodipine'])
            combo = values.get(DE['amlo_valsartan'])
            if mono and combo:
                fail('monotherapy and combined pill together', uid)
            if htn == 'YES' and not (mono or combo):
                fail('hypertensive visit with no antihypertensive', uid)
            if htn == 'NO' and (mono or combo):
                fail('antihypertensive given to a non-hypertensive', uid)
            if mono:
                age = mp.age_from_dob(dob)
                if age < mp.AMLODIPINE_MONOTHERAPY_MIN_AGE:
                    fail('amlodipine monotherapy under the age threshold',
                         f"{uid} age {age}")
            hctz = values.get(DE['hctz'])
            if hctz and combo != 'AMLO_10_VAL_160':
                fail('thiazide added before the combined pill was maximised', uid)
            if combo == 'AMLO_5_VAL_80' and hctz:
                fail('step 1 combined pill with a thiazide', uid)

            # reconstruct the step for reporting
            if combo == 'AMLO_10_VAL_160' and hctz == 'HCTZ_50MG':
                steps_seen[4] += 1
            elif combo == 'AMLO_10_VAL_160' and hctz == 'HCTZ_25MG':
                steps_seen[3] += 1
            elif combo == 'AMLO_10_VAL_160':
                steps_seen[2] += 1
            elif combo or mono:
                steps_seen[1] += 1

            # diabetes
            measure = values.get(DE['dm_measure_type'])
            if dm == 'YES' and not measure:
                fail('diabetic visit with no glycaemic measure', uid)
            if dm == 'NO' and measure:
                fail('glycaemic measure on a non-diabetic', uid)
            if measure == 'HBA1C':
                hba1c = float(values[DE['hba1c']])
                if not (3 <= hba1c <= 20):
                    fail('HbA1c outside form bounds 3-20', f"{uid} {hba1c}")
                if DE['blood_sugar_unit'] in values:
                    fail('blood sugar unit sent with HbA1c', uid)
            elif measure == 'FBS':
                fbs = float(values[DE['fbs_mgdl']])
                if not (70 <= fbs <= 600):
                    fail('FBS outside form bounds 70-600', f"{uid} {fbs}")
                if values.get(DE['blood_sugar_unit']) != 'MG_OR_DL':
                    fail('FBS not locked to mg/dL', uid)
            for mmol_de in ('v7nC8vSTzwh', 'n6k7wJQ1daN', 'zgxvIQneRRX'):
                if mmol_de in values:
                    fail('mmol/L blood sugar sent although the form locks mg/dL',
                         f"{uid} {mmol_de}")

            # anti-diabetics multi-select
            oral = values.get(DE['antidiabetics'])
            if oral:
                codes = oral.split(',')
                molecules = [mp.molecule_key(c) for c in codes]
                if len(set(molecules)) != len(molecules):
                    fail('repeated molecule in anti-diabetics', f"{uid} {oral}")
                if dm != 'YES':
                    fail('anti-diabetics on a non-diabetic', uid)

            # insulin: all three fields together, dose under 100
            insulin_fields = [values.get(DE['insulin_name']),
                              values.get(DE['insulin_dose']),
                              values.get(DE['insulin_frequency'])]
            if any(insulin_fields) and not all(insulin_fields):
                fail('incomplete insulin prescription', uid)
            if values.get(DE['insulin_dose']):
                dose = int(values[DE['insulin_dose']])
                if not (1 <= dose <= 100):
                    fail('insulin dose outside 1-100 units', f"{uid} {dose}")

            # labs
            if DE['total_chol_mgdl'] in values:
                chol = float(values[DE['total_chol_mgdl']])
                if not (50 <= chol <= 500):
                    fail('cholesterol outside form bounds 50-500', f"{uid} {chol}")
                for de_key, (lo, hi) in (('hdl', (20, 100)), ('ldl', (30, 250)),
                                         ('triglycerides', (50, 5000))):
                    if DE[de_key] in values:
                        value = float(values[DE[de_key]])
                        if not (lo <= value <= hi):
                            fail(f'{de_key} outside form bounds {lo}-{hi}',
                                 f"{uid} {value}")
                mmol = float(values.get(DE['total_chol_mmol'], 0))
                if mmol and abs(mmol - chol / 38.67) > 0.05:
                    fail('cholesterol mmol/L does not match mg/dL', uid)

            # statin
            if values.get(DE['statin_drug']) and values.get(DE['statin_prescribed']) != 'true':
                fail('statin drug without the prescribed flag', uid)

    print(f"\nChecked {len(teis)} patients\n")
    print("Blood pressure at the latest visit:")
    total_last = sum(bp_bands.values()) or 1
    for band, count in bp_bands.most_common():
        print(f"  {band:32} {count:5}  {count / total_last:5.1%}")
    print(f"\nProtocol steps across all visits: "
          f"{dict(sorted(steps_seen.items()))}")
    print(f"Hypertensive: {hypertensive}/{len(teis)} = {hypertensive / len(teis):.1%}")
    print(f"No visit in {mp.RECENT_VISIT_WINDOW_DAYS} days: "
          f"{overdue}/{len(teis)} = {overdue / len(teis):.1%}")

    print()
    if failures:
        print("FAILURES")
        for check, count in failures.most_common():
            print(f"  {count:5}  {check}")
        sys.exit(1)

    print("All clinical and metadata checks passed.")


if __name__ == '__main__':
    main()
