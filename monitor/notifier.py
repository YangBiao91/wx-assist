"""Windows-only local tray notifications; no server, push account or sockets."""
import os
import threading

from PIL import Image, ImageDraw
from pystray import Menu, MenuItem
from pystray._win32 import Icon
from pystray._util import win32


class ObservedIcon(Icon):
    def __init__(self, *args, **kwargs):
        self.balloon_shown = threading.Event()
        self.balloon_clicked = None
        super().__init__(*args, **kwargs)

    def _message(self, code, flags, **kwargs):
        import ctypes
        result = win32.Shell_NotifyIcon(code, win32.NOTIFYICONDATAW(
            cbSize=ctypes.sizeof(win32.NOTIFYICONDATAW), hWnd=self._hwnd,
            hID=id(self), uFlags=flags, **kwargs))
        # pystray updates the icon before its initial NIM_ADD; that preliminary
        # NIM_MODIFY legitimately returns FALSE. Check actual add/balloon calls.
        if not result and (code == win32.NIM_ADD or flags & win32.NIF_INFO):
            raise RuntimeError("notification_api_rejected")

    def _notify(self, message, title=None):
        self._message(win32.NIM_MODIFY, win32.NIF_INFO, szInfo=message[:250],
                      szInfoTitle=(title or self.title)[:63], dwInfoFlags=0x81)

    def _on_notify(self, wparam, lparam):
        if lparam == 0x402:  # NIN_BALLOONSHOW, confirmed by the Windows shell.
            self.balloon_shown.set()
        elif lparam == 0x405 and self.balloon_clicked:
            self.balloon_clicked()
        return super()._on_notify(wparam, lparam)


class Notifier:
    def __init__(self, report_path, stop, paused):
        self.report_path = report_path
        self.stop = stop
        self.paused = paused
        self.ready = threading.Event()
        image = Image.new("RGBA", (64, 64), "#173f32")
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((19, 15, 45, 50), radius=6, fill="#c7f293")
        draw.rectangle((23, 9, 41, 16), fill="white")
        menu = Menu(MenuItem("查看优惠候选", lambda icon, item: self.open_report(), default=True),
                    MenuItem(lambda item: "恢复监控" if paused.is_set() else "暂停监控", self.toggle),
                    MenuItem("退出监控", self.exit))
        self.icon = ObservedIcon("protein_deals_local", image, "指定群 · 群聊优惠", menu)
        self.icon.balloon_clicked = self.open_report

    def open_report(self):
        if self.report_path.is_file():
            os.startfile(str(self.report_path))

    def toggle(self, icon, item):
        self.paused.clear() if self.paused.is_set() else self.paused.set()
        self.icon.update_menu()

    def exit(self, icon=None, item=None):
        self.stop.set()
        self.icon.stop()

    def run(self,on_ready=None):
        self.initialization_error=None
        def setup(icon):
            try:
                icon.visible = True
                self.ready.set()
                if on_ready:
                    on_ready()
            except Exception:
                self.initialization_error="tray_initialization_unavailable"
                self.stop.set()
                icon.stop()
        self.icon.run(setup)
        if self.initialization_error:
            raise RuntimeError(self.initialization_error)

    def send(self, title, message, timeout=20):
        if not self.ready.wait(10):
            return False
        self.icon.balloon_shown.clear()
        try:
            self.icon.notify(message, title)
            return self.icon.balloon_shown.wait(timeout)
        except Exception:
            return False
