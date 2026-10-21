# Comparing Egypt instances

How to diff the Hypertension & Diabetes programme metadata between two
instances and move selected differences across, plus what the first real
comparison found.

| Prefix in `.env` | Instance | Role |
|---|---|---|
| `EGYPT_SANDBOX3` | `https://dhis2-sandbox3.simple.org` | internal sandbox, DHIS2 2.42.5.1 |
| `EGYPT_STAGING` | `https://dhis2.space` | Egypt staging, DHIS2 2.42.5.1 |
| `EGYPT_PROD` | blank, deliberately | real patient data |

## The workflow

```
python diff_metadata.py                                 # read both, write a selection
# edit metadata-selection.json: set an action per theme or per object
python apply_metadata_selection.py                      # validate against the server
python apply_metadata_selection.py --confirm APPLY      # commit
```

`diff_metadata.py` is read-only. It compares programmes, program stages,
program rules and their actions, rule variables, program indicators,
indicators and their types and groups, data elements, tracked entity
attributes, option sets and constants. Aggregate data exchanges are compared
only when named in `--only`, since they are maintained separately.

Each difference is tagged with a theme, because reviewing 166 objects
individually is not realistic. Setting an action on a theme covers every
difference in it; setting one on an individual object overrides its theme.
Actions are `skip`, `a_to_b`, `b_to_a`, `delete_a`, `delete_b`.

`apply_metadata_selection.py` re-reads both instances rather than trusting the
selection file, backs up every object it would displace, and without
`--confirm` sends each payload with `importMode=VALIDATE` so the report is the
destination server's own verdict. Objects are written in dependency order:
option sets and data elements before the rules and indicators that reference
them, deletions last and in reverse.

## Two things that shaped the design

**Whole objects do not copy between instances.** A program rule's actions, a
stage's data element assignments and an indicator's period boundaries are
separate objects whose UIDs are minted per instance. Sending side A's rule
wholesale makes the destination reference actions it does not have, and the
import fails with E5002. So an object present on both sides is *patched*: only
the fields the diff reported are taken from the source, and the destination's
own nested objects are left alone. An object absent on the destination is
created whole, together with the children it cannot work without — a rule's
actions, an option set's options.

**Order is not meaning.** DHIS2 returns collection members in no particular
order, and option `sortOrder` is numbered from 0 on one instance and 1 on the
other. Both would make two identically configured instances differ on almost
every object. Collections are therefore compared as sets keyed on meaning, and
option order is compared as rank rather than raw value.

## What the first comparison found

166 differences. The large mechanical ones are changes applied on sandbox 3
and never carried over, recorded in `EGYPT_METADATA_CHANGES.md`:

| Theme | Count | Direction |
|---|---|---|
| `bmi35-boundary` | 45 | sandbox 3 → staging; staging still has the `<= 35` overlap bug |
| `active-patient-filter` | 23 | sandbox 3 → staging; `== 'ACTIVE'` replaces `!= 'DIED'` |
| `cascade-constants` | 10 | sandbox 3 → staging; constants replace a hardcoded 100000 |
| `bp-threshold` | 22 | sandbox 3 → staging, by decision |
| `bp-superseded-on-b` | 3 | delete from staging |
| `rule-service-state` | 5 | see below |

### The blood pressure thresholds

The two instances had solved the same requirement in opposite ways, and the
shared UIDs ended up meaning different things:

- **sandbox 3** repurposed the original chain (`ChIb461qMBC` controlled,
  `N69Wu2mzoFk` uncontrolled) to **130/80**, and added new data elements
  `zX4UHf3HHZ6` / `QmuglZ0Wk1C` for **140/90**.
- **staging** left the original chain at **140/90** and added separate
  "Egypt target" data elements `pQYBuYST69E` / `yiq3ZdNDo7W` for **130/80**.

Both were internally consistent; the threshold lives in the program indicator
filter (`ApG3MF1VmOL`, `HEhrrdabdGQ`), not in any name. Decision taken:
**sandbox 3 is the reference for 140/90 and 130/80**, so its versions override
staging and staging's parallel "Egypt target" set is deleted.

The diff detects this class of difference specifically, by reading the
threshold out of comparisons against the systolic and diastolic data elements,
so a change of definition cannot hide inside a filter that also carries an
intended fix. Four program indicators were initially mis-filed under
`active-patient-filter` for exactly that reason.

### Rules out of service

A program rule has no enabled flag in 2.42. One is taken out of service by
putting `DISABLED` in its name or wiring `false &&` into its condition, and the
diff detects both. Three rules are live on staging and disabled on sandbox 3,
and staging is correct for these, so they flow back:

```
APmP5Vi8oQV  LOCK Scheduling Checkbox when true
Lj7Gk1JBFtZ  LOCK Scheduling Checkbox
nQwR59NXQT9  SHOWERROR if visit not scheduled
```

Two more are disabled on sandbox 3 and absent from staging
(`h9AhA9XdX75`, `ye53TyrqZ0t`); they are sandbox-3 experiments and were left
alone. `ppkPGQqGG4o` is disabled on both.

