import pytest
from wowharness.keys import VK, encode_raw, encode_text, key_lparam

def test_encode_plain():
    assert encode_text("/hx") == [ord("/"), ord("h"), ord("x")]

def test_encode_text_pipe_is_doubled():
    assert encode_text("a|b") == [ord("a"), ord("|"), ord("|"), ord("b")]

def test_encode_text_quotes_backslash_and_latin():
    assert encode_text('"\\ø') == [ord('"'), ord("\\"), ord("ø")]

def test_encode_text_rejects_non_bmp():
    with pytest.raises(ValueError):
        encode_text("😀")

def test_lparam_up_sets_transition_bits():
    assert key_lparam(0x0D, 0x1C, up=False) == 1 | (0x1C << 16)
    assert key_lparam(0x0D, 0x1C, up=True) == 1 | (0x1C << 16) | (0xC0 << 24)

def test_named_keys():
    assert VK["enter"] == 0x0D and VK["w"] == 0x57 and VK["space"] == 0x20

def test_encode_text_rejects_control_characters():
    with pytest.raises(ValueError):
        encode_text("a\nb")
    with pytest.raises(ValueError):
        encode_text("a\x7fb")

def test_encode_text_rejects_wide_latin_and_keeps_o_stroke():
    with pytest.raises(ValueError):
        encode_text("€")
    assert encode_text("ø") == [0xF8]

def test_encode_raw_does_not_double_pipe():
    assert encode_raw("a|b") == [ord("a"), ord("|"), ord("b")]

def test_encode_raw_rejects_same_as_encode_text():
    with pytest.raises(ValueError):
        encode_raw("a\nb")
    with pytest.raises(ValueError):
        encode_raw("€")
