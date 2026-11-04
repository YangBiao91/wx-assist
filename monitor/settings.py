"""Local routing preferences. Importing this module never reads credentials."""
import hashlib
import json
import re
from pathlib import Path

ROOT=Path(__file__).resolve().parent
SETTINGS_FILE=ROOT/'settings.local.json'
CREDENTIAL_FILE=ROOT/'credentials.local.env'
DEFAULTS={
    'chat_id':'example@chatroom',
    'sender':'sender@example.invalid',
    'recipient':'recipient@example.invalid',
    'outlook_exe':r'C:\Program Files\Microsoft Office\Root\Office16\OUTLOOK.EXE',
}

def load_settings(path=SETTINGS_FILE):
    if not path.exists():
        return dict(DEFAULTS)
    value=json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(value,dict):
        raise ValueError('invalid_local_settings')
    return dict(DEFAULTS,**value)

SETTINGS=load_settings()
GROUP_SHA256=hashlib.sha256(SETTINGS['chat_id'].encode()).hexdigest()

def validate_runtime_settings(path=SETTINGS_FILE):
    if not path.is_file():
        raise ValueError('copy_and_edit_settings_example_first')
    value=load_settings(path)
    chat=value.get('chat_id','')
    if (not isinstance(chat,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,120}@chatroom',chat)
            or chat==DEFAULTS['chat_id']):
        raise ValueError('explicit_single_group_id_required')
    for key in ('sender','recipient'):
        address=value.get(key,'')
        if (not isinstance(address,str) or not re.fullmatch(r'[^\s<>@,;]+@[^\s<>@,;]+\.[^\s<>@,;]+',address)
                or address.lower().endswith('@example.invalid')):
            raise ValueError('explicit_single_email_address_required')
    executable=Path(value['outlook_exe'])
    if not executable.is_absolute() or executable.name.lower()!='outlook.exe':
        raise ValueError('absolute_classic_outlook_executable_required')
    return value
