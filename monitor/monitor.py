"""Single-group local monitor. No wx-assist Bot, DLL extraction or external I/O."""
import argparse
import ctypes
import hashlib
import importlib.util
import json
import logging
import re
import sqlite3
import sys
import threading
import time
import types
from contextlib import contextmanager
from pathlib import Path

sys.dont_write_bytecode = True
from Crypto.Cipher import AES
import zstandard

from state import State
from deals import KEYWORD_FORMS
import email_queue
from settings import SETTINGS, GROUP_SHA256, CREDENTIAL_FILE

ROOT = Path(__file__).resolve().parent
BUILD = ROOT.parent
DATA = ROOT / "data"
PINNED_GROUP_SHA256 = GROUP_SHA256
READING = threading.local()
logging.disable(logging.CRITICAL)


class ScopeError(RuntimeError):
    pass


def scope_id():
    scope = json.loads((BUILD / "single-group-scope.json").read_text(encoding="utf-8"))
    chat_id = scope.get("chat_id", "")
    if (not chat_id.endswith("@chatroom") or
            hashlib.sha256(chat_id.encode()).hexdigest() != PINNED_GROUP_SHA256 or
            scope.get("auto_send_enabled") is not False):
        raise ScopeError("scope_mismatch")
    return chat_id


def guard(event, args):
    if event in {"socket.connect", "socket.bind", "socket.getaddrinfo"}:
        raise RuntimeError("network_disabled")
    if event == "ctypes.dlopen" and any(token in str(args[0]).lower() for token in ("wx_key", "keyhook")):
        raise RuntimeError("credential_extraction_disabled")
    if getattr(READING, "active", False):
        if event in {"os.mkdir", "os.remove", "os.rename", "os.rmdir"}:
            raise RuntimeError("read_phase_write_blocked")
        if event == "open":
            mode = args[1] if len(args) > 1 else None
            flags = args[2] if len(args) > 2 else 0
            if ((isinstance(mode, str) and any(c in mode for c in "wax+")) or
                    (isinstance(flags, int) and flags & (1 | 2 | 64 | 512 | 1024))):
                raise RuntimeError("read_phase_write_blocked")


@contextmanager
def read_phase():
    READING.active = True
    try:
        yield
    finally:
        READING.active = False


