#!/bin/bash
# Stops the instance when the GPU has been idle and the ComfyUI queue empty for
# RIG_IDLE_STOP_MINUTES, or when the box has been busy (queued work or GPU
# utilization at 5% or more) for RIG_MAX_JOB_MINUTES without a break (assumed
# hung: neither a busy queue nor a busy GPU with an unreachable queue may defeat
# cost safety).
# Runs from a systemd timer every 5 minutes. The instance is launched with
# InstanceInitiatedShutdownBehavior=stop, so `shutdown -h` stops it (EBS and
# models persist) rather than terminating it.
#
# The file locations below default to the on-box paths; the RIG_WATCHDOG_*
# overrides exist so tests can run the script against a temporary directory.
set -u
source "${RIG_WATCHDOG_ENV_FILE:-/etc/rig.env}"
TICK_MINUTES=5
STATE_DIR=${RIG_WATCHDOG_STATE_DIR:-/var/tmp}
STATE_FILE=$STATE_DIR/rig_idle_ticks
BUSY_FILE=$STATE_DIR/rig_busy_ticks
BOOT_MARKER=${RIG_WATCHDOG_BOOT_MARKER:-/var/lib/rig-bootstrap-done}
UPTIME_FILE=${RIG_WATCHDOG_UPTIME_FILE:-/proc/uptime}
MAX_BOOTSTRAP_SECONDS=$((3 * 3600))
BOOT_GRACE_SECONDS=600
MAX_JOB_MINUTES=${RIG_MAX_JOB_MINUTES:-360}

uptime_s=$(cut -d. -f1 "$UPTIME_FILE")

# While bootstrap is still running (no marker yet), hold off, but never
# indefinitely: a bootstrap that hasn't finished after 3 hours is stuck, and
# the box must not bill unattended.
if [ ! -f "$BOOT_MARKER" ]; then
  if [ "$uptime_s" -lt "$MAX_BOOTSTRAP_SECONDS" ]; then
    exit 0
  fi
  echo "bootstrap never completed after ${MAX_BOOTSTRAP_SECONDS}s; stopping instance"
  shutdown -h now
  exit 0
fi

# Fresh boot grace period; also clears stale tick counts persisted across stops.
if [ "$uptime_s" -lt "$BOOT_GRACE_SECONDS" ]; then
  echo 0 > "$STATE_FILE"
  echo 0 > "$BUSY_FILE"
  exit 0
fi

# A hung or failing nvidia-smi must not keep the box alive: the probe is
# bounded, and a non-numeric reading ("[N/A]", an error, nothing) is treated
# as idle, so an empty queue alone then decides and the idle limit still holds.
util=$(timeout "${RIG_WATCHDOG_GPU_TIMEOUT_S:-30}" nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -d ' ')
if ! [[ "$util" =~ ^[0-9]+$ ]]; then
  echo "GPU utilization unreadable (${util:-no output}); deciding on the ComfyUI queue alone"
  util=0
fi
queue=$(curl -s --max-time 5 "http://127.0.0.1:${RIG_COMFY_PORT}/queue" | python3 -c '
import json, sys
d = json.load(sys.stdin)
print(len(d.get("queue_running", [])) + len(d.get("queue_pending", [])))
' 2>/dev/null || echo 0)
queue=${queue:-0}
if [ "$queue" -gt 0 ] || [ "$util" -ge 5 ]; then
  busy=1
else
  busy=0
fi

# Hung-job ceiling: counts every busy tick, so a queue that never drains, or a
# busy GPU while the queue endpoint is unreachable (read as 0), still reaches it.
if [ "$busy" -eq 1 ]; then
  busy_ticks=$(( $(cat "$BUSY_FILE" 2>/dev/null || echo 0) + 1 ))
else
  busy_ticks=0
fi
echo "$busy_ticks" > "$BUSY_FILE"
if [ $(( busy_ticks * TICK_MINUTES )) -ge "$MAX_JOB_MINUTES" ]; then
  echo "busy for $((busy_ticks * TICK_MINUTES)) minutes (queue ${queue}, GPU ${util}%, max ${MAX_JOB_MINUTES}); assuming a hung job, stopping instance"
  shutdown -h now
  exit 0
fi

if [ "$busy" -eq 0 ]; then
  ticks=$(( $(cat "$STATE_FILE" 2>/dev/null || echo 0) + 1 ))
else
  ticks=0
fi
echo "$ticks" > "$STATE_FILE"

threshold=$(( (RIG_IDLE_STOP_MINUTES + TICK_MINUTES - 1) / TICK_MINUTES ))
if [ "$ticks" -ge "$threshold" ]; then
  echo "GPU idle for $((ticks * TICK_MINUTES)) minutes; stopping instance"
  shutdown -h now
fi
