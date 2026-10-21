"""
Put the program stage sections Capture forgot back into its custom form.

Once a program stage has a custom form layout in the `capture` data store
(key `dataEntryForms`), the Capture app renders that layout and nothing else.
The layout is a list of sections, each with an explicit list of fields and
plugins, written by the form editor at the moment it was saved. A section
added to the stage in Maintenance afterwards is in the metadata but not in the
layout, so Capture never draws it - which is what happened to

    kGNeiLAEe5V  Scheduled next visit  (stage anb2cjLx3WM, sortOrder 7)

holding the single data element Mj9jx9JLmyl (HTN - Scheduling done). The
`htn-scheduler` plugin that writes that data element was configured into
`Z5o8bFTVDSk` (Statins, anti-diabetics and others), the last section the
layout did know about.

This script compares the stored layout against programStageSections and:

  * inserts any missing section at its sortOrder position, carrying the
    section's own data elements as fields;
  * moves a plugin into that section when every data element in the plugin's
    fieldMap belongs to it, so the widget lands under its own heading instead
    of the section it was parked in, while still giving the data element a
    field of its own - a fieldMap is access, not rendering;
  * reports data elements that are in a section's metadata but absent from the
    layout. Those are left alone - dropping a field from a custom form is
    something the form editor lets you do on purpose, and the ones already
    known to be deliberate are listed in DELIBERATELY_OMITTED. Pass
    --restore-missing-fields to add the rest back.

    python fix_capture_form_sections.py                   # show what would change
    python fix_capture_form_sections.py --confirm APPLY   # write it

The whole data store value is backed up before anything is written, and read
back and re-checked afterwards.
"""

import argparse
import json
import sys
from datetime import datetime

from dhis2 import RequestException as DHIS2RequestException
from logzero import logger

from tracker_common import load_api, guard_destructive

TARGET_ENV_PREFIX = 'EGYPT_STAGING'
NAMESPACE = 'capture'
KEY = 'dataEntryForms'
CONFIRM_PHRASE = 'APPLY'

# The form editor writes this type for every non-plugin field, on tracked
# entity attributes and stage data elements alike. New fields match it so the
# layout stays internally consistent.
FIELD_TYPE = 'TrackedEntityAttribute'

# Data elements that belong to a section in the metadata but are kept out of
# the form on purpose. Without this the script reports them on every run as if
# they were the same kind of oversight as a missing section, and
# --restore-missing-fields would put them back.
DELIBERATELY_OMITTED = {
    # CV - MED - Statin prescribed?, in Z5o8bFTVDSk (Statins, anti-diabetics
    # and others). Hidden deliberately, confirmed 2026-10-21.
    'CBkUvBbOIKz',
}


def load_layout(api):
    return api.get(f'dataStore/{NAMESPACE}/{KEY}').json()


def load_sections(api, stage):
    sections = api.get('programStageSections', params={
        'filter': f'programStage.id:eq:{stage}', 'paging': 'false',
        'fields': 'id,name,sortOrder,dataElements[id,name]',
    }).json()['programStageSections']
    return sorted(sections, key=lambda s: s['sortOrder'])


def plugin_targets(element):
    """The data element / attribute UIDs a plugin element reads and writes."""
    return [m['IdFromApp'] for m in element.get('fieldMap') or []]


def field_ids(entry):
    """
    The data elements this section renders as inputs.

    Plugin elements are deliberately not counted. A fieldMap grants the plugin
    access to a data element, it does not draw it: ProgramStageFactory builds
    the mapped data elements into the plugin's own config and adds only the
    plugin to the section. The documented pattern (capture-app
    docs/developer/form-field-plugins/manual-setup.md) maps fields the form
    also renders normally. A data element that appears only in a fieldMap has
    no field in the form, and program rules that act on that field - hide it,
    assign to it, warn on it - have nothing to act on.
    """
    return [e['id'] for e in entry['elements'] if e.get('type') != 'plugin']


