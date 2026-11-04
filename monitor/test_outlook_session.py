"""Synthetic recovery tests; do not launch Outlook, authenticate, or send mail."""
import unittest
from types import SimpleNamespace

from outlook_session import UNAVAILABLE,connect_outlook


class ComFailure(Exception):
    def __init__(self,hresult):
        self.hresult=hresult


class Client:
    def __init__(self,error=None):
        self.error=error
        self.app=object()
        self.factory_calls=0
    def GetActiveObject(self,name):
        if self.error:
            raise self.error
        return self.app
    def Dispatch(self,name):
        self.factory_calls+=1
        return self.app


class OutlookRecovery(unittest.TestCase):
    def test_registered_instance_does_not_launch_or_use_factory(self):
        client=Client()
        launches=[]
        app,metadata=connect_outlook(client,allow_launch=True,launch=lambda:launches.append(1))
        self.assertIs(app,client.app)
        self.assertEqual(client.factory_calls,0)
        self.assertEqual(launches,[])

    def test_running_but_unregistered_instance_uses_existing_process_factory(self):
        client=Client(ComFailure(UNAVAILABLE))
        launches=[]
        app,metadata=connect_outlook(client,running=lambda:[42],launch=lambda:launches.append(1))
        self.assertIs(app,client.app)
        self.assertEqual(client.factory_calls,1)
        self.assertEqual(launches,[])
        self.assertFalse(metadata["started_by_monitor"])

    def test_no_running_app_and_launch_disabled_never_launches(self):
        client=Client(ComFailure(UNAVAILABLE))
        launches=[]
        with self.assertRaises(ComFailure):
            connect_outlook(client,allow_launch=False,running=lambda:[],launch=lambda:launches.append(1))
        self.assertEqual(launches,[])
        self.assertEqual(client.factory_calls,0)

    def test_missing_app_is_launched_once_then_existing_profile_is_used(self):
        client=Client(ComFailure(UNAVAILABLE))
        phases=iter([[],[],[42]])
        launches=[]
        waits=[]
        app,metadata=connect_outlook(client,allow_launch=True,running=lambda:next(phases),launch=lambda:launches.append(1),wait=waits.append)
        self.assertIs(app,client.app)
        self.assertEqual(launches,[1])
        self.assertEqual(waits,[1])
        self.assertTrue(metadata["started_by_monitor"])

    def test_security_rejection_has_no_fallback_or_launch(self):
        client=Client(ComFailure(-2147024891))
        launches=[]
        with self.assertRaises(ComFailure):
            connect_outlook(client,allow_launch=True,running=lambda:[42],launch=lambda:launches.append(1))
        self.assertEqual(client.factory_calls,0)
        self.assertEqual(launches,[])

    def test_missing_executable_does_not_try_other_programs(self):
        client=Client(ComFailure(UNAVAILABLE))
        def launch():
            raise FileNotFoundError("synthetic missing executable")
        with self.assertRaises(FileNotFoundError):
            connect_outlook(client,allow_launch=True,running=lambda:[],launch=launch,wait=lambda seconds:None)
        self.assertEqual(client.factory_calls,0)

    def test_unready_process_wait_is_bounded_without_relaunch(self):
        client=Client(ComFailure(UNAVAILABLE))
        launches=[]
        waits=[]
        with self.assertRaises(ComFailure):
            connect_outlook(client,allow_launch=True,running=lambda:[],launch=lambda:launches.append(1),wait=waits.append,startup_checks=3)
        self.assertEqual(launches,[1])
        self.assertEqual(waits,[1,1,1])
        self.assertEqual(client.factory_calls,0)

    def test_factory_security_rejection_is_propagated(self):
        client=Client(ComFailure(UNAVAILABLE))
        def factory(name):
            raise ComFailure(-2147024891)
        client.Dispatch=factory
        with self.assertRaises(ComFailure) as result:
            connect_outlook(client,running=lambda:[42])
        self.assertEqual(result.exception.hresult,-2147024891)


if __name__=="__main__":
    unittest.main(verbosity=2)
