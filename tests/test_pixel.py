import os
import random
import shutil
import subprocess

import pytest

from wowharness import pixel
from wowharness.pixel import (Page, Stream, capacity, decode, encode_cells, fletcher16, page_bytes, render,
                              select, PixelReader)

COLS, ROWS = 160, 3
HERE = os.path.dirname(__file__)
ADDON = os.path.join(HERE, "..", "addon", "HarnessObserver", "HarnessObserver.lua")


def image(payload=b"chello\n", pitch=3.0, seq=1, off=0, sess=7, drop=0, flags=1, size=(1024, 48), **kw):
    data = page_bytes(COLS, ROWS, sess, seq, off, payload, drop, flags)
    return render(encode_cells(data, COLS, ROWS), COLS, ROWS, pitch, size, **kw)


def test_non_square_cells_of_a_stretched_back_buffer():
    # client 2 live: 1024x768 back buffer in a 1707x1067 window, 3-unit cells = 5.0 x 4.17 px
    payload = bytes(range(250, 0, -1)) + b"y" * 85
    page = decode(image(payload, pitch=5.0, pitch_y=4.168))
    assert page and page.payload == payload
    assert abs(page.pitch[0] - 5.0) < 0.01 and abs(page.pitch[1] - 4.168) < 0.05


def test_fletcher16_known_value():
    assert fletcher16(b"abcde") == 0xC8F0


def test_capacity_of_default_strip():
    assert capacity(COLS, ROWS) == 335


def test_round_trip_integer_pitch():
    payload = bytes(range(256)) + b"x" * 79
    page = decode(image(payload, pitch=3.0))
    assert page and page.payload == payload and page.seq == 1 and page.sess == 7 and page.off == 0


@pytest.mark.parametrize("pitch", [2.5, 3.0, 3.47, 4.17, 5.0])
def test_round_trip_fractional_pitch_with_blurred_edges(pitch):
    rnd = random.Random(int(pitch * 100))
    payload = bytes(rnd.randrange(256) for _ in range(capacity(COLS, ROWS)))
    page = decode(image(payload, pitch=pitch, seq=513, off=123456789, drop=3, flags=0, size=(1024, 24)))
    assert page is not None
    assert (page.payload, page.seq, page.off, page.drop, page.flags) == (payload, 513, 123456789, 3, 0)
    assert abs(page.pitch[0] - pitch) < 0.05 and abs(page.pitch[1] - pitch) < 0.05


def test_levels_survive_gamma_and_noise():
    from PIL import Image
    img = image(b"k" + b"Hits you for 12.\n" * 15, pitch=4.17)
    rnd = random.Random(1)
    gamma = img.point(lambda v: int(255 * (v / 255) ** 1.25))  # darker greys, as a gamma shift would give
    px = gamma.load()
    for y in range(gamma.size[1]):
        for x in range(gamma.size[0]):
            r, g, b = px[x, y]
            n = lambda v: max(0, min(255, v + rnd.randint(-20, 20)))
            px[x, y] = (n(r), n(g), n(b))
    page = decode(gamma)
    assert page and page.payload.startswith(b"kHits you")


def test_corrupt_cell_fails_the_checksum():
    img = image(b"cabcdefghijklmnop\n" * 5)
    px = img.load()
    for y in range(0, 3):  # one payload cell on row 0
        for x in range(120, 123):
            px[x, y] = (255, 255, 255) if px[x, y][0] < 128 else (0, 0, 0)
    assert decode(img) is None


def test_no_strip_decodes_to_none():
    from PIL import Image
    assert decode(Image.new("RGB", (1024, 48), (30, 50, 20))) is None


def pg(seq, off, payload, sess=1, flags=1, drop=0):
    return Page(sess, seq, off, drop, flags, payload, COLS, ROWS, 3.0)


def test_stream_joins_lines_across_pages_and_drops_repeats():
    s = Stream()
    s.feed(pg(1, 0, b"c10/12 10:00:00.000  one\nk10/12 10:00:00.100  tw"), 1.0)
    s.feed(pg(1, 0, b"c10/12 10:00:00.000  one\nk10/12 10:00:00.100  tw"), 1.1)  # same page seen again
    s.feed(pg(2, 48, b"o\n", flags=0), 1.2)
    assert [(l.kind, l.text) for l in s.lines] == [("chat", "10/12 10:00:00.000  one"),
                                                  ("combat", "10/12 10:00:00.100  two")]
    assert s.repeats == 1 and s.gaps == 0 and s.lines[1].t == 1.2


