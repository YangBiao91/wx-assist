"""Local, deterministic keyword rules; no code evaluation or network calls."""
import hashlib
import json
import re
import unicodedata
from pathlib import Path

from deals import Offer,clean

RULES_FILE=Path(__file__).resolve().parent/'rules.local.json'

def normalize(text):
    value=unicodedata.normalize('NFKC',text)
    return re.sub(r'[\u200b-\u200d\ufeff]','',value).casefold()

def load_rules(path=None):
    path=RULES_FILE if path is None else Path(path)
    if not path.is_file():
        raise ValueError('rules_local_file_required')
    if path.stat().st_size>64*1024:
        raise ValueError('rules_file_too_large')
    value=json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(value,dict) or set(value)!={'rules'} or not isinstance(value['rules'],list) or len(value['rules'])>50:
        raise ValueError('invalid_rules_document')
    seen=set()
    for rule in value['rules']:
        if not isinstance(rule,dict) or set(rule)-{'id','name','enabled','any_keywords','exclude_keywords'}:
            raise ValueError('invalid_rule_fields')
        if not isinstance(rule.get('id'),str) or not re.fullmatch(r'[a-z0-9_-]{1,50}',rule['id']) or rule['id'] in seen:
            raise ValueError('unique_rule_id_required')
        seen.add(rule['id'])
        if not isinstance(rule.get('name'),str) or not rule['name'].strip() or len(rule['name'])>80 or re.search(r'[\x00-\x1f\x7f]',rule['name']):
            raise ValueError('invalid_rule_name')
        if type(rule.get('enabled')) is not bool:
            raise ValueError('explicit_rule_enabled_required')
        for field in ('any_keywords','exclude_keywords'):
            terms=rule.get(field,[])
            if (not isinstance(terms,list) or len(terms)>32 or
                    any(not isinstance(term,str) or not term.strip() or len(term)>80 or re.search(r'[\x00-\x1f\x7f]',term) for term in terms)):
                raise ValueError('invalid_rule_keywords')
        if not rule.get('any_keywords'):
            raise ValueError('positive_keywords_required')
    return value['rules']

def signature(rule):
    return hashlib.sha256(json.dumps(rule,sort_keys=True,ensure_ascii=False).encode()).hexdigest()

def evidence(rule,text):
    value=normalize(text)
    if any(normalize(term) in value for term in rule.get('exclude_keywords',[])):
        return []
    return [term for term in rule['any_keywords'] if normalize(term) in value]

def extract(text,now=None,reference_ts=None,rules=None):
    if not isinstance(text,str) or len(text)>100_000:
        return None
    rules=load_rules() if rules is None else rules
    # Retain only a small local excerpt, never a whole chat archive.
    filtered='\n'.join(re.sub(r'(?:联系人|联系方式|微信号|加微)\s*[:：].*$','',line) for line in text.splitlines())
    visible=unicodedata.normalize('NFKC',clean(filtered))
    visible=re.sub(r'[\u200b-\u200d\ufeff]','',visible)
    for rule in rules:
        if not rule['enabled']:
            continue
        if not evidence(rule,text):
            continue
        matched=evidence(rule,visible)
        if not matched:
            continue
        normalized=normalize(visible)
        offset=min(normalized.find(normalize(term)) for term in matched)
        # Map casefold expansions back to visible text while retaining its case.
        position=0
        for index,char in enumerate(visible):
            if position>=offset:
                offset=index
                break
            position+=len(char.casefold())
        excerpt=visible[max(0,offset-100):max(0,offset-100)+500].strip()
        retained=evidence(rule,excerpt)
        if not retained:
            continue
        key=hashlib.sha256(repr((rule['id'],signature(rule),re.sub(r'\s+',' ',excerpt))).encode()).hexdigest()
        return Offer(title=rule['name'],prices_yuan=[],weight_g=None,yuan_per_kg=None,
                     conditions=[],link='',fingerprint=key,brand='',threshold_yuan=None,
                     match_status='matched',reasons=[],expiry_ts=None,expiry_label='未自动判断有效期',
                     trigger_kind='rule',matched_keywords=retained,keyword_excerpt=excerpt,
                     rule_id=rule['id'],rule_name=rule['name'],rule_signature=signature(rule))
    return None

def validate_alert(data):
    rules=load_rules()
    matches=[rule for rule in rules if rule['id']==data.get('rule_id')]
    if len(matches)!=1:
        raise ValueError('alert_rule_missing')
    rule=matches[0]
    excerpt=data.get('keyword_excerpt')
    if (not rule['enabled'] or signature(rule)!=data.get('rule_signature') or
            data.get('rule_name')!=rule['name'] or data.get('match_status')!='matched' or
            not isinstance(excerpt,str) or len(excerpt)>500 or not evidence(rule,excerpt)):
        raise ValueError('alert_rule_evidence_invalid_or_changed')
    return rule
