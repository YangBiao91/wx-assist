"""Synthetic Windows-login conditions; no login, app shutdown or email sends."""
import unittest
import threading
from types import SimpleNamespace
from unittest.mock import patch

import monitor
import startup
from notifier import Notifier


class StartupConditions(unittest.TestCase):
    def test_already_running_never_spawns(self):
        starts=[]
        result=startup.bootstrap(start=lambda:starts.append(1),status=lambda:{"running":True,"pid":42,"state":"monitoring"},write=lambda value:None)
        self.assertEqual(starts,[])
        self.assertEqual(result["monitor_pid"],42)
        self.assertEqual(result["state"],"already_running")

    def test_delayed_initial_launch_retries_then_uses_wechat_wait_state(self):
        state={"running":False}
        starts=[]
        waits=[]
        def start():
            starts.append(1)
            if len(starts)==1:
                return {"error":"synthetic_profile_files_not_ready"}
            state.update(running=True,pid=77,state="retrying")
            return state
        result=startup.bootstrap(start=start,status=lambda:state,wait=waits.append,write=lambda value:None)
        self.assertEqual(len(starts),2)
        self.assertEqual(waits,[5])
        self.assertEqual(result["monitor_state"],"retrying")

    def test_redirector_wait_does_not_start_second_candidate(self):
        state={"running":False}
        starts=[]
        def wait(seconds):
            state.update(running=True,pid=88,state="monitoring")
        result=startup.bootstrap(start=lambda:starts.append(1) or {"running":True,"pid":12},status=lambda:state,wait=wait,write=lambda value:None)
        self.assertEqual(len(starts),1)
        self.assertEqual(result["monitor_pid"],88)

    def test_hollow_process_is_reported_without_duplicate_launches(self):
        starts=[]
        waits=[]
        result=startup.bootstrap(start=lambda:starts.append(1) or {"running":True,"pid":12},status=lambda:{"running":False},wait=waits.append,write=lambda value:None)
        self.assertEqual(len(starts),1)
        self.assertEqual(result["state"],"startup_pending_review")
        self.assertEqual(len(waits),6)

    def test_failed_worker_status_is_not_reported_as_ready(self):
        starts=[]
        result=startup.bootstrap(start=lambda:starts.append(1),status=lambda:{"running":True,"pid":12,"state":"startup_failed"},wait=lambda seconds:None,write=lambda value:None)
        self.assertEqual(starts,[])
        self.assertEqual(result["state"],"startup_pending_review")

    def test_repeated_failures_are_bounded(self):
        starts=[]
        waits=[]
        result=startup.bootstrap(start=lambda:starts.append(1) or {"error":"synthetic_failure"},status=lambda:{"running":False},wait=waits.append,write=lambda value:None,max_attempts=3)
        self.assertEqual(len(starts),3)
        self.assertEqual(waits,[5,10])
        self.assertEqual(result["state"],"startup_failed")

    def test_bootstrap_exits_after_ready_and_does_not_watch_user_exit(self):
        state={"running":False}
        waits=[]
        def start():
            state.update(running=True,pid=99,state="monitoring")
            return state
        result=startup.bootstrap(start=start,status=lambda:state,wait=waits.append,write=lambda value:None)
        self.assertEqual(result["state"],"started")
        self.assertEqual(waits,[])

    def test_initial_worker_failure_closes_tray_instead_of_hanging(self):
        exited=[]
        statuses=[]
        notifier=SimpleNamespace(exit=lambda:exited.append(True))
        with patch.object(monitor,"run_worker",side_effect=OSError("synthetic state unavailable")),patch.object(monitor,"write_status",side_effect=statuses.append):
            monitor.worker_entry(notifier,None,None)
        self.assertEqual(exited,[True])
        self.assertEqual(statuses[0]["state"],"startup_failed")

    def test_worker_starts_only_after_tray_is_ready(self):
        value=Notifier.__new__(Notifier)
        value.stop=threading.Event()
        value.ready=threading.Event()
        class Icon:
            visible=False
            def run(self,setup):
                setup(self)
        value.icon=Icon()
        observed=[]
        value.run(on_ready=lambda:observed.append((value.ready.is_set(),value.icon.visible)))
        self.assertEqual(observed,[(True,True)])

    def test_unready_windows_tray_exits_without_starting_worker(self):
        value=Notifier.__new__(Notifier)
        value.stop=threading.Event()
        value.ready=threading.Event()
        stopped=[]
        class Icon:
            @property
            def visible(self):
                return False
            @visible.setter
            def visible(self,value):
                raise RuntimeError("synthetic shell still starting")
            def run(self,setup):
                setup(self)
            def stop(self):
                stopped.append(True)
        value.icon=Icon()
        workers=[]
        with self.assertRaisesRegex(RuntimeError,"tray_initialization_unavailable"):
            value.run(on_ready=lambda:workers.append(True))
        self.assertEqual(workers,[])
        self.assertEqual(stopped,[True])
        self.assertTrue(value.stop.is_set())


if __name__=="__main__":
    unittest.main(verbosity=2)