def test_stream_gap_skips_the_broken_line_and_counts_lost_bytes():
    s = Stream()
    s.feed(pg(1, 0, b"cfirst\ncsec"), 1.0)
    s.feed(pg(3, 20, b"ond-tail\ncthird\n", flags=0), 2.0)  # page 2 (bytes 11..19) missed
    assert [l.text for l in s.lines] == ["first", "third"]
    assert s.gaps == 1 and s.lost_bytes == 9


def test_stream_new_session_after_reload_restarts_offsets():
    s = Stream()
    s.feed(pg(5, 500, b"cold\n", sess=1), 1.0)
    s.feed(pg(1, 0, b"cnew\n", sess=2), 2.0)
    assert [l.text for l in s.lines] == ["old", "new"] and s.sessions == 2 and s.gaps == 0


def test_stream_counts_addon_drops_and_ignores_empty_heartbeats():
    s = Stream()
    s.feed(pg(1, 0, b"ca\n", drop=0), 1.0)
    s.feed(pg(2, 3, b"", drop=4), 2.0)
    s.feed(pg(3, 3, b"", drop=4), 3.0)
    assert len(s.lines) == 1 and s.dropped_lines == 4 and s.pages == 3


def test_select_filters_by_kind_and_returns_next_offset():
    s = Stream()
    s.feed(pg(1, 0, b"ca\nkb\ncc\n"), 1.0)
    got, nxt = select(s.lines, 1, "chat")
    assert [l.text for l in got] == ["c"] and nxt == 3


def test_reader_with_a_fake_grab():
    frames = [image(b"chi\n", seq=1), None, image(b"chi\n", seq=1), image(b"cthere\n", seq=2, off=4)]
    r = PixelReader(0, grab=lambda: frames.pop(0))
    for _ in range(4):
        r.poll_once()
    lines, nxt = r.lines(0)
    assert [l.text for l in lines] == ["hi", "there"] and nxt == 2
    st = r.stats()
    assert st["pages"] == 2 and st["repeats"] == 1 and st["blank_captures"] == 1 and st["bad_captures"] == 0


LUA = shutil.which("lua5.1")


@pytest.mark.skipif(not LUA, reason="lua5.1 not installed")
def test_addon_lua_encoder_matches_the_python_decoder(tmp_path):
    long = "x" * 400  # longer than one page: must continue on the next page
    ev = tmp_path / "events.txt"
    ev.write_text("VARIABLES_LOADED\nTICK\t0.2\n"
                  "CHAT_MSG_SAY\thello there\tBot\n"
                  f"CHAT_MSG_SYSTEM\t{long}\n"
                  "CHAT_MSG_SPELL_SELF_DAMAGE\tYour Fireball hits Kobold for 42.\n"
                  "TICK\t0.2\nTICK\t0.2\nTICK\t0.05\nTICK\t0.2\nTICK\t1.2\n")
    out = subprocess.run([LUA, os.path.join(HERE, "lua", "pixel_stub.lua"), ADDON, str(ev)],
                         capture_output=True, text=True, check=True).stdout
    pages = [list(map(int, l.split()[1:])) for l in out.splitlines() if l.startswith("PAGE ")]
    assert len(pages) == 6
    s = Stream()
    seqs = []
    for i, cells in enumerate(pages):
        assert len(cells) == COLS * ROWS
        p = decode(render(cells, COLS, ROWS, 3.47, (1024, 24)))
        assert p is not None, f"page {i} did not decode"
        seqs.append(p.seq)
        s.feed(p, float(i))
    assert [l.text for l in s.lines] == ["10/12 10:00:00.450  [Bot] says: hello there",
                                         "10/12 10:00:00.450  " + long,
                                         "10/12 10:00:00.450  Your Fireball hits Kobold for 42."]
    assert [l.kind for l in s.lines] == ["chat", "chat", "combat"]
    assert s.gaps == 0 and s.repeats == 2  # ticks with nothing new repaint nothing
    assert seqs[-1] == seqs[-2] + 1  # the idle heartbeat after 1 s is a new (empty) page


ARROW_STUBS = (
    'LUA\tlocal a = {} function a:IsObjectType(t) return t == "Model" end function a:GetName() return nil end '
    'function a:GetModel() return "Interface\\\\Minimap\\\\MinimapArrow" end function a:GetFacing() return 1.25 end '
    'Minimap = { GetChildren = function() return {}, a end } '
    'function GetPlayerMapPosition() return 0, 0 end function UnitExists() return nil end '
    'function ClearTarget() end function GetRealZoneText() return "Uldaman" end\n')


