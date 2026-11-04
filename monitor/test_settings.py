"""Configuration boundaries only; never read real credentials or send email."""
import json
import unittest
from pathlib import Path
from unittest.mock import patch

import configure
import settings
from test_monitor import temporary

class Configuration(unittest.TestCase):
    def test_missing_configuration_cannot_start(self):
        with temporary() as folder:
            with self.assertRaisesRegex(ValueError,'settings_example'):
                settings.validate_runtime_settings(Path(folder)/'absent.json')

    def test_example_does_not_authorize_a_real_group(self):
        with self.assertRaisesRegex(ValueError,'single_group'):
            settings.validate_runtime_settings(settings.ROOT/'settings.example.json')

    def test_single_group_and_single_recipient_are_required(self):
        with temporary() as folder:
            path=Path(folder)/'settings.json'
            for changed in ({'chat_id':'not-a-group'},{'recipient':'one@example.org,two@example.org'},
                            {'recipient':'one@example.org\nBcc:another@example.org'}):
                value=dict(settings.DEFAULTS,chat_id='123456@chatroom',sender='one@example.org',recipient='two@example.org')
                value.update(changed)
                path.write_text(json.dumps(value),encoding='utf-8')
                with self.assertRaises(ValueError):
                    settings.validate_runtime_settings(path)

    def test_enable_email_requires_explicit_delivery_confirmation(self):
        with self.assertRaisesRegex(ValueError,'explicit_receipt'):
            configure.enable_email('a'*32,False)

    def test_setup_never_overwrites_an_existing_scope(self):
        with temporary() as folder:
            root=Path(folder)/'monitor'
            root.mkdir()
            (root.parent/'single-group-scope.json').write_text('{}',encoding='utf-8')
            with patch.object(configure,'ROOT',root),patch.object(configure,'validate_runtime_settings',return_value=dict(settings.DEFAULTS)):
                with self.assertRaisesRegex(ValueError,'not_overwritten'):
                    configure.initialize()

    def test_initialization_is_disabled_until_test_receipt_is_confirmed(self):
        with temporary() as folder:
            root=Path(folder)/'monitor'
            root.mkdir()
            value=dict(settings.DEFAULTS)
            with patch.object(configure,'ROOT',root),patch.object(configure,'validate_runtime_settings',return_value=value):
                result=configure.initialize()
                self.assertFalse(result['email_enabled'])
                self.assertFalse((root/'credentials.local.env').exists())
                (root/'data').mkdir()
                test_id='b'*32
                (root/'data'/('email-test-'+test_id+'.json')).write_text(json.dumps({
                    'submitted':True,'send_attempted':True,'sender':value['sender'],'to':value['recipient']}))
                result=configure.enable_email(test_id,True)
                self.assertTrue(result['email_enabled'])
                self.assertFalse(result['mail_sent'])

    def test_receipt_from_another_route_cannot_enable_email(self):
        with temporary() as folder:
            root=Path(folder)/'monitor'
            root.mkdir()
            value=dict(settings.DEFAULTS)
            with patch.object(configure,'ROOT',root),patch.object(configure,'validate_runtime_settings',return_value=value):
                configure.initialize()
                (root/'data').mkdir()
                test_id='c'*32
                (root/'data'/('email-test-'+test_id+'.json')).write_text(json.dumps({
                    'submitted':True,'send_attempted':True,'sender':value['sender'],'to':'other@example.invalid'}))
                with self.assertRaisesRegex(ValueError,'route_does_not_match'):
                    configure.enable_email(test_id,True)
                self.assertFalse(json.loads((root/'mail-config.json').read_text())['enabled'])
