"""Synthetic email tests. No real Outlook calls or messages are sent."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import deals
import email_queue as queue
from outlook_mailer import SENDER,RECIPIENT,GROUP_SHA256,build_offer_mail,build_test_mail,submit_existing,assign_sender
from state import State
from test_monitor import temporary,message


def job(text="ON乳清蛋白粉5磅 到手579元"):
    return {"offer":deals.extract(text,now=1002).data(),"source_ts":1001,"alert_id":"1-synthetic","group_id_sha256":GROUP_SHA256}


class Items:
    def __init__(self,values):
        self.values=values
    @property
    def Count(self):
        return len(self.values)
    def Item(self,i):
        return self.values[i-1]


class Recipients(Items):
    def Add(self,address):
        item=SimpleNamespace(Address=address,Type=1)
        self.values.append(item)
        return item
    def ResolveAll(self):
        return True


class Mail:
    def __init__(self,fail=False):
        self.Recipients=Recipients([])
        self.Attachments=Items([])
        self.CC=self.BCC=""
        self.sent=self.discarded=0
        self.fail=fail
    def Send(self):
        self.sent+=1
        if self.fail:
            raise RuntimeError("synthetic ambiguous failure")
    def Close(self,mode):
        self.discarded+=1


class App:
    def __init__(self,sender=SENDER,offline=False,fail=False):
        self.Session=SimpleNamespace(Offline=offline,Accounts=Items([SimpleNamespace(SmtpAddress=sender)]))
        self.mail=Mail(fail)
        self.created=0
    def CreateItem(self,kind):
        self.created+=1
        return self.mail


class MailComposition(unittest.TestCase):
    def test_synthetic_pipeline_mail_has_explicit_subject_and_body(self):
        value=job("合成测试：超好忦，仅验证提醒链路")
        value["synthetic_test_id"]="a"*32
        subject,body=build_offer_mail(value,now=1003)
        self.assertTrue(subject.startswith("[合成测试]"))
        self.assertIn("不是真实群消息或优惠",body)
        self.assertIn("a"*32,body)

    def test_synthetic_marker_cannot_skip_validation_or_be_silent(self):
        value=job()
        value["synthetic_test_id"]="a"*32
        with self.assertRaises(ValueError):
            build_offer_mail(value,now=1003)
        value=job("合成测试：超好忦")
        value["synthetic_test_id"]="invalid"
        with self.assertRaises(ValueError):
            build_offer_mail(value,now=1003)

    def test_fixed_addresses_and_minimum_summary(self):
        subject,body=build_offer_mail(job(),now=1003)
        self.assertIn("ON 5磅",subject)
        self.assertIn("579",subject)
        self.assertIn("群内报价未联网核价",body)
        self.assertNotIn("@chatroom",body)
        self.assertTrue(RECIPIENT.endswith("@example.invalid"))

    def test_rechecks_threshold_size_status_and_group(self):
        for field,value in (("prices_yuan",[580]),("weight_g",1000),("brand","其他品牌"),("match_status","needs_review")):
            value_job=job()
            value_job["offer"][field]=value
            with self.assertRaises(ValueError):
                build_offer_mail(value_job,1003)
        value_job=job()
        value_job["group_id_sha256"]="other"
        with self.assertRaises(ValueError):
            build_offer_mail(value_job,1003)

    def test_expiry_and_private_links_rejected(self):
        value=job()
        value["offer"]["expiry_ts"]=1002
        with self.assertRaises(ValueError):
            build_offer_mail(value,1003)
        value=job()
        value["offer"]["link"]="https://evil.invalid/token"
        with self.assertRaises(ValueError):
            build_offer_mail(value,1003)

    def test_test_message_is_unambiguously_synthetic(self):
        subject,body=build_test_mail("a"*32)
        self.assertTrue(subject.startswith("[测试]"))
        self.assertIn("不含微信群聊天内容",body)
        with self.assertRaises(ValueError):
            build_test_mail("invalid\nrecipient")


class OutlookScope(unittest.TestCase):
    def test_recipient_is_reacquired_after_resolution(self):
        app=App()
        original=app.mail.Recipients.Add
        def add(address):
            value=original(address)
            value.Address=""
            return value
        def resolve():
            app.mail.Recipients.values=[SimpleNamespace(Address=RECIPIENT,Type=1)]
            return True
        app.mail.Recipients.Add=add
        app.mail.Recipients.ResolveAll=resolve
        result=submit_existing("synthetic","body",lambda value:None,app,do_send=False)
        self.assertTrue(result["preflight_ok"])
        self.assertEqual(app.mail.sent,0)

    def test_object_sender_uses_property_reference_setter(self):
        calls=[]
        ole=SimpleNamespace(GetIDsOfNames=lambda name:77,Invoke=lambda *args:calls.append(args))
        mail=SimpleNamespace(_oleobj_=ole)
        account=object()
        assign_sender(mail,account)
        self.assertEqual(calls[0],(77,0,8,0,account))

    def test_preflight_never_sends_and_discards_only_new_item(self):
        app=App()
        result=submit_existing("synthetic","body",lambda value:None,app,do_send=False)
        self.assertTrue(result["preflight_ok"])
        self.assertFalse(result["send_attempted"])
        self.assertEqual(app.mail.sent,0)
        self.assertEqual(app.mail.discarded,1)

    def test_one_recipient_exact_sender_no_attachments(self):
        app=App()
        stages=[]
        result=submit_existing("synthetic","body",stages.append,app)
        self.assertTrue(result["submitted"])
        self.assertEqual(app.mail.Recipients.Count,1)
        self.assertEqual(app.mail.Recipients.Item(1).Address,RECIPIENT)
        self.assertEqual(app.mail.sent,1)
        self.assertEqual(app.mail.discarded,0)
        self.assertTrue(stages[0]["send_attempted"])

    def test_offline_does_not_create_or_send(self):
        app=App(offline=True)
        result=submit_existing("synthetic","body",lambda value:None,app)
        self.assertTrue(result["retry_safe"])
        self.assertEqual(app.created,0)
        self.assertFalse(result["send_attempted"])

    def test_wrong_account_fails_closed(self):
        app=App(sender="other@example.invalid")
        result=submit_existing("synthetic","body",lambda value:None,app)
        self.assertEqual(result["error"],"sender_missing_or_ambiguous")
        self.assertFalse(result["retry_safe"])
        self.assertEqual(app.created,0)

    def test_send_exception_is_uncertain_and_not_discarded(self):
        app=App(fail=True)
        result=submit_existing("synthetic","body",lambda value:None,app)
        self.assertTrue(result["send_attempted"])
        self.assertFalse(result["submitted"])
        self.assertFalse(result["retry_safe"])
        self.assertEqual(app.mail.discarded,0)

    def test_unexpected_extra_recipient_stops_send(self):
        app=App()
        app.mail.Recipients.values.append(SimpleNamespace(Address="other@example.invalid",Type=1))
        result=submit_existing("synthetic","body",lambda value:None,app)
        self.assertEqual(result["error"],"recipient_or_attachment_scope_mismatch")
        self.assertEqual(app.mail.sent,0)
        self.assertEqual(app.mail.discarded,1)


class EmailRecovery(unittest.TestCase):
    def setUp(self):
        self.tmp=temporary()
        from pathlib import Path
        self.root=Path(self.tmp.name)
        self.config=self.root/"config.json"
        self.jobs=self.root/"jobs"
        self.config.write_text(json.dumps({"enabled":True,"enabled_since":1000,"sender":SENDER,"recipient":RECIPIENT}))
        self.patches=[patch.object(queue,"CONFIG",self.config),patch.object(queue,"JOBS",self.jobs)]
        for item in self.patches:
            item.start()
        self.state=State(self.root/"state.sqlite3")
        self.state.baseline([("synthetic",0,[])],1000)
        self.state.ingest("synthetic",1,[message(1,"ON乳清蛋白粉5磅 到手579元")],1002)
        self.calls=0

    def tearDown(self):
        self.state.db.close()
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def submitted(self,path):
        self.calls+=1
        path.with_suffix(".result.json").write_text(json.dumps({"send_attempted":True,"submitted":True}))

    def test_disabled_queue_never_calls_transport(self):
        config=json.loads(self.config.read_text())
        config["enabled"]=False
        self.config.write_text(json.dumps(config))
        status=queue.tick(self.state,1003,self.submitted)
        self.assertFalse(status["email_enabled"])
        self.assertEqual(self.calls,0)

    def test_success_is_durable_and_not_sent_twice(self):
        queue.tick(self.state,1003,self.submitted)
        self.state.db.close()
        self.state=State(self.root/"state.sqlite3")
        status=queue.tick(self.state,1004,self.submitted)
        self.assertEqual(self.calls,1)
        self.assertEqual(status["email_submitted"],1)
        self.assertEqual(list(self.jobs.glob("*.json")),[])

    def test_uncertain_outcome_pauses_new_sends(self):
        def uncertain(path):
            self.calls+=1
            path.with_suffix(".result.json").write_text(json.dumps({"send_attempted":True,"submitted":False}))
        queue.tick(self.state,1003,uncertain)
        self.state.ingest("synthetic",2,[message(2,"熊猫乳清蛋白粉5磅 到手399元")],1004)
        status=queue.tick(self.state,2000,self.submitted)
        self.assertEqual(self.calls,1)
        self.assertEqual(status["email_uncertain"],1)
        self.assertEqual(status["email_state"],"needs_review")

    def test_late_success_reconciles_without_resending(self):
        def waiting(path):
            self.calls+=1
        queue.tick(self.state,1003,waiting)
        path=next(self.jobs.glob("*.json"))
        path.with_suffix(".result.json").write_text(json.dumps({"send_attempted":True,"submitted":True}))
        status=queue.tick(self.state,1004,self.submitted)
        self.assertEqual(self.calls,1)
        self.assertEqual(status["email_submitted"],1)

    def test_closed_outlook_safe_retry_waits_then_succeeds(self):
        def not_ready(path):
            self.calls+=1
            path.with_suffix(".result.json").write_text(json.dumps({"send_attempted":False,"submitted":False,"retry_safe":True,"error":"outlook_not_ready"}))
        waiting=queue.tick(self.state,1003,not_ready)
        self.assertEqual(waiting["email_state"],"waiting_for_outlook")
        self.assertEqual(waiting["email_last_error"],"outlook_not_ready")
        queue.tick(self.state,1004,self.submitted)
        self.assertEqual(self.calls,1)
        status=queue.tick(self.state,1304,self.submitted)
        self.assertEqual(self.calls,2)
        self.assertEqual(status["email_submitted"],1)
        self.assertEqual(status["email_state"],"enabled")
        self.assertIsNone(status["email_last_error"])

    def test_denied_or_preflight_failure_never_auto_retries(self):
        def denied(path):
            self.calls+=1
            path.with_suffix(".result.json").write_text(json.dumps({"send_attempted":False,"submitted":False,"retry_safe":False,"error":"synthetic_access_denied"}))
        queue.tick(self.state,1003,denied)
        status=queue.tick(self.state,5000,self.submitted)
        self.assertEqual(self.calls,1)
        self.assertEqual(status["email_blocked"],1)

    def test_stale_safe_retry_result_is_not_reused_by_hanging_attempt(self):
        def not_ready(path):
            self.calls+=1
            path.with_suffix(".result.json").write_text(json.dumps({"send_attempted":False,"submitted":False,"retry_safe":True,"error":"outlook_not_ready"}))
        def hanging(path):
            self.calls+=1
        queue.tick(self.state,1003,not_ready)
        status=queue.tick(self.state,1304,hanging)
        self.assertEqual(status["email_uncertain"],1)
        queue.tick(self.state,5000,self.submitted)
        self.assertEqual(self.calls,2)

    def test_ambiguous_candidate_is_not_emailed(self):
        self.state.ingest("synthetic",2,[message(2,"熊猫乳清蛋白粉5磅 到手399")],1003)
        status=queue.tick(self.state,1004,self.submitted)
        self.assertEqual(status["email_submitted"],1)
        self.assertEqual(self.calls,1)

    def test_changed_recipient_config_blocks_delivery(self):
        value=json.loads(self.config.read_text())
        value["recipient"]="other@example.invalid"
        self.config.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError,"email_address_scope_mismatch"):
            queue.tick(self.state,1003,self.submitted)
        self.assertEqual(self.calls,0)


if __name__=="__main__":
    unittest.main(verbosity=2)
