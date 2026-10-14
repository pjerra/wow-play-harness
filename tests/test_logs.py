from wowharness.logs import LogTail

def test_reads_only_new_lines(tmp_path):
    f = tmp_path / "log.txt"
    f.write_text("a\nb\n")
    t = LogTail(str(f))
    start = t.size()
    with f.open("a") as w:
        w.write("c\nd\n")
    lines, off, restarted = t.read_since(start)
    assert lines == ["c", "d"] and off == f.stat().st_size and not restarted

def test_partial_last_line_waits(tmp_path):
    f = tmp_path / "log.txt"
    f.write_text("a\nhalf")
    lines, off, _ = LogTail(str(f)).read_since(0)
    assert lines == ["a"] and off == 2

def test_tail_after_truncate(tmp_path):
    f = tmp_path / "log.txt"
    f.write_text("x" * 100 + "\n")
    f.write_text("new\n")
    lines, off, restarted = LogTail(str(f)).read_since(101)
    assert lines == ["new"] and restarted and off == 4

def test_missing_file_is_empty(tmp_path):
    t = LogTail(str(tmp_path / "nope.txt"))
    assert t.size() == 0 and t.read_since(0) == ([], 0, False)

def test_crlf_and_latin1(tmp_path):
    f = tmp_path / "log.txt"
    f.write_bytes("Bjørn hits you for 5.\r\n".encode("utf-8"))
    lines, _, _ = LogTail(str(f)).read_since(0)
    assert lines == ["Bjørn hits you for 5."]
