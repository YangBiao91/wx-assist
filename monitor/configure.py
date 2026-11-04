"""Explicit local setup and email enablement; no WeChat reads or mail sends."""
import argparse
import hashlib
import json
import re
import time
from pathlib import Path

from settings import ROOT,validate_runtime_settings

def save(path,value):
    temporary=path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
    temporary.replace(path)

def initialize():
    value=validate_runtime_settings()
    scope=ROOT.parent/'single-group-scope.json'
    mail=ROOT/'mail-config.json'
    if scope.exists() or mail.exists():
        raise ValueError('existing_local_configuration_not_overwritten')
    if not re.fullmatch(r'[0-9a-f]{64}',hashlib.sha256(value['chat_id'].encode()).hexdigest()):
        raise ValueError('invalid_scope')
    save(scope,{'chat_id':value['chat_id'],'monitoring_enabled':False,'auto_send_enabled':False})
    save(mail,{'enabled':False,'enabled_since':None,'sender':value['sender'].lower(),
               'recipient':value['recipient'].lower(),'outlook_on_demand_start':True,
               'credential_storage':'existing_Outlook_profile_only'})
    return {'configured':True,'email_enabled':False,'credentials_created':False}

def enable_email(test_id,confirmed):
    if not confirmed or not re.fullmatch(r'[0-9a-f]{32}',test_id or ''):
        raise ValueError('test_id_and_explicit_gmail_receipt_confirmation_required')
    settings=validate_runtime_settings()
    result=json.loads((ROOT/'data'/('email-test-'+test_id+'.json')).read_text(encoding='utf-8'))
    if not result.get('submitted') or not result.get('send_attempted'):
        raise ValueError('submitted_test_mail_required')
    path=ROOT/'mail-config.json'
    config=json.loads(path.read_text(encoding='utf-8'))
    for key,actual in (('sender',result.get('sender')),('recipient',result.get('to'))):
        if config[key].lower()!=settings[key].lower() or str(actual).lower()!=settings[key].lower():
            raise ValueError('test_route_does_not_match_local_settings')
    config.update(enabled=True,enabled_since=config.get('enabled_since') or time.time())
    save(path,config)
    return {'email_enabled':True,'gmail_receipt_confirmed_by_user':True,'mail_sent':False}

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=('init','enable-email'))
    parser.add_argument('--test-id')
    parser.add_argument('--confirm-gmail-received',action='store_true')
    args=parser.parse_args()
    try:
        result=initialize() if args.action=='init' else enable_email(args.test_id,args.confirm_gmail_received)
        (ROOT/'data').mkdir(exist_ok=True)
        print(json.dumps(result))
    except (OSError,ValueError,KeyError) as exc:
        print(json.dumps({'error':str(exc) if isinstance(exc,ValueError) else 'local_setup_unavailable'}))
        raise SystemExit(2)
