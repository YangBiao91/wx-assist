"""Synthetic keyword examples; no real chat export or real email sends."""
import json
import unittest
from unittest.mock import patch

import deals
import email_queue
import monitor
from outlook_mailer import GROUP_SHA256,build_offer_mail
from state import State
from test_monitor import temporary,message

SAMPLE=("操作示例：领券、淘金币、购物车凑单，再退掉凑单。非88vip有额外说明。\n"
        "诺特兰德，红桶pro乳清蛋白粉\n5磅 362元（超好忦）\n"
        "送5条能量肌酸+摇杯。这是新鲜日期的。")


def job(text=SAMPLE):
    return {"offer":deals.extract(text,now=1002,reference_ts=1001).data(),"source_ts":1001,
            "alert_id":"1-keyword-synthetic","group_id_sha256":GROUP_SHA256}


class KeywordMatching(unittest.TestCase):
    def test_no_independent_nutrend_product_or_362_price_rule(self):
        self.assertIsNone(deals.extract("诺特兰德红桶PRO乳清蛋白粉5磅362元"))
        self.assertEqual(deals.extract("诺特兰德红桶PRO乳清蛋白粉5磅999元 超好忦").trigger_kind,"keyword")

    def test_actual_group_glyph_is_U5FE6(self):
        self.assertEqual(ord("忦"),0x5FE6)
        offer=deals.extract(SAMPLE)
        self.assertEqual(offer.trigger_kind,"keyword")
        self.assertEqual(offer.matched_keywords,["超好忦"])
        self.assertIn("362元",offer.keyword_excerpt)
        self.assertIn("诺特兰德",offer.title)
        self.assertIsNone(offer.threshold_yuan)

    def test_normal_traditional_observed_and_split_glyph_forms(self):
        for phrase in ("超好价","超好忦","超好價","超 好 忦","超好讠介","超好言介","超\u200b好忦"):
            with self.subTest(phrase=phrase):
                self.assertEqual(deals.extract("群内报价："+phrase).trigger_kind,"keyword")

    def test_nearby_unrelated_words_are_not_keywords(self):
        for phrase in ("超好吃","超好喝","超好用","超好评","超低价","好价"):
            self.assertEqual(deals.keyword_matches(phrase),[])
            self.assertIsNone(deals.extract(phrase))

    def test_keyword_has_no_brand_size_currency_or_price_requirement(self):
        for text in ("其他品牌肌酸300g 到手999元 超好忦", "超好价，价格未写", "ON乳清蛋白粉2磅 999元 超好忦", "熊猫乳清蛋白粉5磅400元 超好价"):
            with self.subTest(text=text):
                offer=deals.extract(text)
                self.assertEqual(offer.trigger_kind,"keyword")
                self.assertEqual(offer.match_status,"matched")

    def test_original_price_boundaries_remain_strict_without_keyword(self):
        self.assertIsNone(deals.extract("ON乳清蛋白粉5磅580元"))
        self.assertIsNone(deals.extract("熊猫乳清蛋白粉5磅400元"))
        self.assertEqual(deals.extract("ON乳清蛋白粉5磅579元").trigger_kind,"price")

    def test_aliases_and_coupon_password_changes_deduplicate(self):
        a=deals.extract("诺特兰德乳清蛋白粉\n5磅362元 超好忦\n复制￥ABCDEFG123￥")
        b=deals.extract("诺特兰德乳清蛋白粉\n5磅362元 超好价\n复制￥ZZZZZZ9999￥")
        self.assertEqual(a.fingerprint,b.fingerprint)
        self.assertNotIn("ABCDEFG123",json.dumps(a.data()))

    def test_non_vip_steps_not_mislabeled_as_requiring_vip(self):
        offer=deals.extract(SAMPLE)
        self.assertNotIn("88VIP",offer.conditions)
        self.assertIn("含非88VIP操作说明",offer.conditions)
        self.assertIn("凑单",offer.conditions)
        self.assertIn("退凑单",offer.conditions)
        self.assertIn("淘金币",offer.conditions)


class KeywordMail(unittest.TestCase):
    def test_message_is_labeled_keyword_not_price_eligibility(self):
        subject,body=build_offer_mail(job(),now=1003)
        self.assertIn("关键词",subject)
        self.assertIn("超好忦",body)
        self.assertIn("5磅 362元",body)
        self.assertIn("不表示已通过",body)
        self.assertNotIn("严格低于 None",body)

    def test_missing_keyword_evidence_is_rejected(self):
        value=job()
        value["offer"]["keyword_excerpt"]="普通商品促销，没有触发词"
        with self.assertRaisesRegex(ValueError,"keyword_evidence"):
            build_offer_mail(value,1003)

    def test_wrong_group_or_unknown_trigger_is_rejected(self):
        value=job()
        value["group_id_sha256"]="other-group"
        with self.assertRaisesRegex(ValueError,"group_scope"):
            build_offer_mail(value,1003)
        value=job()
        value["offer"]["trigger_kind"]="arbitrary-mail"
        with self.assertRaisesRegex(ValueError,"unknown_alert_trigger"):
            build_offer_mail(value,1003)

    def test_one_keyword_offer_enters_native_and_email_outboxes_once(self):
        with temporary() as directory:
            from pathlib import Path
            root=Path(directory)
            state=State(root/"state.sqlite3")
            try:
                state.baseline([("synthetic",0,[])],1000)
                state.ingest("synthetic",2,[message(1,SAMPLE),message(2,SAMPLE)],1002)
                self.assertEqual(state.counts()["offers"],1)
                notifications=[]
                class Notifier:
                    def send(self,title,text):
                        notifications.append(text)
                        return True
                self.assertTrue(monitor.deliver_one(state,Notifier(),1003))
                self.assertIn("关键词",notifications[0])
                self.assertNotIn("< ¥None",notifications[0])
                config=root/"config.json"
                config.write_text(json.dumps({"enabled":True,"enabled_since":1000,"sender":"sender@example.invalid","recipient":"recipient@example.invalid"}))
                sent=[]
                def transport(path):
                    payload=json.loads(path.read_text(encoding="utf-8"))
                    sent.append(build_offer_mail(payload,1004))
                    path.with_suffix(".result.json").write_text(json.dumps({"send_attempted":True,"submitted":True}))
                with patch.object(email_queue,"CONFIG",config),patch.object(email_queue,"JOBS",root/"jobs"):
                    email_queue.tick(state,1004,transport)
                    email_queue.tick(state,1005,transport)
                self.assertEqual(len(sent),1)
            finally:
                state.db.close()


# Exercise the legacy example profile explicitly; the deployed default is generic.
def setUpModule():
    import settings
    global profile_patch
    profile_patch=patch.dict(settings.SETTINGS,{"rule_profile":"protein_deals"})
    profile_patch.start()

def tearDownModule():
    profile_patch.stop()


if __name__=="__main__":
    unittest.main(verbosity=2)
