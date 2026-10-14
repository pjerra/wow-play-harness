"""Per-actor fight totals and a short 'what the bots did' text."""
from collections import defaultdict

from .combat import Event


def _actor() -> dict:
    return {"damage_done": 0, "damage_taken": 0, "healing_done": 0, "casts": {}, "deaths": 0}


def build(events: list[Event], chat_lines: list[str], me: str, party: list[str]) -> dict:
    actors: dict[str, dict] = defaultdict(_actor)
    for name in [me, *party]:
        actors[name]
    killed, auras, unknown = [], [], 0
    for e in events:
        if e.kind in ("melee", "spell", "periodic") and e.amount is not None:
            actors[e.source]["damage_done"] += e.amount
            if e.target:
                actors[e.target]["damage_taken"] += e.amount
        if e.kind in ("spell", "heal", "cast") and e.spell:
            casts = actors[e.source]["casts"]
            casts[e.spell] = casts.get(e.spell, 0) + 1
        if e.kind == "heal" and e.amount is not None:
            actors[e.source]["healing_done"] += e.amount
        if e.kind == "dies":
            actors[e.source]["deaths"] += 1
            if e.source not in party and e.source != me:
                killed.append(e.source)
        if e.kind == "aura":
            aura = f"{e.spell} on {e.target}"
            if aura not in auras:
                auras.append(aura)
        if e.kind == "other":
            unknown += 1
    # Chat-log lines carry a time prefix, so a party member's name anywhere in the line counts.
    bot_lines = [l for l in chat_lines if "TBM:" in l or any(p in l for p in party)]
    return {"actors": dict(actors), "killed": killed, "auras": auras, "bot_lines": bot_lines, "unknown": unknown}


def summary(r: dict, party: list[str], decisions: dict | None = None) -> str:
    """decisions: dashboard.decision_timeline output; adds each bot's distinct AI actions."""
    out = []
    for name in party:
        a = r["actors"].get(name, _actor())
        parts = []
        if a["damage_done"]:
            parts.append(f"{a['damage_done']} damage")
        if a["healing_done"]:
            parts.append(f"healed {a['healing_done']}")
        if a["casts"]:
            parts.append("casts " + ", ".join(f"{s} x{n}" for s, n in sorted(a["casts"].items())))
        if a["deaths"]:
            parts.append(f"died {a['deaths']}x")
        line = f"{name}: " + ("; ".join(parts) if parts else "did nothing in the combat log")
        acts = []
        for d in (decisions or {}).get(name, []):
            a_ = d.get("last_action")
            if a_ and a_ != "check values" and a_ not in acts:
                acts.append(a_)
        if acts:
            line += "; AI: " + ", ".join(acts)
        out.append(line)
    if r.get("auras"):
        out.append("Auras: " + ", ".join(r["auras"]))
    out.append(f"Killed: {', '.join(r['killed']) or 'nothing'}. Unparsed lines: {r['unknown']}.")
    return "\n".join(out)
