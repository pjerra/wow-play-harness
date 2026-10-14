from wowharness.combat import parse_lines
from wowharness.report import build, summary

LINES = [
    "10/11 21:03:40.120  Gorm hits Kobold Vermin for 12.",
    "10/11 21:03:41.300  Kobold Vermin hits Gorm for 4.",
    "10/11 21:03:42.000  Lia begins to cast Lesser Heal.",
    "10/11 21:03:44.500  Lia's Lesser Heal heals Gorm for 48.",
    "10/11 21:03:45.000  Your Fireball hits Kobold Vermin for 33 Fire damage.",
    "10/11 21:03:47.000  Kobold Vermin dies.",
    "10/11 21:03:47.500  Something odd happened.",
]
CHAT = ["10/11 21:03:40.100  [Gorm] whispers: Attacking Kobold Vermin", "10/11 21:03:40.200  TBM:ACTION_ACK|attack|bot:Gorm|1", "10/11 21:03:40.300  [General] spam"]

def r(): return build(parse_lines(LINES, "Harness"), CHAT, "Harness", ["Gorm", "Lia"])

def test_per_actor_totals():
    a = r()["actors"]
    assert a["Gorm"]["damage_done"] == 12 and a["Gorm"]["damage_taken"] == 4
    assert a["Lia"]["healing_done"] == 48 and a["Lia"]["casts"] == {"Lesser Heal": 1}
    assert a["Harness"]["damage_done"] == 33 and a["Harness"]["casts"] == {"Fireball": 1}

def test_killed_and_unknown():
    assert r()["killed"] == ["Kobold Vermin"] and r()["unknown"] == 1

def test_bot_lines_only_party_and_tbm():
    assert r()["bot_lines"] == CHAT[:2]

def test_party_member_with_no_events_still_listed():
    rep = build([], [], "Harness", ["Gorm", "Lia"])
    assert "Gorm" in summary(rep, ["Gorm", "Lia"]) and "did nothing" in summary(rep, ["Gorm", "Lia"])

def test_summary_names_each_bot():
    s = summary(r(), ["Gorm", "Lia"])
    assert "Gorm: 12 damage" in s and "Lia: healed 48" in s and "Lesser Heal x1" in s


def test_periodic_counts_damage_not_casts_and_auras_listed():
    from wowharness.combat import parse_lines
    lines = ["10/11 16:59:13.968  Sornizarae's Fireball hits Kobold Vermin for 36 Fire damage.",
             "10/11 16:59:15.906  Kobold Vermin suffers 1 Fire damage from Sornizarae's Fireball.",
             "10/11 16:59:10.843  Kobold Vermin is afflicted by Hunter's Mark."]
    r = build(parse_lines(lines, "Harnessa"), [], "Harnessa", ["Sornizarae", "Morulader"])
    assert r["actors"]["Sornizarae"]["damage_done"] == 37 and r["actors"]["Sornizarae"]["casts"] == {"Fireball": 1}
    assert r["auras"] == ["Hunter's Mark on Kobold Vermin"] and r["unknown"] == 0
    assert "Auras: Hunter's Mark on Kobold Vermin" in summary(r, ["Sornizarae", "Morulader"])


def test_summary_adds_ai_actions_from_decisions():
    r = build([], [], "Harnessa", ["Morulader"])
    decisions = {"Morulader": [{"last_action": "hunter's mark"}, {"last_action": "check values"},
                               {"last_action": "follow"}, {"last_action": "hunter's mark"}]}
    assert "Morulader: did nothing in the combat log; AI: hunter's mark, follow" in summary(r, ["Morulader"], decisions)
