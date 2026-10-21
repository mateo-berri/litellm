import os
import re
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import psutil

ROOT = Path(sys.argv[1])
APP = Path(sys.argv[2])
STARTED = re.compile(r"Started server process \[(\d+)\]")


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def alive(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health/liveliness", timeout=5) as response:
            return response.status == 200
    except OSError:
        return False


def started(log):
    return [int(pid) for pid in STARTED.findall(log.read_text(errors="replace"))]


def wait_for_workers(log, port, previous, deadline_seconds=240):
    began = time.monotonic()
    while time.monotonic() - began < deadline_seconds:
        fresh = [pid for pid in started(log) if pid not in previous]
        if len(fresh) >= 2 and all(psutil.pid_exists(pid) for pid in fresh[-2:]) and alive(port):
            return frozenset(fresh[-2:]), time.monotonic() - began
        time.sleep(0.5)
    raise SystemExit(f"FAIL: no two fresh workers within {deadline_seconds}s\n{log.read_text(errors='replace')[-4000:]}")


def wait_gone(pids, seconds):
    began = time.monotonic()
    while any(psutil.pid_exists(pid) and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE for pid in pids):
        if time.monotonic() - began > seconds:
            return None
        time.sleep(0.2)
    return time.monotonic() - began


def group_members(pgid):
    members = []
    for process in psutil.process_iter(["pid"]):
        try:
            if os.getpgid(process.pid) == pgid and process.status() != psutil.STATUS_ZOMBIE:
                members.append(process.pid)
        except (ProcessLookupError, PermissionError, psutil.Error):
            pass
    return members


APP.mkdir(parents=True, exist_ok=True)
(APP / "config.yaml").write_text(
    "model_list:\n  - model_name: probe\n    litellm_params:\n      model: openai/gpt-4o-mini\n      api_key: sk-unused\n"
)
port = free_port()
log = APP / "proxy.log"
probe = ROOT / "reload_probe.py"
command = (
    str(ROOT / ".venv/bin/python"),
    "litellm/proxy/proxy_cli.py",
    "--config",
    str(APP / "config.yaml"),
    "--num_workers",
    "2",
    "--reload",
    "--port",
    str(port),
)
probe.write_text("VALUE = 0\n")
print("$", " ".join(command[1:]), flush=True)
with log.open("wb") as output:
    proxy = subprocess.Popen(command, cwd=ROOT, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
failures = []
try:
    first, boot_seconds = wait_for_workers(log, port, frozenset())
    print(f"boot: two workers {sorted(first)} serve /health/liveliness after {boot_seconds:.1f}s", flush=True)

    probe.write_text("VALUE = 1\n")
    second, reload_seconds = wait_for_workers(log, port, first)
    old_left = [pid for pid in first if psutil.pid_exists(pid) and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE]
    print(f"save 1: new workers {sorted(second)} serve after {reload_seconds:.1f}s, old workers left: {old_left}", flush=True)
    if old_left:
        failures.append("old workers survived a save")

    pool_parent = psutil.Process(min(second)).ppid()
    os.kill(pool_parent, signal.SIGKILL)
    orphan_seconds = wait_gone(second, 30)
    print(f"pool parent {pool_parent} SIGKILLed: its workers exited after {orphan_seconds}", flush=True)
    if orphan_seconds is None:
        failures.append("workers outlived their killed pool parent by 30s")

    probe.write_text("VALUE = 2\n")
    third, reload_seconds = wait_for_workers(log, port, first | second)
    print(f"save 2: new workers {sorted(third)} serve after {reload_seconds:.1f}s", flush=True)

    os.killpg(proxy.pid, signal.SIGINT)
    began = time.monotonic()
    try:
        code = proxy.wait(timeout=60)
    except subprocess.TimeoutExpired:
        code = None
    left = group_members(proxy.pid)
    print(f"Ctrl-C: exit code {code} after {time.monotonic() - began:.1f}s, processes left: {left}", flush=True)
    if code is None or left:
        failures.append("Ctrl-C left the proxy or its workers running")
finally:
    probe.unlink(missing_ok=True)
    try:
        os.killpg(proxy.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    print("--- proxy log tail ---")
    print(log.read_text(errors="replace")[-3000:])
if failures:
    raise SystemExit("FAIL: " + "; ".join(failures))
print("PASS")
