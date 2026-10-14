"""Pure key encoding: names to virtual-key codes, text to WM_CHAR codes, lParam bits."""

VK = {
    "enter": 0x0D, "escape": 0x1B, "space": 0x20, "tab": 0x09, "backspace": 0x08, "printscreen": 0x2C,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    **{c: ord(c.upper()) for c in "abcdefghijklmnopqrstuvwxyz"},
    **{str(d): 0x30 + d for d in range(10)},
    **{f"f{n}": 0x6F + n for n in range(1, 13)},
}


def _check_char(ch: str) -> int:
    code = ord(ch)
    if code < 0x20 or code == 0x7F:
        raise ValueError(f"control character {code:#x} cannot be typed into WoW")
    if code > 0xFF:
        raise ValueError(f"character {ch!r} is outside the 1.12 ANSI range and cannot be typed")
    return code


def encode_raw(s: str) -> list[int]:
    """Codes for an edit box (login fields): every character as typed, no escaping."""
    return [_check_char(ch) for ch in s]


def encode_text(s: str) -> list[int]:
    """Codes for chat: WoW chat reads one | as an escape, so a literal bar is sent as ||."""
    out: list[int] = []
    for ch in s:
        code = _check_char(ch)
        out.append(code)
        if ch == "|":
            out.append(code)
    return out


def key_lparam(vk: int, scan: int, up: bool) -> int:
    lp = 1 | (scan << 16)
    return lp | (0xC0 << 24) if up else lp
