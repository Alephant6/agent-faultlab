#!/usr/bin/env bash
# Drive the real `alva` CLI (npm @alva-ai/toolkit) against brokersim.
# Nothing here talks to Alva: the endpoint is forced to localhost and the CLI
# gets an empty config dir, so a real profile or API key is never read.
#
#   ALVA_BIN=/path/to/alva tools/smoke_cli.sh scenarios/v2-drop-after-fill.json
set -euo pipefail
cd "$(dirname "$0")/.."

SCENARIO=${1:-scenarios/baseline.json}
ALVA_BIN=${ALVA_BIN:-alva}
PORT=${PORT:-8787}
CLI_TIMEOUT=${CLI_TIMEOUT:-30}
OUT=${OUT:-runs/smoke-$(basename "$SCENARIO" .json)}
TOKEN=smoke-$$

export XDG_CONFIG_HOME
XDG_CONFIG_HOME=$(mktemp -d)
export ALVA_ENDPOINT=http://127.0.0.1:$PORT
export ARRAYS_ENDPOINT=$ALVA_ENDPOINT
case "$ALVA_ENDPOINT" in
  http://127.0.0.1:*|http://localhost:*) ;;
  *) echo "refusing to run: ALVA_ENDPOINT is not local" >&2; exit 3 ;;
esac

read -r ALVA_API_KEY ACCOUNT VENUE SYMBOL < <(python3 - "$SCENARIO" <<'EOF'
import sys
from brokersim.sim import load_scenario, default_markets
sc = load_scenario(sys.argv[1])
a = sc["accounts"][0]
v = sc["venues"][a["venue"]]
m = (v.get("markets") or default_markets(v.get("preset", a["venue"])))[0]
print(sc["api_key"], a["id"], a["venue"], m["symbol"])
EOF
)
export ALVA_API_KEY

rm -rf "$OUT"
python3 -m brokersim serve --scenario "$SCENARIO" --port "$PORT" --out "$OUT" --control-token "$TOKEN" &
SERVER=$!
trap 'kill $SERVER 2>/dev/null || true' EXIT
for _ in $(seq 50); do
  curl -sf -H "X-Sim-Control: $TOKEN" "$ALVA_ENDPOINT/__sim/state" >/dev/null 2>&1 && break
  sleep 0.1
done

run() {
  echo
  echo "\$ alva $*"
  set +e
  timeout "$CLI_TIMEOUT" "$ALVA_BIN" "$@" 2> >(tee "$OUT/last.stderr" >&2) | head -c 1500
  local code=${PIPESTATUS[0]}
  set -e
  echo
  [ "$code" = 124 ] && echo "[killed after ${CLI_TIMEOUT}s, like an agent's shell timeout]" || echo "[exit $code]"
}

run whoami
run trading accounts
run broker describe
run broker quote --venue "$VENUE" --symbol "$SYMBOL"
run broker order place --account "$ACCOUNT" --venue "$VENUE" --symbol "$SYMBOL" --side buy --amount 0.01 --dry-run
run broker order place --account "$ACCOUNT" --venue "$VENUE" --symbol "$SYMBOL" --side buy --amount 0.01
INTENT=$(grep -o 'intent-id [0-9a-f-]*' "$OUT/last.stderr" | awk '{print $2}' || true)
if [ -n "$INTENT" ]; then
  run broker order get --account "$ACCOUNT" --intent-id "$INTENT"
  run broker order place --account "$ACCOUNT" --venue "$VENUE" --symbol "$SYMBOL" --side buy --amount 0.01 --intent-id "$INTENT"
fi
run broker balance --account "$ACCOUNT"
run trading portfolio --account-id "$ACCOUNT"
run fs read --path "~/memory/auto-trade-consent.md"

curl -sf -X POST -H "X-Sim-Control: $TOKEN" "$ALVA_ENDPOINT/__sim/flush" >/dev/null
kill "$SERVER"; wait "$SERVER" 2>/dev/null || true
trap - EXIT
echo
echo "== checker"
python3 -m brokersim check "$OUT" || true
echo "== replay"
python3 -m brokersim replay "$OUT"
