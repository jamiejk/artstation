#!/usr/bin/env bash
set -u

readonly CONTROL_URL="http://127.0.0.1:8765/control"

if ! systemctl --user start artstation-plotter.service; then
  notify-send "Plotter" "Could not start the plotter service."
  exit 1
fi

for _attempt in $(seq 1 50); do
  if curl --fail --silent --output /dev/null "$CONTROL_URL"; then
    exec xdg-open "$CONTROL_URL"
  fi
  sleep 0.2
done

notify-send "Plotter" "The service started, but the GUI did not become reachable."
exit 1
