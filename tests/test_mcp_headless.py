import json

from tests.test_headless import agent  # noqa: F401  (the scripted turtle-agent fixture)
from wowharness import mcp_headless
from wowharness.headless import HeadlessClient, HeadlessError


def test_option_from_argv():
    assert mcp_headless.option_from_argv(["--rpc", "127.0.0.1:9000"], "--rpc") == "127.0.0.1:9000"
    assert mcp_headless.option_from_argv(["--rpc=127.0.0.1:9001"], "--rpc") == "127.0.0.1:9001"
    assert mcp_headless.option_from_argv([], "--rpc") is None


def test_guarded_turns_headless_errors_into_ok_false():
    @mcp_headless.guarded
    def tool():
        raise HeadlessError("chat: not in world (-32001)", -32001)
    assert tool() == {"ok": False, "error": "chat: not in world (-32001)"}


def test_tools_delegate(agent):  # noqa: F811
    mcp_headless._state["client"] = HeadlessClient(agent.rpc)
    assert mcp_headless.chat("/p hi") == {"ok": True}
    assert mcp_headless.party()["party"]["in_group"] is True
    assert mcp_headless.tail_combat_log()["offset"] == 10
    assert mcp_headless.attack() == {"ok": True}
    assert mcp_headless.fight_report(0, 0, "Harnessb", ["Gorm", "Lia"])["ok"] is True


def test_mvp_script_against_scripted_agent(agent, capsys):  # noqa: F811
    from tools.headless_mvp import main
    assert main(["--rpc", agent.rpc, "--bots", "Gorm", "Lia", "--mob", "Kobold Vermin", "--fight-s", "1", "--wait-s", "0.1"]) == 0
    out = capsys.readouterr().out
    assert "Gorm: 12 damage" in out and "Killed: Kobold Vermin" in out
    assert ("target", {"name": "Kobold Vermin"}) in agent.calls and ("attack", {}) in agent.calls


def _mvp_steps(agent):  # noqa: F811
    keep = ("target", "walk_to", "face", "attack")
    return [(m, p) for m, p in agent.calls if m in keep or (m == "gm" and p["text"] == ".bot action attack")]


def test_mvp_script_closes_in_faces_and_waits_before_the_bot_order(agent, capsys):  # noqa: F811
    from tools.headless_mvp import main
    assert main(["--rpc", agent.rpc, "--bots", "Gorm", "Lia", "--mob", "Kobold Vermin", "--fight-s", "1", "--wait-s", "0.1"]) == 0
    # The player stands at (1, 2, 0) and the mob at (11, 2, 0): walk to 2 yd short of it, face it, then attack.
    assert _mvp_steps(agent) == [("target", {"name": "Kobold Vermin"}), ("walk_to", {"x": 9.0, "y": 2.0, "z": 0.0}),
                                 ("face", {"guid": "0xF130000000000001"}), ("attack", {}),
                                 ("gm", {"text": ".bot action attack"})]
    at = {m if m != "gm" else p["text"]: t for (m, p), t in zip(agent.calls, agent.times)}
    assert at[".bot action attack"] - at["target"] >= 0.95  # selection and chat run in different server queues
    # Gorm and Lia are already in the party: no .bot add and no invite.
    assert not [x for x in agent.calls if x[0] == "invite" or (x[0] == "gm" and x[1]["text"].startswith(".bot add"))]


def test_mvp_script_adds_only_the_missing_bot(agent, capsys, monkeypatch):  # noqa: F811
    import tools.headless_mvp as mvp
    monkeypatch.setattr(mvp.time, "sleep", lambda s: None)
    orig, joined = agent.handle, []

    def handle(method, params):
        if method == "invite":
            joined.append(params["name"])
        if method == "party":
            agent.calls.append((method, params))
            return {"in_group": True, "members": [{"name": n, "health": 1, "max_health": 1} for n in ["Gorm", *joined]]}
        return orig(method, params)

    agent.handle = handle
    assert mvp.main(["--rpc", agent.rpc, "--bots", "Gorm", "Mira", "--mob", "Kobold Vermin", "--fight-s", "1",
                     "--wait-s", "0.1"]) == 0
    assert [x for x in agent.calls if x[0] == "invite"] == [("invite", {"name": "Mira"})]
    assert [p["text"] for m, p in agent.calls if m == "gm" and p["text"].startswith(".bot add")] == [".bot add Mira"]


