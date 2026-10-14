"""Live pixel channel: read the observer addon's colour strip from the WoW window, without ReloadUI.

The addon (HXPixel in HarnessObserver.lua) paints a strip of solid-colour cells at the top-left of
the screen. Each cell carries 6 bits (2 bits = 4 levels per colour channel: 0, 85, 170, 255), so
gamma, scaling blur and compression stay far from a decision boundary. A page is:

    cells 0..5   sync: white, black, white, black, grey 1/3, grey 2/3 (pitch and level calibration)
    column 0 of rows 1..: row markers, black on odd rows, white on even (vertical pitch)
    other cells  bytes, 3 bytes -> 4 cells, big-endian, row-major, zero padded
    bytes        header (19) + payload:
                 'H' 'X' ver cols(2) rows flags sess seq(2) off(4) len(2) drop ck(2)

`off` is the payload's byte offset in the addon's stream since the UI loaded (session `sess`), so
a missed page shows as a gap; `drop` counts lines the addon had to drop (its queue was full);
`flags` bit 0 says the payload starts at a line boundary; `ck` is Fletcher-16 over every other
header byte and the payload. The stream is lines `<kind><text>\\n`, kind `c` = chat, `k` = combat,
text in the HXDB format ("MM/DD HH:MM:SS.mmm  message"); kind `p` = the walk_to pose stream (`HX|pose=...`,
no timestamp, never in HXDB).

Pure encode/decode below runs on Linux (tests); PixelReader captures the window on Windows.
"""
from __future__ import annotations

import logging
import math
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field

log = logging.getLogger("wowharness.pixel")

MAGIC = b"HX"
VERSION = 1
HEADER_LEN = 19
SYNC = [63, 0, 63, 0, 21, 42]  # level*21 = the same level on all three channels
LEVEL_BYTES = (0, 85, 170, 255)
FLAG_LINE_START = 1


def fletcher16(data: bytes) -> int:
    s1 = s2 = 0
    for b in data:
        s1 = (s1 + b) % 255
        s2 = (s2 + s1) % 255
    return s2 * 256 + s1


def data_cells(cols: int, rows: int) -> list[int]:
    """Cell indices (row-major) that carry bytes: all but the 6 sync cells and the row markers
    (column 0 of rows 1.., black on odd rows, white on even) that give the vertical pitch."""
    return [i for i in range(len(SYNC), cols * rows) if i % cols]