@pytest.mark.skipif(not LUA, reason="lua5.1 not installed")
def test_addon_pose_stream_and_walk_begin_reach_the_decoder(tmp_path):
    ev = tmp_path / "events.txt"
    ev.write_text("VARIABLES_LOADED\n" + ARROW_STUBS +
                  "LUA\tHX_WalkBegin()\nTICK\t0.15\nTICK\t0.15\nLUA\tHX_Probe()\nLUA\tHX_WalkEnd()\n"
                  "TICK\t0.15\nTICK\t0.15\n")
    out = subprocess.run([LUA, os.path.join(HERE, "lua", "pixel_stub.lua"), ADDON, str(ev)],
                         capture_output=True, text=True, check=True).stdout
    s = Stream()
    for i, l in enumerate(x for x in out.splitlines() if x.startswith("PAGE ")):
        p = decode(render(list(map(int, l.split()[1:])), COLS, ROWS, 3.47, (1024, 24)))
        assert p is not None
        s.feed(p, float(i))
    chat = [l.text for l in s.lines if l.kind == "chat"]
    pose = [l.text for l in s.lines if l.kind == "pose"]
    assert chat[0].endswith("HX|walk=begin|target=|facing=arrow|pos=none")
    assert any("HX|probe|UnitPosition=nil|GetPlayerFacing=nil|arrow=Interface\\Minimap\\MinimapArrow:1.25" in c
               for c in chat)
    assert chat[-1].endswith("HX|walk=end|retarget=none")
    assert pose and all(p.startswith("HX|pose=") and ",1.250,,,,0.0000,0.0000" in p for p in pose)
    n = len(pose)
    assert 1 <= n <= 3  # 0.1 s cadence over 0.3 s of ticks, then off


class Clock:
    def __init__(self, t=1000.0):
        self.t = t
    def __call__(self):
        return self.t


def frames_of(*pages):
    """A grab function that returns these images in turn, then repeats the last one."""
    left = list(pages)
    return lambda: left.pop(0) if len(left) > 1 else left[0]


def test_stale_window_frames_switch_to_the_next_capture_method():
    # live 2026-10-12: the window-DC BitBlt kept returning seq 1 while the game painted on
    clock = Clock()
    stale = image(b"cone\n", seq=1)
    printed = frames_of(image(b"ctwo\n", seq=7, off=5000))
    r = PixelReader(0, grabs=[("window", lambda: stale), ("print", printed)], clock=clock)
    r.poll_once()
    for _ in range(4):
        clock.t += 1.0
        r.poll_once()
    assert r.grab_method == "print" and r.switches == 1
    assert "stale frame" in r.events[-1][1]
    clock.t += 0.1
    r.poll_once()
    assert [l.text for l in r.lines(0)[0]] == ["one", "two"]
    assert r.stats()["gaps"] == 1  # the pages the stale capture hid
    assert r.health()["ok"] is False  # thread not started in this test
    assert r.health()["grab"] == "print"


def test_back_to_the_window_dc_once_it_shows_the_current_page_again():
    clock = Clock()
    cur, fixed = [1], [False]
    page = lambda: image(b"", seq=cur[0], off=0)  # the addon's heartbeat: a new seq every second
    window = lambda: page() if fixed[0] else image(b"", seq=1, off=0)
    r = PixelReader(0, grabs=[("window", window), ("print", page)], clock=clock, primary_retry_s=30)

    def tick(dt=1.0):
        clock.t += dt
        cur[0] += 1
        r.poll_once()
    r.poll_once()
    for _ in range(4):
        clock.t += 1.0
        r.poll_once()  # window DC stuck on seq 1
    assert r.grab_method == "print"
    for _ in range(29):
        tick()
    assert r.grab_method == "print" and r.switches == 1  # retried at 30 s: still stale, stay
    fixed[0] = True
    for _ in range(31):
        tick()
    assert r.grab_method == "window" and r.switches == 2
    assert "current page again" in r.events[-1][1]


def test_a_fallback_without_a_strip_moves_on():
    from PIL import Image
    clock = Clock()
    nothing = Image.new("RGB", (1024, 48), (30, 50, 20))
    r = PixelReader(0, grabs=[("window", lambda: image(b"cone\n", seq=1)), ("screen", lambda: nothing)],
                    clock=clock)
    r.poll_once()
    for _ in range(4):
        clock.t += 1.0
        r.poll_once()
    assert r.grab_method == "screen"
    for _ in range(4):
        clock.t += 1.0
        r.poll_once()
    assert r.grab_method == "window"  # wrapped round; no strip on the screen DC either


def test_no_strip_on_the_window_dc_is_not_staleness():
    from PIL import Image
    clock = Clock()
    nothing = Image.new("RGB", (1024, 48), (30, 50, 20))  # loading screen, /hx pix off
    r = PixelReader(0, grabs=[("window", lambda: nothing), ("print", lambda: nothing)], clock=clock)
    for _ in range(10):
        clock.t += 1.0
        r.poll_once()
    assert r.grab_method == "window" and r.switches == 0


