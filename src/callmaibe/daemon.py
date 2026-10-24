"""Local HTTP daemon (see docs/API.md): owns the line, enforces policy."""

import hmac
import json
import logging
import os
import secrets
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import __version__, paths
from .config import Config
from .policy import Policy
from .status import Status, TurnResult

log = logging.getLogger("callmaibe.daemon")

_UP = {Status.IN_CALL, Status.NO_REPLY}
_KEEP_PERMIT = {Status.NO_ANSWER, Status.BUSY, Status.LINE_BUSY, Status.ERROR, Status.UNAVAILABLE}


def _result(status: Status, reason: str = "", call_id: str = "") -> dict:
    return TurnResult(status, call_id=call_id, reason=reason).to_dict()


def make_server(cfg: Config, manager: Any, policy: Policy,
                host: str | None = None, port: int | None = None) -> ThreadingHTTPServer:
    owners: dict[str, str] = {}
    lock = threading.Lock()

    def set_owner(call_id: str, session: str) -> None:
        with lock:
            owners[call_id] = session

    def owner_ok(call_id: str, session: str) -> bool:
        with lock:
            return owners.get(call_id) == session

    def drop_owner(call_id: str) -> None:
        with lock:
            owners.pop(call_id, None)

    def do_start(body: dict) -> dict:
        session, message = body["session"], body["message"]
        allowed, reason = policy.check(session)
        if not allowed:
            return _result(Status.UNAVAILABLE, reason)
        res = manager.start(message, body.get("topic"))
        status = Status(res.status)
        if status == Status.DECLINED:
            policy.on_declined()
        if status not in _KEEP_PERMIT:
            policy.consume_once(session)
        if res.call_id and status in _UP:
            set_owner(res.call_id, session)
        return res.to_dict()

    def do_turn(body: dict, ending: bool) -> dict:
        call_id, session = body["call_id"], body["session"]
        if not owner_ok(call_id, session):
            return _result(Status.NOT_FOUND, "no such call for this session", call_id)
        if ending:
            res = manager.end(call_id, body.get("message"))
        else:
            res = manager.say(call_id, body["message"], bool(body.get("expect_reply", True)))
        if Status(res.status) not in _UP:     # an interrupted goodbye keeps the call up
            drop_owner(call_id)
        return res.to_dict()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:
            log.debug("%s " + fmt, self.address_string(), *args)

        def _send(self, code: int, obj: dict) -> None:
            data = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authed(self) -> bool:
            try:
                token = paths.token_file().read_text().strip()
            except OSError:
                return False
            given = self.headers.get("Authorization", "")
            return bool(token) and hmac.compare_digest(given, f"Bearer {token}")

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b"{}"
            data = json.loads(raw or b"{}")
            if not isinstance(data, dict):
                raise TypeError("body must be an object")
            return data

        def do_GET(self) -> None:
            url = urlparse(self.path)
            if url.path == "/health":
                return self._send(200, {
                    "ok": True, "version": __version__,
                    "registered": getattr(self.server, "registered", None),
                    "backend": cfg.media.backend,
                })
            if not self._authed():
                return self._send(401, {"error": "unauthorized"})
            if url.path == "/status":
                session = parse_qs(url.query).get("session", [""])[0]
                st = policy.status(session)
                st["active_call"] = manager.active()
                return self._send(200, st)
            self._send(404, {"error": "not found"})

        def do_POST(self) -> None:
            url = urlparse(self.path)
            if not self._authed():
                return self._send(401, {"error": "unauthorized"})
            try:
                body = self._body()
                if url.path == "/shutdown":
                    self._send(200, {"ok": True})
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                    return
                if url.path == "/call/start":
                    return self._send(200, do_start(body))
                if url.path == "/call/say":
                    return self._send(200, do_turn(body, False))
                if url.path == "/call/end":
                    return self._send(200, do_turn(body, True))
            except (KeyError, ValueError, TypeError) as e:
                return self._send(400, {"error": f"bad request: {e!r}"})
            except Exception as e:
                log.exception("handler failed")
                return self._send(200, _result(Status.ERROR, f"{type(e).__name__}: {e}"))
            self._send(404, {"error": "not found"})

    server = ThreadingHTTPServer(
        (host or cfg.daemon.host, cfg.daemon.port if port is None else port), Handler)
    server.daemon_threads = True
    return server


def _setup_logging() -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    handlers: list[logging.Handler] = [
        logging.FileHandler(paths.logs_dir() / "daemon.log", encoding="utf-8"),
        logging.StreamHandler(sys.stderr),
    ]
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in handlers:
        h.setFormatter(fmt)
        root.addHandler(h)


def serve(cfg: Config) -> None:
    from . import stt, telephony, tts
    from .conversation import CallManager

    _setup_logging()
    lock, token = paths.lock_file(), paths.token_file()
    owned = False

    tel = tts_ = stt_ = mgr = server = None
    try:
        tel = telephony.create(cfg)
        tts_ = tts.create(cfg.tts)
        stt_ = stt.create(cfg.stt)
        tel.start()
        mgr = CallManager(tel, tts_, stt_, cfg)

        def warm() -> None:
            for comp in (tts_, stt_):
                try:
                    comp.warmup()
                except Exception:
                    log.exception("warmup failed for %r", comp)

        threading.Thread(target=warm, daemon=True).start()
        server = make_server(cfg, mgr, Policy(cfg.policy))
        owned = True
        token.write_text(secrets.token_urlsafe(32))
        try:
            os.chmod(token, 0o600)
        except OSError:
            pass
        lock.write_text(str(os.getpid()))
        server.registered = tel.registered  # type: ignore[attr-defined]
        log.info("daemon listening on %s:%s", *server.server_address[:2])
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        log.info("daemon shutting down")
        if server:
            server.server_close()
        for closer in (mgr and mgr.close, tel and tel.stop):
            if closer:
                try:
                    closer()
                except Exception:
                    log.exception("close failed")
        if owned:
            for f in (lock, token):
                f.unlink(missing_ok=True)
