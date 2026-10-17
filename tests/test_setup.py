import argparse
import io
import json
import sys
import tomllib

import pytest

from callmaibe import cli, config, credentials, paths, setup


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Tmp home for callmaibe and for every client; fake keyring; recording runner."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CALLMAIBE_HOME", str(tmp_path / "cm"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setattr(setup, "user_home", lambda: home)
    monkeypatch.setattr(setup.shutil, "which", lambda name: None)
    ring: dict[str, str] = {}
    monkeypatch.setattr(credentials, "backend_name", lambda: "FakeKeyring")
    monkeypatch.setattr(credentials, "get", lambda k: ring.get(k, ""))
    monkeypatch.setattr(credentials, "set", lambda k, v: ring.__setitem__(k, v))
    calls: list[list[str]] = []
    results: dict[str, tuple[int, str]] = {}

    def runner(cmd):
        calls.append(cmd)
        return results.get(" ".join(cmd[:3]), (0, ""))

    return type("Env", (), {"home": home, "ring": ring, "calls": calls, "results": results,
                            "runner": staticmethod(runner), "tmp": tmp_path})


def go(env, *argv):
    ap = argparse.ArgumentParser()
    setup.add_arguments(ap)
    return setup.run(ap.parse_args(list(argv)), runner=env.runner)


def backups(path):
    return list(path.parent.glob(path.name + ".bak-*"))


def test_normalize_uri():
    assert setup.normalize_uri("my-agent") == "sip:my-agent@sip.linphone.org"
    assert setup.normalize_uri("my-agent@Example.com") == "sip:my-agent@example.com"
    assert setup.normalize_uri(" sip:my-agent@sip.linphone.org ") == "sip:my-agent@sip.linphone.org"
    for bad in ("", "a b", "x@", "@h", "sip:"):
        with pytest.raises(ValueError):
            setup.normalize_uri(bad)


def test_noninteractive_full_run(env, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO("s3cret\n"))
    rc = go(env, "--agent-uri", "my-agent", "--callee-uri", "me", "--backend", "pjsua2",
            "--runner", "native", "--password-stdin")
    assert rc == 0
    cfg = config.load(paths.config_file())
    assert cfg.account.uri == "sip:my-agent@sip.linphone.org"
    assert cfg.callee.uri == "sip:me@sip.linphone.org"
    assert env.ring == {"sip:my-agent@sip.linphone.org": "s3cret"}
    out = capsys.readouterr().out
    assert "s3cret" not in out and "callmaibe test-call" in out


def test_no_keyring_explains_env_var(env, monkeypatch, capsys):
    monkeypatch.setattr(credentials, "backend_name", lambda: "")
    monkeypatch.setattr(sys, "stdin", io.StringIO("s3cret\n"))
    go(env, "--agent-uri", "a", "--callee-uri", "me", "--password-stdin")
    assert env.ring == {} and "CALLMAIBE_SIP_PASSWORD" in capsys.readouterr().out


def test_same_address_rejected(env):
    assert go(env, "--agent-uri", "a", "--callee-uri", "sip:a@sip.linphone.org") == 2
    assert not paths.config_file().exists()


def test_preserves_unmanaged_keys(env):
    paths.config_file().write_text(
        '[tts]\nvoice = "af_bella"\n[policy]\nquiet_hours = '
        '[{ days = "mon-fri", from = "08:00", to = "14:00" }]\n[account]\nuri = "sip:old@x"\n',
        encoding="utf-8")
    go(env, "--yes", "--callee-uri", "sip:me@x")
    cfg = config.load(paths.config_file())
    assert cfg.tts.voice == "af_bella" and cfg.account.uri == "sip:old@x"
    assert cfg.callee.uri == "sip:me@x"
    q = cfg.policy.quiet_hours[0]
    assert (q.days, q.start, q.end) == ("mon-fri", "08:00", "14:00")


def test_cli_wiring(env):
    assert cli.main(["setup", "--yes", "--backend", "local"]) == 0
    assert config.load(paths.config_file()).media.backend == "local"


def test_dumps_valid_toml():
    d = {"a": {"s": "q\"x\\", "n": 1.5, "b": True, "l": ["x", "y"], "t": [{"k": 1}]}}
    assert tomllib.loads(setup.dumps(d)) == d


@pytest.mark.parametrize("client,rel,session", [
    ("cursor", ".cursor/mcp.json", "cursor"),
    ("gemini", ".gemini/settings.json", "gemini"),
])
def test_json_clients_merge_and_backup(env, client, rel, session):
    f = env.home / rel
    f.parent.mkdir()
    f.write_text(json.dumps({"theme": "x", "mcpServers": {"other": {"command": "o"}}}), encoding="utf-8")
    go(env, "--yes", "--backend", "local", "--clients", client)
    d = json.loads(f.read_text(encoding="utf-8"))
    assert d["theme"] == "x" and d["mcpServers"]["other"] == {"command": "o"}
    e = d["mcpServers"]["callmaibe"]
    assert e["env"] == {"CALLMAIBE_SESSION": session} and e["args"][-1] == "mcp"
    assert ("timeout" in e) == (client == "gemini")
    assert len(backups(f)) == 1
    go(env, "--yes", "--backend", "local", "--clients", client)  # idempotent: no new backup
    assert json.loads(f.read_text(encoding="utf-8")) == d and len(backups(f)) == 1


def test_claude_desktop_merge(env, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    f = env.home / ".config" / "Claude" / "claude_desktop_config.json"
    f.parent.mkdir(parents=True)
    f.write_text('{"mcpServers": {"x": {"command": "y"}}}', encoding="utf-8")
    go(env, "--yes", "--backend", "local", "--clients", "claude-desktop")
    d = json.loads(f.read_text(encoding="utf-8"))
    assert d["mcpServers"]["x"] == {"command": "y"}
    assert d["mcpServers"]["callmaibe"]["env"]["CALLMAIBE_SESSION"] == "claude-desktop"
    assert len(backups(f)) == 1


def test_codex_toml_idempotent_and_agents_markers(env):
    cdir = env.home / ".codex"
    cdir.mkdir()
    (cdir / "config.toml").write_text('model = "o3"\n', encoding="utf-8")
    (cdir / "AGENTS.md").write_text(
        f"# mine\n\n{setup.MARK_START}\nOLD\n{setup.MARK_END}\n\ntail\n", encoding="utf-8")
    for _ in range(2):
        go(env, "--yes", "--backend", "local", "--clients", "codex")
    text = (cdir / "config.toml").read_text(encoding="utf-8")
    assert text.count("[mcp_servers.callmaibe]") == 1 and text.startswith('model = "o3"')
    t = tomllib.loads(text)["mcp_servers"]["callmaibe"]
    assert t["args"][-1] == "mcp"
    assert t["env"] == {"CALLMAIBE_SESSION": "codex"} and t["tool_timeout_sec"] == 600
    md = (cdir / "AGENTS.md").read_text(encoding="utf-8")
    assert md.count(setup.MARK_START) == 1 and "OLD" not in md
    assert "call_start" in md and md.startswith("# mine") and md.rstrip().endswith("tail")
    assert env.calls == []


def test_codex_cli_used_when_on_path(env, monkeypatch):
    monkeypatch.setattr(setup.shutil, "which", lambda n: "/x/codex" if n == "codex" else None)
    go(env, "--yes", "--backend", "local", "--clients", "codex")
    cmd = env.calls[0]
    assert cmd[:6] == ["codex", "mcp", "add", "callmaibe", "--env", "CALLMAIBE_SESSION=codex"]
    assert cmd[6] == "--" and cmd[-1] == "mcp"
    assert (env.home / ".codex" / "AGENTS.md").exists()


def test_claude_code_plugin_commands_and_timeout(env):
    s = env.home / ".claude" / "settings.json"
    s.parent.mkdir()
    s.write_text('{"env": {"A": "1"}, "model": "m"}', encoding="utf-8")
    go(env, "--yes", "--backend", "local", "--clients", "claude-code")
    assert [c[:4] for c in env.calls] == [
        ["claude", "plugin", "marketplace", "add"],
        ["claude", "plugin", "install", "callmaibe@callmaibe-marketplace"]]
    assert env.calls[0][-2:] == ["--scope", "user"] and env.calls[1][-2:] == ["--scope", "user"]
    d = json.loads(s.read_text(encoding="utf-8"))
    assert d["env"] == {"A": "1", "MCP_TOOL_TIMEOUT": "600000"} and d["model"] == "m"
    assert len(backups(s)) == 1


def test_claude_code_falls_back_to_mcp_add(env):
    env.results["claude plugin install"] = (1, "boom")
    go(env, "--yes", "--backend", "local", "--clients", "claude-code")
    last = env.calls[-1]
    assert last[:7] == ["claude", "mcp", "add", "--scope", "user", "callmaibe", "--"]
    assert last[-1] == "mcp"


def test_dry_run_writes_nothing(env, monkeypatch, capsys):
    (env.home / ".cursor").mkdir()
    monkeypatch.setattr(sys, "stdin", io.StringIO("s3cret\n"))
    rc = go(env, "--dry-run", "--agent-uri", "a", "--callee-uri", "me", "--backend", "pjsua2",
            "--runner", "native", "--password-stdin", "--clients", "claude-code,codex,cursor,other")
    assert rc == 0
    assert env.ring == {} and env.calls == []
    assert not paths.config_file().exists()
    assert not (env.home / ".cursor" / "mcp.json").exists()
    assert not (env.home / ".codex").exists() and not (env.home / ".claude").exists()
    out = capsys.readouterr().out
    assert "dry run" in out.lower() and "claude plugin install" in out and "s3cret" not in out


def test_wsl_check_warns_when_missing(env, monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "win32")
    env.results["wsl.exe --exec sh"] = (1, "")
    go(env, "--yes", "--agent-uri", "a", "--callee-uri", "me", "--backend", "pjsua2")
    assert config.load(paths.config_file()).daemon.runner == "wsl"
    assert env.calls[0][:4] == ["wsl.exe", "--exec", "sh", "-c"] and env.calls[0][-2] == "sh"
    assert "install.ps1" in capsys.readouterr().out