def _hiring_agent(agent):  # noqa: F811
    orig, hired = agent.handle, []

    def handle(method, params):
        if method == "gm" and params["text"].startswith(".bot hire "):
            hired.append({"warrior tank": "Branbewan", "priest holy": "Khelkhos"}[params["text"][len(".bot hire "):]])
        if method == "party":
            agent.calls.append((method, params))
            return {"in_group": bool(hired), "members": [{"name": n, "health": 1, "max_health": 1} for n in hired]}
        return orig(method, params)

    agent.handle = handle


def test_mvp_script_hires_when_the_settings_have_no_bots(agent, capsys, monkeypatch, tmp_path):  # noqa: F811
    import tools.headless_mvp as mvp
    monkeypatch.setattr(mvp.time, "sleep", lambda s: None)
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"realmd": "127.0.0.1:3724", "accounts": {"SECRETACCT": "SECRETPW"}}), encoding="utf-8")
    _hiring_agent(agent)
    assert mvp.main(["--rpc", agent.rpc, "--settings", str(settings), "--mob", "Kobold Vermin", "--fight-s", "1",
                     "--wait-s", "0.1"]) == 0
    hires = [p["text"] for m, p in agent.calls if m == "gm" and p["text"].startswith(".bot hire")]
    assert hires == [".bot hire warrior tank", ".bot hire priest holy"]
    out = capsys.readouterr().out
    assert "Branbewan:" in out and "Khelkhos:" in out
    assert "SECRET" not in out and not [x for x in agent.calls if "SECRET" in json.dumps(x)]


def test_mvp_script_takes_the_bots_from_the_settings(agent, capsys, tmp_path):  # noqa: F811
    from tools.headless_mvp import main
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"accounts": {"SECRETACCT": "SECRETPW"}, "bots": ["Gorm", "Lia"]}), encoding="utf-8")
    assert main(["--rpc", agent.rpc, "--settings", str(settings), "--mob", "Kobold Vermin", "--fight-s", "1",
                 "--wait-s", "0.1"]) == 0
    assert "Gorm: 12 damage" in capsys.readouterr().out
    assert not [x for x in agent.calls if x[0] == "gm" and x[1]["text"].startswith((".bot hire", ".bot add"))]


def test_mvp_script_hires_only_the_missing_specs(agent, capsys, monkeypatch, tmp_path):  # noqa: F811
    import tools.headless_mvp as mvp
    monkeypatch.setattr(mvp.time, "sleep", lambda s: None)
    _hiring_agent(agent)
    agent.handle("gm", {"text": ".bot hire warrior tank"})  # hired by an earlier run
    assert mvp.main(["--rpc", agent.rpc, "--settings", str(tmp_path / "missing.json"), "--mob", "Kobold Vermin",
                     "--fight-s", "1", "--wait-s", "0.1"]) == 0
    hires = [p["text"] for m, p in agent.calls if m == "gm" and p["text"].startswith(".bot hire")]
    assert hires == [".bot hire warrior tank", ".bot hire priest holy"]  # the first is the earlier run's