def test_capture_errors_are_counted_logged_and_reading_goes_on(caplog):
    import threading, time as _t
    seen = threading.Event()
    calls = []
    def grab():
        calls.append(1)
        if len(calls) <= 2:
            raise OSError("BitBlt failed")
        seen.set()
        return image(b"cok\n", seq=1)
    r = PixelReader(0, grab=grab, interval_s=0.001)
    with caplog.at_level("WARNING", logger="wowharness.pixel"):
        r.start()
        assert seen.wait(5)
        deadline = _t.time() + 5
        while not r.lines(0)[0] and _t.time() < deadline:
            _t.sleep(0.01)
        r.stop()
    assert [l.text for l in r.lines(0)[0]] == ["ok"]
    assert r.errors == 2 and r.error == "OSError: BitBlt failed" and r.restarts == 0
    assert any("capture error x1: OSError: BitBlt failed" in m for m in caplog.messages)


def test_a_dead_reader_loop_is_logged_and_restarted(caplog):
    import threading
    r = PixelReader(0, grab=lambda: None)
    r.restart_pause_s = 0.01
    again = threading.Event()
    runs = []
    def loop():
        runs.append(1)
        if len(runs) == 1:
            raise MemoryError("decode blew up")
        again.set()
        r._stop.wait(5)
    r._loop = loop
    with caplog.at_level("ERROR", logger="wowharness.pixel"):
        r.start()
        assert again.wait(5)
        assert r.running()
        r.stop()
    assert r.restarts == 1 and "MemoryError" in r.error
    assert any("reader loop died (MemoryError: decode blew up); restart 1" in m for m in caplog.messages)


def test_health_says_when_no_new_page_arrives():
    import threading
    clock = Clock()
    r = PixelReader(0, grab=lambda: image(b"cone\n", seq=1), clock=clock)
    r._thread = threading.Thread(target=lambda: None)  # stands in for a running thread
    r._thread.is_alive = lambda: True
    h = r.health()
    assert h["ok"] is False and "no page read yet" in h["reason"]
    r.poll_once()
    clock.t += 1.5
    r.poll_once()
    assert r.health()["ok"] is True and r.health()["new_page_age_s"] == 1.5
    clock.t += 3
    r.poll_once()  # same seq, one grab method: nothing to switch to
    h = r.health()
    assert h["ok"] is False and "no new page for 4.5 s" in h["reason"] and r.switches == 0


def test_health_is_not_ok_while_every_method_shows_the_same_old_page():
    # drv1 2026-10-12 16:14-16:36: display off, window DC / PrintWindow both returned seq 2401, the screen
    # DC no strip; the switches reset the stale clock and health said ok. It must follow NEW pages only.
    import threading
    from PIL import Image
    clock = Clock()
    old = image(b"", seq=2401, off=0)
    nothing = Image.new("RGB", (1024, 48), (30, 50, 20))
    r = PixelReader(0, grabs=[("window", lambda: old), ("print", lambda: old), ("screen", lambda: nothing)],
                    clock=clock)
    r._thread = threading.Thread(target=lambda: None)
    r._thread.is_alive = lambda: True
    r.poll_once()
    order = []
    for _ in range(60):  # 30 s at 2 polls/s
        clock.t += 0.5
        r.poll_once()
        if not order or order[-1] != r.grab_method:
            order.append(r.grab_method)
        if clock.t - 1000 > 4:
            h = r.health()
            assert h["ok"] is False, h
    assert order[:7] == ["window", "print", "screen", "window", "print", "screen", "window"]  # keeps cycling
    h = r.health()
    assert "every capture method shows the same old frame (page 2401)" in h["reason"]
    assert h["new_page_age_s"] == 30.0 and h["seen"] == [7, 2401]
    assert r.stats()["new_page_age_s"] == 30.0


def test_health_recovers_on_the_first_new_page():
    import threading
    clock = Clock()
    cur = [5]
    r = PixelReader(0, grabs=[("window", lambda: image(b"", seq=cur[0])), ("print", lambda: image(b"", seq=cur[0]))],
                    clock=clock)
    r._thread = threading.Thread(target=lambda: None)
    r._thread.is_alive = lambda: True
    r.poll_once()
    for _ in range(10):
        clock.t += 1
        r.poll_once()
    assert r.health()["ok"] is False
    cur[0] = 6
    clock.t += 1
    r.poll_once()
    h = r.health()
    assert h["ok"] is True and h["new_page_age_s"] == 0 and r.stale_switches == 0


def test_the_reader_holds_the_display_on_only_where_windows_is():
    r = PixelReader(0, grab=lambda: None, interval_s=0.001)
    assert r.keep_display_on is True
    r.start(); r.stop()  # on Linux ctypes.windll is missing: the request is skipped, the thread runs
    assert r.restarts == 0
