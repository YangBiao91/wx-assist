"""Durable independent email outbox, disabled until the real send test succeeds."""
import json
import re
import subprocess
import sys
import time
from pathlib import Path

from outlook_mailer import GROUP_SHA256,RECIPIENT,SENDER,build_offer_mail

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "mail-config.json"
JOBS = ROOT / "data/email-jobs"


def configuration():
    value = json.loads(CONFIG.read_text(encoding="utf-8"))
    if value.get("sender","").lower()!=SENDER or value.get("recipient","").lower()!=RECIPIENT:
        raise ValueError("email_address_scope_mismatch")
    return value


def schema(state):
    state.db.execute("""CREATE TABLE IF NOT EXISTS email_outbox(
      offer_id INTEGER PRIMARY KEY, job_name TEXT NOT NULL, state TEXT NOT NULL,
      last_attempt REAL NOT NULL DEFAULT 0,next_attempt REAL NOT NULL DEFAULT 0,
      attempts INTEGER NOT NULL DEFAULT 0,error TEXT)""")
    state.db.commit()


def reconcile(state,now):
    # A restart never blindly retries a potentially submitted message.
    for row in state.db.execute("SELECT * FROM email_outbox WHERE state IN ('inflight','uncertain')").fetchall():
        result_path = (JOBS/row["job_name"]).with_suffix(".result.json")
        result = json.loads(result_path.read_text(encoding="utf-8")) if result_path.exists() else {}
        if result.get("submitted"):
            new,error = "submitted",None
        elif result.get("error") and not result.get("send_attempted") and result.get("retry_safe"):
            new,error = "pending",result["error"]
        elif result.get("error") and not result.get("send_attempted"):
            new,error = "blocked",result["error"]
        else:
            new,error = "uncertain","send_outcome_requires_review"
        with state.db:
            state.db.execute("UPDATE email_outbox SET state=?,error=?,next_attempt=? WHERE offer_id=?",(new,error,now+300,row["offer_id"]))


def enqueue(state,enabled_since,now):
    rows = state.db.execute("SELECT id,data,source_ts,fingerprint FROM offers WHERE created>=?",(enabled_since,)).fetchall()
    for row in rows:
        data = json.loads(row["data"])
        job = {"offer":data,"source_ts":row["source_ts"],"alert_id":str(row["id"])+"-"+row["fingerprint"][:12],"group_id_sha256":GROUP_SHA256}
        try:
            build_offer_mail(job,now)
        except (ValueError,TypeError,KeyError):
            continue
        name = str(row["id"])+"-"+row["fingerprint"][:12]+".json"
        # Existing jobs are immutable; never overwrite an attempted payload.
        if state.db.execute("SELECT 1 FROM email_outbox WHERE offer_id=?",(row["id"],)).fetchone():
            continue
        JOBS.mkdir(exist_ok=True)
        (JOBS/name).write_text(json.dumps(job,ensure_ascii=False),encoding="utf-8")
        with state.db:
            state.db.execute("INSERT OR IGNORE INTO email_outbox(offer_id,job_name,state) VALUES(?,?,'pending')",(row["id"],name))


def launch(job_path):
    process = subprocess.Popen([sys.executable,"-B",str(ROOT/"outlook_mailer.py"),"--job",str(job_path),"--submit"],
                               cwd=ROOT,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                               close_fds=True,creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        process.wait(timeout=35)
    except subprocess.TimeoutExpired:
        # Outlook may be showing a security prompt. Do not click it, terminate
        # Outlook, or start another send. The result may arrive after user action.
        return False
    return True


def cleanup_completed_payloads(state):
    # Keep only a small sent tombstone for deduplication. The private product
    # summary remains in the ordinary 30-day offer store, not a second archive.
    for row in state.db.execute("SELECT job_name FROM email_outbox WHERE state IN ('submitted','expired_or_invalid')"):
        name=row["job_name"]
        if not re.fullmatch(r"\d+-[0-9a-f]{12}\.json",name):
            raise ValueError("invalid_owned_job_name")
        path=(JOBS/name).resolve()
        if path.parent!=JOBS.resolve():
            raise ValueError("owned_job_path_outside_outbox")
        path.unlink(missing_ok=True)
        path.with_suffix(".result.json").unlink(missing_ok=True)


def tick(state,now=None,transport=None):
    now = time.time() if now is None else now
    schema(state)
    config = configuration()
    reconcile(state,now)
    enabled = config.get("enabled") is True
    if enabled:
        enqueue(state,float(config["enabled_since"]),now)
        uncertain = state.db.execute("SELECT COUNT(*) FROM email_outbox WHERE state IN ('uncertain','blocked')").fetchone()[0]
        row = state.db.execute("SELECT * FROM email_outbox WHERE state='pending' AND next_attempt<=? ORDER BY offer_id LIMIT 1",(now,)).fetchone()
        if row and not uncertain:
            path = JOBS/row["job_name"]
            try:
                build_offer_mail(json.loads(path.read_text(encoding="utf-8")),now)
            except (ValueError,TypeError,KeyError):
                with state.db:
                    state.db.execute("UPDATE email_outbox SET state='expired_or_invalid' WHERE offer_id=?",(row["offer_id"],))
            else:
                with state.db:
                    state.db.execute("UPDATE email_outbox SET state='inflight',last_attempt=?,attempts=attempts+1 WHERE offer_id=?",(now,row["offer_id"]))
                # Invalidate the previous attempt's safe-retry result before a new
                # helper can start. A crash/prompt must not reuse stale success.
                path.with_suffix(".result.json").write_text(json.dumps({"stage":"starting_attempt","send_attempted":False,"submitted":False,"retry_safe":False}),encoding="utf-8")
                (transport or launch)(path)
                reconcile(state,now)
    cleanup_completed_payloads(state)
    counts = {row[0]:row[1] for row in state.db.execute("SELECT state,COUNT(*) FROM email_outbox GROUP BY state")}
    waiting=state.db.execute("SELECT error FROM email_outbox WHERE state='pending' AND error IS NOT NULL ORDER BY last_attempt DESC LIMIT 1").fetchone()
    last_error=waiting[0] if waiting else None
    mail_state="needs_review" if counts.get("uncertain",0) or counts.get("blocked",0) else "waiting_for_outlook" if enabled and last_error in {"outlook_not_ready","outlook_offline"} else "enabled" if enabled else "disabled"
    return {"email_enabled":enabled,"email_pending":counts.get("pending",0),
            "email_submitted":counts.get("submitted",0),"email_uncertain":counts.get("uncertain",0),
            "email_blocked":counts.get("blocked",0),
            "email_state":mail_state,"email_last_error":last_error}
