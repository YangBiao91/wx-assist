"""Transactional cursors and a local, durable notification outbox."""
import hashlib
import html
import json
import sqlite3
import time
from pathlib import Path

from matcher import extract


class State:
    def __init__(self, path):
        self.path = Path(path)
        self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        PRAGMA journal_mode=DELETE;
        PRAGMA synchronous=FULL;
        CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sources(id TEXT PRIMARY KEY, cursor INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS seen(id TEXT PRIMARY KEY, ts REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS offers(
          id INTEGER PRIMARY KEY, fingerprint TEXT NOT NULL, data TEXT NOT NULL,
          created REAL NOT NULL, source_ts INTEGER NOT NULL, last_seen REAL NOT NULL,
          occurrences INTEGER NOT NULL DEFAULT 1, delivery TEXT NOT NULL DEFAULT 'pending',
          attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0);
        CREATE INDEX IF NOT EXISTS offer_fingerprint ON offers(fingerprint,last_seen);
        """)

    def meta(self, key, default=None):
        row = self.db.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
        return row[0] if row else default

    def cursor(self, source):
        row = self.db.execute("SELECT cursor FROM sources WHERE id=?", (source,)).fetchone()
        return row[0] if row else None

    def baseline(self, batches, now):
        if self.meta("started") is not None:
            return
        with self.db:
            for source, maximum, _ in batches:
                self.db.execute("INSERT OR REPLACE INTO sources VALUES(?,?)", (source, maximum))
            self.db.execute("INSERT INTO meta VALUES('started',?)", (str(now),))

    def ingest(self, source, maximum, rows, now=None, fault=None):
        now = time.time() if now is None else now
        start = float(self.meta("started", str(now)))
        cursor = self.cursor(source)
        new_source = cursor is None or cursor > maximum
        cursor = 0 if new_source else cursor
        count = 0
        with self.db:
            for row in rows:
                local_id, server_id, kind, stamp, text = row
                identity = "server:"+str(server_id) if server_id else repr((source, local_id, stamp, hashlib.sha256(text.encode()).hexdigest()))
                digest = hashlib.sha256(identity.encode()).hexdigest()
                if (not new_source or stamp >= int(start)) and self.db.execute("INSERT OR IGNORE INTO seen VALUES(?,?)", (digest, now)).rowcount:
                    offer = extract(text, now=now, reference_ts=stamp) if kind == 1 else None
                    if offer:
                        previous = self.db.execute("SELECT id FROM offers WHERE fingerprint=? AND last_seen>=? ORDER BY id DESC LIMIT 1", (offer.fingerprint, now-72*3600)).fetchone()
                        if previous:
                            self.db.execute("UPDATE offers SET last_seen=?,occurrences=occurrences+1 WHERE id=?", (now, previous[0]))
                        else:
                            delivery = "pending" if offer.match_status=="matched" else offer.match_status
                            # Email tombstones refer to these IDs. Preserve a monotonic
                            # sequence even after every old offer has been purged.
                            latest = self.db.execute("SELECT COALESCE(MAX(id),0) FROM offers").fetchone()[0]
                            next_id = max(latest,int(self.meta("offer_sequence","0")))+1
                            self.db.execute("INSERT INTO offers(id,fingerprint,data,created,source_ts,last_seen,delivery) VALUES(?,?,?,?,?,?,?)", (next_id,offer.fingerprint, json.dumps(offer.data(), ensure_ascii=False), now, stamp, now, delivery))
                            self.db.execute("INSERT OR REPLACE INTO meta VALUES('offer_sequence',?)",(str(next_id),))
                            count += 1
                cursor = max(cursor, local_id)
            if fault:
                fault()
            # Never skip an unread batch; max can be much larger than the batch.
            self.db.execute("INSERT OR REPLACE INTO sources VALUES(?,?)", (source, cursor))
            self.db.execute("DELETE FROM seen WHERE ts<?", (now-30*86400,))
            self.db.execute("DELETE FROM offers WHERE last_seen<? AND delivery!='pending'", (now-30*86400,))
        return count

    def pending(self, now):
        return self.db.execute("SELECT * FROM offers WHERE delivery='pending' AND next_attempt<=? ORDER BY id LIMIT 1", (now,)).fetchone()

    def delivered(self, offer_id, shown, now):
        with self.db:
            self.db.execute("UPDATE offers SET delivery=?,attempts=attempts+1,next_attempt=? WHERE id=?",
                            ("shown" if shown else "pending", now+(300 if not shown else 0), offer_id))

    def counts(self):
        return {"offers": self.db.execute("SELECT COUNT(*) FROM offers").fetchone()[0],
                "pending": self.db.execute("SELECT COUNT(*) FROM offers WHERE delivery='pending'").fetchone()[0],
                "needs_review": self.db.execute("SELECT COUNT(*) FROM offers WHERE delivery='needs_review'").fetchone()[0]}

    def render(self, path, status):
        escape=html.escape
        cards=[]
        for row in self.db.execute("SELECT * FROM offers ORDER BY id DESC LIMIT 100"):
            data=json.loads(row["data"])
            kind=data.get("trigger_kind","price")
            if kind=="rule":
                detail="规则："+data["rule_name"]+"；命中词："+"、".join(data["matched_keywords"])
                excerpt=data["keyword_excerpt"]
            elif kind=="keyword":
                detail="示例规则命中："+"、".join(data.get("matched_keywords",[]))
                excerpt=data.get("keyword_excerpt","")
            else:
                prices=" / ".join(f"¥{value:g}" for value in data["prices_yuan"])
                detail=f"价格示例：{data['brand']}；报价 {prices}；状态 {data.get('match_status','')}"
                excerpt="；".join(data.get("reasons",[])+data.get("conditions",[]))
            label={"shown":"已显示通知","pending":"通知待显示","needs_review":"待核未通知","expired":"已过期"}.get(row["delivery"],"待核")
            stamp=time.strftime("%Y-%m-%d %H:%M:%S",time.localtime(row["source_ts"]))
            cards.append(f'<article><h2>{escape(data["title"])}</h2><p>{escape(detail)}</p><p>{escape(excerpt)}</p><small>{escape(stamp)} · 重复 {row["occurrences"]} 次 · {escape(label)}</small></article>')
        content=('<!doctype html><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'">'
                 '<title>群聊自动监控与通知</title><style>body{font:16px system-ui;max-width:850px;margin:40px auto;padding:0 20px;background:#f5f6f3;color:#17352b}article{background:white;padding:20px;margin:16px 0;border-radius:12px}h2{font-size:19px}small{color:#65776d}</style>'
                 '<h1>群聊自动监控与通知</h1><p>只处理明确配置的一个群。匹配结果请回到原群核对；文字片段不等于完整通知或事实核验。</p>'
                 f'<p>状态：{escape(status)} · 刷新页面查看更新 · 托盘右键可暂停或退出</p>'
                 +(''.join(cards) or '<article>暂无新命中；首次运行前的历史消息不回扫。</article>'))
        target=Path(path)
        temporary=target.with_suffix(".tmp")
        temporary.write_text(content,encoding="utf-8")
        temporary.replace(target)
