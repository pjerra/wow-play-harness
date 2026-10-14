"""New lines of a growing log file, by byte offset. A file shorter than the offset was truncated or recreated: read it from 0."""
import os


class LogTail:
    def __init__(self, path: str):
        self.path = path

    def size(self) -> int:
        try:
            return os.path.getsize(self.path)
        except OSError:
            return 0

    def read_since(self, offset: int) -> tuple[list[str], int, bool]:
        size = self.size()
        if size == 0 and not os.path.exists(self.path):
            return [], 0, False
        restarted = size < offset
        start = 0 if restarted else offset
        with open(self.path, "rb") as f:
            f.seek(start)
            data = f.read()
        end = data.rfind(b"\n")
        if end < 0:
            return [], start, restarted
        chunk = data[: end + 1]
        try:
            text = chunk.decode("utf-8")
        except UnicodeDecodeError:
            text = chunk.decode("latin-1")
        lines = [l.rstrip("\r") for l in text.split("\n")[:-1]]
        return lines, start + end + 1, restarted
