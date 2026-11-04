"""School/deal/team examples with fake sources, in-memory state and fake sends."""
import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch

import email_queue
import generic_rules
import matcher
import monitor
import settings
from outlook_mailer import GROUP_SHA256,SENDER,RECIPIENT,build_offer_mail
from state import State
from test_monitor import temporary,FakeSource,FakeNotifier,message

RULE={'id':'school','name':'学校通知','enabled':True,'any_keywords':['报名截止','停课通知'],
      'exclude_keywords':['已取消']}

class GenericRules(unittest.TestCase):
    def setUp(self):
        self.tmp=temporary()
        self.root=Path(self.tmp.name)
        self.rule_file=self.root/'rules.json'
        self.rule_file.write_text(json.dumps({'rules':[RULE]}),encoding='utf-8')
        self.profile=patch.dict(settings.SETTINGS,{'rule_profile':'generic'})
        self.path_patch=patch.object(generic_rules,'RULES_FILE',self.rule_file)
        self.profile.start(); self.path_patch.start()

    def tearDown(self):
        self.path_patch.stop(); self.profile.stop(); self.tmp.cleanup()

    def job(self,text='合成测试：报名截止为本周五，请提交材料。'):
        return {'offer':matcher.extract(text).data(),'source_ts':1001,'alert_id':'1-generic-synthetic',
                'group_id_sha256':GROUP_SHA256}

    def test_school_notification_does_not_require_a_product_or_price(self):
        offer=matcher.extract('教务通知：报名截止为本周五。')
        self.assertEqual(offer.trigger_kind,'rule')
        self.assertEqual(offer.rule_id,'school')
        self.assertEqual(offer.prices_yuan,[])

    def test_unrelated_or_excluded_messages_are_not_alerts(self):
        self.assertIsNone(matcher.extract('周末一起吃饭'))
        self.assertIsNone(matcher.extract('停课通知已取消'))

    def test_disabled_rules_never_match(self):
        rule=dict(RULE,enabled=False)
        self.assertIsNone(generic_rules.extract('报名截止',rules=[rule]))

    def test_first_matching_rule_wins(self):
        other=dict(RULE,id='second',name='另一规则')
        offer=generic_rules.extract('报名截止',rules=[RULE,other])
        self.assertEqual(offer.rule_id,'school')

    def test_nfkc_case_and_zero_width_are_normalized(self):
        rule=dict(RULE,any_keywords=['Deadline'])
        self.assertIsNotNone(generic_rules.extract('ＤＥＡＤ\u200bＬＩＮＥ',rules=[rule]))

    def test_excerpt_is_bounded_and_near_the_match(self):
        offer=matcher.extract('无关内容'*1000+'报名截止'+('其他内容'*1000))
        self.assertLessEqual(len(offer.keyword_excerpt),500)
        self.assertIn('报名截止',offer.keyword_excerpt)

    def test_rule_revision_changes_fingerprint(self):
        a=generic_rules.extract('报名截止',rules=[RULE])
        b=generic_rules.extract('报名截止',rules=[dict(RULE,name='新版通知')])
        self.assertNotEqual(a.fingerprint,b.fingerprint)

    def test_excerpt_preserves_case_and_removes_links_and_contact_lines(self):
        offer=matcher.extract('Class ABC：报名截止为周五。\nhttps://example.invalid/path?token=synthetic-only\n联系人：合成示例')
        self.assertIn('ABC',offer.keyword_excerpt)
        self.assertNotIn('https://',offer.keyword_excerpt)
        self.assertNotIn('synthetic-only',offer.keyword_excerpt)
        self.assertNotIn('联系人',offer.keyword_excerpt)

    def test_exclusion_is_checked_before_excerpt_cleanup(self):
        self.assertIsNone(matcher.extract('报名截止\nhttps://example.invalid/已取消'))

    def test_school_email_has_generic_subject_and_no_price_claim(self):
        subject,body=build_offer_mail(self.job())
        self.assertEqual(subject,'[群聊监控] 学校通知')
        self.assertIn('报名截止',body)
        self.assertNotIn('蛋白粉',subject+body)
        self.assertNotIn('@chatroom',body)

    def test_changed_or_disabled_rule_cannot_send_a_stale_job(self):
        job=self.job()
        self.rule_file.write_text(json.dumps({'rules':[dict(RULE,enabled=False)]}),encoding='utf-8')
        with self.assertRaises(ValueError):
            build_offer_mail(job)

    def test_rule_evidence_scope_and_profile_are_rechecked(self):
        for field,value in [('rule_signature','wrong'),('rule_name','wrong'),('keyword_excerpt','没有命中词')]:
            job=self.job(); job['offer'][field]=value
            with self.assertRaises(ValueError): build_offer_mail(job)
        job=self.job(); job['group_id_sha256']='other'
        with self.assertRaises(ValueError): build_offer_mail(job)
        with patch.dict(settings.SETTINGS,{'rule_profile':'protein_deals'}):
            with self.assertRaises(ValueError): build_offer_mail(self.job_for_profile_test())

    def job_for_profile_test(self):
        return {'offer':generic_rules.extract('报名截止').data(),'source_ts':1001,
                'alert_id':'1-generic-synthetic','group_id_sha256':GROUP_SHA256}

    def test_generic_profile_does_not_enable_the_protein_example(self):
        self.assertIsNone(matcher.extract('ON乳清蛋白粉5磅到手579元'))

    def test_generic_event_is_deduplicated_in_tray_and_mail_queue(self):
        state=State(self.root/'state.sqlite3')
        try:
            monitor.process_cycle(state,FakeSource({'fake':[]}),1000)
            monitor.process_cycle(state,FakeSource({'fake':[message(1,'报名截止'),message(2,'报名截止')]}),1002)
            self.assertEqual(state.counts()['offers'],1)
            notifier=FakeNotifier([True])
            self.assertTrue(monitor.deliver_one(state,notifier,1003))
            config=self.root/'mail.json'
            config.write_text(json.dumps({'enabled':True,'enabled_since':1000,'sender':SENDER,'recipient':RECIPIENT}))
            jobs=self.root/'jobs'; jobs.mkdir()
            calls=[]
            def transport(path):
                calls.append(path.name)
                path.with_suffix('.result.json').write_text(json.dumps({'submitted':True,'send_attempted':True}))
            with patch.object(email_queue,'CONFIG',config),patch.object(email_queue,'JOBS',jobs):
                email_queue.tick(state,now=1004,transport=transport)
                email_queue.tick(state,now=1005,transport=transport)
            self.assertEqual(len(calls),1)
        finally:
            state.db.close()

    def test_malformed_rules_fail_closed(self):
        for rule in [dict(RULE,any_keywords=[]),dict(RULE,name='name\nheader'),dict(RULE,enabled='yes'),dict(RULE,regex='.*')]:
            self.rule_file.write_text(json.dumps({'rules':[rule]}),encoding='utf-8')
            with self.assertRaises(ValueError): generic_rules.load_rules()

    def test_example_rules_are_all_disabled(self):
        rules=generic_rules.load_rules(settings.ROOT/'rules.example.json')
        self.assertTrue(rules)
        self.assertFalse(any(rule['enabled'] for rule in rules))

    def test_runtime_generic_configuration_requires_an_enabled_rule(self):
        path=self.root/'settings.json'
        value=dict(settings.DEFAULTS,chat_id='123456@chatroom',sender='sender@example.org',recipient='recipient@example.org')
        path.write_text(json.dumps(value),encoding='utf-8')
        self.assertEqual(settings.validate_runtime_settings(path)['rule_profile'],'generic')
        self.rule_file.write_text(json.dumps({'rules':[dict(RULE,enabled=False)]}),encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'enabled_rule'):
            settings.validate_runtime_settings(path)
