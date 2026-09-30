#!/usr/bin/env bash
# Run the evidence loop unattended, and keep it running.
#
# WHY A SCRIPT AND NOT JUST A COMMAND
# -----------------------------------
# The question this answers — "does the edge ever clear its costs, and for how
# long at a time?" — needs HOURS of rows, not minutes. Hours means the loop has
# to survive things: an RPC that drops, a rate limit, a machine that reboots, a
# shell that closes. Running the survey once inside a terminal that will
# eventually close is how a day of evidence turns into four rows.
#
# So this wrapper:
#   * installs the dependencies if they are missing (a fresh machine, or a
#     sandbox that reset between sessions — both have cost a run already),
#   * restarts the survey when it exits for any reason, forever,
#   * appends to the same log, so restarts accumulate instead of replacing,
#   * costs nothing but a process, since the survey never sends a transaction.
#
# USAGE
#   scripts/run_evidence.sh                 # both loops, 10 hours, in the foreground
#   nohup scripts/run_evidence.sh > /tmp/evidence.log 2>&1 &     # or detached
#   tmux new -s evidence 'scripts/run_evidence.sh'
#
# Stop it with Ctrl-C (or `pkill -f run_evidence.sh`). The logs are already on
# disk by then; nothing is lost.

set -u

cd "$(dirname "$0")/.." || exit 1

NETWORK="${NETWORK:-bsc}"
HOURS="${HOURS:-10}"
SURVEY_EVERY="${SURVEY_EVERY:-20}"      # seconds between deep scans
BURST_EVERY="${BURST_EVERY:-3}"         # seconds between fast prefilter sweeps
PAIRS="${PAIRS:-12}"                    # families the burst loop watches
BURST_SIZE="${BURST_SIZE:-1000}"         # probe size for the fast loop, see below
DEADLINE=$(( $(date +%s) + HOURS * 3600 ))

echo "evidence loop starting $(date -u +%FT%TZ), ${HOURS}h budget"
echo "  survey  every ${SURVEY_EVERY}s  -> logs/arb_survey_run2.jsonl"
echo "  burst   every ${BURST_EVERY}s   -> logs/arb_burst.jsonl"
echo "  nothing here signs or sends; the survey is a dry run of the planner"

if ! python -c "import web3" >/dev/null 2>&1; then
  echo "installing dependencies (web3 was missing)"
  python -m pip install -q -r requirements.txt || echo "WARNING: install failed; survey will retry"
fi

pass=0
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  pass=$((pass + 1))

  if [ $((pass % 2)) -eq 1 ]; then
    # Alternating, one pass each. The survey answers "is the median positive" at
    # a 1-unit probe; the burst loop answers "if a win appears, does it survive
    # long enough to be caught" at a 1,000-unit probe, which is the size where
    # gas stops dominating: 600k gas at 0.05 gwei is 231 bps of a 1 USDT trade
    # and 0.231 bps of a 1,000 USDT one. Both sizes are recorded with their own
    # numbers, so the comparison is honest rather than apples-to-oranges.
    python main.py arb survey --network "$NETWORK" --base WBNB --quote USDT \
        --size 1 --iterations 100000 --interval "$SURVEY_EVERY" \
        --duration 900 --log logs/arb_survey_run2.jsonl
  else
    python main.py arb burst --network "$NETWORK" --pairs "$PAIRS" \
        --size "$BURST_SIZE" --interval "$BURST_EVERY" --duration 900 \
        --heartbeat 60 --log logs/arb_burst.jsonl
  fi

  code=$?
  echo "=== pass $pass exited ($code) at $(date -u +%FT%TZ); restarting ==="
  sleep 3
done

echo "evidence loop finished at $(date -u +%FT%TZ)"