def test_tool_params_reach_the_agent(agent):  # noqa: F811
    mcp_headless._state["client"] = HeadlessClient(agent.rpc)
    assert mcp_headless.teleport(1.5, 2.5, 3.5) == {"ok": True}
    assert mcp_headless.teleport(1.5, 2.5, 3.5, map=1) == {"ok": True}
    assert mcp_headless.walk_to(4.0, 5.0, 6.0) == {"ok": True}
    assert mcp_headless.units(hostile=True, name="Kobold")["units"][0]["name"] == "Kobold Vermin"
    assert mcp_headless.target(guid="0xF130000000000001")["unit"]["name"] == "Kobold Vermin"
    assert mcp_headless.cast(133, target="0xF130000000000001") == {"ok": True}
    assert ("teleport", {"x": 1.5, "y": 2.5, "z": 3.5}) in agent.calls
    assert ("teleport", {"x": 1.5, "y": 2.5, "z": 3.5, "map": 1}) in agent.calls
    assert ("walk_to", {"x": 4.0, "y": 5.0, "z": 6.0}) in agent.calls
    assert ("units", {"near": 40, "limit": 50, "hostile": True, "name": "Kobold"}) in agent.calls
    assert ("target", {"guid": "0xF130000000000001"}) in agent.calls
    assert ("cast", {"spell_id": 133, "target": "0xF130000000000001"}) in agent.calls
    err = mcp_headless.logout()  # the scripted agent does not know logout: an RPC error, not an exception
    assert err["ok"] is False and "Method not found" in err["error"]


def test_spell_names_file_loads_int_keys(tmp_path):
    from wowharness.headless import load_spell_names
    p = tmp_path / "names.json"
    p.write_text(json.dumps({"133": "Fireball", "2050": "Lesser Heal"}), encoding="utf-8")
    assert load_spell_names(str(p)) == {133: "Fireball", 2050: "Lesser Heal"}


def test_mvp_script_waits_for_the_mob_after_teleport(agent, capsys, monkeypatch):  # noqa: F811
    import tools.headless_mvp as mvp
    monkeypatch.setattr(mvp.time, "sleep", lambda s: None)
    orig, seen = agent.handle, []

    def handle(method, params):  # the mob's create block arrives on the third units poll
        if method == "units" and params.get("name") == "Kobold Vermin":
            seen.append(params)
            if len(seen) < 3:
                agent.calls.append((method, params))
                return []
        return orig(method, params)

    agent.handle = handle
    assert mvp.main(["--rpc", agent.rpc, "--bots", "Gorm", "Lia", "--mob", "Kobold Vermin", "--at", "1", "2", "3", "0",
                     "--fight-s", "1", "--wait-s", "0.1"]) == 0
    order = [m for m, _ in agent.calls if m in ("teleport", "units", "target")]
    assert order[:5] == ["teleport", "units", "units", "units", "target"]


def test_mvp_script_reports_rpc_errors_with_exit_3(capsys):
    from tools.headless_mvp import main
    assert main(["--rpc", "127.0.0.1:1", "--bots", "Gorm", "Lia", "--mob", "Kobold Vermin"]) == 3
    assert "not reachable" in capsys.readouterr().out


def _safe_agent(agent, refusals=0):  # noqa: F811
    """The scripted agent plus leave_group and logout; logout is refused in combat `refusals` times."""
    from tests.test_headless import RpcError
    orig, left = agent.handle, [refusals]

    def handle(method, params):
        if method in ("leave_group", "logout"):
            agent.calls.append((method, params))
            if method == "logout" and left[0] > 0:
                left[0] -= 1
                raise RpcError(-32003, "logout refused: in combat (reason 1)")
            return {}
        return orig(method, params)

    agent.handle = handle


def _gm_texts(agent):  # noqa: F811
    return [p["text"] for m, p in agent.calls if m == "gm"]


def test_mvp_script_teleports_before_gm_off(agent, capsys):  # noqa: F811
    from tools.headless_mvp import main
    _safe_agent(agent)
    assert main(["--rpc", agent.rpc, "--bots", "Gorm", "Lia", "--mob", "Kobold Vermin", "--at", "1", "2", "3", "0",
                 "--fight-s", "1", "--wait-s", "0.1"]) == 0
    seq = [m if m != "gm" else p["text"] for m, p in agent.calls]
    assert seq.index("teleport") < seq.index(".gm off") < seq.index(".gm visible on")


