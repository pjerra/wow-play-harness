# wowharness/client.py
"""The game client: Config.wtf, launch, chat/lua/keys by handle, login, waits on the chat log."""
import glob
import os
import re
import time
from typing import Callable

from .config import Config
from .keys import VK, encode_raw, encode_text
from .pixel import PixelReader
from .savedvars import SavedLog

WTF_KEYS = {"gxWindow": "1", "movie": "0"}


class ClientNotRunning(Exception):
    pass


class Client:
    def __init__(self, cfg: Config, window_factory: Callable[[], object]):
        self.cfg, self.window_factory, self.window = cfg, window_factory, None
        # Logs\WoWChatLog.txt / WoWCombatLog.txt are written only at logout, so the observer addon keeps
        # the same lines in its SavedVariables, which the client writes on ReloadUI (see flush).
        saved = os.path.join(cfg.client_dir, "WTF", "Account", cfg.account.upper(), "SavedVariables",
                             "HarnessObserver.lua")
        self.chat_log = SavedLog(saved, "chat")
        self.combat_log = SavedLog(saved, "combat")
        self._sleep = time.sleep
        self._convert = _tga_to_png
        # Live channel: the addon's pixel strip read off the window (no ReloadUI); see pixel.py.
        self._reader: PixelReader | None = None
        self._reader_factory: Callable[[int], PixelReader] = PixelReader

    def _check(self):
        if self.window is None:
            raise ClientNotRunning("client not running (exit code None)")
        if not self.window.alive():
            raise ClientNotRunning(f"client not running (exit code {self.window.exit_code()})")
        return self.window

    def ensure_config_wtf(self) -> None:
        path = os.path.join(self.cfg.client_dir, "WTF", "Config.wtf")
        lines = open(path, encoding="utf-8").read().splitlines() if os.path.exists(path) else []
        # accountName makes the login screen open with focus in the password box: posted clicks
        # do not move focus in 1.18.1 and TAB does not either.
        want = {**WTF_KEYS, "realmlist": self.cfg.server_host, "accountName": self.cfg.account}
        kept = [l for l in lines if not any(re.match(rf'SET {k} ', l, re.I) for k in want)]
        kept += [f'SET {k} "{v}"' for k, v in want.items()]
        with open(path, "w", encoding="utf-8", newline="\r\n") as f:
            f.write("\n".join(kept) + "\n")
        # 1.12 clients read realmlist.wtf too; a stale one there would win.
        with open(os.path.join(self.cfg.client_dir, "realmlist.wtf"), "w", encoding="utf-8", newline="\r\n") as f:
            f.write(f"set realmlist {self.cfg.server_host}\n")

    def launch(self) -> dict:
        self.ensure_config_wtf()
        self.window = self.window_factory()
        return {"pid": self.window.proc.pid, "hwnd": self.window.hwnd}

    def _send(self, method: str, *args) -> None:
        """Every Window call goes through here: a window that dies mid-call answers ClientNotRunning."""
        w = self._check()
        try:
            getattr(w, method)(*args)
        except Exception:
            if self.window is not None and not self.window.alive():
                raise ClientNotRunning(f"client not running (exit code {self.window.exit_code()})") from None
            raise

    def _tap(self, vk: int) -> None:
        self._send("post_key", vk, True); self._sleep(0.03); self._send("post_key", vk, False)

    def chat(self, text: str) -> None:
        # WoW's chat edit box cuts input at 255 characters: a longer /script would arrive truncated and
        # raise a Lua error in game. Refuse it here; put long probes into the HarnessObserver addon instead.
        if len(text) > 255:
            raise ValueError(f"chat line is {len(text)} chars; WoW cuts at 255 - use an HX_* addon helper or split it")
        codes = encode_text(text)
        self._check()
        self._tap(VK["enter"])
        self._sleep(0.1)
        for c in codes:
            self._send("post_char", c); self._sleep(0.01)
        self._tap(VK["enter"])

    def run_lua(self, code: str) -> None:
        self.chat("/script " + code)

    def key(self, name: str, hold_ms: int = 0) -> None:
        self._check()
        vk = VK[name.lower()]
        self._send("post_key", vk, True); self._sleep(max(hold_ms, 30) / 1000); self._send("post_key", vk, False)

    def key_down(self, name: str) -> None:
        """Hold a key until key_up (walk_to: W while turning with A/D)."""
        self._send("post_key", VK[name.lower()], True)

    def key_up(self, name: str) -> None:
        self._send("post_key", VK[name.lower()], False)

    def click(self, fx: float, fy: float) -> None:
        self._send("click", fx, fy)

    def flush(self, timeout_s: float = 30) -> None:
        """ReloadUI makes the client write SavedVariables; wait for the file to change, then for the UI."""
        path = self.chat_log.path
        def mtime():
            try:
                return os.path.getmtime(path)
            except OSError:
                return None
        before = mtime()
        self.run_lua("ReloadUI()")
        for _ in range(int(timeout_s / 0.25)):
            now = mtime()
            if now is not None and now != before:
                self._sleep(3)  # the UI and addons load again before the next chat line is typed
                return
            self._sleep(0.25)
        raise RuntimeError(f"{path} was not written within {timeout_s} s (addon HarnessObserver missing or disabled?)")

    def chat_offset(self) -> int:
        self.flush()
        return self.chat_log.size()

    def combat_offset(self) -> int:
        self.flush()
        return self.combat_log.size()

    def read_chat(self, since: int) -> tuple[list[str], int, bool]:
        self.flush()
        return self.chat_log.read_since(since)

    def read_combat(self, since: int) -> tuple[list[str], int, bool]:
        self.flush()
        return self.combat_log.read_since(since)

    def wait_chat(self, pattern: str, since: int, timeout_s: float) -> str | None:
        """Each look costs a ReloadUI (about 5 s), so this polls slowly."""
        rx, offset, deadline = re.compile(pattern), since, time.time() + timeout_s
        while True:
            lines, offset, _ = self.read_chat(offset)
            for line in lines:
                if rx.search(line):
                    return line
            if time.time() >= deadline:
                return None

    def live(self) -> PixelReader:
        """The pixel-channel reader for the current window, started on first use. It only sees lines
        the addon shows after it starts, so start it (live_offset) before the thing you want to read."""
        w = self._check()
        r = self._reader
        if r is None or r.window is not w or not r.running():
            if r is not None:
                r.stop()
            # alive() follows a re-created game window to its new handle
            r = self._reader = self._reader_factory(lambda: (w.alive(), w.hwnd)[1])
            r.window = w
            r.start()
        return r

    def close_live(self) -> None:
        """Stop the reader thread (before replacing this Client, e.g. after a module reload)."""
        if self._reader is not None:
            self._reader.stop()
            self._reader = None

    def live_health(self) -> dict:
        """The live channel's health (PixelReader.health): ok, reason, age of the newest page, capture
        method, switches/restarts/errors and the last events. Starts (or restarts) the reader if needed."""
        try:
            r = self.live()
        except ClientNotRunning as err:
            return {"ok": False, "reason": str(err), "running": False}
        return r.health()

    def live_offset(self) -> int:
        return self.live().lines(0)[1]

    def live_lines(self, since: int, kind: str | None = None) -> tuple[list[str], int]:
        """Lines (chat, combat or both) the addon showed since a live offset, in the HXDB line format."""
        lines, nxt = self.live().lines(since, kind)
        return [l.text for l in lines], nxt

    def wait_live(self, pattern: str, since: int, timeout_s: float, kind: str | None = None) -> str | None:
        line = self.live().wait(pattern, since, timeout_s, kind)
        return line.text if line else None

    def screenshot(self, out_path: str, timeout_s: float = 10) -> str:
        """The game's own screenshot (PRINTSCREEN): renders even when other windows cover WoW."""
        shots = os.path.join(self.cfg.client_dir, "Screenshots")
        before = set(glob.glob(os.path.join(shots, "*.tga")))
        self._send("post_key", VK["printscreen"], True)
        self._send("post_key", VK["printscreen"], False)
        for _ in range(int(timeout_s / 0.25)):
            new = set(glob.glob(os.path.join(shots, "*.tga"))) - before
            if new:
                src = new.pop()
                self._sleep(0.5)  # let the client finish writing the file
                self._convert(src, out_path)
                os.remove(src)
                return out_path
            self._sleep(0.25)
        raise RuntimeError(f"no screenshot appeared in {shots} within {timeout_s} s")

    def login(self, account: str, password: str, char_index: int = 0, timeout_s: float = 90) -> dict:
        self._check()
        self._sleep(5)  # login screen settles; never ESC here (it quits during the intro)
        # account comes pre-filled from Config.wtf accountName (ensure_config_wtf); focus is on the password
        for c in encode_raw(password): self._send("post_char", c)
        self._tap(VK["enter"])
        self._sleep(10)  # realm handshake to character select
        for _ in range(char_index):
            self._tap(VK["down"])
        self._tap(VK["enter"])
        self._sleep(15)  # loading screen
        since = self.chat_offset()
        self.run_lua("HX_Ready()")
        line = self.wait_chat(r"HX\|ready", since, timeout_s)
        if line is None:
            return {"ok": False, "reason": "no HX|ready in WoWChatLog.txt (chat log not live, addon missing, or not in world)"}
        return {"ok": True, "line": line}


def _tga_to_png(src: str, dst: str) -> None:
    from PIL import Image
    Image.open(src).convert("RGB").save(dst)
