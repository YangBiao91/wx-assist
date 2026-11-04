"""Current-user Windows-login bootstrap. No authentication, new scope or email.

The bootstrap exits once the monitor is running. It does not restart a later
deliberate pause/exit. Initial executable/state failures receive bounded retries;
WeChat/Outlook readiness is handled by the monitor's existing queues.
"""
import json
import os
import time
from pathlib import Path

import control

ROOT=Path(__file__).resolve().parent
DATA=ROOT/"data"


def ready(value):
    return value.get("running") and value.get("state") in {"monitoring","retrying","blocked","paused"}


def record(value):
    DATA.mkdir(exist_ok=True)
    value=dict(value,updated_at=time.time(),bootstrap_pid=os.getpid())
    temporary=DATA/("startup-status-"+str(os.getpid())+".tmp")
    temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding="utf-8")
    temporary.replace(DATA/"startup-status.json")


def bootstrap(start=None,status=None,wait=None,write=None,max_attempts=6):
    start=start or control.start
    status=status or control.status
    wait=wait or time.sleep
    write=write or record
    for attempt in range(1,max_attempts+1):
        current=status()
        if ready(current):
            result={"state":"already_running","monitor_pid":current["pid"],"attempts":attempt-1}
            write(result)
            return result
        write({"state":"starting","attempts":attempt})
        if current.get("running"):
            launched=current
        else:
            try:
                launched=start()
            except Exception:
                launched={"error":"launcher_not_ready"}
        current=status()
        if ready(current):
            result={"state":"started","monitor_pid":current["pid"],"monitor_state":current.get("state"),"attempts":attempt}
            write(result)
            return result
        if launched.get("running"):
            # The Windows venv redirector may still be starting its child. Wait
            # for a status heartbeat; do not spawn a second candidate meanwhile.
            for _ in range(6):
                wait(5)
                current=status()
                if ready(current):
                    result={"state":"started","monitor_pid":current["pid"],"monitor_state":current.get("state"),"attempts":attempt}
                    write(result)
                    return result
            result={"state":"startup_pending_review","attempts":attempt,
                    "candidate_pid":launched.get("pid"),"error":"process_started_without_ready_status"}
            write(result)
            return result
        if attempt<max_attempts:
            delay=min(5*2**(attempt-1),30)
            write({"state":"retry_wait","attempts":attempt,"retry_in_seconds":delay,
                   "error":launched.get("error","monitor_not_ready")})
            wait(delay)
    result={"state":"startup_failed","attempts":max_attempts,"error":"initial_start_retry_limit_reached"}
    write(result)
    return result


if __name__=="__main__":
    try:
        bootstrap()
    except Exception:
        try:
            record({"state":"startup_failed","error":"local_bootstrap_error"})
        except Exception:
            pass