def test_mvp_script_ends_safe_after_the_report(agent, capsys):  # noqa: F811
    from tools.headless_mvp import main
    _safe_agent(agent)
    assert main(["--rpc", agent.rpc, "--bots", "Gorm", "Lia", "--mob", "Kobold Vermin", "--fight-s", "1", "--wait-s", "0.1"]) == 0
    out = capsys.readouterr().out
    assert out.index("Killed: Kobold Vermin") < out.index("end safe: logged out")
    tail = [m if m != "gm" else p["text"] for m, p in agent.calls if m in ("stop_attack", "leave_group", "logout", "gm")]
    assert tail[-4:] == ["stop_attack", ".gm on", "leave_group", "logout"]  # GM on before the companions go
    assert [m for m, _ in agent.calls].count("stop_attack") == 1


def test_mvp_script_retries_a_refused_logout(agent, capsys, monkeypatch):  # noqa: F811
    import tools.headless_mvp as mvp
    monkeypatch.setattr(mvp.time, "sleep", lambda s: None)
    _safe_agent(agent, refusals=2)
    assert mvp.main(["--rpc", agent.rpc, "--bots", "Gorm", "Lia", "--mob", "Kobold Vermin", "--fight-s", "1",
                     "--wait-s", "0.1"]) == 0
    assert [m for m, _ in agent.calls].count("logout") == 3
    assert "end safe: logged out" in capsys.readouterr().out


def test_mvp_script_end_safe_runs_every_step_after_a_failure(agent, capsys):  # noqa: F811
    # The plain scripted agent knows neither leave_group nor logout: each failure is printed, the
    # later steps still run, and the fight's exit code stands.
    from tools.headless_mvp import main
    assert main(["--rpc", agent.rpc, "--bots", "Gorm", "Lia", "--mob", "Kobold Vermin", "--fight-s", "1", "--wait-s", "0.1"]) == 0
    out = capsys.readouterr().out
    assert "end safe: leave_group:" in out and "end safe: logout:" in out
    assert _gm_texts(agent)[-1] == ".gm on"
    assert [m for m, _ in agent.calls][-1] == "logout"


def test_mvp_script_follows_the_targeted_guid(agent, capsys, monkeypatch):  # noqa: F811
    import tools.headless_mvp as mvp
    monkeypatch.setattr(mvp.time, "sleep", lambda s: None)
    _safe_agent(agent)
    orig, polls = agent.handle, []
    corpse = {"guid": "0xF130000000000009", "name": "Kobold Vermin", "health": 0, "max_health": 50, "dead": True}

    def handle(method, params):
        if method == "units" and agent.calls and ("attack", {}) in agent.calls:
            agent.calls.append((method, params))
            polls.append(params)
            hp = 50 if len(polls) < 3 else 0  # the targeted mob dies on the third poll
            # A nearer corpse of the same name comes first: it is not the fight's mob.
            return [corpse, {"guid": "0xF130000000000001", "name": "Kobold Vermin", "health": hp, "max_health": 50,
                             "dead": hp == 0}]
        return orig(method, params)

    agent.handle = handle
    assert mvp.main(["--rpc", agent.rpc, "--bots", "Gorm", "Lia", "--mob", "Kobold Vermin", "--fight-s", "60",
                     "--wait-s", "0.1"]) == 0
    assert len(polls) == 3


def test_mvp_script_will_not_fight_a_corpse(agent, capsys):  # noqa: F811
    from tools.headless_mvp import main
    _safe_agent(agent)
    orig = agent.handle

    def handle(method, params):  # turtle-agent returns a corpse only when nothing of that name is alive
        if method == "target":
            agent.calls.append((method, params))
            return {"guid": "0xF130000000000001", "name": "Kobold Vermin", "health": 0, "max_health": 50, "dead": True,
                    "x": 11.0, "y": 2.0, "z": 0.0}
        return orig(method, params)

    agent.handle = handle
    assert main(["--rpc", agent.rpc, "--bots", "Gorm", "Lia", "--mob", "Kobold Vermin", "--fight-s", "1", "--wait-s", "0.1"]) == 4
    assert "no living Kobold Vermin" in capsys.readouterr().out
    methods = [m for m, _ in agent.calls]
    assert "attack" not in methods and "leave_group" in methods and methods[-1] == "logout"
    assert _gm_texts(agent)[-1] == ".gm on"
