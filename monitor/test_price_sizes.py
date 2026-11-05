"""Synthetic size-specific price rules and delivery payloads; no real sends."""
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
from test_monitor import FakeSource, message, temporary


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
            "alert_id": "1-synthetic", "group_id_sha256": mailer.GROUP_SHA256}


class PriceSizes(unittest.TestCase):
    def test_on_564_strict_boundary_and_units(self):
        for unit in ("磅", "lb", "lbs"):
            for price in (599, 599.99):
                offer = deals.extract(f"ON乳清蛋白粉 单件5.64{unit} 到手{price}元")
                self.assertEqual(offer.match_status, "matched")
                self.assertEqual((offer.pounds, offer.threshold_yuan), (5.64, 600))
                self.assertEqual(offer.weight_g, 2558.26)
            for price in (600, 600.01, 699):
                self.assertIsNone(deals.extract(f"ON乳清蛋白粉 5.64{unit} 到手{price}元"))

    def test_original_thresholds_remain_size_specific(self):
        for brand, limit in (("ON", 580), ("熊猫", 400)):
            self.assertEqual(deals.extract(f"{brand}乳清蛋白粉 5磅 到手{limit-0.01}元").match_status, "matched")
            for price in (limit, limit+10):
                self.assertIsNone(deals.extract(f"{brand}乳清蛋白粉 5磅 到手{price}元"))

    def test_unsupported_ambiguous_and_multi_item_sizes_never_match(self):
        for text in ("熊猫乳清蛋白粉 5.64磅 到手399元", "ON乳清蛋白粉 5.6磅 到手599元",
                     "ON乳清蛋白粉 5.65磅 到手599元", "ON乳清蛋白粉 15.64磅 到手599元",
                     "ON乳清蛋白粉 5磅/5.64磅 到手579元", "ON乳清蛋白粉 5.64磅×2 到手599元",
                     "ON乳清蛋白粉 2.56kg 到手599元", "ON乳清蛋白粉 到手599元"):
            with self.subTest(text=text):
                offer = deals.extract(text)
                self.assertTrue(offer is None or offer.match_status != "matched")

    def test_final_cny_price_and_validity_still_required(self):
        for quote in ("到手599", "到手599美元", "到手约599元", "到手599元起", "到手599-699元"):
            offer = deals.extract("ON乳清蛋白粉 5.64磅 " + quote)
            self.assertTrue(offer is None or offer.match_status != "matched", quote)
        self.assertIsNone(deals.extract("ON乳清蛋白粉 5.64磅 原价799元，200元优惠券，券后600元"))
        offer = deals.extract("ON乳清蛋白粉 5.64磅 到手599元 截止2020-01-01", now=1790000000)
        self.assertEqual(offer.match_status, "expired")

    def test_keyword_rule_is_independent(self):
        offer = deals.extract("ON乳清蛋白粉 5.64磅 到手699元 超好忦")
        self.assertEqual((offer.trigger_kind, offer.match_status), ("keyword", "matched"))

    def test_mail_renders_actual_size_and_limit(self):
        subject, body = mailer.build_offer_mail(job("ON乳清蛋白粉 5.64磅 到手599.99元"), now=1003)
        self.assertIn("ON 5.64磅", subject)
        self.assertIn("600", subject)
        self.assertIn("599.99", subject)
        self.assertIn("规格：单件5.64磅", body)

    def test_mail_rechecks_size_weight_brand_threshold_and_price(self):
        for field, value in (("pounds", 5), ("pounds", None), ("pounds", "5.64"),
                             ("weight_g", 2267.96), ("weight_g", float("nan")),
                             ("brand", "熊猫"), ("threshold_yuan", 580), ("prices_yuan", [600])):
            with self.subTest(field=field, value=value):
                payload = job("ON乳清蛋白粉 5.64磅 到手599元")
                payload["offer"][field] = value
                with self.assertRaises(ValueError):
                    mailer.build_offer_mail(payload, now=1003)

    def test_preexisting_five_pound_jobs_remain_valid(self):
        payload = job("ON乳清蛋白粉 5磅 到手579元")
        payload["offer"].pop("pounds")
        self.assertIn("ON 5磅", mailer.build_offer_mail(payload, now=1003)[0])

    def test_new_size_flows_through_notification_and_durable_email_dedup(self):
        with temporary() as folder, ExitStack() as cleanup:
            root = Path(folder)
            config, jobs, database = root/"mail.json", root/"jobs", root/"state.sqlite3"
            config.write_text(json.dumps({"enabled": True, "enabled_since": 1000,
                                         "sender": mailer.SENDER, "recipient": mailer.RECIPIENT}))
            state = State(database)
            cleanup.callback(lambda: state.db.close())
            source = FakeSource({"synthetic": []})
            monitor.process_cycle(state, source, now=1000)
            source.streams["synthetic"] = [message(1, "ON乳清蛋白粉 5.64磅 到手599元"),
                                          message(2, "ON乳清蛋白粉 5.64磅 到手600元"),
                                          message(3, "ON乳清蛋白粉 5.64磅 到手599元")]
            monitor.process_cycle(state, source, now=1002)
            self.assertEqual(state.counts()["offers"], 1)
            notifications, submitted = [], []
            class Notifier:
                def send(self, title, body):
                    notifications.append((title, body))
                    return True
            monitor.deliver_one(state, Notifier(), now=1003)
            self.assertIn("5.64磅", notifications[0][1])
            def transport(path):
                payload = json.loads(path.read_text(encoding="utf-8"))
                submitted.append(mailer.build_offer_mail(payload, now=1003))
                path.with_suffix(".result.json").write_text(json.dumps({"send_attempted": True, "submitted": True}))
            with patch.object(queue, "CONFIG", config), patch.object(queue, "JOBS", jobs):
                queue.tick(state, 1003, transport)
                state.db.close()
                state = State(database)
                queue.tick(state, 1004, transport)
            self.assertEqual(len(submitted), 1)
            self.assertEqual(state.db.execute("SELECT state FROM email_outbox").fetchone()[0], "submitted")
