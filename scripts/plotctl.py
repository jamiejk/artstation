#!/usr/bin/env python3
import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


BASE_DIR = Path.home() / "plotter"
ENV_PATH = BASE_DIR / ".env"
SERVER = "http://127.0.0.1:8765"


def load_env_token() -> str:
    if not ENV_PATH.exists():
        return ""
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        if line.startswith("PLOTTER_TOKEN="):
            return line.split("=", 1)[1].strip()
    return ""


TOKEN = load_env_token()


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
        with urllib.request.urlopen(request, timeout=10) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"Server unavailable: {exc}") from exc

    return json.loads(body) if body else {}


def print_json(data):
    print(json.dumps(data, indent=2, sort_keys=True))


def print_jobs(data):
    jobs = data.get("jobs", [])
    if not jobs:
        print("No jobs.")
        return
    for job in jobs:
        current = job.get("current_layer_name") or "-"
        print(f"{job['id']}  {job.get('status'):<20}  layer={job.get('current_layer') or '-'}  {current}")


def cmd_state(_args):
    data = request_json("GET", "/plotter/state")
    hardware = data.get("hardware", {})
    server = data.get("server", {})

    print(f"server: ok queue={server.get('queue_size')}")
    print(f"hardware: busy={hardware.get('busy')} port={hardware.get('port')}")
    if hardware.get("busy"):
        print(hardware.get("message", "Hardware busy"))
    else:
        pos = hardware.get("position_estimate") or {}
        steps = hardware.get("steps") or {}
        print(f"firmware: {hardware.get('firmware')}")
        print(f"pen_up: {hardware.get('pen_up')}  button_pressed: {hardware.get('button_pressed')}")
        print(f"steps: axis_1={steps.get('axis_1')} axis_2={steps.get('axis_2')}")
        if pos:
            print(f"position_estimate: x={pos.get('x_mm', 0):.3f}mm y={pos.get('y_mm', 0):.3f}mm")

    prompt = server.get("operator_prompt") or {}
    if prompt.get("active"):
        print(f"operator: waiting job={prompt.get('job_id')} {prompt.get('message')}")

    print("\njobs:")
    print_jobs({"jobs": data.get("jobs", [])})


def cmd_jobs(_args):
    print_jobs(request_json("GET", "/jobs"))


def cmd_log(args):
    data = request_json("GET", f"/jobs/{args.job_id}/log")
    print(data.get("tail", ""), end="")


def cmd_continue(_args):
    print_json(request_json("POST", "/operator/continue", token=False))


def cmd_cancel(args):
    print_json(request_json("POST", f"/jobs/{args.job_id}/cancel"))


def cmd_pause(args):
    print_json(request_json("POST", f"/jobs/{args.job_id}/pause"))


def cmd_rerun(args):
    print_json(request_json("POST", f"/jobs/{args.job_id}/rerun"))


def cmd_clear(args):
    payload = {"keep_files": not args.delete_files}
    if args.status:
        payload["statuses"] = args.status
    print_json(request_json("POST", "/jobs/clear", payload))


def cmd_pen(args):
    payload = {"position": args.position}
    if args.down is not None:
        payload["pen_pos_down"] = args.down
    if args.up is not None:
        payload["pen_pos_up"] = args.up
    print_json(request_json("POST", "/plotter/pen", payload))


def cmd_motors(args):
    print_json(request_json("POST", "/plotter/motors", {"enabled": args.action == "enable"}))


def cmd_home(args):
    if args.action == "set":
        print_json(request_json("POST", "/plotter/home/set", {}))
    else:
        print_json(request_json("POST", "/plotter/home/return", {}))


def cmd_move(args):
    payload = {"absolute": args.absolute}
    if args.x is not None:
        payload["x_mm"] = args.x
    if args.y is not None:
        payload["y_mm"] = args.y
    print_json(request_json("POST", "/plotter/move", payload))


def cmd_watch(args):
    while True:
        print("\033c", end="")
        cmd_state(args)
        time.sleep(args.interval)


def build_parser():
    parser = argparse.ArgumentParser(prog="plotctl", description="Local plotter control CLI")
    sub = parser.add_subparsers(required=True)

    state = sub.add_parser("state", help="Show server, hardware, and recent job state")
    state.set_defaults(func=cmd_state)

    watch = sub.add_parser("watch", help="Refresh state repeatedly")
    watch.add_argument("--interval", type=float, default=2.0)
    watch.set_defaults(func=cmd_watch)

    jobs = sub.add_parser("jobs", help="List jobs")
    jobs.set_defaults(func=cmd_jobs)

    log = sub.add_parser("log", help="Show job log tail")
    log.add_argument("job_id")
    log.set_defaults(func=cmd_log)

    cont = sub.add_parser("continue", help="Approve the current operator prompt")
    cont.set_defaults(func=cmd_continue)

    cancel = sub.add_parser("cancel", help="Cancel a queued or operator-waiting job")
    cancel.add_argument("job_id")
    cancel.set_defaults(func=cmd_cancel)

    pause = sub.add_parser("pause", help="Send SIGINT to the active AxiCLI plot process")
    pause.add_argument("job_id")
    pause.set_defaults(func=cmd_pause)

    rerun = sub.add_parser("rerun", help="Rerun a stopped job from the beginning")
    rerun.add_argument("job_id")
    rerun.set_defaults(func=cmd_rerun)

    clear = sub.add_parser("clear", help="Clear stopped jobs from the server view")
    clear.add_argument("--delete-files", action="store_true", help="Also delete job folders and logs")
    clear.add_argument("--status", action="append", help="Status to clear; may be repeated")
    clear.set_defaults(func=cmd_clear)

    pen = sub.add_parser("pen", help="Raise or lower pen")
    pen.add_argument("position", choices=["up", "down"])
    pen.add_argument("--down", type=int, help="Override pen_pos_down")
    pen.add_argument("--up", type=int, help="Override pen_pos_up")
    pen.set_defaults(func=cmd_pen)

    motors = sub.add_parser("motors", help="Enable or disable XY motors")
    motors.add_argument("action", choices=["enable", "disable"])
    motors.set_defaults(func=cmd_motors)

    home = sub.add_parser("home", help="Set or return to XY home")
    home.add_argument("action", choices=["set", "return"])
    home.set_defaults(func=cmd_home)

    move = sub.add_parser("move", help="Move XY in millimeters")
    move.add_argument("--x", type=float, help="X mm, relative by default")
    move.add_argument("--y", type=float, help="Y mm, relative by default")
    move.add_argument("--absolute", action="store_true", help="Treat x/y as target position from current home")
    move.set_defaults(func=cmd_move)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
