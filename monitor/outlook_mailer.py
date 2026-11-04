"""Pinned-address Outlook sender. Uses the user's existing desktop session only.

No passwords, OAuth tokens, SMTP login, mailbox reads, or security-setting changes.
The command defaults to preview; --submit is required for a real message.
"""
import argparse
import json
import os
import re
import time
from pathlib import Path

from deals import public_link,keyword_matches,KEYWORD_FORMS
from outlook_session import connect_outlook

ROOT = Path(__file__).resolve().parent
JOBS = ROOT / "data/email-jobs"
from settings import SETTINGS, GROUP_SHA256
SENDER = SETTINGS["sender"].lower()
RECIPIENT = SETTINGS["recipient"].lower()


def _build_offer_mail(job, now=None):
    now = time.time() if now is None else now
    if job.get("group_id_sha256") != GROUP_SHA256:
        raise ValueError("group_scope_mismatch")
    data = job["offer"]
    if data.get("trigger_kind")=="rule":
        if SETTINGS.get("rule_profile","generic")!="generic":
            raise ValueError("alert_profile_mismatch")
        return build_rule_mail(job)
    if SETTINGS.get("rule_profile","generic")!="protein_deals":
        raise ValueError("alert_profile_mismatch")
    if data.get("trigger_kind","price")=="keyword":
        return build_keyword_mail(job)
    if data.get("trigger_kind","price")!="price":
        raise ValueError("unknown_alert_trigger")
    threshold = {"ON":580,"熊猫":400}.get(data.get("brand"))
    prices = data.get("prices_yuan",[])
    if (threshold is None or data.get("threshold_yuan")!=threshold or
        data.get("match_status")!="matched" or data.get("reasons") or
        len(prices)!=1 or type(prices[0]) not in {int,float} or not 1<=prices[0]<threshold or
        type(data.get("weight_g")) not in {int,float} or abs(data["weight_g"]-2267.96)>0.02):
        raise ValueError("offer_does_not_match_rules")
    if data.get("expiry_ts") is not None and data["expiry_ts"]<=now:
        raise ValueError("offer_expired")
    price = prices[0]
    title = re.sub(r"[\r\n\x00-\x1f]"," ",str(data.get("title","")))[:120]
    conditions = "、".join(str(x)[:20] for x in data.get("conditions",[])[:10]) or "未注明明确门槛，请回群确认"
    link = str(data.get("link",""))
    if link and public_link(link)!=link:
        raise ValueError("non_public_product_link")
    subject = f"[蛋白粉提醒] {data['brand']} 5磅 ¥{price:g}（低于¥{threshold}）"
    body = (f"在已授权的指定群中发现符合价格规则的群内报价：\n\n"
            f"品牌：{data['brand']}\n规格：单件5磅\n群内报价：人民币 {price:g} 元\n"
            f"你的规则：严格低于 {threshold} 元\n商品摘要：{title}\n"
            f"条件：{conditions}\n优惠截止：{str(data.get('expiry_label','未说明'))[:80]}\n"
            f"消息时间：{time.strftime('%Y-%m-%d %H:%M:%S',time.localtime(int(job['source_ts'])))}（电脑本地时间）\n"
            + (f"商品链接：{link}\n" if link else "请在指定群查看原消息、店铺及完整下单条件。\n")
            + "\n群内报价未联网核价，库存、券和资格可能变化；不会替你下单。\n"
            + f"告警标识：{str(job['alert_id'])[:80]}\n")
    return subject,body


def build_rule_mail(job):
    from generic_rules import validate_alert
    data=job["offer"]
    rule=validate_alert(data)
    terms="、".join(data.get("matched_keywords",[]))
    subject=f"[群聊监控] {rule['name']}"
    body=(f"指定群的新文字消息命中规则：{rule['name']}\n"
          f"规则标识：{rule['id']}\n命中词：{terms}\n\n"
          f"消息片段：{data['keyword_excerpt']}\n\n"
          f"消息时间：{time.strftime('%Y-%m-%d %H:%M:%S',time.localtime(int(job['source_ts'])))}（电脑本地时区）\n"
          "这是规则匹配提醒；请回到原群核对完整通知、有效期或操作要求。\n"
          f"告警标识：{str(job['alert_id'])[:80]}\n")
    return subject,body


