import json

import pytest

from wowharness.config import load_config
from wowharness.dashboard import Dashboard, DashboardUnavailable, decision_timeline


class Resp:
    def __init__(self, status=200, body=None, text=""):
        self.status_code = status
        self._body = body
        self.text = text or json.dumps(body)

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, gets=(), posts=()):
        self.gets = list(gets)
        self.posts = list(posts)
        self.calls = []

    def post(self, url, json=None, timeout=None):
        self.calls.append(("POST", url, json))
        return self.posts.pop(0)

    def get(self, url, timeout=None):
        self.calls.append(("GET", url))
        return self.gets.pop(0)


BOTS = [{"name": "Aela"}, {"name": "Brum"}]


def dash(session):
    return Dashboard("http://h:8095/", "gm", "s3cret", session)


def test_login_posts_credentials():
    s = FakeSession(posts=[Resp(200, {})])
    dash(s).login()
    assert s.calls == [("POST", "http://h:8095/api/v1/auth/login", {"username": "gm", "password": "s3cret"})]


def test_login_failure_hides_password():
    s = FakeSession(posts=[Resp(403, None, "nope s3cret " + "x" * 500)])
    with pytest.raises(DashboardUnavailable) as e:
        dash(s).login()
    msg = str(e.value)
    assert msg.startswith("dashboard login failed: 403")
    assert "s3cret" not in msg
    assert len(msg) < 260


def test_bots_filters_case_insensitive_list_and_object():
    for body in (BOTS, {"bots": BOTS}):
        s = FakeSession(gets=[Resp(200, body)])
        assert dash(s).bots(["AELA"]) == [{"name": "Aela"}]
    s = FakeSession(gets=[Resp(200, BOTS)])
    assert dash(s).bots() == BOTS


def test_401_relogs_once_and_retries():
    s = FakeSession(gets=[Resp(401, None, "x"), Resp(200, BOTS)], posts=[Resp(200, {})])
    assert dash(s).bots() == BOTS
    assert [c[0] for c in s.calls] == ["GET", "POST", "GET"]


def test_401_twice_raises():
    s = FakeSession(gets=[Resp(401, None, "x"), Resp(401, None, "x")], posts=[Resp(200, {})])
    with pytest.raises(DashboardUnavailable):
        dash(s).issues()
    assert len(s.calls) == 3


def test_network_error_is_unavailable():
    class Boom:
        def get(self, *a, **k):
            raise OSError("down")
    with pytest.raises(DashboardUnavailable):
        dash(Boom()).anomalies()


def b(name, action="a", trig="t", target="", state="idle", hp=50, mhp=100):
    return {"name": name, "last_action": action, "last_trigger": trig, "target": target,
            "state": state, "hp": hp, "max_hp": mhp}


def test_timeline_first_seen_and_changes_only():
    samples = [(0.0, [b("Aela")]), (1.0, [b("Aela", hp=40)]), (2.0, [b("Aela", action="z", hp=30)])]
    ev = decision_timeline(samples, ["Aela"])["Aela"]
    assert [e["t"] for e in ev] == [0.0, 2.0]
    assert ev[1]["last_action"] == "z" and ev[1]["hp_pct"] == 30
    assert set(ev[0]) == {"t", "last_action", "last_trigger", "target", "state", "hp_pct"}


def test_timeline_each_field_triggers():
    base = b("Aela")
    for ch in ({"last_trigger": "q"}, {"target": "Wolf"}, {"state": "combat"}):
        ev = decision_timeline([(0, [base]), (1, [{**base, **ch}])], ["Aela"])["Aela"]
        assert len(ev) == 2


def test_timeline_hp_zero_max_and_missing_bot_and_case():
    samples = [(0, [b("aela", hp=5, mhp=0)]), (1, []), (2, [b("AELA", hp=5, mhp=0)])]
    res = decision_timeline(samples, ["Aela", "Ghost"])
    assert [e["hp_pct"] for e in res["Aela"]] == [0]
    assert res["Ghost"] == []


def test_config_defaults_for_dashboard(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({
        "client_dir": "c", "soap_user": "u", "soap_password": "p", "account": "a", "password": "p",
        "login_account_xy": [1, 2], "login_password_xy": [3, 4]}))
    c = load_config(str(p))
    assert (c.dashboard_url, c.dashboard_user, c.dashboard_password) == ("", "", "")
    assert c.server_ssh == ()


def test_non_json_2xx_is_unavailable():
    class Bad(Resp):
        def json(self):
            raise ValueError("no json")
    s = FakeSession(gets=[Bad(200, None, "<html>oops</html>")])
    with pytest.raises(DashboardUnavailable, match="non-JSON"):
        dash(s).issues()


class _FakeDash:
    def __init__(self):
        self.n = 0

    def bots(self, names=None):
        self.n += 1
        return [b("Aela")]


def _patched(monkeypatch):
    from wowharness import mcp
    clock = [0.0]
    sleeps = []
    monkeypatch.setattr(mcp.time, "time", lambda: clock[0])
    monkeypatch.setattr(mcp.time, "sleep", lambda x: (sleeps.append(x), clock.__setitem__(0, clock[0] + x)))
    d = _FakeDash()
    monkeypatch.setattr(mcp, "_dashboard", lambda: d)
    monkeypatch.setitem(mcp._state, "watch", [])
    return mcp, d, sleeps


def test_watch_bots_clamps_interval_and_seconds(monkeypatch):
    mcp, d, sleeps = _patched(monkeypatch)
    r = mcp.watch_bots(["Aela"], seconds=1, interval_s=0)
    assert min(sleeps) >= 0.2 and r["ok"] and r["samples"] == d.n == 6
    r = mcp.watch_bots(["Aela"], seconds=1, interval_s=-5)
    assert r["ok"] and min(sleeps) >= 0.2


def test_watch_bots_caps_seconds(monkeypatch):
    mcp, d, sleeps = _patched(monkeypatch)
    r = mcp.watch_bots(["Aela"], seconds=9999, interval_s=100)
    assert r["samples"] == 4 and sum(sleeps) <= 300
    assert mcp.watch_bots(["Aela"], seconds=-3)["samples"] == 1


def test_unconfigured_error_names_default_path(monkeypatch, tmp_path):
    from wowharness import mcp
    from wowharness.config import DEFAULT_PATH
    monkeypatch.setattr(mcp, "_config_path", None)
    monkeypatch.setattr(mcp, "load_config", lambda p: type("C", (), {"dashboard_url": ""})())
    mcp._state.pop("dashboard", None)
    mcp._state.pop("cfg", None)
    r = mcp.dashboard()
    assert r == {"ok": False, "error": f"dashboard not configured (dashboard_url missing in {DEFAULT_PATH})"}


def test_dashboard_unknown_what(monkeypatch):
    mcp, d, _ = _patched(monkeypatch)
    r = mcp.dashboard(what="zzz")
    assert r["ok"] is False and r["error"].startswith("unknown what: zzz")
