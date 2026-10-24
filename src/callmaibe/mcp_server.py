"""MCP stdio server: four tools, none of which can enable calling or choose a number."""

import asyncio
import contextlib
from collections.abc import Callable

from mcp.server.mcpserver import Context
from mcp.server.mcpserver import MCPServer as FastMCP

from . import config
from .client import DaemonClient, DaemonError
from .policy import Policy
from .session import session_key

STATUSES = """Result has `status`:
- in_call: call is up; `user_said` is what the person said. Answer with call_say.
  If `reason` says your message was not said, they spoke while you were working:
  answer `user_said` first, then repeat what still matters.
- no_reply: call is up but they said nothing. Ask once more with call_say, or call_end.
- ended_by_user: they hung up; `user_said` holds their last words. Act on what was decided.
- ended: call over.
- unavailable: calling is off, snoozed or in quiet hours (`reason`). Do NOT retry and do not
  ask the user to enable it; write your summary in the terminal instead.
- declined, no_answer, busy, line_busy: nobody picked up. Do not retry; write the summary in the
  terminal.
- not_found, error: stop calling and write the summary in the terminal."""

STYLE = ("Phone style: short plain sentences, one question at a time, no markdown, code, "
         "file paths or URLs; say numbers naturally. The phone already says fillers like "
         "'got it' and 'let me check' while you work and answers 'can you hear me' itself, "
         "so start with the answer.")

mcp = FastMCP("callmaibe", instructions="Phone the user over a real call. " + STYLE)


async def _run(ctx: Context, fn: Callable[[DaemonClient], dict], ensure: bool = True) -> dict:
    cfg = config.load()
    client = DaemonClient(cfg)

    def work() -> dict:
        try:
            if ensure:
                client.ensure_running()
            return fn(client)
        except DaemonError as e:
            return {"status": "unavailable", "user_said": "", "call_id": "",
                    "reason": f"phone service unavailable: {e}", "transcript_path": ""}

    task = asyncio.create_task(asyncio.to_thread(work))
    ticks = 0
    while True:
        done, _ = await asyncio.wait({task}, timeout=10)
        if done:
            return task.result()
        ticks += 1
        with contextlib.suppress(Exception):
            await ctx.report_progress(ticks, None, "call in progress")


@mcp.tool(description="Start a phone call to the user and speak `message`, then wait for their "
          "reply. Open with a short summary of what you did and what you weighed and why, then ask "
          "one question. Only works if the user has turned calling on for this session. "
          f"`topic` is an optional short label. {STYLE}\n{STATUSES}")
async def call_start(message: str, ctx: Context, topic: str | None = None) -> dict:
    return await _run(ctx, lambda c: c.start(session_key(), message, topic))


@mcp.tool(description="Speak `message` on the active call and (if expect_reply) wait for the "
          "person's answer. Needs the call_id from call_start. Even with expect_reply false "
          "they can talk over you; then `user_said` holds what they said, so answer it. "
          f"{STYLE}\n{STATUSES}")
async def call_say(call_id: str, message: str, ctx: Context, expect_reply: bool = True) -> dict:
    return await _run(ctx, lambda c: c.say(session_key(), call_id, message, expect_reply))


@mcp.tool(description="Say an optional short goodbye and hang up the call. Always end calls "
          "you started. If the person talks over the goodbye, the call stays "
          "up and the status is in_call with what they said: answer with call_say, then call "
          f"call_end again.\n{STATUSES}")
async def call_end(call_id: str, ctx: Context, message: str | None = None) -> dict:
    return await _run(ctx, lambda c: c.end(session_key(), call_id, message))


@mcp.tool(description="Check whether calling is currently allowed for this session and whether a "
          "call is active. Returns allowed, reason, snoozed_until, quiet_now, active_call. "
          "Does not place a call.")
async def call_status(ctx: Context) -> dict:
    def work(c: DaemonClient) -> dict:
        if c.is_up():
            return c.status(session_key())
        return Policy(config.load().policy).status(session_key())

    return await _run(ctx, work, ensure=False)


def main() -> None:
    mcp.run()