def build_keyword_mail(job):
    data=job["offer"]
    matches=data.get("matched_keywords",[])
    excerpt=str(data.get("keyword_excerpt",""))
    observed=keyword_matches(excerpt)
    if (data.get("match_status")!="matched" or not isinstance(matches,list) or not matches or
        any(value not in KEYWORD_FORMS for value in matches) or not set(observed).intersection(matches) or
        len(excerpt)>240):
        raise ValueError("keyword_evidence_missing_or_invalid")
    title=re.sub(r"[\r\n\x00-\x1f]"," ",str(data.get("title","")))[:120]
    link=str(data.get("link",""))
    if link and public_link(link)!=link:
        raise ValueError("non_public_product_link")
    terms="、".join(matches)
    conditions="、".join(str(value)[:20] for value in data.get("conditions",[])[:10]) or "未识别到明确下单条件，请回群确认"
    subject=f"[指定群关键词提醒] {terms}"
    body=(f"指定的指定群群内有消息提到：{terms}\n\n"
          f"消息摘要：{title}\n关键词上下文：{excerpt}\n"
          f"识别到的条件：{conditions}\n优惠截止：{str(data.get('expiry_label','未说明'))[:80]}\n"
          f"消息时间：{time.strftime('%Y-%m-%d %H:%M:%S',time.localtime(int(job['source_ts'])))}（电脑本地时间）\n"
          + (f"公开商品链接：{link}\n" if link else "请在指定群查看完整报价和操作条件。\n")
          + "\n这是关键词提及提醒，不表示已通过 ON/熊猫的价格规则，也未联网核实优惠是否仍有效。\n"
          + f"告警标识：{str(job['alert_id'])[:80]}\n")
    return subject,body


def build_test_mail(test_id):
    if not re.fullmatch(r"[0-9a-f]{32}",test_id):
        raise ValueError("invalid_test_id")
    return (f"[测试] 群聊监控邮件链路 {test_id[:12]}",
            "这是一封链路测试邮件，不含微信群聊天内容，不代表实际命中事件。\n\n"
            f"规则配置：{SETTINGS.get('rule_profile','generic')}\n"
            f"发件账户：{SENDER}\n收件人：{RECIPIENT}\n测试标识：{test_id}\n")



def build_offer_mail(job,now=None):
    subject,body=_build_offer_mail(job,now)
    if "synthetic_test_id" not in job:
        return subject,body
    test_id=job["synthetic_test_id"]
    data=job["offer"]
    if (not isinstance(test_id,str) or not re.fullmatch(r"[0-9a-f]{32}",test_id) or
        "合成测试" not in (str(data.get("title",""))+str(data.get("keyword_excerpt","")))):
        raise ValueError("invalid_synthetic_test_marker")
    subject=f"[合成测试] 未来提醒链路验证 {test_id[:12]}"
    body=("这是一封明确授权的合成端到端测试邮件，不是真实群消息或优惠。\n"
          f"测试编号：{test_id}\n以下为模拟的提醒格式：\n\n"+body)
    return subject,body


def assign_sender(mail,account):
    if hasattr(mail,"_oleobj_"):
        # Outlook's object-valued setter requires COM PROPERTYPUTREF (VBA Set).
        # Resolve the DISPID by name instead of relying on a version-specific ID.
        import pythoncom
        dispid = mail._oleobj_.GetIDsOfNames("SendUsingAccount")
        mail._oleobj_.Invoke(dispid,0,pythoncom.DISPATCH_PROPERTYPUTREF,0,account)
    else:
        mail.SendUsingAccount = account


