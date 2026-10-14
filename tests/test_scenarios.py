from wowharness import scenarios


class FakeClient:
    def __init__(self, combat, chat):
        self.sent, self._combat, self._chat = [], combat, chat

    def live_offset(self):
        return 0

    def live_lines(self, since, kind=None):
        return (self._combat if kind == "combat" else self._chat), 0

    def chat(self, text):
        self.sent.append(text)

    def run_lua(self, code):
        self.sent.append("/script " + code)

    def key(self, k, ms=0):
        self.sent.append(("key", k, ms))


class FakeDash:
    def __init__(self, states):
        self.states, self.i = states, 0

    def bots(self, names):
        s = self.states[min(self.i, len(self.states) - 1)]
        self.i += 1
        return [{"name": n, "state": s, "hp": 100, "max_hp": 100, "x": 0, "y": 0,
                 "last_action": "attack", "last_trigger": "t", "target": "Mob"} for n in names]


def test_fight_stops_when_party_calm(monkeypatch):
    monkeypatch.setattr(scenarios.time, "sleep", lambda s: None)
    c = FakeClient(["10/12 10:00:00.000  Bot1 hits Mob for 10."], ["10/12 10:00:01.000  [Party] [Bot1]: hi"])
    d = FakeDash(["combat", "combat", "none"])
    r = scenarios.fight(c, d, "Me", ["Bot1"], order=".bot action attack", target="Mob", max_s=1, quiet_s=0)
    assert c.sent[0] == '/script HX_TargetLive("Mob")' and c.sent[1] == ".bot action attack"
    assert r["combat_lines"] == 1 and "summary" in r and "Bot1" in r["decisions"]


def test_ask_bots_groups_replies(monkeypatch):
    monkeypatch.setattr(scenarios.time, "sleep", lambda s: None)
    c = FakeClient([], ["10/12 10:00:00.000  [Bot1] whispers: Strategies: follow, grind",
                        "10/12 10:00:00.100  [Bot2] whispers: Strategies: follow"])
    r = scenarios.ask_bots(c, ["Bot1", "Bot2"], "nc ?", 0)
    assert c.sent == ["/w Bot1 nc ?", "/w Bot2 nc ?"]
    assert len(r["Bot1"]) == 1 and len(r["Bot2"]) == 1


def test_hire_party_reads_joins(monkeypatch):
    monkeypatch.setattr(scenarios.time, "sleep", lambda s: None)
    c = FakeClient([], ["10/12 10:00:00.000  Kel joins the party.", "10/12 10:00:02.000  Mae joins the party."])
    r = scenarios.hire_party(c, None, ["warrior tank", "priest holy"], 1)
    assert r["joined"] == ["Kel", "Mae"] and c.sent == [".bot hire warrior tank", ".bot hire priest holy"]
