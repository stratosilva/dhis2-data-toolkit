#!/usr/bin/env bash
#
# Monthly dummy-data run for the HTN/DM instances, for cron on Linux.
#
# For each instance, in order:
#
#   enrol this month's new patients  ->  top up visits for existing patients
#   ->  analytics  ->  aggregate data exchange  ->  analytics
#
# Analytics runs twice on purpose. The exchange reads its program indicators
# out of the analytics tables, so they have to see the new patients first; the
# dashboard reads the aggregate data elements out of the same tables, so they
# have to be rebuilt again after the exchange writes. Skip the second one and
# the dashboard keeps showing last month.
#
# Install:
#
#   git clone <repo> /opt/dhis2-data-toolkit
#   cd /opt/dhis2-data-toolkit
#   python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
#   cp /path/to/.env .env          # the instance credentials, not in git
#   chmod +x monthly_data.sh
#
# Cron, 03:00 on the first of each month. Give cron an absolute PATH; it runs
# with almost no environment:
#
#   0 3 1 * * /opt/dhis2-data-toolkit/monthly_data.sh >> /var/log/htn-dummy.log 2>&1
#
# Run one instance by hand, or see what it would do:
#
#   ./monthly_data.sh --instances nigeria
#   ./monthly_data.sh --dry-run

set -euo pipefail

REPO="${REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
PYTHON="${PYTHON:-$REPO/.venv/bin/python}"
[ -x "$PYTHON" ] || PYTHON="$(command -v python3 || true)"
if [ -z "$PYTHON" ]; then
    # Say so, loudly. Without the "|| true" above, set -e killed the script
    # here with status 1 and not a word of output - from cron that is a month
    # silently skipped, which is the one failure this run is meant to catch.
    echo "monthly_data.sh: no interpreter found. Create $REPO/.venv, or set" >&2
    echo "PYTHON to one. Looked for $REPO/.venv/bin/python and python3." >&2
    exit 2
fi

LOG_DIR="${LOG_DIR:-$REPO/logs}"
LOCK_FILE="${LOCK_FILE:-/tmp/htn-monthly-data.lock}"

# How much to add per instance each month, as "new_patients:topup_visits".
#
# Nigeria is sized from the live programme it is meant to resemble: that chart
# runs 2,700 to 4,700 new registrations a month and climbing, so 3,500 sits in
# the middle of it. Demo matches, being the same size. Egypt is a tenth of the
# cohort and gets a tenth of the intake.
#
# The top-up figure is the one that moves "patients under care", since that
# counts patients seen recently rather than patients registered. It is set to
# roughly a third of each cohort per month, which keeps a 28-day appointment
# cycle turning over. Raise it to raise that line.
VOLUMES_nigeria="${VOLUMES_nigeria:-3500:4000}"
VOLUMES_demo="${VOLUMES_demo:-3500:4000}"
VOLUMES_egypt="${VOLUMES_egypt:-350:400}"

# Which .env credential prefix each instance uses, for the steps that take a
# target rather than an instance name.
TARGET_nigeria="SANDBOX1"
TARGET_demo="HTN_DM_DEMO"
TARGET_egypt="EGYPT_SANDBOX3"

# Nigeria spreads over this many facilities per pilot state; 20 gives the 40
# facilities the existing cohort already sits in. The other two instances have
# no pilot states and use the generator's own default.
FACILITIES_PER_STATE_nigeria="${FACILITIES_PER_STATE_nigeria:-20}"

INSTANCES="nigeria demo egypt"
DRY_RUN=""
MONTH=""
# Skip generating and topping up and run only analytics, the exchange
# and analytics again. For re-running the aggregate half after it
# failed, without adding a second month of patients.
AGGREGATE_ONLY=0

while [ $# -gt 0 ]; do
    case "$1" in
        --instances) INSTANCES="$2"; shift 2 ;;
        --month)     MONTH="$2"; shift 2 ;;
        --dry-run)   DRY_RUN="--dry-run"; shift ;;
        --aggregate-only) AGGREGATE_ONLY=1; shift ;;
        --help|-h)   sed -n '2,40p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

case "$MONTH" in
    ""|[0-9][0-9][0-9][0-9]-[0-9][0-9]) ;;
    *) echo "--month takes YYYY-MM, got '$MONTH'" >&2; exit 2 ;;
esac

mkdir -p "$LOG_DIR"
RUN_LOG="$LOG_DIR/monthly-${MONTH:-$(date +%Y-%m)}.log"

say() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$RUN_LOG"; }

run_step() {
    # Everything the Python scripts write goes to the run log as well as to
    # cron's own output, so a failed month can be read back afterwards.
    say "  \$ $*"
    if ! "$@" >>"$RUN_LOG" 2>&1; then
        say "  !! FAILED: $*"
        return 1
    fi
}

# One run at a time. A month's run can take hours, and two overlapping ones
# would both enrol this month's patients. flock is there on the Linux box this
# is cronned on; elsewhere the script still runs, unguarded, so it can be
# tried out.
if command -v flock >/dev/null 2>&1; then
    exec 9>"$LOCK_FILE"
    if ! flock -n 9; then
        say "another run holds $LOCK_FILE; exiting"
        exit 0
    fi
