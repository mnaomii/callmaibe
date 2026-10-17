from callmaibe import credentials, runtime
from callmaibe.config import Config


def _cfg() -> Config:
    cfg = Config()
    cfg.account.uri = "sip:agent@example.org"
    return cfg


def test_env_var_beats_keyring(monkeypatch):
    monkeypatch.setattr(credentials, "get", lambda key: "from-keyring")
    cfg = _cfg()
    monkeypatch.delenv(cfg.account.password_env, raising=False)
    assert cfg.account.password == "from-keyring"
    monkeypatch.setenv(cfg.account.password_env, "from-env")
    assert cfg.account.password == "from-env"


def test_keyring_key_is_the_account_uri(monkeypatch):
    seen = []
    monkeypatch.setattr(credentials, "get", lambda key: seen.append(key) or "")
    monkeypatch.delenv("CALLMAIBE_SIP_PASSWORD", raising=False)
    assert _cfg().account.password == ""
    assert seen == ["sip:agent@example.org"]


def test_wsl_daemon_env_carries_keyring_password_only_to_the_child(monkeypatch):
    monkeypatch.setattr(credentials, "get", lambda key: "s3cret" if key.startswith("sip:") else "")
    monkeypatch.setattr(runtime, "uses_wsl", lambda cfg: True)
    monkeypatch.delenv("CALLMAIBE_SIP_PASSWORD", raising=False)
    monkeypatch.setenv("WSLENV", "FOO")
    env = runtime.daemon_env(_cfg())
    assert env["CALLMAIBE_SIP_PASSWORD"] == "s3cret"
    assert "CALLMAIBE_TURN_PASSWORD" not in env
    assert env["WSLENV"] == "FOO:CALLMAIBE_SIP_PASSWORD:CALLMAIBE_TURN_PASSWORD"
    import os
    assert "CALLMAIBE_SIP_PASSWORD" not in os.environ


def test_no_keyring_backend_reads_empty(monkeypatch):
    monkeypatch.setattr(credentials, "backend_name", lambda: "")
    assert credentials.get("sip:x@y") == ""
