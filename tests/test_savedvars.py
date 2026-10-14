from wowharness.savedvars import SavedLog, parse_lua_assignments

SAMPLE = '''
HXDB = {
\t["chat"] = {
\t\t"10/11 16:30:00.123  HX|ready|Harnessa", -- [1]
\t\t"10/11 16:30:01.000  [Aela] whispers: say \\"hi\\" \\\\ ok", -- [2]
\t},
\t["chat_first"] = 1,
\t["combat"] = {
\t\t"10/11 16:30:02.500  You hit Kobold Vermin for 5.", -- [1]
\t},
\t["combat_first"] = 41,
\t["flag"] = true,
}
Other = nil
'''


def test_parse_strings_numbers_nested_and_escapes():
    v = parse_lua_assignments(SAMPLE)
    assert v["HXDB"]["chat"] == ["10/11 16:30:00.123  HX|ready|Harnessa",
                                 '10/11 16:30:01.000  [Aela] whispers: say "hi" \\ ok']
    assert v["HXDB"]["combat_first"] == 41 and v["HXDB"]["flag"] is True and v["Other"] is None


def write(tmp_path, text):
    p = tmp_path / "HarnessObserver.lua"
    p.write_text(text)
    return SavedLog(str(p), "combat")


def test_offsets_are_absolute_line_numbers(tmp_path):
    log = write(tmp_path, SAMPLE)
    assert log.size() == 42  # first 41 + 1 line
    assert log.read_since(41) == (["10/11 16:30:02.500  You hit Kobold Vermin for 5."], 42, False)
    assert log.read_since(42) == ([], 42, False)


def test_dropped_lines_report_restarted(tmp_path):
    log = write(tmp_path, SAMPLE)
    lines, off, restarted = log.read_since(10)  # 10..40 were dropped from the ring
    assert lines == ["10/11 16:30:02.500  You hit Kobold Vermin for 5."] and off == 42 and restarted


def test_offset_past_end_means_reset(tmp_path):
    log = write(tmp_path, SAMPLE)
    lines, off, restarted = log.read_since(500)
    assert restarted and off == 42 and len(lines) == 1


def test_missing_file_is_empty(tmp_path):
    log = SavedLog(str(tmp_path / "nope.lua"), "chat")
    assert log.size() == 1 and log.read_since(1) == ([], 1, False)