else
    say "flock not found; running without the overlap guard"
fi

# The month that has just finished, not the one in progress. A cron firing at
# 03:00 on the 1st would otherwise have a window one day wide, and every
# patient it enrolled would share a birthday on the dashboard. Pass
# --month YYYY-MM to fill a month that was missed.
if [ -n "$MONTH" ]; then
    PERIOD_START="$MONTH-01"
else
    PERIOD_START="$(date -d "$(date +%Y-%m-01) -1 month" +%Y-%m-01)"
fi
PERIOD_END="$(date -d "$PERIOD_START +1 month -1 day" +%Y-%m-%d)"

# Never claim a visit happened in the future, which a re-run of the current
# month would otherwise do.
TODAY="$(date +%Y-%m-%d)"
[ "$PERIOD_END" \> "$TODAY" ] && PERIOD_END="$TODAY"

# A seed tied to the month being filled: different every month, but the same
# on a re-run, so retrying a half-finished month does not reshuffle what it
# already made. A fixed seed would regenerate identical patients every time
# and collide on the unique QR code attribute.
MONTH_SEED="$(date -d "$PERIOD_START" +%Y%m)"

say "================ monthly run for $PERIOD_START to $PERIOD_END"
say "repo    $REPO"
say "python  $PYTHON"
say "log     $RUN_LOG"

cd "$REPO"

say "pulling the latest scripts"
run_step git pull --ff-only || { say "git pull failed; running the checked-out version"; }

failed=""

for instance in $INSTANCES; do
    eval "volumes=\${VOLUMES_$instance:-}"
    eval "target=\${TARGET_$instance:-}"
    eval "per_state=\${FACILITIES_PER_STATE_$instance:-}"

    if [ -z "$volumes" ] || [ -z "$target" ]; then
        say "!! no configuration for instance '$instance'; skipping"
        failed="$failed $instance"
        continue
    fi

    new_patients="${volumes%%:*}"
    topup_visits="${volumes##*:}"

    say "---------------- $instance ($target): $new_patients new, $topup_visits top-up"

    facility_args=""
    [ -n "$per_state" ] && facility_args="--facilities-per-state $per_state"

    ok=1

    if [ "$AGGREGATE_ONLY" -eq 1 ]; then
        say "  aggregate-only: skipping new patients and the top-up"
    fi

    # This month's arrivals. The enrolment window is this month only, so each
    # run adds new patients rather than backfilling history again.
    [ "$AGGREGATE_ONLY" -eq 0 ] && { run_step "$PYTHON" htn_engine.py --instance "$instance" \
        --patients "$new_patients" \
        --enrol-from "$PERIOD_START" --enrol-to "$PERIOD_END" \
        --seed "$MONTH_SEED" $facility_args $DRY_RUN || ok=0; }

    # Existing patients: the open appointment becomes a completed visit and a
    # fresh one is booked. Only patients with no visit in the period qualify,
    # so this cannot give anyone two visits in one month.
    [ $ok -eq 1 ] && [ "$AGGREGATE_ONLY" -eq 0 ] && { run_step "$PYTHON" htn_engine.py --instance "$instance" \
        --visits-only "$topup_visits" \
        --period-from "$PERIOD_START" --period-to "$PERIOD_END" \
        --seed "$MONTH_SEED" $DRY_RUN || ok=0; }

    if [ -n "$DRY_RUN" ]; then
        say "  dry run: skipping analytics and the exchange"
        [ $ok -eq 1 ] || failed="$failed $instance"
        continue
    fi

    [ $ok -eq 1 ] && { run_step "$PYTHON" run_analytics.py --target "$target" || ok=0; }

    # The instance's own exchange job first, so that the month this stops
    # working is recorded. It is a single long request, and the proxy closes
    # the connection at about 60 seconds; as the cohorts grow, that is the
    # limit they will hit.
    #
    # run_exchange.py does not trust the response. A 504 cannot tell a
    # finished job from a dead one, and import counts cannot either: a
    # re-post by a different writer touches storedBy, so DHIS2 calls it
    # "updated" even when the number is unchanged. It reads the data back
    # instead and asks whether each request wrote anything at all, which is
    # the shape the sandbox 1 failure took - the DM requests had written
    # nothing, for any period or facility.
    if [ $ok -eq 1 ]; then
        if run_step "$PYTHON" run_exchange.py --target "$target"; then
            say "  exchange job: OK, and the readback found values for every request"
        else
            say "  !! exchange job failed or left requests empty; running the script"
            run_step "$PYTHON" run_adex_staging.py --target "$target" || ok=0
        fi
    fi

    [ $ok -eq 1 ] && { run_step "$PYTHON" run_analytics.py --target "$target" || ok=0; }

    if [ $ok -eq 1 ]; then
        say "  $instance done"
    else
        say "  $instance INCOMPLETE - see $RUN_LOG"
        failed="$failed $instance"
    fi
done

if [ -n "$failed" ]; then
    say "================ finished with failures:$failed"
    exit 1
fi

say "================ all instances done"
