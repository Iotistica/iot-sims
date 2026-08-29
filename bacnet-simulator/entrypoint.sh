#!/bin/bash
set -u

# model runtime: conda base env (pyfmi/fastapi/uvicorn), its own subtree at /app/model_runtime.
( cd /app/model_runtime && exec uvicorn app:app --host 0.0.0.0 --port 8000 ) &
MODELS_PID=$!

# bacnet-simulator: dedicated conda env, its own subtree at /app/bacnet-sim. `exec` inside the
# subshell means the subshell PID *becomes* the python PID (same process, not a wrapper), so
# signals reach the app directly.
( cd /app/bacnet-sim && exec /opt/conda/envs/bacnet-sim/bin/python -u -m src.main ) &
SIM_PID=$!

term_handler() {
    trap - TERM INT
    kill -TERM "$MODELS_PID" "$SIM_PID" 2>/dev/null
    wait "$MODELS_PID" 2>/dev/null
    wait "$SIM_PID" 2>/dev/null
    exit 0
}
trap term_handler TERM INT

# Blocks until either child actually exits (reaped, real exit status) -- not a poll.
wait -n
exit_code=$?

if kill -0 "$MODELS_PID" 2>/dev/null; then
    echo "bacnet-simulator exited (code $exit_code) -- stopping model runtime" >&2
    kill -TERM "$MODELS_PID" 2>/dev/null
    wait "$MODELS_PID" 2>/dev/null
else
    echo "model runtime exited (code $exit_code) -- stopping bacnet-simulator" >&2
    kill -TERM "$SIM_PID" 2>/dev/null
    wait "$SIM_PID" 2>/dev/null
fi

exit "$exit_code"
