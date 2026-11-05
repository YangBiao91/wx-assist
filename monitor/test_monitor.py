"""Synthetic only: extraction, scope, transactional recovery and local outbox."""
import hashlib
import json
import tempfile
import unittest
import sqlite3
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import deals
import monitor
from state import State

TEST_ROOT = Path(__file__).parent / "synthetic-tests"
TEST_ROOT.mkdir(exist_ok=True)


def temporary():
    # Keep fixtures inside the approved workspace, not Windows sandbox Temp.
    return tempfile.TemporaryDirectory(dir=TEST_ROOT)


class FakeSource:
    def __init__(self, streams):
        self.streams = streams
        self.fail = False
        self.acked = 0

    def collect(self, state, baseline=False):
        if self.fail:
            raise OSError("synthetic unavailable")
        batches = []
        for identity, stream in self.streams.items():
            maximum = max((row[0] for row in stream), default=0)
            cursor = state.cursor(identity)
            cursor = 0 if cursor is None or cursor > maximum else cursor
            rows = [] if baseline else [r for r in stream if r[0] > cursor][:250]
            batches.append((identity, maximum, rows))
        return batches

    def acknowledge(self):
        self.acked += 1


class FakeNotifier:
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.calls = 0

    def send(self, title, message):
        self.calls += 1
        return next(self.outcomes)


def message(n, text="合成ON乳清蛋白粉 5磅 到手199元", stamp=1001, server=None):
    return (n, server if server is not None else n+1000, 1, stamp, text)