def load_core():
    package = types.ModuleType("monitor_wechat")
    package.__path__ = []
    sys.modules[package.__name__] = package
    modules = []
    for name in ("db_crypto", "db_reader"):
        spec = importlib.util.spec_from_file_location("monitor_wechat."+name, BUILD / "src/wechat" / (name+".py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        modules.append(module)
    return modules[1]


def decode(raw):
    if isinstance(raw, str):
        return raw if len(raw) <= 100_000 else ""
    if not isinstance(raw, (bytes, bytearray)):
        return ""
    if raw[:4] == b"\x28\xb5\x2f\xfd":
        raw = zstandard.ZstdDecompressor().decompress(raw, max_output_size=2*1024*1024)
    return bytes(raw).decode("utf-8", errors="strict")[:100_001]


class RealSource:
    def __init__(self):
        self.chat_id = scope_id()
        self.table = "Msg_" + hashlib.md5(self.chat_id.encode()).hexdigest()
        values = {}
        for line in CREDENTIAL_FILE.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip().strip('"\'')
        secret = values.pop("WCDB_KEY", "")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", secret):
            raise RuntimeError("saved_credential_unavailable")
        candidate = Path(values.get("DB_PATH", ""))
        account = next((p for p in (candidate, *candidate.parents)
                        if (p / "db_storage/session/session.db").is_file()), None)
        if account is None:
            raise RuntimeError("account_directory_unavailable")
        self.folder = account / "db_storage/message"
        module = load_core()
        self.reader = module.WeChatDbReader(account, secret)
        self.limit = module._MAX_SNAPSHOT_BYTES
        self.stamps = {}
        self.pending_stamps = {}
        self.shard_count = 0

    def collect(self, state, baseline=False):
        # Revalidate each cycle; editing a config cannot broaden access.
        if scope_id() != self.chat_id:
            raise ScopeError("scope_mismatch")
        batches = []
        self.pending_stamps = {}
        with read_phase():
            paths = sorted(p for p in self.folder.glob("message_*.db")
                           if re.fullmatch(r"message_\d+\.db", p.name))
            if not paths:
                raise RuntimeError("message_shards_unavailable")
            self.shard_count = len(paths)
            for path in paths:
                stamp = self.reader._source_stamp(path)
                if not baseline and self.stamps.get(path.name) == stamp:
                    continue
                if stamp[1] + stamp[3] > self.limit:
                    raise RuntimeError("memory_snapshot_limit")
                with path.open("rb") as stream:
                    salt_digest = hashlib.sha256(stream.read(16)).hexdigest()
                source = path.name+":"+salt_digest
                try:
                    conn = self.reader._connect(path)
                    found = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (self.table,)).fetchone()
                    rows = []
                    maximum = 0
                    if found:
                        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info([{self.table}])")}
                        if not {"local_id", "local_type", "create_time", "message_content"} <= columns:
                            raise RuntimeError("unsupported_message_schema")
                        maximum = int(conn.execute(f"SELECT COALESCE(MAX(local_id),0) AS n FROM [{self.table}]").fetchone()["n"])
                        cursor = state.cursor(source)
                        cursor = 0 if cursor is None or cursor > maximum else cursor
                        if not baseline:
                            server = "server_id" if "server_id" in columns else "0 AS server_id"
                            compressed = "compress_content" if "compress_content" in columns else "NULL AS compress_content"
                            result = conn.execute(f"SELECT local_id,{server},local_type,create_time,message_content,{compressed} FROM [{self.table}] WHERE local_id>? ORDER BY local_id LIMIT 250", (cursor,)).fetchall()
                            for row in result:
                                kind = int(row["local_type"]) & 0xffffffff
                                text = ""
                                if kind == 1:
                                    # Malformed single messages are skipped; ciphertext/read errors abort the cycle.
                                    try:
                                        text = decode(row["message_content"] or row["compress_content"])
                                    except (UnicodeError, zstandard.ZstdError):
                                        pass
                                rows.append((int(row["local_id"]), int(row["server_id"] or 0), kind,
                                             int(row["create_time"]), text))
                    # A changed encrypted source during any query invalidates the batch.
                    if self.reader._source_stamp(path) != stamp:
                        raise RuntimeError("source_changed_during_read")
                    batches.append((source, maximum, rows))
                    if baseline or not rows or rows[-1][0] >= maximum:
                        self.pending_stamps[path.name] = stamp
                finally:
                    self.reader.close()
        return batches

    def acknowledge(self):
        # Read optimization becomes valid only after the durable state commits.
        self.stamps.update(self.pending_stamps)
        self.pending_stamps = {}


def process_cycle(state, source, now=None):
    now = time.time() if now is None else now
    baseline = state.meta("started") is None
    batches = source.collect(state, baseline)
    if baseline:
        state.baseline(batches, now)
        source.acknowledge()
        return {"baseline": True, "messages": 0, "new_offers": 0}
    messages = new = 0
    for identity, maximum, rows in batches:
        new += state.ingest(identity, maximum, rows, now)
        messages += len(rows)
    source.acknowledge()
    return {"baseline": False, "messages": messages, "new_offers": new}


def deliver_one(state, notifier, now=None):
    now = time.time() if now is None else now
    row = state.pending(now)
    if not row:
        return None
    data = json.loads(row["data"])
    if data.get("match_status") != "matched" or (data.get("expiry_ts") is not None and data["expiry_ts"]<=now):
        with state.db:
            state.db.execute("UPDATE offers SET delivery=? WHERE id=?", ("expired" if data.get("expiry_ts") is not None and data["expiry_ts"]<=now else "needs_review", row["id"]))
        return None
    if data.get("trigger_kind")=="rule":
        message="规则："+data["rule_name"]+"\n"+data["keyword_excerpt"][:180]
    elif data.get("trigger_kind","price")=="keyword":
        message="关键词："+"、".join(data["matched_keywords"])+"\n"+data["title"][:75]+"\n"+data["keyword_excerpt"][:100]
    else:
        prices = " / ".join(f"¥{p:g}" for p in data["prices_yuan"]) or "报价待确认"
        message = f"{data['brand']} 5磅 {prices} < ¥{data['threshold_yuan']}\n"+data["title"][:75]+"\n群内报价未核价；点击查看条件与截止时间"
    title="群聊监控 · "+data["rule_name"] if data.get("trigger_kind")=="rule" else "群聊监控 · 关键词提醒" if data.get("trigger_kind","price")=="keyword" else "群聊监控 · 价格示例"
    shown = notifier.send(title, message)
    state.delivered(row["id"], shown, now)
    return shown


def write_status(values):
    values.update(updated_at=time.time(), pid=__import__('os').getpid(),
                  group_id_sha256=PINNED_GROUP_SHA256, external_transport=values.get("email_enabled",False),
                  auto_send=False, ai=False, credential_extraction=False)
    values["rule_profile"] = SETTINGS.get("rule_profile","generic")
    values["rules"] = {"ON":{"pounds":5,"price_cny_lt":580},"熊猫":{"pounds":5,"price_cny_lt":400},
                       "keyword":{"terms":list(KEYWORD_FORMS),"any_brand_or_price":True}}
    if values["rule_profile"]=="generic":
        from generic_rules import load_rules
        try:
            values["rules"]={"enabled_rule_ids":[r["id"] for r in load_rules() if r["enabled"]]}
        except (OSError,ValueError):
            values["rules"]={"error":"invalid_local_rules"}
    tmp = DATA / "status.tmp"
    tmp.write_text(json.dumps(values, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(DATA / "status.json")


def instance_lock():
    import msvcrt
    handle = (DATA / "instance.lock").open("a+b")
    if handle.tell() == 0:
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        handle.close()
        raise RuntimeError("already_running")
    return handle


def safe_error(exc):
    if isinstance(exc, ScopeError):
        return "scope_mismatch"
    safe = {"saved_credential_unavailable", "account_directory_unavailable", "message_shards_unavailable", "memory_snapshot_limit", "unsupported_message_schema", "source_changed_during_read", "network_disabled", "credential_extraction_disabled", "tray_initialization_unavailable"}
    value = str(exc)
    if value in safe:
        return value
    if isinstance(exc,ValueError) and str(exc).startswith(("rules_","invalid_rule","unique_rule_","positive_keywords_","explicit_rule_")):
        return "rule_configuration_invalid"
    if isinstance(exc, (sqlite3.Error, ValueError)):
        return "database_or_credential_unavailable"
    return "local_read_temporarily_unavailable"


def run_worker(notifier, stop, paused):
    state = State(DATA / "offers.sqlite3")
    source = None
    status = {"state": "starting", "cycles": 0, "messages_processed": 0}
    try:
        while not stop.is_set():
            try:
                if (DATA / "stop.request").exists():
                    stop.set()
                    break
                if paused.is_set():
                    status["state"] = "paused"
                else:
                    if source is None:
                        source = RealSource()
                    result = process_cycle(state, source)
                    status.update(state="monitoring", last_success=time.time(), error=None,
                                  cycles=status["cycles"]+1,
                                  messages_processed=status["messages_processed"]+result["messages"],
                                  shard_count=source.shard_count)
                    delivered = deliver_one(state, notifier)
                    if delivered is not None:
                        status["notification_last_shown"] = delivered
                    try:
                        status.update(email_queue.tick(state))
                        status["email_error"] = status.get("email_last_error")
                    except Exception:
                        status.update(email_state="error",email_error="local_email_queue_check_failed")
                status.update(state.counts())
                write_status(status)
                state.render(DATA / "offers.html", "监控中" if status["state"]=="monitoring" else "已暂停")
            except Exception as exc:
                code = safe_error(exc)
                status.update(state="blocked" if isinstance(exc, ScopeError) else "retrying", error=code)
                write_status(status)
                state.render(DATA / "offers.html", "读取暂停，正在重试："+code)
                if isinstance(exc, ScopeError):
                    paused.set()
                # Credentials are never re-extracted. A restored saved file is re-read on the next attempt.
                if source:
                    source.reader.close()
                source = None
            stop.wait(15 if status["state"]=="monitoring" else 30)
    finally:
        if source:
            source.reader.close()
        status.update(state="stopped", **state.counts())
        write_status(status)
        state.render(DATA / "offers.html", "已停止；重新启动可继续读取离线期间的新消息")
        state.db.close()
        notifier.exit()


def worker_entry(notifier,stop,paused):
    try:
        run_worker(notifier,stop,paused)
    except Exception as exc:
        # Initial state/tray failures must not leave a hollow, unresponsive icon.
        try:
            write_status({"state":"startup_failed","error":safe_error(exc),"cycles":0,
                          "messages_processed":0,"email_enabled":False})
        finally:
            notifier.exit()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--notify-test", action="store_true")
    parser.add_argument("--read-once", action="store_true")
    args = parser.parse_args()
    DATA.mkdir(exist_ok=True)
    if args.read_once:
        sys.addaudithook(guard)
        state = State(DATA / "offers.sqlite3")
        source = RealSource()
        result = process_cycle(state, source)
        source.reader.close()
        result.update(state.counts(), single_group=True, external_transport=False)
        state.render(DATA / "offers.html", "单次读取已完成，持续监控尚未启动")
        state.db.close()
        print(json.dumps(result))
        return
    from notifier import Notifier
    sys.addaudithook(guard)
    stop, paused = threading.Event(), threading.Event()
    notifier = Notifier(DATA / "offers.html", stop, paused)
    if args.notify_test:
        def test():
            shown = notifier.send("蛋白粉监控 · 本机通知测试", "这是一条合成测试通知，不含真实群消息。只在这台电脑显示。")
            result = {"notification_api": "Shell_NotifyIconW", "shell_balloon_shown_callback": shown,
                      "synthetic_only": True, "external_transport": False}
            (DATA / "notification-test.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
            print(json.dumps(result), flush=True)
            stop.wait(5)
            notifier.exit()
        thread = threading.Thread(target=test, daemon=True)
    else:
        lock = instance_lock()
        (DATA / "stop.request").unlink(missing_ok=True)
        thread = threading.Thread(target=worker_entry, args=(notifier, stop, paused), daemon=False)
    try:
        # Start the worker after the native tray/message loop is ready. A failed
        # initialization then exits cleanly so the login bootstrap can retry.
        notifier.run(on_ready=thread.start)
    finally:
        stop.set()
        if thread.ident is not None:
            thread.join(35)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Do not print exception repr/tracebacks that may disclose paths or secrets.
        print(json.dumps({"error": safe_error(exc)}), flush=True)
        sys.exit(2)
