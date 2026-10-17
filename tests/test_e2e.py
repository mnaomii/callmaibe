"""Full stack minus SIP: MCP client -> `callmaibe mcp` subprocess -> daemon HTTP -> CallManager
with real Kokoro + faster-whisper, and a fake phone line whose "user" speech is Kokoro audio."""

import asyncio
import json
import os
import socket
import sys
import threading

import pytest

from callmaibe import paths, stt, tts
from callmaibe.config import load
from callmaibe.conversation import CallManager
from callmaibe.daemon import make_server
from callmaibe.policy import Policy
from callmaibe.telephony.fake import FakeTelephony

pytestmark = pytest.mark.slow


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _payload(result) -> dict:
    if getattr(result, "structuredContent", None):
        data = result.structuredContent
        return data.get("result", data)
    return json.loads(result.content[0].text)


def test_full_conversation_through_mcp(tmp_path, monkeypatch):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    monkeypatch.setenv("CALLMAIBE_HOME", str(tmp_path))
    monkeypatch.setenv("CALLMAIBE_SESSION", "e2e")
    port = _free_port()
    paths.config_file().write_text(
        f'[daemon]\nport = {port}\n[media]\nbackend = "fake"\n'
        '[callee]\nuri = "sip:me@example.org"\n[stt]\nmodel = "tiny"\n'
        "[conversation]\nthinking_cue = false\n"
    )
    cfg = load()
    speaker, ear = tts.create(cfg.tts), stt.create(cfg.stt)
    reply = b"".join(speaker.synthesize("Yes, go ahead and push the branch."))
    # Real-time pacing: in fast mode the "user" would answer while the first TTS chunk is still
    # being synthesized, which never happens on a real line.
    phone = FakeTelephony(speed=1.0, incoming=[6.0, reply, 4.0])
    phone.start()
    manager = CallManager(phone, speaker, ear, cfg)
    policy = Policy(cfg.policy)
    policy.enable_session("e2e", True)
    paths.token_file().write_text("e2e-token")
    server = make_server(cfg, manager, policy)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    async def run() -> tuple[dict, dict]:
        params = StdioServerParameters(command=sys.executable, args=["-m", "callmaibe", "mcp"],
                                       env=dict(os.environ))
        async with stdio_client(params) as (r, w), ClientSession(r, w) as session:
            await session.initialize()
            started = _payload(await session.call_tool(
                "call_start", {"message": "Hi, the tests pass. Should I push the branch?"}))
            ended = _payload(await session.call_tool(
                "call_end", {"call_id": started["call_id"], "message": "Okay, pushing now. Bye."}))
            return started, ended

    try:
        started, ended = asyncio.run(run())
    finally:
        server.shutdown()
        manager.close()

    assert started["status"] == "in_call", started
    assert "push" in started["user_said"].lower(), started
    assert ended["status"] == "ended", ended
    assert ended["transcript_path"] and os.path.exists(ended["transcript_path"])
