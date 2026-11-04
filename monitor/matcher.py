"""Select the configured rule profile without enabling examples implicitly."""
import deals
import generic_rules
from settings import SETTINGS

def extract(text,now=None,reference_ts=None):
    profile=SETTINGS.get('rule_profile','generic')
    if profile=='generic':
        return generic_rules.extract(text,now=now,reference_ts=reference_ts)
    if profile=='protein_deals':
        return deals.extract(text,now=now,reference_ts=reference_ts)
    raise ValueError('unknown_rule_profile')