def submit_existing(subject,body,progress,app=None,do_send=True):
    result = {"send_attempted":False,"submitted":False,"retry_safe":False,"to":RECIPIENT,"sender":SENDER}
    mail = None
    initialized = False
    try:
        if app is None:
            import pythoncom
            import win32com.client
            pythoncom.CoInitialize()
            initialized = True
            config=json.loads((ROOT/"mail-config.json").read_text(encoding="utf-8"))
            if config.get("sender","").lower()!=SENDER or config.get("recipient","").lower()!=RECIPIENT:
                raise ValueError("email_address_scope_mismatch")
            app,connection=connect_outlook(win32com.client,allow_launch=config.get("outlook_on_demand_start") is True)
            result["outlook_connection"]=connection
        if app.Session.Offline:
            result.update(error="outlook_offline",retry_safe=True)
            return result
        accounts = [app.Session.Accounts.Item(i) for i in range(1,int(app.Session.Accounts.Count)+1)]
        matches = [account for account in accounts if str(account.SmtpAddress).lower()==SENDER]
        if len(matches)!=1:
            result["error"] = "sender_missing_or_ambiguous"
            return result
        mail = app.CreateItem(0)
        assign_sender(mail,matches[0])
        if not mail.SendUsingAccount or str(mail.SendUsingAccount.SmtpAddress).lower()!=SENDER:
            result["error"] = "sender_assignment_failed"
            return result
        mail.CC,mail.BCC = "",""
        recipient = mail.Recipients.Add(RECIPIENT)
        recipient.Type = 1
        if not mail.Recipients.ResolveAll():
            result["error"] = "recipient_unresolved"
            return result
        recipient = mail.Recipients.Item(1)
        observed = {"recipient_count":int(mail.Recipients.Count),"recipient_type":int(recipient.Type),
                    "recipient_address":str(recipient.Address),"cc_present":bool(mail.CC),"bcc_present":bool(mail.BCC),
                    "attachment_count":int(mail.Attachments.Count)}
        if (observed["recipient_count"]!=1 or observed["recipient_type"]!=1 or
            observed["recipient_address"].lower()!=RECIPIENT or observed["cc_present"] or observed["bcc_present"] or observed["attachment_count"]!=0):
            result["error"] = "recipient_or_attachment_scope_mismatch"
            result["observed_new_item_scope"] = observed
            return result
        mail.Subject = subject
        mail.BodyFormat = 1
        mail.Body = body
        if not do_send:
            result["preflight_ok"] = True
            return result
        # Persist BEFORE crossing the send boundary. Any later ambiguity stops retries.
        result.update(send_attempted=True,stage="send_called")
        progress(dict(result))
        mail.Send()
        result.update(submitted=True,stage="submitted")
        return result
    except Exception as exc:
        result["error"] = "outlook_send_outcome_uncertain" if result["send_attempted"] else "outlook_not_ready"
        if hasattr(exc,"hresult"):
            result["hresult"] = int(exc.hresult)
            result["retry_safe"] = not result["send_attempted"] and int(exc.hresult)==-2147221021
        return result
    finally:
        if mail is not None and not result["send_attempted"]:
            try:
                mail.Close(1)  # Discard only this new, unsaved item; never touch existing mail.
            except Exception:
                pass
        if initialized:
            import pythoncom
            pythoncom.CoUninitialize()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--job",type=Path)
    parser.add_argument("--test-id")
    parser.add_argument("--submit",action="store_true")
    parser.add_argument("--preflight",action="store_true")
    args = parser.parse_args()
    if bool(args.job)==bool(args.test_id):
        raise ValueError("choose_exactly_one_mail_source")
    if args.test_id:
        subject,body = build_test_mail(args.test_id)
        result_path = ROOT / "data" / ("email-test-"+args.test_id+".json")
    else:
        path = args.job.resolve()
        if path.parent!=JOBS.resolve() or not re.fullmatch(r"\d+-[0-9a-f]{12}\.json",path.name):
            raise ValueError("job_path_outside_outbox")
        job = json.loads(path.read_text(encoding="utf-8"))
        subject,body = build_offer_mail(job)
        result_path = path.with_suffix(".result.json")
    if not args.submit and not args.preflight:
        print(json.dumps({"preview_only":True,"to":RECIPIENT,"sender":SENDER,"subject":subject,"body":body}),flush=True)
        return
    if result_path.exists():
        previous = json.loads(result_path.read_text(encoding="utf-8"))
        if previous.get("send_attempted"):
            print(json.dumps({"error":"already_attempted_do_not_resend","submitted":previous.get("submitted",False)}),flush=True)
            return
    def progress(value):
        value.update(updated_at=time.time(),helper_pid=os.getpid())
        temporary = result_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value),encoding="utf-8")
        temporary.replace(result_path)
    progress({"stage":"preflight_started","send_attempted":False,"submitted":False,"retry_safe":False})
    result = submit_existing(subject,body,progress,do_send=args.submit)
    progress(result)
    print(json.dumps(result),flush=True)


if __name__=="__main__":
    try:
        main()
    except Exception:
        print(json.dumps({"error":"mail_job_validation_failed","send_attempted":False,"submitted":False}),flush=True)
        raise SystemExit(2)