def capacity(cols: int, rows: int) -> int:
    """Payload bytes one page can carry."""
    return (len(data_cells(cols, rows)) // 4) * 3 - HEADER_LEN


def page_bytes(cols: int, rows: int, sess: int, seq: int, off: int, payload: bytes, drop: int = 0,
               flags: int = FLAG_LINE_START) -> bytes:
    if len(payload) > capacity(cols, rows):
        raise ValueError(f"payload {len(payload)} > capacity {capacity(cols, rows)}")
    head = (MAGIC + bytes([VERSION, cols >> 8, cols & 255, rows, flags & 255, sess & 255, (seq >> 8) & 255, seq & 255])
            + (off & 0xFFFFFFFF).to_bytes(4, "big") + len(payload).to_bytes(2, "big") + bytes([drop & 255]))
    ck = fletcher16(head + payload)
    return head + bytes([ck >> 8, ck & 255]) + payload


def encode_cells(data: bytes, cols: int, rows: int) -> list[int]:
    """Cells (0..63) of one page, as the addon paints them: sync, row markers, then 4 cells per
    3 bytes in data_cells order, then zeros."""
    cells = [0] * (cols * rows)
    cells[:len(SYNC)] = SYNC
    for r in range(1, rows):
        cells[r * cols] = 0 if r % 2 else 63
    pad = data + bytes((-len(data)) % 3)
    where = data_cells(cols, rows)
    if len(pad) // 3 * 4 > len(where):
        raise ValueError("page does not fit the strip")
    for k, i in enumerate(range(0, len(pad), 3)):
        v = (pad[i] << 16) | (pad[i + 1] << 8) | pad[i + 2]
        for j, sh in enumerate((18, 12, 6, 0)):
            cells[where[4 * k + j]] = (v >> sh) & 63
    return cells


def cell_rgb(v: int) -> tuple[int, int, int]:
    return LEVEL_BYTES[v >> 4], LEVEL_BYTES[(v >> 2) & 3], LEVEL_BYTES[v & 3]


def render(cells: list[int], cols: int, rows: int, pitch: float, size: tuple[int, int],
           background=(40, 60, 30), pitch_y: float | None = None):
    """Test helper: a PIL image of the strip at a (possibly fractional, non-square) pitch,
    antialiased the way a stretched back buffer blends a cell edge that falls inside a pixel."""
    from PIL import Image
    ss, py = 8, pitch_y or pitch
    big = Image.new("RGB", (size[0] * ss, size[1] * ss), background)
    px = big.load()
    for i, v in enumerate(cells):
        r, c = divmod(i, cols)
        x0, x1 = round(c * pitch * ss), round((c + 1) * pitch * ss)
        y0, y1 = round(r * py * ss), round((r + 1) * py * ss)
        rgb = cell_rgb(v)
        for y in range(y0, min(y1, size[1] * ss)):
            for x in range(x0, min(x1, size[0] * ss)):
                px[x, y] = rgb
    return big.resize(size, Image.BOX)


@dataclass
class Page:
    sess: int
    seq: int
    off: int
    drop: int
    flags: int
    payload: bytes
    cols: int
    rows: int
    pitch: tuple[float, float]  # (x, y) pixels per cell: a 1024x768 back buffer stretched to the window is not square


def _lum(p) -> float:
    return (p[0] + p[1] + p[2]) / 3


def _edges(samples: list[float], want: int) -> list[float]:
    """Positions (pixel units, from the image edge) where a run starting light crosses 128."""
    if not samples or samples[0] < 128:
        return []
    edges, light = [], True
    for x in range(1, len(samples)):
        now = samples[x] >= 128
        if now != light:
            a, b = samples[x - 1], samples[x]
            edges.append(x - 1 + (a - 128) / (a - b) + 0.5 if a != b else x)
            light = now
            if len(edges) == want:
                break
    return edges


def _fit(edges: list[float], ks) -> float | None:
    if len(edges) < len(ks):
        return None
    p = sum(k * t for k, t in zip(ks, edges)) / sum(k * k for k in ks)
    if p < 1.5 or any(abs(t - k * p) > max(1.0, p / 3) for k, t in zip(ks, edges)):
        return None
    return p


def find_pitch(px, width: int, height: int) -> float | None:
    """Horizontal cell pitch in pixels from the sync cells: light/dark edges at 1, 2, 3 and 5 pitches."""
    y = 1 if height > 1 else 0
    return _fit(_edges([_lum(px[x, y]) for x in range(min(width, 200))], 4), (1, 2, 3, 5))


def find_pitch_y(px, width: int, height: int, pitch_x: float, rows: int) -> float | None:
    """Vertical pitch from column 0: white sync cell, then the row markers (black, white, ...)."""
    if rows < 2:
        return pitch_x
    x = min(int(pitch_x / 2), width - 1)
    return _fit(_edges([_lum(px[x, y]) for y in range(height)], rows - 1), range(1, rows))


class _Sampler:
    def __init__(self, px, width: int, height: int, pitch_x: float, pitch_y: float):
        self.px, self.w, self.h = px, width, height
        self.p = (pitch_x, pitch_y)
        self._spans: dict[tuple[int, int], list[int]] = {}
        lv = [self.rgb(0, c) for c in (1, 4, 5, 0)]  # black, 1/3, 2/3, white
        # per channel: thresholds halfway between the observed levels
        self.thr = [[(lv[k][ch] + lv[k + 1][ch]) / 2 for k in range(3)] for ch in range(3)]
        self.ok = all(lv[3][ch] - lv[0][ch] > 120 for ch in range(3))

    def _span(self, axis: int, i: int) -> list[int]:
        """Pixels wholly inside cell i along one axis (an edge pixel can be a blend of two cells);
        the middle three at most. Averaging them rides out noise."""
        got = self._spans.get((axis, i))
        if got is None:
            p = self.p[axis]
            lo, hi = math.ceil(i * p - 0.05), math.floor((i + 1) * p + 0.05) - 1
            got = list(range(lo, hi + 1)) or [int((i + 0.5) * p)]
            while len(got) > 3:
                got = got[1:-1] if len(got) > 4 else got[:3]
            self._spans[(axis, i)] = got
        return got

    def rgb(self, r: int, c: int):
        xs, ys = self._span(0, c), self._span(1, r)
        if xs[-1] >= self.w or ys[-1] >= self.h:
            raise IndexError("cell outside the captured region")
        if len(xs) == 1 and len(ys) == 1:
            return self.px[xs[0], ys[0]]
        acc = [0, 0, 0]
        for y in ys:
            for x in xs:
                q = self.px[x, y]
                acc[0] += q[0]; acc[1] += q[1]; acc[2] += q[2]
        n = len(xs) * len(ys)
        return acc[0] / n, acc[1] / n, acc[2] / n

    def cell(self, r: int, c: int) -> int:
        rgb = self.rgb(r, c)
        v = 0
        for ch in range(3):
            t = self.thr[ch]
            lvl = 0 if rgb[ch] < t[0] else 1 if rgb[ch] < t[1] else 2 if rgb[ch] < t[2] else 3
            v = v * 4 + lvl
        return v

    def bytes_(self, where: list[int], nbytes: int, cols: int) -> bytes:
        out = bytearray()
        k = 0
        while len(out) < nbytes:
            v = 0
            for j in range(4):
                r, c = divmod(where[k + j], cols)
                v = (v << 6) | self.cell(r, c)
            k += 4
            out += bytes([(v >> 16) & 255, (v >> 8) & 255, v & 255])
        return bytes(out[:nbytes])


_HEAD_CELLS = list(range(len(SYNC), len(SYNC) + 28))  # the header's 28 cells, on row 0 when cols >= 34


def decode_at(px, width: int, height: int, pitch_x: float, pitch_y: float | None = None) -> Page | None:
    try:
        s = _Sampler(px, width, height, pitch_x, pitch_y or pitch_x)
        if not s.ok:
            return None
        head = s.bytes_(_HEAD_CELLS, 21, 1 << 30)
        if head[:2] != MAGIC or head[2] != VERSION:
            return None
        cols, rows = (head[3] << 8) | head[4], head[5]
        n = int.from_bytes(head[14:16], "big")
        if cols < 34 or rows < 1 or n > capacity(cols, rows):
            return None
        if pitch_y is None:
            pitch_y = find_pitch_y(px, width, height, pitch_x, rows)
            if pitch_y is None:
                return None
            s = _Sampler(px, width, height, pitch_x, pitch_y)
        data = s.bytes_(data_cells(cols, rows), HEADER_LEN + n, cols)
    except IndexError:
        return None
    ck = (data[17] << 8) | data[18]
    if fletcher16(data[:17] + data[HEADER_LEN:]) != ck:
        return None
    return Page(sess=data[7], seq=(data[8] << 8) | data[9], off=int.from_bytes(data[10:14], "big"),
                drop=data[16], flags=data[6], payload=data[HEADER_LEN:], cols=cols, rows=rows,
                pitch=(pitch_x, pitch_y))


def decode(img, pitch_hint: tuple[float, float] | None = None) -> Page | None:
    """One page from a PIL image whose top-left pixel is the strip's top-left, or None."""
    px, (w, h) = img.load(), img.size
    if pitch_hint:
        page = decode_at(px, w, h, *pitch_hint)
        if page:
            return page
    p0 = find_pitch(px, w, h)
    if p0 is None:
        return None
    # The sync edges give the pitch to ~0.1 px, but cell 160 needs it to ~0.002 px; let the checksum
    # pick, nearest candidates first. Slow (up to ~100 decodes), so PixelReader keeps the found pitch.
    for k in range(0, 97):
        p = p0 + (k + 1) // 2 * 0.0025 * (1 if k % 2 else -1)
        if p < 1.5:
            continue
        page = decode_at(px, w, h, p)
        if page:
            return page
    return None


@dataclass
class LiveLine:
    t: float  # harness clock (time.time()) when the page carrying the line's end was decoded
    kind: str  # "chat" | "combat" | "pose" | "other"
    text: str


KINDS = {"c": "chat", "k": "combat", "p": "pose"}  # pose: HX|pose lines for walk_to (strip only)


@dataclass
class Stream:
    """Reassembles pages into lines: drops repeats, notes gaps (missed pages) and addon-side drops."""
    lines: list[LiveLine] = field(default_factory=list)
    pages: int = 0
    repeats: int = 0
    gaps: int = 0
    lost_bytes: int = 0
    dropped_lines: int = 0
    sessions: int = 0
    sess: int | None = None
    expect: int = 0
    last_seq: int | None = None
    last_drop: int = 0
    partial: bytes = b""
    skip: bool = False
    last_page_t: float = 0.0

    def feed(self, page: Page, t: float) -> int:
        """Add a page; returns how many complete lines it finished."""
        self.last_page_t = t
        if page.sess != self.sess:
            self.sess, self.sessions = page.sess, self.sessions + 1
            self.expect, self.partial, self.last_seq = page.off, b"", None
            self.skip = not (page.flags & FLAG_LINE_START)
            self.last_drop = page.drop
        if page.seq == self.last_seq:
            self.repeats += 1
            return 0
        self.last_seq = page.seq
        self.pages += 1
        if page.drop != self.last_drop:
            self.dropped_lines += (page.drop - self.last_drop) % 256
            self.last_drop = page.drop
        end = page.off + len(page.payload)
        if page.off > self.expect:
            self.gaps += 1
            self.lost_bytes += page.off - self.expect
            self.partial = b""
            self.skip = not (page.flags & FLAG_LINE_START)
            data = page.payload
        elif end <= self.expect:
            return 0
        else:
            data = page.payload[self.expect - page.off:]
        self.expect = end
        parts = (self.partial + data).split(b"\n")
        self.partial = parts.pop()
        n = 0
        for raw in parts:
            if self.skip:  # the tail of a line whose start was never seen
                self.skip = False
                continue
            if not raw:
                continue
            text = raw.decode("utf-8", errors="replace")
            self.lines.append(LiveLine(t, KINDS.get(text[0], "other"), text[1:]))
            n += 1
        return n


def select(lines: list[LiveLine], since: int, kind: str | None = None) -> tuple[list[LiveLine], int]:
    since = max(0, since)
    picked = lines[since:]
    if kind:
        picked = [l for l in picked if l.kind == kind]
    return picked, len(lines)


class PixelReader:
    """Background thread: capture the strip region of the WoW window, decode, append to a Stream.

    Capture is BitBlt from the window's own DC, which returns the game's frame even while another
    window covers it (checked live 2026-10-12: both clients full-screen at the same spot). A
    minimised window stops rendering, so nothing new arrives until it is restored.

    Stale capture (seen live 2026-10-12, builds 12 and 13): the window-DC BitBlt kept returning the
    same old frame for minutes while the game went on rendering - the game's own screenshots showed
    the strip several hundred pages further on. The addon paints a new page (new seq) at least once a
    second, so a page that stays the same for `stale_s` means the capture is stale: the reader then
    moves to the next capture method (PrintWindow with PW_RENDERFULLCONTENT, then the screen DC),
    and every `primary_retry_s` tries the window DC again and goes back to it once it is fresh.

    The thread logs every error and switch (logger "wowharness.pixel" and the `events` ring), and a
    loop that dies on an exception is restarted after a short pause; health() sums it up."""

    STALE_S = 3.0  # the addon's idle heartbeat is 1 s (PIX_IDLE_S)
    PRIMARY_RETRY_S = 30.0

    def __init__(self, hwnd, interval_s: float = 0.03, region: tuple[int, int] = (1024, 48), grab=None,
                 grabs: list | None = None, stale_s: float | None = None, primary_retry_s: float | None = None,
                 clock=time.time):
        """hwnd: the window handle, or a callable returning the current one (the client can re-create
        its window, see Window.alive). grab: one capture function (tests); grabs: [(name, fn), ...]
        in order of preference; default window DC, PrintWindow, screen DC."""
        self._hwnd, self.interval_s, self.region = hwnd, interval_s, region
        self.stream = Stream()
        self.bad = 0  # captures with a strip-like sync but no valid page (corrupt / mid-paint)
        self.blank = 0  # captures with no strip at all
        self.captures = 0
        self.grab_s = 0.0
        self.cpu_s = 0.0
        self.started = 0.0
        self.error: str | None = None
        self.errors = 0
        self.restarts = 0  # times the capture loop died on an exception and was started again
        self.switches = 0  # capture-method changes (stale frames, or back to the window DC)
        self.events: deque = deque(maxlen=50)  # (time, text): errors, restarts, switches
        self.window = None  # the Window this reader follows (set by Client.live)
        self.pitch: tuple[float, float] | None = None
        self._fails = 0
        if grabs is None:
            grabs = [("test", grab)] if grab else [("window", self._gdi_grab), ("print", self._print_grab),
                                                   ("screen", self._screen_grab)]
        self._grabs = list(grabs)
        self._gi = 0
        self.stale_s = self.STALE_S if stale_s is None else stale_s
        self.primary_retry_s = self.PRIMARY_RETRY_S if primary_retry_s is None else primary_retry_s
        self._clock = clock
        self.restart_pause_s = 1.0
        self.keep_display_on = True
        self._switch_log_t = -1e9
        self._seen: tuple[int, int] | None = None  # (sess, seq) of the newest page
        self.fresh_t = 0.0  # stale clock of the current capture method (reset on a switch)
        self.new_page_t = 0.0  # when a page with a NEW (sess, seq) was last read: what health() trusts
        self.stale_switches = 0  # method switches since the last new page
        self._method_t = 0.0  # when the current capture method was picked
        self._primary_try_t = 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def hwnd(self) -> int:
        return self._hwnd() if callable(self._hwnd) else self._hwnd

    @property
    def grab_method(self) -> str:
        return self._grabs[self._gi][0]

    def _event(self, text: str, level: int = logging.WARNING) -> None:
        self.events.append((round(self._clock(), 3), text))
        log.log(level, "pixel reader: %s", text)

    # -- capture (Windows) --
    def _capture(self, mode: str):
        import win32gui, win32ui, win32con
        from PIL import Image
        hwnd = self.hwnd
        _, _, cw, ch = win32gui.GetClientRect(hwnd)
        w, h = min(cw, self.region[0]), min(ch, self.region[1])
        if w <= 0 or h <= 0:
            return None
        src_wnd = 0 if mode == "screen" else hwnd
        src = win32gui.GetDC(src_wnd)
        mdc = bmp = None
        try:
            mdc = win32gui.CreateCompatibleDC(src)
            bmp = win32gui.CreateCompatibleBitmap(src, w, h)
            old = win32gui.SelectObject(mdc, bmp)
            if mode == "print":
                import ctypes
                # PW_CLIENTONLY | PW_RENDERFULLCONTENT: DWM renders the window's current content (also
                # DirectX), clipped to the bitmap; ~15 ms against ~2 ms for the window-DC BitBlt.
                if not ctypes.windll.user32.PrintWindow(hwnd, mdc, 3):
                    raise OSError("PrintWindow failed")
            elif mode == "screen":
                x, y = win32gui.ClientToScreen(hwnd, (0, 0))  # only right while nothing covers the strip
                win32gui.BitBlt(mdc, 0, 0, w, h, src, x, y, win32con.SRCCOPY)
            else:
                win32gui.BitBlt(mdc, 0, 0, w, h, src, 0, 0, win32con.SRCCOPY)
            win32gui.SelectObject(mdc, old)
            raw = win32ui.CreateBitmapFromHandle(bmp).GetBitmapBits(True)
        finally:
            if bmp:
                win32gui.DeleteObject(bmp)
            if mdc:
                win32gui.DeleteDC(mdc)
            win32gui.ReleaseDC(src_wnd, src)
        return Image.frombuffer("RGB", (w, h), raw, "raw", "BGRX", 0, 1)

    def _gdi_grab(self):
        return self._capture("window")

    def _print_grab(self):
        return self._capture("print")

    def _screen_grab(self):
        return self._capture("screen")

    def _decode(self, img) -> Page | None:
        if self.pitch and self._fails < 5:  # a torn or covered frame: retry the known pitch first
            return decode_at(img.load(), img.width, img.height, *self.pitch)
        return decode(img, self.pitch)

    def _switch(self, to: int, why: str, now: float) -> None:
        was = self.grab_method
        self._gi = to
        self.switches += 1
        self.stale_switches += 1
        self._method_t = self._primary_try_t = now
        self.fresh_t = now  # give the new method its own stale_s
        # a minimised game cycles through every method; say so once a minute, not every stale_s
        loud = now - self._switch_log_t >= 60
        if loud:
            self._switch_log_t = now
        self._event(f"capture {was} -> {self.grab_method}: {why}", logging.WARNING if loud else logging.DEBUG)

    def _try_primary(self, now: float) -> None:
        """On a fallback method: go back to the cheaper window DC once it shows the newest page."""
        self._primary_try_t = now
        try:
            img = self._grabs[0][1]()
            page = self._decode(img) if img is not None else None
        except Exception as err:
            self._event(f"window-DC retry failed: {type(err).__name__}: {err}", logging.INFO)
            return
        # as new as the newest page the fallback saw (seq is 16 bits and wraps)
        if page is not None and self._seen and page.sess == self._seen[0] and (page.seq - self._seen[1]) % 65536 < 32768:
            self._switch(0, "the window DC shows the current page again", now)

    def poll_once(self) -> Page | None:
        now0 = self._clock()
        if self._gi and self._grabs and now0 - self._primary_try_t >= self.primary_retry_s:
            self._try_primary(now0)
        t0 = time.perf_counter()
        img = self._grabs[self._gi][1]()
        self.grab_s += time.perf_counter() - t0
        self.captures += 1
        now = self._clock()
        page = self._decode(img) if img is not None else None
        self._fails = 0 if page else self._fails + 1
        if page is None:
            if img is None or find_pitch(img.load(), *img.size) is None:
                self.blank += 1
            else:
                self.bad += 1
            # a fallback that shows no strip at all (the screen DC under another window) is no better
            if self._gi and now - self.fresh_t > self.stale_s:
                self._switch((self._gi + 1) % len(self._grabs), f"no strip for {self.stale_s:g} s", now)
            return None
        self.pitch = page.pitch
        if (page.sess, page.seq) != self._seen:
            self._seen, self.fresh_t, self.new_page_t = (page.sess, page.seq), now, now
            self.stale_switches = 0
        elif len(self._grabs) > 1 and now - self.fresh_t > self.stale_s:
            self._switch((self._gi + 1) % len(self._grabs),
                         f"page seq {page.seq} unchanged for {now - self.fresh_t:.1f} s (stale frame)", now)
        with self._lock:
            self.stream.feed(page, now)
        return page

    def _loop(self) -> None:
        c0 = time.thread_time()
        errs = 0
        while not self._stop.is_set():
            try:
                self.poll_once()
                errs = 0
            except Exception as err:  # keep reading; report the last error in stats and the log
                self.errors += 1
                errs += 1
                self.error = f"{type(err).__name__}: {err}"
                if errs in (1, 10) or errs % 1000 == 0:
                    self._event(f"capture error x{errs}: {self.error}")
                if errs >= 10:
                    self.pitch, self._fails = None, 0  # start the pitch search over
            self.cpu_s = time.thread_time() - c0
            self._stop.wait(self.interval_s)

    def _run(self):
        try:
            import ctypes
            # the game draws at its DPI-unaware size; read it 1:1
            ctypes.windll.user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-1))
        except Exception:
            pass
        if self.keep_display_on:
            # Seen live 2026-10-12: when the laptop's display went off (Kernel-Power 566 at 15:15:28,
            # 15:54:07, 16:13:19) the desktop stopped being composed and EVERY capture (window DC,
            # PrintWindow, screen) returned the frame of that moment, while the game rendered on.
            # Hold the display on while the reader runs, as a remote-control session does
            # (ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED; per thread, dropped when it ends).
            # It does not wake a display that is already off.
            try:
                import ctypes
                ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x1 | 0x2)
            except Exception:
                pass
        while not self._stop.is_set():
            try:
                self._loop()
            except Exception as err:  # the loop itself died: log, pause, start it again
                self.restarts += 1
                self.error = f"{type(err).__name__}: {err}"
                self._event(f"reader loop died ({self.error}); restart {self.restarts} in "
                            f"{self.restart_pause_s:g} s", logging.ERROR)
                self._stop.wait(self.restart_pause_s)

    def start(self) -> "PixelReader":
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self.started = time.time()
            self._thread = threading.Thread(target=self._run, name="hx-pixel", daemon=True)
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(2)

    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # -- reading --
    def lines(self, since: int = 0, kind: str | None = None) -> tuple[list[LiveLine], int]:
        with self._lock:
            return select(self.stream.lines, since, kind)

    def wait(self, pattern: str, since: int, timeout_s: float, kind: str | None = None) -> LiveLine | None:
        rx, deadline = re.compile(pattern), time.time() + timeout_s
        while True:
            got, since = self.lines(since, kind)
            for line in got:
                if rx.search(line.text):
                    return line
            if time.time() >= deadline:
                return None
            time.sleep(0.05)

    def stats(self) -> dict:
        s = self.stream
        up = max(time.time() - self.started, 1e-9) if self.started else 0
        return {"running": self.running(), "pages": s.pages, "repeats": s.repeats, "gaps": s.gaps,
                "lost_bytes": s.lost_bytes, "addon_dropped_lines": s.dropped_lines, "sessions": s.sessions,
                "bad_captures": self.bad, "blank_captures": self.blank, "captures": self.captures,
                "lines": len(s.lines), "pitch": self.pitch,
                "last_page_age_s": round(time.time() - s.last_page_t, 2) if s.last_page_t else None,
                "grab_ms_avg": round(1000 * self.grab_s / self.captures, 2) if self.captures else None,
                "cpu_pct": round(100 * self.cpu_s / up, 2) if up else None, "error": self.error,
                "grab": self.grab_method, "switches": self.switches, "restarts": self.restarts,
                "errors": self.errors,
                "new_page_age_s": round(self._clock() - self.new_page_t, 2) if self.new_page_t else None,
                "seen": list(self._seen) if self._seen else None}

    def health(self) -> dict:
        """Is the live channel delivering? ok = the thread runs and a NEW page (the addon paints one at
        least every second) arrived within stale_s. A tester checks this before trusting live reads."""
        now = self._clock()
        age = now - self.new_page_t if self.new_page_t else None
        if not self.running():
            ok, why = False, "reader thread not running"
        elif age is None:
            ok, why = False, "no page read yet (strip hidden, loading screen, old addon, or client not in world?)"
        elif age > self.stale_s and self.stale_switches >= len(self._grabs) > 1:
            ok, why = False, (f"no new page for {age:.1f} s and every capture method shows the same old frame "
                              f"(page {self._seen[1]}): the desktop is not being composed - display off, "
                              f"screen locked or a remote session ended? The game's own screenshot still "
                              f"renders; wake the display (a key or mouse touch on that PC)")
        elif age > self.stale_s:
            ok, why = False, (f"no new page for {age:.1f} s: game minimised or not rendering, the addon "
                              f"stopped painting, or the capture is stale")
        else:
            ok, why = True, "ok"
        s = self.stream
        return {"ok": ok, "reason": why, "running": self.running(), "seen": list(self._seen) if self._seen else None,
                "new_page_age_s": round(age, 2) if age is not None else None, "grab": self.grab_method,
                "switches": self.switches, "restarts": self.restarts, "errors": self.errors,
                "last_error": self.error, "lines": len(s.lines), "gaps": s.gaps, "lost_bytes": s.lost_bytes,
                "addon_dropped_lines": s.dropped_lines,
                "events": [f"{time.strftime('%H:%M:%S', time.localtime(t))} {e}" for t, e in list(self.events)[-8:]]}
