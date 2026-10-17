"""Stdlib HTTP client for the daemon, plus detached auto-start."""

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

from . import paths, runtime
from .config import Config

SPAWN_WAIT_S = 90


class DaemonError(RuntimeError):
    pass


class DaemonClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.base = f"http://{cfg.daemon.host}:{cfg.daemon.port}"

    def _req(self, method: str, path: str, body: dict | None = None,
             timeout: float | None = 10, auth: bool = True) -> dict:
        headers = {"Content-Type": "application/json"}
        if auth:
            try:
                headers["Authorization"] = "Bearer " + paths.token_file().read_text().strip()
            except OSError:
                pass
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise DaemonError(f"daemon returned HTTP {e.code}: {e.read()[:200]!r}") from e
        except (urllib.error.URLError, OSError) as e:
            raise DaemonError(f"daemon unreachable: {e}") from e

    def health(self) -> dict:
        return self._req("GET", "/health", auth=False, timeout=3)

    def status(self, session: str) -> dict:
        return self._req("GET", f"/status?session={urllib.request.quote(session)}")

    def start(self, session: str, message: str, topic: str | None = None) -> dict:
        return self._req("POST", "/call/start",
                         {"session": session, "message": message, "topic": topic}, timeout=None)

    def say(self, session: str, call_id: str, message: str, expect_reply: bool = True) -> dict:
        return self._req("POST", "/call/say", {"session": session, "call_id": call_id,
                                               "message": message, "expect_reply": expect_reply},
                         timeout=None)

    def end(self, session: str, call_id: str, message: str | None = None) -> dict:
        return self._req("POST", "/call/end",
                         {"session": session, "call_id": call_id, "message": message}, timeout=None)

    def shutdown(self) -> dict:
        return self._req("POST", "/shutdown", {})

    def is_up(self) -> bool:
        try:
            return bool(self.health().get("ok"))
        except DaemonError:
            return False

    def ensure_running(self, wait_s: float = SPAWN_WAIT_S) -> None:
        if self.is_up():
            return
        kwargs: dict = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = (subprocess.DETACHED_PROCESS
                                       | subprocess.CREATE_NEW_PROCESS_GROUP
                                       | subprocess.CREATE_NO_WINDOW)
        else:
            kwargs["start_new_session"] = True
        with (paths.logs_dir() / "daemon-spawn.log").open("ab") as log:
            subprocess.Popen(runtime.daemon_command(self.cfg), env=runtime.daemon_env(self.cfg),
                             stdin=subprocess.DEVNULL, stdout=log, stderr=log, **kwargs)
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            if self.is_up():
                return
            time.sleep(0.5)
        raise DaemonError(f"daemon did not come up within {wait_s:.0f}s "
                          f"(see {paths.logs_dir() / 'daemon.log'})")
