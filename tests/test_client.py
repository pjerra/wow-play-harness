import pytest
from wowharness.client import Client, ClientNotRunning
from wowharness.config import Config
import os

class FakeWindow:
    def __init__(self):
        self.events, self.is_alive, self.code = [], True, None
    def alive(self): return self.is_alive
    def exit_code(self): return self.code
    def post_key(self, vk, down): self.events.append(("key", vk, down))
    def post_char(self, c): self.events.append(("char", c))
    def click(self, fx, fy): self.events.append(("click", fx, fy))
    def screenshot(self, path): return path

def cfg(tmp_path):
    (tmp_path / "WTF").mkdir()
    (tmp_path / "WTF" / "Config.wtf").write_text('SET movie "1"\nSET realmlist "x"\n')
    (tmp_path / "Logs").mkdir()
    return Config(str(tmp_path), "192.0.2.10", 7878, "u", "p", "player", "pw", (0.5, 0.5), (0.5, 0.6), "")

def test_chat_sends_enter_text_enter(tmp_path):
    w = FakeWindow()
    c = Client(cfg(tmp_path), lambda: w); c.window = w
    c.chat("/hx")
    keys = [e for e in w.events if e[0] == "key"]
    chars = [e[1] for e in w.events if e[0] == "char"]
    assert keys[0] == ("key", 0x0D, True) and keys[-1] == ("key", 0x0D, False)
    assert chars == [ord("/"), ord("h"), ord("x")]

def test_run_lua_prefixes_script(tmp_path):
    w = FakeWindow()
    c = Client(cfg(tmp_path), lambda: w); c.window = w
    c.run_lua("AttackTarget()")
    chars = "".join(chr(e[1]) for e in w.events if e[0] == "char")
    assert chars == "/script AttackTarget()"

def test_dead_client_answers_not_running(tmp_path):
    w = FakeWindow(); w.is_alive, w.code = False, 3
    c = Client(cfg(tmp_path), lambda: w); c.window = w
    with pytest.raises(ClientNotRunning, match=r"client not running \(exit code 3\)"):
        c.chat("/hx")

def test_no_window_yet_is_not_running(tmp_path):
    c = Client(cfg(tmp_path), lambda: FakeWindow())
    with pytest.raises(ClientNotRunning):
        c.chat("/hx")

def test_ensure_config_wtf_sets_window_movie_realm(tmp_path):
    c = Client(cfg(tmp_path), lambda: FakeWindow())
    c.ensure_config_wtf()
    text = (tmp_path / "WTF" / "Config.wtf").read_text()
    assert 'SET gxWindow "1"' in text and 'SET movie "0"' in text and 'SET realmlist "192.0.2.10"' in text
    assert 'SET movie "1"' not in text
    assert 'SET accountName "player"' in text
    assert (tmp_path / "realmlist.wtf").read_bytes() == b"set realmlist 192.0.2.10\r\n"

def test_login_types_password_only_no_clicks_no_tab(tmp_path):
    w = FakeWindow()
    c = Client(cfg(tmp_path), lambda: w); c.window = w
    (tmp_path / "Logs" / "WoWChatLog.txt").write_text("")
    def fake_wait(pattern, since, timeout_s):
        return "HX|ready"
    c.wait_chat = fake_wait
    c.flush = lambda timeout_s=30: None
    c._sleep = lambda s: None
    c.login("player", "pw", 0, timeout_s=1)
    # posted clicks do not move focus in 1.18.1; accountName in Config.wtf puts focus on the password box
    assert not any(e[0] == "click" for e in w.events)
    assert not any(e[0] == "key" and e[1] == 0x09 for e in w.events)
    chars = [e[1] for e in w.events if e[0] == "char"]
    assert chars[:2] == [ord("p"), ord("w")] and ord("l") not in chars[:2]

class DyingWindow(FakeWindow):
    def __init__(self, die_after):
        super().__init__()
        self.die_after, self.n = die_after, 0
    def post_char(self, c):
        self.n += 1
        if self.n > self.die_after:
            self.is_alive, self.code = False, 7
            raise OSError("win32 error: window gone")
        super().post_char(c)

