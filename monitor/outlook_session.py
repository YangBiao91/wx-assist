"""Recover the installed Outlook session without logon or credential changes."""
import ctypes
import os
import subprocess
import time
from pathlib import Path

import psutil

UNAVAILABLE=-2147221021  # MK_E_UNAVAILABLE, not an access/security rejection.
from settings import SETTINGS
OUTLOOK_EXE=Path(SETTINGS["outlook_exe"])


def session_id(pid):
    value=ctypes.c_ulong()
    if not ctypes.windll.kernel32.ProcessIdToSessionId(int(pid),ctypes.byref(value)):
        raise OSError("process_session_unavailable")
    return value.value


def running_outlook():
    current_session=session_id(os.getpid())
    result=[]
    for process in psutil.process_iter(["pid","name"]):
        if (process.info["name"] or "").lower()!="outlook.exe":
            continue
        try:
            if process.exe().lower()==str(OUTLOOK_EXE).lower() and session_id(process.pid)==current_session:
                result.append(process.pid)
        except (psutil.Error,OSError):
            continue
    return result


def launch_installed_outlook():
    if not OUTLOOK_EXE.is_file():
        raise FileNotFoundError("installed_classic_outlook_missing")
    info=subprocess.STARTUPINFO()
    info.dwFlags|=subprocess.STARTF_USESHOWWINDOW
    info.wShowWindow=7  # Start minimized without activation; prompts remain user-owned.
    return subprocess.Popen([str(OUTLOOK_EXE)],cwd=OUTLOOK_EXE.parent,
                            stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                            close_fds=True,creationflags=subprocess.CREATE_NO_WINDOW,startupinfo=info)


def connect_outlook(client,allow_launch=False,running=None,launch=None,wait=None,startup_checks=10):
    running=running or running_outlook
    launch=launch or launch_installed_outlook
    wait=wait or time.sleep
    try:
        return client.GetActiveObject("Outlook.Application"),{"method":"active_object","started_by_monitor":False}
    except Exception as exc:
        # Never retry or fall back around access-denied/security errors.
        if getattr(exc,"hresult",None)!=UNAVAILABLE:
            raise
        unavailable=exc
    processes=running()
    started=False
    if not processes:
        if not allow_launch:
            raise unavailable
        launch()
        started=True
        for _ in range(startup_checks):
            processes=running()
            if processes:
                break
            wait(1)
        if not processes:
            raise unavailable
    # Outlook may not have published its active object yet. Its supported class
    # factory can return the application in the verified, same-session process.
    # Do not call NameSpace.Logon, enter credentials, or alter security options.
    app=client.Dispatch("Outlook.Application")
    return app,{"method":"existing_process_class_factory","started_by_monitor":started,
                "process_ids":processes}
