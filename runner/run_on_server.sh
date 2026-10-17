#!/usr/bin/env bash
# Run the Claude Code batch on a machine with a logged-in `claude`.
# From the repository root:
#
#   ALVA_BIN=/path/to/real/alva bash runner/run_on_server.sh check   # data, unit tests, scripted batch, one Claude run
#   ALVA_BIN=/path/to/real/alva bash runner/run_on_server.sh batch   # every task x 5 runs, 3 at a time
#   bash runner/run_on_server.sh pack                                # report.md, digest.md and a tarball
#
# ALVA_BIN must be the CLI itself, not a wrapper that pins ALVA_ENDPOINT:
# every episode runs its own server on a free port. Optional: OUT, K, JOBS,
# MODEL (default: the account's model), TASKS (e.g. v3:10,v3+gate:10,v3k:5).
set -euo pipefail
cd "$(dirname "$0")/.."
OUT=${OUT:-runner/runs/claude-1}
K=${K:-5}
JOBS=${JOBS:-3}

need_alva() {
  : "${ALVA_BIN:?set ALVA_BIN to the real alva CLI}"
  "$ALVA_BIN" --version || true
}

case "${1:-}" in
  check)
    need_alva
    (cd brokersim && python3 -m brokersim fetch-data && python3 -m brokersim selftest 2>&1 | tail -3)
    python3 gate/test_gate.py 2>&1 | tail -3
    python3 runner/faultlab_run.py batch --agent scripted --k 1 --cli-timeout 30 \
      --alva-bin "$ALVA_BIN" --out runner/runs/scripted-check
    python3 runner/faultlab_run.py episode --task baseline --agent claude ${MODEL:+--model "$MODEL"} \
      --alva-bin "$ALVA_BIN" --out "runner/runs/claude-smoke-$(date -u +%Y%m%dT%H%M%S)"
    ;;
  batch)
    need_alva
    python3 runner/faultlab_run.py batch --agent claude --k "$K" --jobs "$JOBS" ${MODEL:+--model "$MODEL"} \
      ${TASKS:+--tasks "$TASKS"} --alva-bin "$ALVA_BIN" --out "$OUT"
    ;;
  pack)
    python3 runner/faultlab_run.py report "$OUT" >/dev/null
    tar czf "$OUT.tgz" --exclude='*/workspace/.claude' --exclude='*/xdg' "$OUT"
    ls -la "$OUT.tgz" "$OUT/report.md" "$OUT/digest.md"
    ;;
  *)
    echo "usage: $0 check|batch|pack" >&2
    exit 2
    ;;
esac
