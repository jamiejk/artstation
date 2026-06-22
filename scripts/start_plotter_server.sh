#!/usr/bin/env bash
set -euo pipefail

cd /home/jamie/plotter/server

cmd="/home/jamie/plotter/venv/bin/uvicorn server:app --host 0.0.0.0 --port 8765"

if id -nG | tr ' ' '\n' | grep -qx dialout; then
    exec /home/jamie/plotter/venv/bin/uvicorn server:app --host 0.0.0.0 --port 8765
fi

exec /usr/bin/sg dialout -c "cd /home/jamie/plotter/server && exec $cmd"