class Parsing(unittest.TestCase):
    def test_final_price_not_coupon_or_original(self):
        offer = deals.extract("合成ON乳清蛋白粉 5磅 原价699元\n70元淘礼金，88VIP券后579元")
        self.assertEqual(offer.prices_yuan, [579])
        self.assertAlmostEqual(offer.weight_g, 2267.96, places=2)
        self.assertIn("88VIP", offer.conditions)
        self.assertIn("领券", offer.conditions)

    def test_general_quote_and_coupon_filter(self):
        offer = deals.extract("合成ON乳清蛋白粉 5磅\n补贴70元，199元好价")
        self.assertEqual(offer.prices_yuan, [199])

    def test_multiple_sizes_no_misleading_unit_price(self):
        offer = deals.extract("合成ON乳清蛋白粉 2磅 到手199元，5磅 到手399元")
        self.assertEqual(offer.prices_yuan, [199,399])
        self.assertIsNone(offer.yuan_per_kg)

    def test_multipack_is_ambiguous(self):
        self.assertIsNone(deals.extract("ON乳清蛋白粉 5磅×2 到手299元").yuan_per_kg)

    def test_false_positives(self):
        for text in ("肌酸 ON 300g 到手59元", "蛋白粉摇摇杯 优惠19元", "乳清蛋白棒 优惠9元", "求推荐蛋白粉，哪里买有优惠？", "今天吃了乳清蛋白粉"):
            self.assertIsNone(deals.extract(text), text)

    def test_rotated_coupon_password_deduplicates(self):
        a = deals.extract("合成ON乳清蛋白粉 5磅 到手199元\n复制 ￥ABcdef123￥")
        b = deals.extract("合成ON乳清蛋白粉 5磅 到手199元\n复制 ￥zzZZ987654￥")
        self.assertEqual(a.fingerprint, b.fingerprint)
        self.assertNotIn("ABcdef", json.dumps(a.data()))

    def test_links_are_public_item_ids_only(self):
        text = "合成ON乳清蛋白粉 5磅 到手199元 https://detail.tmall.com/item.htm?id=123456&spm=secret&token=private"
        offer = deals.extract(text)
        self.assertEqual(offer.link, "https://detail.tmall.com/item.htm?id=123456")
        self.assertEqual(deals.public_link("https://evil.invalid/?id=1"), "")
        self.assertEqual(deals.public_link("https://t.cn/PRIVATE"), "")

    def test_malformed_and_oversized(self):
        self.assertIsNone(deals.extract(None))
        self.assertIsNone(deals.extract("乳清蛋白粉 到手99元"+"x"*100_000))
        self.assertEqual(deals.public_link("http://item.jd.com:bad/1.html"), "")

    def test_on_strict_boundary(self):
        self.assertIsNone(deals.extract("ON乳清蛋白粉5磅 到手580元"))
        self.assertEqual(deals.extract("ON乳清蛋白粉5磅 到手579.99元").match_status,"matched")
        self.assertIsNone(deals.extract("ON乳清蛋白粉5磅 到手580.01元"))

    def test_panda_strict_boundary(self):
        self.assertIsNone(deals.extract("熊猫乳清蛋白粉5磅 到手400元"))
        self.assertEqual(deals.extract("熊猫乳清蛋白粉5磅 到手399.99元").match_status,"matched")
        self.assertIsNone(deals.extract("熊猫乳清蛋白粉5磅 到手400.01元"))

    def test_only_explicit_target_brand(self):
        for text in ("康比特乳清蛋白粉5磅 到手199元", "Myprotein protein powder5lb 到手199元", "ON肌酸 5磅 到手199元"):
            self.assertIsNone(deals.extract(text))
        self.assertEqual(deals.extract("Optimum Nutrition Whey 5 lbs 实付CNY579").brand,"ON")

    def test_size_must_be_explicit_and_supported(self):
        for size in ("2磅","5.6lb","10磅","1kg"):
            self.assertIsNone(deals.extract(f"ON乳清蛋白粉 {size} 到手199元"))
        for size in ("2.27kg","5磅+2磅","5磅×2",""):
            self.assertEqual(deals.extract(f"ON乳清蛋白粉 {size} 到手199元").match_status,"needs_review")
        self.assertEqual(deals.extract("ON乳清蛋白粉5.0lbs 券后¥579").match_status,"matched")

    def test_currency_and_uncertain_prices_never_match(self):
        for quote in ("到手579", "到手USD79", "到手79美元", "到手约579元", "到手579-599元", "到手579元起", "返利后折合579元"):
            self.assertEqual(deals.extract("ON乳清蛋白粉5磅 "+quote).match_status,"needs_review",quote)

    def test_coupon_is_not_final_price(self):
        self.assertIsNone(deals.extract("ON乳清蛋白粉5磅 原价799元，200元淘礼金，券后599元"))
        self.assertEqual(deals.extract("熊猫乳清蛋白粉5磅 100元优惠券，实付399元").prices_yuan,[399])

    def test_multi_brand_or_multi_quote_needs_review(self):
        self.assertEqual(deals.extract("ON和熊猫乳清蛋白粉5磅 到手399元").match_status,"needs_review")
        self.assertEqual(deals.extract("ON乳清蛋白粉5磅，到手499元或到手579元").match_status,"needs_review")

    def test_expiry_and_relative_day_use_message_date(self):
        now = datetime(2026,11,3,12).timestamp()
        expired = deals.extract("ON乳清蛋白粉5磅 到手579元 截止2026-11-03 11:59",now=now)
        self.assertEqual(expired.match_status,"expired")
        future = deals.extract("熊猫乳清蛋白粉5磅 到手399元 有效期至2026-11-03 23:59",now=now)
        self.assertEqual(future.match_status,"matched")
        self.assertGreater(future.expiry_ts,now)
        delayed = deals.extract("ON乳清蛋白粉5磅 到手579元 截止今天23:59",now=now+86400,reference_ts=now)
        self.assertEqual(delayed.match_status,"expired")

    def test_bad_date_needs_review(self):
        self.assertEqual(deals.extract("ON乳清蛋白粉5磅 到手579元 截止2026-13-35").match_status,"needs_review")