### Objects staging is missing

The stage assignment the user expected: **Risk Classification `m5WImperUYv`
and Statin prescribed `CBkUvBbOIKz` are not assigned to the visit stage on
staging**, so a rule assigning to them there would do nothing. Staging is also
missing 4 option sets, several data elements including those two, the tracked
entity attribute `azB2Ab4o9Mm` (Weight), both cascade constants, and the
program rules that assign Risk Classification and Statin prescribed.

Dependency order matters here: the option sets have to exist before the data
elements that use them, which have to exist before the rule variables, the
stage assignment and the rules. In `VALIDATE` mode 13 objects are reported as
*pending a dependency* — they reference something the same run would create
earlier, which a validate pass cannot see because nothing is committed.

## The Capture custom form is a separate axis

`diff_metadata.py` compares metadata. It does not look at
`dataStore/capture/dataEntryForms`, which is where the Capture app keeps the
custom form layout, and the two drift independently: a section can be correct
in the metadata on both instances and rendered on neither.

Once a program stage has a layout under that key, Capture renders that layout
and ignores any section the layout does not mention. The form editor writes
the layout as it stood when it was last saved, so a section added in
Maintenance afterwards is in the metadata and never drawn. That is what had
happened to `kGNeiLAEe5V` (Scheduled next visit) on staging - 8 sections in the
metadata, 7 in the layout. `fix_capture_form_sections.py --target PREFIX`
reconciles the two.

A `fieldMap` is access, not rendering. `ProgramStageFactory` builds the mapped
data elements into the plugin's own config and adds only the plugin to the
section, so a data element that appears nowhere but a `fieldMap` has no field
in the form, and the program rules that act on that field have nothing to act
on. The documented pattern maps fields the form also renders normally. Both
instances now hold the plugin and the field.

Comparing the two layouts found one real difference beyond that: on sandbox 3
the CVD risk plugin read `totalCholesterol` from `LktTflPfk1z`, which is
**HTN - Blood sugar reading: PPBS (mg/dL)**, not `pSqsIKFb8NX`
(HTN - Total Cholesterol (mmol/L)). Both data elements exist on both instances
and both run cvd-risk-plugin 1.0.0 with identical code, so this was a
mismapping rather than two UIDs for one concept: a glucose reading around 140
to 200 was reaching an input expecting cholesterol around 3 to 7. Corrected on
sandbox 3 on 2026-10-21; staging was already right.

Two differences are left deliberately:

- The layouts render in a different order. Sandbox 3 follows `sortOrder`;
  staging puts BMI first, contradicting its own metadata. Cosmetic.
- Staging's BMI section carries `WGKAijL898k` (HTN/DM - Smoking at this visit),
  which does not exist on sandbox 3 at all. A metadata gap, not a layout one -
  copying the layout would reference an object that is not there.

`CBkUvBbOIKz` (Statin prescribed?) is in the Statins section in the metadata on
both instances and in the form on neither, confirmed deliberate. It is listed
in `DELIBERATELY_OMITTED` so the reconciler reports it as intentional and
`--restore-missing-fields` leaves it alone.

## Preflight

`preflight_target.py --target PREFIX` stays the quick read-only check that an
instance can be written to at all. It now also reports:

- data elements the generator writes that are **not assigned** to the visit stage
- program rules that are out of service, and why
- exported program indicators with **no decimals** set, whose values the
  exchange rejects with E7619
- EVENT-type program indicators with **no period boundaries**, whose SQL is
  malformed

## Gotchas worth keeping

1. A newly created program indicator's filter is ignored until the analytics
   tables are rebuilt. Create, run analytics, then measure.
2. Analytics responses are cached; clear with
   `POST /api/maintenance?analyticsCacheClear=true` between measurements.
3. Build a program indicator by cloning a working one. Hand-built ones lose
   `decimals`, `orgUnitField` and `analyticsPeriodBoundaries`.
4. Reading programme attributes needs `program=` on the tracked entity request,
   or they come back absent.
5. `tracker/trackedEntities` takes `ouMode`; `tracker/events` needs
   `orgUnitMode` and silently returns nothing for the wrong one.
6. Program rules do not chain on import: one rule's `ASSIGN` is not visible to
   another rule's condition.
7. E3012 "does not have read access" from the metadata importer can simply mean
   the referenced object does not exist.
8. Use `fields=:owner` for anything that will be written back.

## Loose ends

- The `attributeValues` on program indicators — the `vudyDP7jUy5` mapping to an
  aggregate data element — are reported as differing but not patched, since the
  exchange configuration is maintained separately. One indicator is affected.
- The contactable-overdue indicator reports 80 where the tracker data suggests
  129; unexplained, deprioritised.
- `EGYPT_METADATA_CHANGES.md` names the sandbox hostname and is in a public
  repository.
- The `d2pat_` production token that was briefly in `.env.example` should be
  rotated.