def plan_changes(layout_entries, sections, restore_missing_fields):
    """
    Work out the new layout. Returns (entries, added, moved, dropped_fields).

    `entries` is a fresh list; the input is not modified.
    """
    entries = [json.loads(json.dumps(e)) for e in layout_entries]
    by_id = {e['id']: e for e in entries}
    meta = {s['id']: s for s in sections}

    missing = [s for s in sections if s['id'] not in by_id]
    added, moved, dropped = [], [], []

    for section in missing:
        section_des = [d['id'] for d in section['dataElements']]

        # A plugin already carrying every data element of this section belongs
        # under this section's heading, not wherever the editor left it.
        plugins = []
        for entry in entries:
            for element in list(entry['elements']):
                if element.get('type') != 'plugin':
                    continue
                targets = plugin_targets(element)
                if targets and set(targets) <= set(section_des):
                    entry['elements'].remove(element)
                    moved.append((element['id'], entry['id'], section['id']))
                    plugins.append(element)

        # A data element stays a field of its own even when a plugin maps it:
        # see field_ids() on why a fieldMap is not a rendering.
        elements = plugins + [{'id': de, 'type': FIELD_TYPE} for de in section_des]

        new_entry = {'id': section['id'], 'elements': elements}

        # Insert where the section sorts, so the form reads in sortOrder.
        order = [s['id'] for s in sections]
        position = len(entries)
        for index, entry in enumerate(entries):
            if entry['id'] in order and order.index(entry['id']) > order.index(section['id']):
                position = index
                break
        entries.insert(position, new_entry)
        by_id[section['id']] = new_entry
        added.append((section, elements))

    # Fields the layout leaves out of a section it does know about.
    for entry in entries:
        section = meta.get(entry['id'])
        if not section:
            continue
        shown = set(field_ids(entry))
        meta_order = [d['id'] for d in section['dataElements']]
        absent = [d for d in section['dataElements']
                  if d['id'] not in shown and d['id'] not in DELIBERATELY_OMITTED]
        for data_element in absent:
            dropped.append((section, data_element))
            if not restore_missing_fields:
                continue
            # Slot it among the fields in metadata order, leaving the plugins
            # where they are rather than reshuffling a section that works.
            rank = meta_order.index(data_element['id'])
            index = len(entry['elements'])
            for position, element in enumerate(entry['elements']):
                if element.get('type') == 'plugin' or element['id'] not in meta_order:
                    continue
                if meta_order.index(element['id']) > rank:
                    index = position
                    break
            entry['elements'].insert(index, {'id': data_element['id'], 'type': FIELD_TYPE})

    return entries, added, moved, dropped


