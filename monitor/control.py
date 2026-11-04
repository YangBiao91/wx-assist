"""Start/stop only this workspace's monitor, without an OS startup task."""
import json
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
SCRIPT = ROOT / "monitor.py"


def status():
    try:
        data = json.loads((DATA / "status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"running": False, "state": "not_started"}
    try:
        process = psutil.Process(int(data["pid"]))
        expected = str(SCRIPT).lower()
        running = any(str(arg).lower() == expected for arg in process.cmdline()) and process.is_running()
    except (psutil.Error, KeyError, ValueError):
        running = False
    return dict(data, running=running)


def set_scope_enabled(enabled):
    path = ROOT.parent / "single-group-scope.json"
    scope = json.loads(path.read_text(encoding="utf-8"))
    scope.update(monitoring_enabled=enabled, auto_send_enabled=False)
    path.write_text(json.dumps(scope, ensure_ascii=False, indent=2), encoding="utf-8")


@contextmanager
def launcher_lock():
    import msvcrt
    DATA.mkdir(exist_ok=True)
    handle=(DATA/"launch.lock").open("a+b")
    if handle.tell()==0:
        handle.write(b"0")
        handle.flush()
    deadline=time.monotonic()+25
    try:
        while True:
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
                break
            except OSError:
                if time.monotonic()>=deadline:
                    raise RuntimeError("another_launcher_is_starting")
                time.sleep(0.25)
        yield
    finally:
        handle.close()


def start():
    # Serializes login bootstrap, manual start, and a second shortcut activation.
    # The monitor's own lifetime lock remains an independent final safeguard.
    with launcher_lock():
        return start_locked()


def start_locked():
    current = status()
    if current["running"]:
        return dict(current, already_running=True)
    from settings import validate_runtime_settings
    validate_runtime_settings()
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    process = subprocess.Popen([str(pythonw), "-B", str(SCRIPT)], cwd=ROOT,
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, close_fds=True,
                               creationflags=subprocess.CREATE_NO_WINDOW)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        current = status()
        candidate = current.get("pid")
        try:
            own_child = candidate == process.pid or (candidate and process.pid in {p.pid for p in psutil.Process(candidate).parents()})
        except psutil.Error:
            own_child = False
        # Windows venv pythonw.exe can redirect to a separate child PID.
        if own_child and current.get("state") in {"monitoring", "retrying", "blocked"}:
            set_scope_enabled(current["running"])
            return current
        if process.poll() is not None:
            return {"running":False, "error":"monitor_exited_during_start", "exit_code":process.returncode}
        time.sleep(0.25)
    return {"running":process.poll() is None, "pid":process.pid, "state":"starting", "startup_outcome":"pending"}


def stop():
    current = status()
    if not current["running"]:
        set_scope_enabled(False)
        return current
    (DATA / "stop.request").write_text("stop", encoding="ascii")
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        current = status()
        if not current["running"]:
            set_scope_enabled(False)
            return current
        time.sleep(0.25)
    return dict(current, stop_outcome="pending")


def enable_email():
    return {"error":"use_configure_py_to_confirm_test_delivery"}


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv)>1 else "status"
    try:
        result = {"start":start, "stop":stop, "status":status,"enable-email":enable_email}[command]()
        print(json.dumps(result), flush=True)
        sys.exit(0 if result.get("error") is None else 2)
    except Exception:
        print(json.dumps({"error":"local_control_failed"}), flush=True)
        sys.exit(2)
