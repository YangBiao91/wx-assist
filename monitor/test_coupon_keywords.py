"""Synthetic must-buy coupon alerts; no chat exports or real mail transport."""
import json
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import deals
import email_queue as queue
import monitor
import outlook_mailer as mailer
from state import State
from test_monitor import message, temporary


def setUpModule():
    global profile
    profile = patch.dict(mailer.SETTINGS, {"rule_profile": "protein_deals"}) if hasattr(mailer, "SETTINGS") else None
    if profile:
        profile.start()


def tearDownModule():
    if profile:
        profile.stop()


def job(text):
    return {"offer": deals.extract(text, now=1002).data(), "source_ts": 1001,
            "alert_id": "1-coupon-synthetic", "group_id_sha256": mailer.GROUP_SHA256}


class CouponKeywords(unittest.TestCase):
    def test_coupon_keyword_is_independent_of_product_price_and_size(self):
        for text in ("合成测试：抵用券（必买！）", "预定券（必买！）", "必买的优惠券本周可用",
                     "预订券\n必 买", "这张券必\u200b买", "ON乳清蛋白粉 5磅 到手699元，优惠券必买"):
            with self.subTest(text=text):
                offer = deals.extract(text)
                self.assertEqual((offer.trigger_kind, offer.match_status), ("keyword", "matched"))
                self.assertIn("必买", offer.matched_keywords)
                self.assertIsNone(offer.threshold_yuan)
                subject, body = mailer.build_offer_mail(job(text), now=1003)
                self.assertIn("必买", subject)
                self.assertIn("券", body)

    def test_coupon_must_be_the_recommended_object(self):
        for text in ("这个产品必买", "优惠券补货了", "必看：领券", "必卖优惠券"):
            self.assertEqual(deals.keyword_matches(text), [])
            self.assertIsNone(deals.extract(text))
        self.assertEqual(deals.extract("超好忦").trigger_kind, "keyword")

    def test_bounded_excerpt_preserves_coupon_evidence_on_another_line(self):
        text = "合成说明"*150 + "抵用券\n（必买！）" + "更多说明"*150
        offer = deals.extract(text)
        self.assertLessEqual(len(offer.keyword_excerpt), 240)
        self.assertIn("必买", deals.keyword_matches(offer.keyword_excerpt))
        payload = job(text)
        payload["offer"]["keyword_excerpt"] = "这个产品必买"
        with self.assertRaisesRegex(ValueError, "keyword_evidence"):
            mailer.build_offer_mail(payload, now=1003)

    def test_unrelated_coupon_and_product_recommendations_do_not_match(self):
        for text in ("优惠券已领，蛋白粉必买", "这款蛋白粉必买！\n另有优惠券待领取",
                     "优惠券\nON蛋白粉必买", "必买的蛋白粉，请领优惠券",
                     "抵用券\n"+"合成说明"*150+"必买！"):
            with self.subTest(text=text):
                self.assertEqual(deals.keyword_matches(text), [])
                offer = deals.extract(text)
                self.assertTrue(offer is None or offer.match_status != "matched")

    def test_negated_coupon_recommendations_do_not_match(self):
        for text in ("这张券不是必买", "不是必买的券", "不必买优惠券", "这张券不推荐必买"):
            self.assertNotIn("必买", deals.keyword_matches(text))

    def test_mail_rejects_unrelated_terms_in_saved_evidence(self):
        payload = job("合成测试：抵用券（必买！）")
        payload["offer"]["keyword_excerpt"] = "优惠券已领，蛋白粉必买"
        with self.assertRaisesRegex(ValueError, "keyword_evidence"):
            mailer.build_offer_mail(payload, now=1003)

    def test_coupon_password_changes_do_not_create_duplicate_alerts(self):
        first = deals.extract("合成测试：优惠券（必买！）\n复制 ￥ABCD1234￥")
        second = deals.extract("合成测试：优惠券（必买！）\n复制 ￥EFGH5678￥")
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertNotIn("ABCD1234", json.dumps(first.data()))

    def test_native_and_durable_email_queue_send_one_synthetic_alert(self):
        with temporary() as folder, ExitStack() as cleanup:
            root = Path(folder)
            config, jobs, database = root/"mail.json", root/"jobs", root/"state.sqlite3"
            config.write_text(json.dumps({"enabled": True, "enabled_since": 1000,
                                         "sender": mailer.SENDER, "recipient": mailer.RECIPIENT}))
            state = State(database)
            cleanup.callback(lambda: state.db.close())
            state.baseline([("synthetic", 0, [])], 1000)
            text = "合成测试：优惠券（必买！）"
            state.ingest("synthetic", 2, [message(1, text), message(2, text)], 1002)
            self.assertEqual(state.counts()["offers"], 1)
            notifications, submitted = [], []
            class Notifier:
                def send(self, title, body):
                    notifications.append(body)
                    return True
            self.assertTrue(monitor.deliver_one(state, Notifier(), 1003))
            self.assertIn("必买", notifications[0])
            def transport(path):
                payload = json.loads(path.read_text(encoding="utf-8"))
                submitted.append(mailer.build_offer_mail(payload, 1003))
                path.with_suffix(".result.json").write_text(json.dumps({"send_attempted": True, "submitted": True}))
            with patch.object(queue, "CONFIG", config), patch.object(queue, "JOBS", jobs):
                queue.tick(state, 1003, transport)
                state.db.close()
                state = State(database)
                queue.tick(state, 1004, transport)
            self.assertEqual(len(submitted), 1)
            self.assertEqual(state.db.execute("SELECT state FROM email_outbox").fetchone()[0], "submitted")
