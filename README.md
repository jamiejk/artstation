# ArtStation Plotter Server

A local server and browser-based control panel for operating a UUNA TEK ArtStation or compatible pen plotter.

## Features

- Send SVG plot jobs to the ArtStation from any authenticated device on your local network.
- Manage the queue and job history from the web interface, including pausing, resuming, rerunning, and cancelling jobs.
- View the plot head on a bed map, move it to precise coordinates, and set or return to a custom home position from the browser.
- Tune manual travel speed, pen-up and pen-down plotting speeds, servo positions, pen timing, and pen-raise rate.
- Submit ordered, multi-layer jobs. The server pauses before the first layer and between layers so the operator can fit or change pens.
- Persist queued jobs, job history, plot settings, pen settings, and position estimates across server restarts.
- Validate SVG dimensions and reject movements outside the configured plotter bed.
- Monitor and control the plotter through either the web interface or the included terminal tools.

This is an early release for operators who are comfortable configuring and supervising physical plotter hardware. Keep a hand near the power switch and verify the configured bed dimensions before moving the carriage.

## Web interface

![ArtStation web interface showing motion, pen, speed, bed-position, and job controls](docs/images/web-interface.png)

## Requirements

- Linux with Python 3.12 or newer
- An AxiDraw-compatible controller accessible through a serial device
- Membership in the `dialout` group, or an equivalent way to access the serial device

## Installation

```bash
git clone <repository-url> ~/plotter
cd ~/plotter
python3 -m venv venv
venv/bin/pip install -r requirements.txt
cp .env.example .env
chmod 600 .env
```

Edit `.env`, set a strong `PLOTTER_TOKEN`, and confirm the serial port and bed dimensions. Then start the service:

```bash
scripts/start_plotter_server.sh
```

Open `http://127.0.0.1:8765/control` on the plotter computer. The service currently binds to all interfaces so authenticated upload clients can reach it; use only on a trusted network unless you put it behind HTTPS and an appropriately configured proxy.

## Operation

The browser panel provides positioning and job controls. The local CLI provides status and emergency operational commands:

```bash
scripts/plotctl state
scripts/plotctl jobs
scripts/plotctl watch
scripts/plotconsole
```

Upload one or more SVG layers in plotting order:

```bash
curl -H "X-Plotter-Token: $PLOTTER_TOKEN" \
  -F "files=@layer-1.svg" \
  -F "files=@layer-2.svg" \
  -F "layer_names=Blue,Black" \
  http://127.0.0.1:8765/plot/layers
```

The worker waits for local operator confirmation before the first layer and between layers. Uploaded SVG dimensions must fit within `MAX_PLOTTER_WIDTH_MM` and `MAX_PLOTTER_HEIGHT_MM`.

Calibration establishes the bed coordinate system at the top-left point and initially sets that point as Home. Move the head to any calibrated bed position and use **Set Home** to replace it. **Home** and automatic post-layer returns then go to that saved point without changing the bed coordinates.

## Testing

The automated suite does not access hardware:

```bash
PLOTTER_DISABLE_WORKER=1 venv/bin/python -m unittest discover -v
```

[`manual_tests/hardware_smoke.py`](manual_tests/hardware_smoke.py) is deliberately outside automated test discovery. It moves real hardware and must only be run explicitly.

## Service installation

The included start script assumes the repository is at `~/plotter`. A user-level systemd service can use:

```ini
[Service]
WorkingDirectory=%h/plotter/server
EnvironmentFile=%h/plotter/.env
ExecStart=%h/plotter/scripts/start_plotter_server.sh
Restart=on-failure
```

Run a single Uvicorn worker. Job state and hardware access are process-local and are not designed for multiple server workers.

## Contributing

Issues and pull requests are welcome. Changes to movement, homing, cancellation, or serial commands should include non-hardware tests and a clear manual verification procedure.

## License

Copyright (C) 2026 ArtStation Plotter Server contributors.

This project is free software licensed under the GNU General Public License, version 3. See [`LICENSE`](LICENSE).