def test_client_dying_mid_chat_answers_not_running(tmp_path):
    w = DyingWindow(die_after=2)
    c = Client(cfg(tmp_path), lambda: w); c.window = w
    c._sleep = lambda s: None
    with pytest.raises(ClientNotRunning, match="exit code 7"):
        c.chat("/hello")

def test_login_types_password_without_doubling_pipe(tmp_path):
    w = FakeWindow()
    c = Client(cfg(tmp_path), lambda: w); c.window = w
    (tmp_path / "Logs" / "WoWChatLog.txt").write_text("")
    c.wait_chat = lambda pattern, since, timeout_s: "HX|ready"
    c.flush = lambda timeout_s=30: None
    c._sleep = lambda s: None
    c.login("player", "a|b", 0, timeout_s=1)
    chars = [e[1] for e in w.events if e[0] == "char"]
    assert chars[:3] == [ord("a"), ord("|"), ord("b")]

def test_chat_pipe_is_escaped_end_to_end(tmp_path):
    w = FakeWindow()
    c = Client(cfg(tmp_path), lambda: w); c.window = w
    c.chat('/script print("a|b")')
    typed = "".join(chr(e[1]) for e in w.events if e[0] == "char")
    assert typed == '/script print("a||b")'


class ShotWindow(FakeWindow):
    def __init__(self, shots):
        super().__init__(); self.shots = shots
    def post_key(self, vk, down):
        super().post_key(vk, down)
        if vk == 0x2C and down:
            (self.shots / "WoWScrnShot_1.tga").write_bytes(b"tga")

def test_screenshot_uses_the_games_own_key_and_converts(tmp_path):
    shots = tmp_path / "Screenshots"; shots.mkdir(parents=True)
    w = ShotWindow(shots)
    c = Client(cfg(tmp_path), lambda: w); c.window = w
    c._sleep = lambda s: None
    seen = []
    c._convert = lambda src, dst: (seen.append((src, dst)), open(dst, "wb").write(b"png"))
    out = c.screenshot(str(tmp_path / "out.png"))
    assert out == str(tmp_path / "out.png") and (tmp_path / "out.png").read_bytes() == b"png"
    assert seen[0][0].endswith("WoWScrnShot_1.tga")
    assert not (shots / "WoWScrnShot_1.tga").exists()

def test_screenshot_without_new_file_says_so(tmp_path):
    w = FakeWindow()
    c = Client(cfg(tmp_path), lambda: w); c.window = w
    c._sleep = lambda s: None
    with pytest.raises(RuntimeError, match="no screenshot"):
        c.screenshot(str(tmp_path / "out.png"))


def saved_path(tmp_path):
    return tmp_path / "WTF" / "Account" / "PLAYER" / "SavedVariables" / "HarnessObserver.lua"

class ReloadWindow(FakeWindow):
    """Writes the SavedVariables file when a typed /script ReloadUI() is sent, like the client does."""
    def __init__(self, path, chat_lines):
        super().__init__(); self.path, self.chat_lines, self.typed, self.reloads = path, chat_lines, "", 0
    def post_char(self, c):
        super().post_char(c); self.typed += chr(c)
    def post_key(self, vk, down):
        super().post_key(vk, down)
        if vk == 0x0D and not down:
            if "ReloadUI()" in self.typed:
                self.reloads += 1
                self.path.parent.mkdir(parents=True, exist_ok=True)
                body = "".join('\t\t"%s",\n' % l for l in self.chat_lines)
                self.path.write_text('HXDB = {\n\t["chat"] = {\n%s\t},\n\t["chat_first"] = 1,\n}\n' % body)
                os.utime(self.path, (1000 + self.reloads, 1000 + self.reloads))
            self.typed = ""

def test_logs_live_in_the_addons_savedvariables(tmp_path):
    c = Client(cfg(tmp_path), lambda: FakeWindow())
    assert c.chat_log.path == str(saved_path(tmp_path)) and c.chat_log.key == "chat"
    assert c.combat_log.path == str(saved_path(tmp_path)) and c.combat_log.key == "combat"

def test_offsets_and_waits_flush_with_reloadui(tmp_path):
    w = ReloadWindow(saved_path(tmp_path), ["a", "HX|ready|Harnessa"])
    c = Client(cfg(tmp_path), lambda: w); c.window = w
    c._sleep = lambda s: None
    assert c.chat_offset() == 3 and w.reloads == 1
    assert c.wait_chat(r"HX\|ready", 1, timeout_s=5) == "HX|ready|Harnessa" and w.reloads == 2
    assert c.read_chat(2) == (["HX|ready|Harnessa"], 3, False) and w.reloads == 3

