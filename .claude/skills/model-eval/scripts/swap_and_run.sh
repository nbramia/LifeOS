#!/usr/bin/env bash
# Swap :8080 from the incumbent (lifeos-llm) to a candidate GGUF, run the
# execution question set, then ALWAYS restore the incumbent.
#
# The restore runs from an EXIT trap, so a benchmark crash, a failed model load,
# or a kill still puts the live service back.
#
# Usage: swap_and_run.sh <model.gguf> <label> [reasoning-mode] [extra llama-server args...]
#   reasoning-mode: off | low   (passed through to qrun.py; default: low)
#
# Env:
#   LLAMA_BIN   llama-server binary (default: ~/llama.cpp/build/bin/llama-server)
#   OUT_DIR     where results land  (default: a mktemp dir, printed on exit)
#   CTX_LIST    context sizes to try, largest first (default: "131072 65536 32768")
set -uo pipefail

MODEL="${1:?usage: swap_and_run.sh <model.gguf> <label> [off|low] [extra args...]}"
LABEL="${2:?missing label}"
MODE="${3:-low}"
shift 3 2>/dev/null || shift 2
EXTRA=("$@")

LLAMA_BIN="${LLAMA_BIN:-$HOME/llama.cpp/build/bin/llama-server}"
OUT_DIR="${OUT_DIR:-$(mktemp -d -t model-eval-XXXX)}"
CTX_LIST="${CTX_LIST:-131072 65536 32768}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID=""

log(){ echo "[$(date +%T)] $*"; }

restore(){
  log "=== RESTORE ==="
  if [ -n "$PID" ]; then
    kill "$PID" 2>/dev/null
    for _ in $(seq 1 20); do kill -0 "$PID" 2>/dev/null || break; sleep 2; done
    # A plain kill has been survived before, holding VRAM. Escalate.
    kill -0 "$PID" 2>/dev/null && { log "candidate survived SIGTERM; SIGKILL"; kill -9 "$PID" 2>/dev/null; }
  fi
  for _ in $(seq 1 20); do curl -sf --max-time 2 http://localhost:8080/health >/dev/null 2>&1 || break; sleep 2; done
  sleep 3
  # NOTE: `restart` is not sudo-allowlisted for lifeos-llm; start only.
  sudo -n systemctl start lifeos-llm 2>&1 | tail -1
  for _ in $(seq 1 90); do
    curl -sf --max-time 3 http://localhost:8080/health >/dev/null 2>&1 && { log "RESTORE OK — incumbent back"; return 0; }
    sleep 5
  done
  log "!!! RESTORE FAILED — check 'systemctl status lifeos-llm' by hand"
  return 1
}
trap restore EXIT

[ -f "$MODEL" ] || { log "no such model: $MODEL"; exit 1; }
[ -x "$LLAMA_BIN" ] || { log "no llama-server at $LLAMA_BIN (set LLAMA_BIN)"; exit 1; }
log "results -> $OUT_DIR"

log "stopping incumbent (lifeos-llm)"
sudo -n systemctl stop lifeos-llm 2>&1 | tail -1
for _ in $(seq 1 30); do curl -sf --max-time 2 http://localhost:8080/health >/dev/null 2>&1 || break; sleep 2; done
sleep 5

# Largest context that will actually load. Too-large fails at load — loud and safe —
# whereas too-small fails mid-run with HTTP 400 and silently voids the results.
for CTX in $CTX_LIST; do
  log "trying -c $CTX"
  # ROCBLAS_USE_HIPBLASLT=1 matches what systemd sets for the incumbent; without it
  # the comparison is not fair.
  ROCBLAS_USE_HIPBLASLT=1 setsid nohup "$LLAMA_BIN" -m "$MODEL" \
    -ngl 99 -fa on --jinja --no-mmap -c "$CTX" \
    --host 0.0.0.0 --port 8080 "${EXTRA[@]}" \
    > "$OUT_DIR/server-c$CTX.log" 2>&1 < /dev/null &
  PID=$!
  UP=0
  for _ in $(seq 1 150); do
    curl -sf --max-time 3 http://localhost:8080/health >/dev/null 2>&1 && { UP=1; break; }
    kill -0 "$PID" 2>/dev/null || break
    sleep 5
  done
  [ "$UP" = 1 ] && { log "up at -c $CTX (pid $PID)"; echo "$CTX" > "$OUT_DIR/ctx_used.txt"; break; }
  log "  -c $CTX failed to start (likely VRAM); trying smaller"
  grep -iE "error|alloc|out of memory" "$OUT_DIR/server-c$CTX.log" | tail -3
  kill -9 "$PID" 2>/dev/null; PID=""; sleep 8
done
[ -n "$PID" ] || { log "no context size worked"; exit 1; }

# Confirm MTP actually engaged if it was requested — the flag is easy to forget,
# and its absence looks like "the model is slow" rather than "a flag is missing".
if printf '%s\n' "${EXTRA[@]}" | grep -q draft-mtp; then
  grep -qi "MTP draft context" "$OUT_DIR/server-c$(cat "$OUT_DIR/ctx_used.txt").log" \
    && log "MTP: engaged" || log "MTP: WARNING — requested but not confirmed in log"
fi

log "running question set (label=$LABEL mode=$MODE)"
OUT_DIR="$OUT_DIR" PYTHONUNBUFFERED=1 \
  "${LIFEOS_PY:-$HOME/.venvs/lifeos/bin/python}" "$HERE/qrun.py" "$LABEL" "$MODE" \
  > "$OUT_DIR/q_$LABEL.out" 2>&1
log "done (exit $?) — results in $OUT_DIR"
