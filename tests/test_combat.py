from pathlib import Path
from wowharness.combat import parse_line, parse_lines

ME = "Harness"
LINES = Path(__file__).with_name("fixtures").joinpath("combat_sample.txt").read_text().splitlines()

def ev(i): return parse_line(LINES[i + 1], ME)  # line 0 is the comment

def test_melee_hit_and_crit():
    a, b = ev(0), ev(1)
    assert (a.kind, a.source, a.target, a.amount, a.crit) == ("melee", "Gorm", "Kobold Vermin", 12, False)
    assert b.crit and b.amount == 25

def test_you_becomes_me():
    e = ev(2)
    assert e.source == ME and e.target == "Kobold Vermin" and e.amount == 9

def test_cast_start_and_heal():
    assert (ev(4).kind, ev(4).source, ev(4).spell) == ("cast_start", "Lia", "Lesser Heal")
    h = ev(5)
    assert (h.kind, h.source, h.spell, h.target, h.amount) == ("heal", "Lia", "Lesser Heal", "Gorm", 48)

def test_spell_school_damage_from_you():
    e = ev(6)
    assert (e.kind, e.source, e.spell, e.target, e.amount) == ("spell", ME, "Fireball", "Kobold Vermin", 33)

def test_names_with_spaces_and_apostrophes():
    e = ev(7)
    assert (e.source, e.spell, e.target, e.amount) == ("Lia", "Shadow Word: Pain", "Mor'Ladim", 20)

def test_cast_on_gain_dies_slain():
    assert (ev(8).kind, ev(8).spell, ev(8).target) == ("cast", "Power Word: Fortitude", "Gorm")
    assert (ev(9).kind, ev(9).source, ev(9).spell) == ("gain", "Gorm", "Battle Shout")
    assert (ev(10).kind, ev(10).source) == ("dies", "Kobold Vermin")
    assert (ev(11).kind, ev(11).source, ev(11).target) == ("slain", ME, "Kobold Vermin")

def test_unknown_line_kept():
    e = ev(12)
    assert e.kind == "other" and e.raw.endswith("Something odd happened.")

def test_parse_lines_skips_comments_and_blanks():
    assert len(parse_lines(LINES + [""], ME)) == 13

# Lines from the live fight on test-vm, 2026-10-11 16:59
LIVE = [
    "10/11 16:59:10.843  Kobold Vermin is afflicted by Hunter's Mark.",
    "10/11 16:59:15.906  Kobold Vermin suffers 1 Fire damage from Sornizarae's Fireball.",
    "10/11 16:59:15.218  Kobold Vermin is slain by Sornizarae!",
    "10/11 16:59:16.000  You are afflicted by Poison.",
    "10/11 16:59:17.000  You suffer 3 Nature damage from Kobold Vermin's Poison.",
    "10/11 16:59:18.000  Kobold Vermin suffers 4 Shadow damage from your Corruption.",
]

def test_afflicted_is_an_aura_without_caster():
    e = parse_line(LIVE[0], ME)
    assert (e.kind, e.source, e.spell, e.target) == ("aura", "", "Hunter's Mark", "Kobold Vermin")
    e = parse_line(LIVE[3], ME)
    assert (e.kind, e.spell, e.target) == ("aura", "Poison", ME)

def test_suffers_is_periodic_damage():
    e = parse_line(LIVE[1], ME)
    assert (e.kind, e.source, e.spell, e.target, e.amount) == ("periodic", "Sornizarae", "Fireball", "Kobold Vermin", 1)
    e = parse_line(LIVE[4], ME)
    assert (e.kind, e.source, e.target, e.amount) == ("periodic", "Kobold Vermin", ME, 3)
    e = parse_line(LIVE[5], ME)
    assert (e.kind, e.source, e.spell, e.amount) == ("periodic", ME, "Corruption", 4)

def test_slain_by_names_the_killer():
    e = parse_line(LIVE[2], ME)
    assert (e.kind, e.source, e.target) == ("slain", "Sornizarae", "Kobold Vermin")
