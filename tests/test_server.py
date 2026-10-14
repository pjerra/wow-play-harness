from types import SimpleNamespace
from wowharness.server import Server

def ok_post(url, data, auth, headers, timeout):
    assert url == "http://h:7878/" and auth == ("u", "p") and b".bot version" in data
    return SimpleNamespace(status_code=200, text="<result>TBM:VERSION|1.2</result>")

def test_gm_returns_result_text():
    s = Server("h", 7878, "u", "p", "c", post=ok_post)
    assert s.gm(".bot version") == "TBM:VERSION|1.2"

def test_gm_fault_is_verbatim():
    def fault(*a, **k):
        return SimpleNamespace(status_code=500, text="<faultstring>Command not found</faultstring>")
    assert Server("h", 7878, "u", "p", "c", post=fault).gm(".nope") == "SOAP error: Command not found"

def test_gm_escapes_xml():
    seen = {}
    def post(url, data, auth, headers, timeout):
        seen["d"] = data
        return SimpleNamespace(status_code=200, text="<result>ok</result>")
    Server("h", 7878, "u", "p", "c", post=post).gm(".say <a&b>")
    assert b"&lt;a&amp;b&gt;" in seen["d"]

def test_log_since_uses_ssh_docker_logs():
    calls = []
    def run(argv, capture_output, text, timeout):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="line\n", stderr="")
    ssh = ["-i", "k", "user@192.0.2.13"]
    assert Server("h", 7878, "u", "p", "mangosd", ssh=ssh, run=run).log_since(60) == "line\n"
    assert calls[0] == ["ssh", "-o", "BatchMode=yes", "-i", "k", "user@192.0.2.13",
                        "docker", "logs", "--since", "60s", "mangosd"]

def test_log_since_without_ssh_says_not_configured():
    def run(*a, **k):
        raise AssertionError("must not run ssh")
    assert Server("h", 7878, "u", "p", "mangosd", run=run).log_since(60) == \
        "server log not configured (server_ssh missing in settings)"