class Recovery(unittest.TestCase):
    def setUp(self):
        self.tmp = temporary()
        self.path = Path(self.tmp.name) / "state.sqlite3"
        self.state = State(self.path)
        self.source = FakeSource({"message_0:salt": [message(1, stamp=900)]})
        monitor.process_cycle(self.state, self.source, now=1000)

    def tearDown(self):
        self.state.db.close()
        self.tmp.cleanup()

    def test_baseline_skips_history_then_reads_new(self):
        self.assertEqual(self.state.counts()["offers"], 0)
        self.source.streams["message_0:salt"].append(message(2))
        result = monitor.process_cycle(self.state, self.source, now=1002)
        self.assertEqual(result["new_offers"], 1)
        monitor.process_cycle(self.state, self.source, now=1003)
        self.assertEqual(self.state.counts()["offers"], 1)

    def test_unavailable_source_does_not_advance_cursor(self):
        self.source.streams["message_0:salt"].append(message(2))
        self.source.fail = True
        with self.assertRaises(OSError):
            monitor.process_cycle(self.state, self.source, now=1002)
        self.assertEqual(self.state.cursor("message_0:salt"), 1)
        self.source.fail = False
        self.assertEqual(monitor.process_cycle(self.state, self.source, now=1003)["new_offers"], 1)

    def test_failure_rolls_back_cursor_offer_and_dedup(self):
        def fault():
            raise RuntimeError("synthetic crash before commit")
        with self.assertRaises(RuntimeError):
            self.state.ingest("message_0:salt", 2, [message(2)], now=1002, fault=fault)
        self.assertEqual(self.state.cursor("message_0:salt"), 1)
        self.assertEqual(self.state.counts()["offers"], 0)
        self.assertEqual(self.state.ingest("message_0:salt", 2, [message(2)], now=1003), 1)

    def test_uncommitted_read_is_not_acknowledged(self):
        self.source.streams["message_0:salt"].append(message(2))
        acknowledgements = self.source.acked
        with patch.object(self.state,"ingest",side_effect=sqlite3.OperationalError("synthetic write failure")):
            with self.assertRaises(sqlite3.OperationalError):
                monitor.process_cycle(self.state,self.source,now=1002)
        self.assertEqual(self.source.acked,acknowledgements)
        self.assertEqual(monitor.process_cycle(self.state,self.source,now=1003)["new_offers"],1)

    def test_restart_catches_backlog_and_keeps_pending(self):
        self.state.ingest("message_0:salt", 2, [message(2)], now=1002)
        self.state.db.close()
        self.state = State(self.path)
        self.source.streams["message_0:salt"].extend([message(2), message(3, "合成ON分离乳清蛋白粉 5磅 到手299元")])
        monitor.process_cycle(self.state, self.source, now=1005)
        self.assertEqual(self.state.counts(), {"offers":2,"pending":2,"needs_review":0})
        self.assertEqual(self.state.cursor("message_0:salt"), 3)

    def test_rollover_dedups_server_and_offer(self):
        self.state.ingest("message_0:salt", 2, [message(2)], now=1002)
        self.source.streams["message_1:newsalt"] = [message(1, stamp=200), message(2), message(3)]
        monitor.process_cycle(self.state, self.source, now=1006)
        self.assertEqual(self.state.counts()["offers"], 1)
        self.assertEqual(self.state.db.execute("SELECT occurrences FROM offers").fetchone()[0], 2)

    def test_reset_local_id_same_file_and_salt_recovers(self):
        self.state.ingest("message_0:salt", 100, [message(100)], now=1002)
        self.source.streams["message_0:salt"] = [message(1, "合成ON分离乳清蛋白粉 5磅 到手299元", server=9999)]
        monitor.process_cycle(self.state, self.source, now=1006)
        self.assertEqual(self.state.counts()["offers"], 2)
        self.assertEqual(self.state.cursor("message_0:salt"), 1)

    def test_large_backlog_does_not_jump_to_max(self):
        self.source.streams["message_0:salt"] += [message(n) for n in range(2,600)]
        monitor.process_cycle(self.state, self.source, now=1002)
        self.assertEqual(self.state.cursor("message_0:salt"), 251)
        monitor.process_cycle(self.state, self.source, now=1003)
        monitor.process_cycle(self.state, self.source, now=1004)
        self.assertEqual(self.state.cursor("message_0:salt"), 599)

    def test_notify_failure_survives_restart_then_delivers(self):
        self.state.ingest("message_0:salt", 2, [message(2)], now=1002)
        notifier = FakeNotifier([False,True])
        self.assertFalse(monitor.deliver_one(self.state, notifier, now=1002))
        self.state.db.close()
        self.state = State(self.path)
        self.assertIsNone(monitor.deliver_one(self.state, notifier, now=1003))
        self.assertTrue(monitor.deliver_one(self.state, notifier, now=1303))
        self.assertEqual(self.state.counts()["pending"], 0)
        self.assertEqual(notifier.calls, 2)

    def test_changed_price_or_conditions_alert_again(self):
        self.state.ingest("message_0:salt", 2, [message(2)], now=1002)
        rows = [message(3, "合成ON乳清蛋白粉 5磅 到手189元"), message(4, "合成ON乳清蛋白粉 5磅 到手199元 88VIP")]
        self.assertEqual(self.state.ingest("message_0:salt", 4, rows, now=1003), 2)

    def test_offer_ids_are_not_reused_after_retention_cleanup(self):
        self.state.ingest("message_0:salt",2,[message(2)],now=1002)
        old_id=self.state.db.execute("SELECT id FROM offers").fetchone()[0]
        with self.state.db:
            self.state.db.execute("DELETE FROM offers")
        self.state.ingest("message_0:salt",3,[message(3,"熊猫乳清蛋白粉5磅 到手399元")],now=1003)
        new_id=self.state.db.execute("SELECT id FROM offers").fetchone()[0]
        self.assertGreater(new_id,old_id)

    def test_html_escapes_untrusted_message(self):
        self.state.ingest("message_0:salt", 2, [message(2, 'ON乳清蛋白粉5磅 到手199元 <script>alert(1)</script>')], now=1002)
        report = Path(self.tmp.name) / "offers.html"
        self.state.render(report, "合成测试")
        self.assertNotIn("<script>", report.read_text(encoding="utf-8"))
        self.assertIn("&lt;script&gt;", report.read_text(encoding="utf-8"))

    def test_uncertain_and_other_brands_never_reach_notifier(self):
        rows = [message(2,"ON乳清蛋白粉5磅 到手579"),message(3,"熊猫乳清蛋白粉2.27kg 实付399元"),message(4,"其他品牌乳清蛋白粉5磅 实付99元")]
        self.state.ingest("message_0:salt",4,rows,now=1002)
        notifier = FakeNotifier([])
        self.assertIsNone(monitor.deliver_one(self.state,notifier,now=1003))
        self.assertEqual(notifier.calls,0)
        self.assertEqual(self.state.counts()["needs_review"],2)

    def test_offer_expired_while_notification_was_pending(self):
        self.state.ingest("message_0:salt",2,[message(2)],now=1002)
        data = json.loads(self.state.db.execute("SELECT data FROM offers").fetchone()[0])
        data["expiry_ts"] = 1100
        with self.state.db:
            self.state.db.execute("UPDATE offers SET data=?",(json.dumps(data),))
        notifier = FakeNotifier([])
        self.assertIsNone(monitor.deliver_one(self.state,notifier,now=1101))
        self.assertEqual(notifier.calls,0)
        self.assertEqual(self.state.counts()["pending"],0)


