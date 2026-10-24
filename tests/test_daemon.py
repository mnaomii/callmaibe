import json
import threading
import urllib.error
import urllib.request

import pytest

from callmaibe import paths
from callmaibe.config import Config
from callmaibe.daemon import make_server
from callmaibe.policy import Policy
from callmaibe.status import Status, TurnResult

TOKEN = "tok"


class FakeManager:
    def __init__(self):
        self.next = TurnResult(Status.IN_CALL, user_said="hi", call_id="c1")
        self.calls: list[tuple] = []

    def start(self, message, topic=None):
        self.calls.append(("start", message, topic))
        return self.next

    def say(self, call_id, message, expect_reply=True):
        self.calls.append(("say", call_id, message, expect_reply))
        return TurnResult(Status.IN_CALL, user_said="ok", call_id=call_id)

    def end(self, call_id, message=None):
        self.calls.append(("end", call_id, message))
        return TurnResult(Status.ENDED, call_id=call_id)

    def active(self):
        return None

    def close(self):
        pass


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CALLMAIBE_HOME", str(tmp_path))
    paths.token_file().write_text(TOKEN)
    cfg = Config()
    policy = Policy(cfg.policy)
    mgr = FakeManager()
    server = make_server(cfg, mgr, policy, "127.0.0.1", 0)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def call(method, path, body=None, token=TOKEN):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    yield call, mgr, policy
    server.shutdown()
    server.server_close()


def test_health_no_auth(env):
    call, _, _ = env
    code, body = call("GET", "/health", token=None)
    assert code == 200 and body["ok"] is True


def test_auth_required(env):
    call, mgr, _ = env
    assert call("GET", "/status?session=s", token=None)[0] == 401
    assert call("GET", "/status?session=s", token="wrong")[0] == 401
    assert call("POST", "/call/start", {"session": "s", "message": "m"}, token="wrong")[0] == 401
    assert mgr.calls == []


def test_status(env):
    call, _, policy = env
    policy.enable_session("s", True)
    code, body = call("GET", "/status?session=s")
    assert code == 200 and body["allowed"] and body["session_enabled"]
    assert body["active_call"] is None


def test_unavailable_never_dials(env):
    call, mgr, _ = env
    _, body = call("POST", "/call/start", {"session": "s", "message": "hello"})
    assert body["status"] == "unavailable" and "/call on" in body["reason"]
    assert mgr.calls == []


def test_allowed_start(env):
    call, mgr, policy = env
    policy.enable_session("s", True)
    _, body = call("POST", "/call/start", {"session": "s", "message": "hello", "topic": "t"})
    assert body["status"] == "in_call" and body["call_id"] == "c1"
    assert mgr.calls == [("start", "hello", "t")]


def test_once_permit_consumed(env):
    call, mgr, policy = env
    policy.grant_once("s")
    assert call("POST", "/call/start", {"session": "s", "message": "a"})[1]["status"] == "in_call"
    assert call("POST", "/call/start", {"session": "s", "message": "b"})[1]["status"] == "unavailable"
    assert len(mgr.calls) == 1


def test_once_permit_kept_on_no_answer(env):
    call, mgr, policy = env
    mgr.next = TurnResult(Status.NO_ANSWER)
    policy.grant_once("s")
    call("POST", "/call/start", {"session": "s", "message": "a"})
    assert policy.check("s")[0]


def test_declined_snoozes(env):
    call, mgr, policy = env
    policy.enable_session("s", True)
    mgr.next = TurnResult(Status.DECLINED)
    assert call("POST", "/call/start", {"session": "s", "message": "a"})[1]["status"] == "declined"
    ok, reason = policy.check("s")
    assert not ok and "snoozed" in reason
    assert call("POST", "/call/start", {"session": "s", "message": "b"})[1]["status"] == "unavailable"
    assert len(mgr.calls) == 1


def test_session_mismatch(env):
    call, mgr, policy = env
    policy.enable_session("s", True)
    call("POST", "/call/start", {"session": "s", "message": "a"})
    body = {"session": "other", "call_id": "c1", "message": "x"}
    assert call("POST", "/call/say", body)[1]["status"] == "not_found"
    assert call("POST", "/call/end", body)[1]["status"] == "not_found"
    assert [c[0] for c in mgr.calls] == ["start"]
    # owner can say and end
    assert call("POST", "/call/say", {**body, "session": "s"})[1]["status"] == "in_call"
    assert call("POST", "/call/end", {"session": "s", "call_id": "c1"})[1]["status"] == "ended"
    assert call("POST", "/call/say", {**body, "session": "s"})[1]["status"] == "not_found"


def test_interrupted_goodbye_keeps_the_call(env):
    call, mgr, policy = env
    policy.enable_session("s", True)
    call("POST", "/call/start", {"session": "s", "message": "a"})
    body = {"session": "s", "call_id": "c1", "message": "bye"}
    mgr.end = lambda call_id, message=None: TurnResult(Status.IN_CALL, user_said="wait",
                                                       call_id=call_id)
    assert call("POST", "/call/end", body)[1]["user_said"] == "wait"
    assert call("POST", "/call/say", body)[1]["status"] == "in_call"


def test_bad_request(env):
    call, _, _ = env
    assert call("POST", "/call/start", {"session": "s"})[0] == 400


def test_shutdown(env):
    call, _, _ = env
    assert call("POST", "/shutdown", {})[1] == {"ok": True}
