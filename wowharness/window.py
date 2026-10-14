"""The WoW window by handle. Imports Win32 lazily so the package loads on Linux for tests."""
import ctypes
import subprocess
import time

from .keys import key_lparam


# Startup dialogs the harness may answer; any other dialog stops the launch with its text.
DIALOG_ANSWERS = {"Hardware changed": "&No"}  # No keeps gxWindow/movie as Config.wtf has them


def dialog_answer(text: str) -> str | None:
    return next((btn for key, btn in DIALOG_ANSWERS.items() if key in text), None)


class Window:
    def __init__(self, proc: subprocess.Popen, hwnd: int):
        self.proc, self.hwnd = proc, hwnd

    @classmethod
    def launch(cls, exe: str, cwd: str, timeout_s: float = 60) -> "Window":
        import win32gui, win32process
        import win32con
        proc = subprocess.Popen([exe], cwd=cwd)
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            found: list[int] = []
            def cb(h, _):
                if win32gui.IsWindowVisible(h) and win32process.GetWindowThreadProcessId(h)[1] == proc.pid:
                    found.append(h)
            win32gui.EnumWindows(cb, None)
            for h in found:
                cls_name = win32gui.GetClassName(h)
                if cls_name.startswith("GxWindowClass"):
                    return cls(proc, h)
                if cls_name == "#32770":  # a message box before the game window
                    texts: list[str] = []
                    win32gui.EnumChildWindows(h, lambda c, _: texts.append(win32gui.GetWindowText(c)), None)
                    text = " ".join(t for t in texts if not t.startswith("&"))
                    btn = dialog_answer(text)
                    if btn is None:
                        raise RuntimeError(f"WoW.exe dialog: {text}")
                    win32gui.SendMessage(win32gui.FindWindowEx(h, 0, "Button", btn), win32con.BM_CLICK, 0, 0)
            if proc.poll() is not None:
                break
            time.sleep(0.5)
        raise RuntimeError(f"WoW.exe window did not appear (exit code {proc.poll()})")

    def alive(self) -> bool:
        import win32gui
        if self.proc.poll() is not None:
            return False
        if not win32gui.IsWindow(self.hwnd):
            # Seen live 2026-10-12: a display change made both clients re-create their game window
            # (new handle, same process). Follow the process to its new window.
            self.hwnd = self.find_game_window(self.proc.pid) or self.hwnd
        return bool(win32gui.IsWindow(self.hwnd))

    @staticmethod
    def find_game_window(pid: int) -> int | None:
        import win32gui, win32process
        found: list[int] = []
        def cb(h, _):
            if (win32gui.IsWindowVisible(h) and win32gui.GetClassName(h).startswith("GxWindowClass")
                    and win32process.GetWindowThreadProcessId(h)[1] == pid):
                found.append(h)
        win32gui.EnumWindows(cb, None)
        return found[0] if found else None

    def exit_code(self) -> int | None:
        return self.proc.poll()

    def _restore_if_minimised(self) -> None:
        import win32con, win32gui
        if win32gui.IsIconic(self.hwnd):
            win32gui.ShowWindow(self.hwnd, win32con.SW_SHOWNOACTIVATE)

    def post_key(self, vk: int, down: bool) -> None:
        import win32con, win32gui
        self._restore_if_minimised()
        scan = ctypes.windll.user32.MapVirtualKeyW(vk, 0)
        msg = win32con.WM_KEYDOWN if down else win32con.WM_KEYUP
        win32gui.PostMessage(self.hwnd, msg, vk, key_lparam(vk, scan, up=not down))

    def post_char(self, code: int) -> None:
        import win32con, win32gui
        win32gui.PostMessage(self.hwnd, win32con.WM_CHAR, code, 1)

    def click(self, fx: float, fy: float) -> None:
        """WoW reads the real cursor position, so a posted click alone does nothing: move the cursor
        there for ~0.3 s, post the click, and put the cursor back."""
        import win32api, win32con, win32gui
        self._restore_if_minimised()
        _, _, w, h = win32gui.GetClientRect(self.hwnd)
        x, y = int(fx * w), int(fy * h)
        old = win32api.GetCursorPos()
        win32api.SetCursorPos(win32gui.ClientToScreen(self.hwnd, (x, y)))
        try:
            time.sleep(0.05)
            lp = (y << 16) | x
            win32gui.PostMessage(self.hwnd, win32con.WM_MOUSEMOVE, 0, lp)
            time.sleep(0.05)
            win32gui.PostMessage(self.hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lp)
            time.sleep(0.08)
            win32gui.PostMessage(self.hwnd, win32con.WM_LBUTTONUP, 0, lp)
            time.sleep(0.1)
        finally:
            win32api.SetCursorPos(old)
