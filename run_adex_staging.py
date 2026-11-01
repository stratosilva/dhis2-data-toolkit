"""
Run the HTN/DM aggregate exchange against a target, from the .env credentials.

replace_adex.py takes the password as a command-line argument, which puts it in
shell history and process listings. This calls the same code with an Api built
from .env instead, and applies the organisation unit level override so one
config can serve instances whose facility level has a different UID.

By default the exchange is read from the target itself rather than from an
exported JSON. The hand-maintained exports had drifted: the sandbox 1 one
carried the HTN requests only, so a run from it left every DM aggregate empty
and said nothing about it. --config still takes a file for the cases where a
run has to be pinned to something other than what the instance currently says.

This posts the analytics results as data values itself, rather than calling the
instance's own exchange endpoint, because that endpoint answers 504 when the
proxy stops waiting at 60 seconds and the job does not survive it.

    python run_adex_staging.py --target SANDBOX1
    python run_adex_staging.py --target EGYPT_STAGING --org-unit-level sVmUkeVEPyE
    python run_adex_staging.py --target SANDBOX1 --config pinned.json
"""

import argparse
import json
import sys
import tempfile
from datetime import date
from pathlib import Path

from logzero import logger

ADEX_DIR = Path(r'C:\Users\User\Documents\HTN & DM ADEx')
sys.path.insert(0, str(ADEX_DIR))

import replace_adex  # noqa: E402

import adex_config  # noqa: E402
from tracker_common import load_api  # noqa: E402


class Tally:
    """Running totals of what the data value posts did."""

    def __init__(self):
        self.posts = self.imported = self.updated = self.ignored = 0
        self.conflicts = 0

    @property
    def changed(self):
        return self.imported + self.updated

    def add(self, response):
        self.posts += 1
        try:
            body = response.json()
        except (ValueError, AttributeError):
            return
        body = body.get('response', body)
        counts = body.get('importCount') or body
        self.imported += counts.get('imported') or 0
        self.updated += counts.get('updated') or 0
        self.ignored += counts.get('ignored') or 0
        self.conflicts += len(body.get('conflicts') or [])


class Counted:
    """
    The Api, with dataValueSets posts counted on the way through.

    replace_adex.py reports each post to its own log and returns nothing, so
    the totals are taken here rather than by reading that log back. Everything
    other than the counting is the real Api's.
    """

    def __init__(self, api, tally):
        self._api = api
        self._tally = tally

    def post(self, endpoint, *args, **kwargs):
        response = self._api.post(endpoint, *args, **kwargs)
        if str(endpoint).strip('/') == 'dataValueSets':
            self._tally.add(response)
        return response

    def __getattr__(self, name):
        return getattr(self._api, name)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--target', default='EGYPT_STAGING',
                        help='Credential prefix in .env naming the instance to '
                             'run against (default: %(default)s)')
    parser.add_argument('--org-unit-level', default=None,
                        help='Override the organisation unit level in the '
                             'config. Not needed when the config is read from '
                             'the instance, which already carries its own.')
    parser.add_argument('--config', type=Path, default=None,
                        help='Run from this exported JSON instead of from the '
                             "exchanges defined on the target. Pins a run to "
                             'something other than what the instance says.')
    parser.add_argument('--save-config', type=Path, default=None,
                        help='Write the config that was built to this file, '
                             'for inspection. Nothing reads it back.')
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, OSError):
        pass

    api = load_api(args.target, f'target ({args.target})')
    logger.info(f"Target: {api.base_url} as {args.target}")

    replace_adex.configure_logging(Path('replace_adex.log'))

    if args.config:
        logger.info(f"Config: {args.config} (pinned)")
        config_path = args.config
        temporary = None
    else:
        logger.info(f"Config: read from the exchanges on {args.target}")
        config, dropped = adex_config.build(api)
        adex_config.report(config, dropped)

        # replace_adex.process reads a path, so what was built is written out
        # before it runs.
        temporary = tempfile.NamedTemporaryFile(
            mode='w', suffix='.json', prefix=f'adex-{args.target}-',
            delete=False, encoding='utf-8')
        json.dump(config, temporary, indent=1)
        temporary.close()
        config_path = Path(temporary.name)

        if args.save_config:
            args.save_config.write_text(json.dumps(config, indent=1),
                                        encoding='utf-8')
            logger.info(f"  config written to {args.save_config}")

    # How much this run wrote, counted off the posts as they go because
    # replace_adex does not return it. Read "imported" and leave "updated"
    # alone: a value re-posted by a different writer counts as updated even
    # when the number is unchanged, because storedBy moves, so the figure says
    # nothing about whether the instance's own exchange job had done its work.
    # run_exchange.py settles that by reading the data back.
    tally = Tally()
    try:
        replace_adex.process(config_path, Counted(api, tally), date.today(),
                             args.org_unit_level)
    finally:
        if temporary is not None:
            Path(temporary.name).unlink(missing_ok=True)

    logger.info(f"ADEx run complete: {tally.posts} post(s), "
                f"imported {tally.imported}, updated {tally.updated}, "
                f"ignored {tally.ignored}, conflicts {tally.conflicts}")
    if tally.imported:
        logger.warning(f"  {tally.imported} value(s) did not exist before this "
                       f"run and have now been created")


if __name__ == '__main__':
    main()
