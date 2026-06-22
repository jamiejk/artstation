#!/usr/bin/env python3
import curses
import json
import time
import urllib.error
import urllib.request
from pathlib import Path


BASE_DIR = Path.home() / "plotter"
ENV_PATH = BASE_DIR / ".env"
SERVER = "http://127.0.0.1:8765"
REFRESH_INTERVAL = 0.75
JOG_SPEED_MM_S = 80.0


def load_env_token() -> str:
    if not ENV_PATH.exists():
        return ""
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        if line.startswith("PLOTTER_TOKEN="):
            return line.split("=", 1)[1].strip()
    return ""


TOKEN = load_env_token()


class ApiError(RuntimeError):
    pass


def request_json(method: str, path: str, payload: dict | None = None, *, token: bool = True):
    data = None
    headers = {}
    if token and TOKEN:
        headers["X-Plotter-Token"] = TOKEN
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(SERVER + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise ApiError(f"HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise ApiError(f"Server unavailable: {exc}") from exc

    return json.loads(body) if body else {}


def clamp_line(text: str, width: int) -> str:
    if width <= 1:
        return ""
    if len(text) <= width:
        return text
    return text[: max(0, width - 1)]


def draw_box(stdscr, y: int, x: int, height: int, width: int, title: str):
    if height < 3 or width < 4:
        return
    stdscr.addstr(y, x, "+" + "-" * (width - 2) + "+")
    for row in range(y + 1, y + height - 1):
        stdscr.addstr(row, x, "|")
        stdscr.addstr(row, x + width - 1, "|")
    stdscr.addstr(y + height - 1, x, "+" + "-" * (width - 2) + "+")
    if title:
        stdscr.addstr(y, x + 2, f" {clamp_line(title, width - 6)} ")


def add_lines(stdscr, y: int, x: int, width: int, lines: list[str]):
    for index, line in enumerate(lines):
        try:
            stdscr.addstr(y + index, x, clamp_line(line, width))
        except curses.error:
            pass


def format_state(state: dict) -> list[str]:
    hardware = state.get("hardware", {})
    server = state.get("server", {})
    prompt = server.get("operator_prompt") or {}

    lines = [
        f"server queue: {server.get('queue_size', '?')}",
        f"hardware busy: {hardware.get('busy')}",
    ]

    if hardware.get("connected") is False:
        lines.extend([
            f"connected: no",
            f"port: {hardware.get('port')}",
            f"error: {hardware.get('error')}",
        ])
    elif hardware.get("busy"):
        lines.append(hardware.get("message", "serial port in use"))
    else:
        pos = hardware.get("position_estimate") or {}
        steps = hardware.get("steps") or {}
        lines.extend([
            f"port: {hardware.get('port')}",
            f"firmware: {hardware.get('firmware')}",
            f"pen up: {hardware.get('pen_up')}    pause button: {hardware.get('button_pressed')}",
            f"steps: axis1={steps.get('axis_1')} axis2={steps.get('axis_2')}",
            f"relative xy: x={pos.get('x_mm', 0):.2f}mm  y={pos.get('y_mm', 0):.2f}mm",
        ])

    if prompt.get("active"):
        lines.extend(["", f"operator waiting: {prompt.get('job_id')}", prompt.get("message") or ""])
    return lines


def format_jobs(state: dict) -> list[str]:
    jobs = state.get("jobs", [])
    if not jobs:
        return ["No jobs."]
    lines = []
    for job in jobs:
        name = job.get("current_layer_name") or "-"
        lines.append(f"{job.get('id')}  {job.get('status')}  L{job.get('current_layer') or '-'}  {name}")
    return lines


def draw_position(stdscr, y: int, x: int, height: int, width: int, state: dict):
    draw_box(stdscr, y, x, height, width, " Relative Arm Position ")
    inner_w = width - 4
    inner_h = height - 4
    if inner_w < 10 or inner_h < 5:
        return

    hardware = state.get("hardware", {})
    pos = hardware.get("position_estimate") or {}
    x_mm = float(pos.get("x_mm", 0) or 0)
    y_mm = float(pos.get("y_mm", 0) or 0)

    # Show a coarse 300 mm square around the current home. Values outside clamp to edge.
    span = 300.0
    px = int(round((max(-span, min(span, x_mm)) + span) / (2 * span) * (inner_w - 1)))
    py = int(round((span - max(-span, min(span, y_mm))) / (2 * span) * (inner_h - 1)))

    for row in range(inner_h):
        try:
            stdscr.addstr(y + 2 + row, x + 2, "." * inner_w)
        except curses.error:
            pass

    try:
        stdscr.addstr(y + 2 + inner_h // 2, x + 2 + inner_w // 2, "+")
        stdscr.addstr(y + 2 + py, x + 2 + px, "@")
    except curses.error:
        pass

    label = f"@ x={x_mm:.1f} y={y_mm:.1f} mm; + home"
    try:
        stdscr.addstr(y + height - 2, x + 2, clamp_line(label, inner_w))
    except curses.error:
        pass


def send_move(dx: float, dy: float):
    payload = {}
    if dx:
        payload["x_mm"] = dx
    if dy:
        payload["y_mm"] = dy
    if payload:
        payload["speed_mm_s"] = JOG_SPEED_MM_S
        request_json("POST", "/plotter/jog", payload)


def handle_key(key: int, step: float) -> tuple[bool, float, str]:
    message = ""
    keep_running = True
    try:
        if key in (ord("q"), ord("Q")):
            keep_running = False
        elif key in (ord("u"), ord("U")):
            request_json("POST", "/plotter/pen", {"position": "up"})
            message = "pen up"
        elif key in (ord("d"), ord("D")):
            request_json("POST", "/plotter/pen", {"position": "down"})
            message = "pen down"
        elif key in (ord("h"), ord("H")):
            request_json("POST", "/plotter/home/return", {})
            message = "returning home"
        elif key in (ord("s"), ord("S")):
            request_json("POST", "/plotter/home/set", {})
            message = "home set here"
        elif key in (ord("e"), ord("E")):
            request_json("POST", "/plotter/motors", {"enabled": True})
            message = "motors enabled"
        elif key in (ord("x"), ord("X")):
            request_json("POST", "/plotter/motors", {"enabled": False})
            message = "motors disabled"
        elif key in (ord("c"), ord("C")):
            request_json("POST", "/operator/continue", {}, token=False)
            message = "operator continue sent"
        elif key in (ord("+"), ord("=")):
            step = min(50.0, step * 2)
            message = f"step {step:g}mm"
        elif key in (ord("-"), ord("_")):
            step = max(0.1, step / 2)
            message = f"step {step:g}mm"
    except ApiError as exc:
            message = str(exc)
    return keep_running, step, message


def arrow_delta(key: int, step: float) -> tuple[float, float] | None:
    if key == curses.KEY_UP:
        return 0, step
    if key == curses.KEY_DOWN:
        return 0, -step
    if key == curses.KEY_LEFT:
        return -step, 0
    if key == curses.KEY_RIGHT:
        return step, 0
    return None


def main(stdscr):
    curses.curs_set(0)
    stdscr.keypad(True)
    stdscr.nodelay(True)
    stdscr.timeout(40)

    step = 2.0
    state = {}
    message = ""
    next_refresh = 0.0
    keep_running = True

    while keep_running:
        now = time.monotonic()
        if now >= next_refresh:
            try:
                state = request_json("GET", "/plotter/state")
            except ApiError as exc:
                state = {}
                message = str(exc)
            next_refresh = now + REFRESH_INTERVAL

        stdscr.erase()
        height, width = stdscr.getmaxyx()

        title = f" Plotter Console  step={step:g}mm "
        stdscr.addstr(0, 0, clamp_line(title, width - 1), curses.A_REVERSE)

        left_w = max(34, width // 2)
        right_w = max(20, width - left_w - 1)
        panel_h = max(8, height - 6)

        draw_box(stdscr, 2, 0, panel_h, left_w, " State ")
        add_lines(stdscr, 4, 2, left_w - 4, format_state(state))

        draw_position(stdscr, 2, left_w + 1, max(10, panel_h // 2), right_w, state)
        jobs_y = 3 + max(10, panel_h // 2)
        draw_box(stdscr, jobs_y, left_w + 1, max(6, height - jobs_y - 4), right_w, " Jobs ")
        add_lines(stdscr, jobs_y + 2, left_w + 3, right_w - 4, format_jobs(state))

        help_lines = [
            "Arrows move continuously while held   +/- step   U/D pen   H home   S set home   E/X motors   C continue   Q quit",
            f"last: {message}",
        ]
        add_lines(stdscr, height - 3, 0, width - 1, help_lines)
        stdscr.refresh()

        jog_delta = None
        while True:
            key = stdscr.getch()
            if key == -1:
                break

            delta = arrow_delta(key, step)
            if delta is not None:
                jog_delta = delta
                continue

            keep_running, step, message = handle_key(key, step)
            next_refresh = 0.0
            if not keep_running:
                break

        if keep_running and jog_delta is not None:
            try:
                send_move(*jog_delta)
                message = f"jog x={jog_delta[0]:g}mm y={jog_delta[1]:g}mm"
            except ApiError as exc:
                message = str(exc)
            # Drop repeats accumulated while the HTTP jog was in flight. Holding
            # the key will generate fresh repeats; releasing will not replay stale ones.
            curses.flushinp()
            next_refresh = 0.0


if __name__ == "__main__":
    curses.wrapper(main)
