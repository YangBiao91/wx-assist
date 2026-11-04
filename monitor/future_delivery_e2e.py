"""One labeled synthetic alert through production filter/queue/Outlook code.

Uses a separate SQLite state, never real WeChat data or the real alert queue.
The Outlook work guard must pass immediately before a normal, non-forced Quit.
"""
import argparse
import ctypes
import json
import re
import sqlite3
import time
import uuid
from pathlib import Path

import control
import deals
import email_queue
import monitor
from outlook_mailer import build_offer_mail,SENDER,RECIPIENT
from outlook_session import connect_outlook,running_outlook
from state import State
from settings import SETTINGS

ROOT=Path(__file__).resolve().parent
RUNS=ROOT/"e2e-runs"


def save(path,value):
    temporary=path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding="utf-8")
    temporary.replace(path)


def emit(value):
    print(json.dumps(value),flush=True)


def run_path(run_id):
    if not re.fullmatch(r"[0-9a-f]{32}",run_id):
        raise ValueError("invalid_test_run_id")
    path=(RUNS/run_id).resolve()
    if path.parent!=RUNS.resolve():
        raise ValueError("test_path_outside_workspace")
    return path


class SyntheticSource:
    def __init__(self,rows):
        self.rows=rows
    def collect(self,state,baseline=False):
        cursor=state.cursor("synthetic-test") or 0
        return [("synthetic-test",max((r[0] for r in self.rows),default=0),[] if baseline else [r for r in self.rows if r[0]>cursor])]
    def acknowledge(self):
        pass


def prepare():
    run_id=uuid.uuid4().hex
    path=run_path(run_id)
    path.mkdir(parents=True)
    now=time.time()
    config={"enabled":True,"enabled_since":now-2,"sender":SENDER,"recipient":RECIPIENT}
    save(path/"mail-config.json",config)
    state=State(path/"state.sqlite3")
    try:
        monitor.process_cycle(state,SyntheticSource([]),now-1)
        # Globally distinct job filename in the normal constrained job directory;
        # the production monitor never scans jobs outside its own SQLite outbox.
        with state.db:
            state.db.execute("INSERT OR REPLACE INTO meta VALUES('offer_sequence',?)",(str(900000000000+int(run_id[:8],16)),))
        if SETTINGS.get("rule_profile","generic")=="generic":
            from generic_rules import load_rules
            enabled=[rule for rule in load_rules() if rule["enabled"]]
            if not enabled:
                raise RuntimeError("an_enabled_generic_rule_is_required")
            term=enabled[0]["any_keywords"][0]
        else:
            term="超好忦"
        text=f"合成测试：{term}，仅验证未来提醒链路，不是真实群通知。测试编号 {run_id}"
        rows=[(1,1,1,int(now),text),(2,2,1,int(now),text)]
        result=monitor.process_cycle(state,SyntheticSource(rows),now)
        assert state.counts()["offers"]==1
        data=json.loads(state.db.execute("SELECT data FROM offers").fetchone()[0])
        title,body=build_offer_mail({"group_id_sha256":monitor.PINNED_GROUP_SHA256,"offer":data,
                                   "source_ts":int(now),"alert_id":"preview-only","synthetic_test_id":run_id})
        assert title.startswith("[合成测试]")
        report={"run_id":run_id,"phase":"prepared","created_at":now,"subject":title,
                "sender":SENDER,"recipient":RECIPIENT,"two_duplicate_input_records":True,
                "filtered_offer_count":1,"new_offer_count":result["new_offers"],
                "real_chat_used":False,"production_queue_touched":False,"send_attempted":False}
        save(path/"report.json",report)
        emit(report)
    finally:
        state.db.close()


def safe_close_outlook(report,path):
    pids=running_outlook()
    if not pids:
        report["cold_before_send"]=True
        report["outlook_initially_closed"]=True
        save(path/"report.json",report)
        return
    import pythoncom,win32com.client
    pythoncom.CoInitialize()
    try:
        app,connection=connect_outlook(win32com.client,allow_launch=False)
        active=ctypes.windll.user32.GetForegroundWindow()
        foreground_pid=ctypes.c_ulong()
        ctypes.windll.user32.GetWindowThreadProcessId(active,ctypes.byref(foreground_pid))
        inline_count=sum(app.Explorers.Item(i).ActiveInlineResponse is not None for i in range(1,int(app.Explorers.Count)+1))
        accounts=[str(app.Session.Accounts.Item(i).SmtpAddress).lower() for i in range(1,int(app.Session.Accounts.Count)+1)]
        guard={"inspectors":int(app.Inspectors.Count),"inline_responses":inline_count,
               "outbox_items":int(app.Session.GetDefaultFolder(4).Items.Count),
               "outlook_foreground":foreground_pid.value in pids,"account_matches":accounts==[SENDER]}
        report["close_guard"]=guard
        save(path/"report.json",report)
        if guard["inspectors"] or guard["inline_responses"] or guard["outbox_items"] or guard["outlook_foreground"] or not guard["account_matches"]:
            raise RuntimeError("outlook_user_work_guard_blocked_close")
        # Normal supported application exit. No process kill or forced discard.
        app.Quit()
        app=None
    finally:
        pythoncom.CoUninitialize()
    for _ in range(30):
        if not running_outlook():
            report["cold_before_send"]=True
            report["outlook_closed_normally_for_test"]=True
            save(path/"report.json",report)
            emit({"phase":"outlook_confirmed_closed","run_id":report["run_id"]})
            return
        time.sleep(1)
    raise RuntimeError("outlook_did_not_exit_cleanly_no_force_used")