def test_flush_without_file_write_says_addon_missing(tmp_path):
    w = FakeWindow()
    c = Client(cfg(tmp_path), lambda: w); c.window = w
    c._sleep = lambda s: None
    with pytest.raises(RuntimeError, match="HarnessObserver"):
        c.flush(timeout_s=1)


def test_click_goes_to_the_window(tmp_path):
    w = FakeWindow()
    c = Client(cfg(tmp_path), lambda: w); c.window = w
    c.click(0.25, 0.75)
    assert w.events == [("click", 0.25, 0.75)]


class FakeReader:
    def __init__(self, hwnd):
        from wowharness.pixel import LiveLine
        self.hwnd, self.started, self.stopped, self.window = hwnd(), 0, False, None
        self.data = [LiveLine(1.0, "chat", "10/12 10:00:00.000  HX|p=Me,100"),
                     LiveLine(1.1, "combat", "10/12 10:00:00.100  You hit X for 3.")]
    def start(self): self.started += 1
    def stop(self): self.stopped = True
    def running(self): return self.started > 0 and not self.stopped
    def lines(self, since, kind=None):
        from wowharness.pixel import select
        return select(self.data, since, kind)
    def wait(self, pattern, since, timeout_s, kind=None):
        import re
        got, _ = self.lines(since, kind)
        return next((l for l in got if re.search(pattern, l.text)), None)


def test_live_lines_start_one_reader_on_the_window_and_filter_by_kind(tmp_path):
    w = FakeWindow(); w.hwnd = 42
    c = Client(cfg(tmp_path), lambda: w); c.window = w
    made = []
    c._reader_factory = lambda h: made.append(FakeReader(h)) or made[-1]
    assert c.live_lines(0) == (["10/12 10:00:00.000  HX|p=Me,100", "10/12 10:00:00.100  You hit X for 3."], 2)
    assert c.live_lines(0, "combat") == (["10/12 10:00:00.100  You hit X for 3."], 2)
    assert c.wait_live(r"HX\|p=", 0, 1) == "10/12 10:00:00.000  HX|p=Me,100"
    assert c.live_offset() == 2
    assert len(made) == 1 and made[0].hwnd == 42 and made[0].started == 1
    assert not [e for e in w.events if e == ("key", 0x0D, True)]  # reading never types (no ReloadUI)


def test_live_needs_a_running_client(tmp_path):
    c = Client(cfg(tmp_path), lambda: FakeWindow())
    with pytest.raises(ClientNotRunning):
        c.live_lines(0)

def test_key_down_and_up_are_separate_events(tmp_path):
    w = FakeWindow()
    c = Client(cfg(tmp_path), lambda: w); c.window = w
    c.key_down("w"); c.key_down("A"); c.key_up("a"); c.key_up("w")
    assert w.events == [("key", 0x57, True), ("key", 0x41, True), ("key", 0x41, False), ("key", 0x57, False)]


def test_live_health_without_a_client_says_not_running(tmp_path):
    c = Client(cfg(tmp_path), lambda: FakeWindow())
    h = c.live_health()
    assert h["ok"] is False and h["running"] is False and "not running" in h["reason"]


def test_live_health_asks_the_reader(tmp_path):
    w = FakeWindow(); w.hwnd = 42
    c = Client(cfg(tmp_path), lambda: w); c.window = w
    made = []
    def factory(h):
        r = FakeReader(h)
        r.health = lambda: {"ok": True, "reason": "ok"}
        made.append(r)
        return r
    c._reader_factory = factory
    assert c.live_health() == {"ok": True, "reason": "ok"}
    assert len(made) == 1 and made[0].started == 1


def test_close_live_stops_the_reader(tmp_path):
    w = FakeWindow(); w.hwnd = 42
    c = Client(cfg(tmp_path), lambda: w); c.window = w
    made = []
    c._reader_factory = lambda h: made.append(FakeReader(h)) or made[-1]
    c.live_offset()
    c.close_live()
    assert made[0].stopped and c._reader is None
    c.close_live()  # twice is fine
