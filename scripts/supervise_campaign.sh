#!/bin/zsh
#
# Keep a campaign's run stage advancing until its budget is met.
#
#     scripts/supervise_campaign.sh nq-<you>-01 [N]
#
# Why this exists: a campaign is three to four hours of wall clock on a laptop,
# and on a machine that is short of memory the OS or a watchdog will kill it
# partway. That is survivable -- `--stage run` is resumable and restores
# `src/strategies/current.py` from HEAD each time it resumes -- but only if
# something restarts it. Unattended, one kill at 02:00 costs the whole night.
#
# A hard kill costs the trial that was in flight and nothing else. The trial is
# not written to the ledger, so it is not a look at the data and does not count
# toward N.
#
# This does NOT retry forever against a wall: eight consecutive attempts that
# advance zero trials stops it. A campaign that cannot make progress is a
# campaign to look at in the morning, not one to run 200 times.

set -u
name=${1:?usage: supervise_campaign.sh <campaign-name> [N]}
budget=${2:-50}

root=${0:A:h:h}
cd "$root" || exit 1
[[ -f secrets/r2.env ]] && { set -a; . ./secrets/r2.env; set +a; }

log=runs/campaigns/$name/supervisor.log
ledger=runs/campaigns/$name/ledger.jsonl
mkdir -p "runs/campaigns/$name"

attempt=0
stalled=0

while :; do
  done_n=0
  [[ -f $ledger ]] && done_n=$(grep -c . "$ledger")
  if (( done_n >= budget )); then
    echo "=== $done_n/$budget complete, stopping $(date) ===" >> "$log"
    break
  fi
  attempt=$((attempt + 1))
  if (( attempt > 200 )); then
    echo "=== giving up after $attempt attempts at $done_n/$budget $(date) ===" >> "$log"
    break
  fi

  echo "=== attempt $attempt, $done_n/$budget done, $(date) ===" >> "$log"
  caffeinate -i .venv/bin/python scripts/nq_campaign.py --stage run \
      --name "$name" >> "$log" 2>&1
  rc=$?
  after=0
  [[ -f $ledger ]] && after=$(grep -c . "$ledger")
  echo "=== attempt $attempt exited rc=$rc, $done_n -> $after $(date) ===" >> "$log"

  if (( after <= done_n )); then
    stalled=$((stalled + 1))
    if (( stalled >= 8 )); then
      echo "=== 8 attempts with no progress at $after/$budget, stopping $(date) ===" >> "$log"
      break
    fi
    sleep $((30 * stalled))
  else
    stalled=0
    sleep 5
  fi
done
echo "=== finished $(date) ===" >> "$log"