def report(stage, sections, layout_entries, added, moved, dropped):
    meta = {s['id']: s for s in sections}
    logger.info(f"Stage {stage}: {len(sections)} section(s) in metadata, "
                f"{len(layout_entries)} in the stored layout")
    for section in sections:
        present = any(e['id'] == section['id'] for e in layout_entries)
        mark = ' ' if present else '+'
        logger.info(f"  {mark} {section['id']}  sortOrder {section['sortOrder']}  "
                    f"{section['name']}" + ('' if present else '   <- not rendered by Capture'))

    for section, elements in added:
        names = {d['id']: d['name'] for d in section['dataElements']}
        logger.info(f"ADD section {section['id']} ({section['name']}) with "
                    f"{len(elements)} element(s):")
        for element in elements:
            if element.get('type') == 'plugin':
                logger.info(f"    plugin {element['id']} "
                            f"-> {', '.join(plugin_targets(element))}")
            else:
                logger.info(f"    field  {element['id']}  {names.get(element['id'], '')}")

    for plugin, source, target in moved:
        logger.info(f"MOVE plugin {plugin}: {source} "
                    f"({meta[source]['name'] if source in meta else '?'}) "
                    f"-> {target} ({meta[target]['name']})")

    for section, data_element in dropped:
        logger.warning(f"NOT IN LAYOUT: {data_element['id']} ({data_element['name']}) "
                       f"is in section {section['id']} ({section['name']}) but has no "
                       f"field in the form")

    for section in sections:
        for data_element in section['dataElements']:
            if data_element['id'] in DELIBERATELY_OMITTED:
                logger.info(f"  left out on purpose: {data_element['id']} "
                            f"({data_element['name']}) in {section['id']} "
                            f"({section['name']})")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--target', default=TARGET_ENV_PREFIX,
                        help='Credential prefix in .env naming the instance to act on (default: %(default)s).')
    parser.add_argument('--stage', action='append', default=None,
                        help='Program stage UID to reconcile. Repeatable. Default: every stage already in the layout.')
    parser.add_argument('--restore-missing-fields', action='store_true',
                        help="Also add back data elements that are in a section's metadata but have no field in the layout.")
    parser.add_argument('--confirm', default=None,
                        help=f'Type {CONFIRM_PHRASE} to write the new layout.')
    parser.add_argument('--allow-production', action='store_true',
                        help='Required if the target looks like a production instance.')
    args = parser.parse_args()

    api = load_api(args.target, f'target ({args.target})')
    guard_destructive(args.target, api, args.allow_production,
                      'overwrite the Capture custom form layout')
    logger.info(f"Target DHIS2 {api.version} at {api.base_url}")

    try:
        layout = load_layout(api)
    except DHIS2RequestException as e:
        logger.error(f"Cannot read dataStore/{NAMESPACE}/{KEY}: {str(e)[:200]}")
        sys.exit(2)

    stages = args.stage or list(layout.keys())
    new_layout = json.loads(json.dumps(layout))
    changed = False

    for stage in stages:
        if stage not in new_layout:
            logger.warning(f"Stage {stage} has no custom form layout; skipping. "
                           f"Capture renders its sections from metadata already.")
            continue

        sections = load_sections(api, stage)
        entries, added, moved, dropped = plan_changes(
            new_layout[stage], sections, args.restore_missing_fields)
        report(stage, sections, new_layout[stage], added, moved, dropped)

        if added or moved or (dropped and args.restore_missing_fields):
            new_layout[stage] = entries
            changed = True
        else:
            logger.info(f"Stage {stage}: layout already matches the sections.")

    if not changed:
        logger.info("Nothing to change.")
        return

    if args.confirm != CONFIRM_PHRASE:
        logger.warning(f"Nothing saved. Re-run with --confirm {CONFIRM_PHRASE} to write "
                       f"the layout above to dataStore/{NAMESPACE}/{KEY}.")
        return

    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    backup = f'capture-forms-backup-{stamp}.json'
    with open(backup, 'w', encoding='utf-8') as f:
        json.dump(layout, f, indent=2)
    logger.info(f"Current layout backed up to {backup}")

    try:
        api.put(f'dataStore/{NAMESPACE}/{KEY}', json=new_layout)
    except DHIS2RequestException as e:
        logger.error(f"Save failed: {str(e)[:300]}")
        logger.error(f"The instance is unchanged. Restore from {backup} if in doubt.")
        sys.exit(4)

    saved = load_layout(api)
    if saved == new_layout:
        logger.info("Saved and read back identical.")
    else:
        logger.error("Read-back differs from what was sent. Check the instance "
                     f"and restore from {backup} if needed.")
        sys.exit(5)

    for stage in stages:
        if stage not in saved:
            continue
        sections = load_sections(api, stage)
        absent = [s for s in sections
                  if not any(e['id'] == s['id'] for e in saved[stage])]
        if absent:
            logger.error(f"Stage {stage}: still not in the layout: "
                         f"{', '.join(s['id'] + ' ' + s['name'] for s in absent)}")
        else:
            logger.info(f"Stage {stage}: every section is now in the layout "
                        f"({len(saved[stage])} of {len(sections)} rendered).")
    logger.info("Hard-refresh Capture (the app caches the data store value) to see it.")


if __name__ == '__main__':
    main()