def execute(run_id):
    path=run_path(run_id)
    report=json.loads((path/"report.json").read_text(encoding="utf-8"))
    if report.get("phase")!="prepared":
        raise RuntimeError("test_already_executed_do_not_resend")
    if control.status().get("running"):
        raise RuntimeError("pause_production_monitor_before_controlled_test")
    config=email_queue.configuration()
    if config.get("outlook_on_demand_start") is not True:
        raise RuntimeError("outlook_on_demand_start_not_enabled")
    realdb=sqlite3.connect("file:"+(ROOT/"data/offers.sqlite3").as_posix()+"?mode=ro",uri=True)
    real_before=[tuple(row) for row in realdb.execute("SELECT offer_id,state,attempts FROM email_outbox ORDER BY offer_id")]
    realdb.close()
    safe_close_outlook(report,path)
    report.update(phase="executing",production_queue_before=real_before)
    save(path/"report.json",report)
    state=State(path/"state.sqlite3")
    previous_config=email_queue.CONFIG
    email_queue.CONFIG=path/"mail-config.json"
    calls=[]
    def transport(job_path):
        job=json.loads(job_path.read_text(encoding="utf-8"))
        if "合成测试" not in job["offer"].get("keyword_excerpt",""):
            raise RuntimeError("not_a_synthetic_test_payload")
        job["synthetic_test_id"]=run_id
        save(job_path,job)
        calls.append(job["alert_id"])
        emit({"phase":"synthetic_job_submitting","run_id":run_id,"alert_id":job["alert_id"]})
        completed=email_queue.launch(job_path)
        result_path=job_path.with_suffix(".result.json")
        result=json.loads(result_path.read_text(encoding="utf-8")) if result_path.exists() else {"error":"helper_result_missing"}
        save(path/"transport-result.json",result)
        report["transport_result"]=result
        report["helper_completed"]=completed
        report["send_attempted"]=result.get("send_attempted",False)
    try:
        result=email_queue.tick(state,transport=transport)
        report.update(phase="submitted" if result["email_submitted"]==1 else "not_submitted",
                      queue_result=result,transport_invocations=len(calls),alert_ids=calls,
                      actual_outlook_auto_start=report.get("transport_result",{}).get("outlook_connection",{}).get("started_by_monitor") is True)
        save(path/"report.json",report)
        if result["email_submitted"]!=1:
            raise RuntimeError("synthetic_mail_not_submitted_no_blind_retry")
        state.db.close()
        state=State(path/"state.sqlite3")
        after_restart=email_queue.tick(state,transport=transport)
        after_duplicate=email_queue.tick(state,transport=transport)
        attempts=state.db.execute("SELECT attempts FROM email_outbox").fetchone()[0]
        assert len(calls)==1 and attempts==1
        assert after_restart["email_submitted"]==after_duplicate["email_submitted"]==1
        report.update(phase="awaiting_mailbox_receipt",dedup_verified=True,reopened_state_verified=True,
                      transport_invocations=1,send_attempt_count=attempts,
                      outlook_running_after_send=bool(running_outlook()))
        save(path/"report.json",report)
        emit({key:report[key] for key in ("run_id","phase","subject","actual_outlook_auto_start","dedup_verified","send_attempt_count","alert_ids")})
    finally:
        state.db.close()
        email_queue.CONFIG=previous_config


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("action",choices=("prepare","execute"))
    parser.add_argument("--run-id")
    parser.add_argument("--allow-outlook-close",action="store_true")
    parser.add_argument("--send-test",action="store_true")
    args=parser.parse_args()
    try:
        if args.action=="execute" and not (args.allow_outlook_close and args.send_test):
            raise RuntimeError("explicit_outlook_close_and_test_send_flags_required")
        prepare() if args.action=="prepare" else execute(args.run_id)
    except Exception as exc:
        emit({"phase":"stopped","error":str(exc),"error_type":type(exc).__name__,"no_forced_retry":True})
        raise SystemExit(2)