class Boundaries(unittest.TestCase):
    def test_scope_cannot_be_broadened(self):
        with temporary() as tmp:
            base = Path(tmp)
            (base/"single-group-scope.json").write_text(json.dumps({"chat_id":"other@chatroom","auto_send_enabled":False}))
            with patch.object(monitor, "BUILD", base), self.assertRaises(monitor.ScopeError):
                monitor.scope_id()

    def test_missing_credential_fails_without_extraction(self):
        with temporary() as tmp:
            base = Path(tmp)
            (base/"dist").mkdir()
            (base/"dist/.env").write_text("WCDB_KEY=\nDB_PATH=invalid\n")
            with patch.object(monitor, "BUILD", base), patch.object(monitor,"CREDENTIAL_FILE",base/"dist/.env"), patch.object(monitor,"scope_id",return_value="synthetic@chatroom"), patch.object(monitor,"load_core") as loader:
                with self.assertRaisesRegex(RuntimeError, "saved_credential_unavailable"):
                    monitor.RealSource()
                loader.assert_not_called()

    def test_network_and_key_dll_disabled(self):
        for event, args in (("socket.connect", ()), ("socket.bind", ()), ("socket.getaddrinfo", ()), ("ctypes.dlopen", ("wx_key.dll",))):
            with self.assertRaises(RuntimeError):
                monitor.guard(event,args)

    def test_read_phase_write_blocked(self):
        with monitor.read_phase():
            with self.assertRaises(RuntimeError):
                monitor.guard("open", ("synthetic", "w", 0))
        monitor.guard("open", ("synthetic", "w", 0))


# Exercise the legacy example profile explicitly; the deployed default is generic.
def setUpModule():
    import settings
    global profile_patch
    profile_patch=patch.dict(settings.SETTINGS,{"rule_profile":"protein_deals"})
    profile_patch.start()

def tearDownModule():
    profile_patch.stop()


if __name__ == "__main__":
    unittest.main(verbosity=2)
