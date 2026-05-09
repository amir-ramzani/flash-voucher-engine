#!/usr/bin/env bash
# Chaos: kill the worker mid-sale, restart it, and prove no claim was lost.
#
# The correctness run waits for persistence; events the dead worker left pending are
# recovered via XAUTOCLAIM once idle > WORKER_MIN_IDLE_MS (compose sets 5s locally).
set -euo pipefail
cd "$(dirname "$0")/../.."

USERS=${USERS:-20000}
STOCK=${STOCK:-1000}

python -m loadtest.correctness --users "$USERS" --stock "$STOCK" --wait-persist 120 &
LT=$!

sleep "${KILL_AFTER:-2}"
echo ">>> killing worker"
docker compose kill -s SIGKILL worker
sleep "${DOWN_FOR:-10}"
echo ">>> restarting worker"
docker compose start worker

wait "$LT"
echo ">>> drift check"
docker compose run --rm reconciler python -m reconciler.main
