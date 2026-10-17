"""Session key shared by the MCP server, the CLI and the /call hook.

The switch is per Claude Code session, but Claude Code does not document any session
id env var for MCP servers (only hooks get `session_id` on stdin, and command bodies get
${CLAUDE_SESSION_ID}). So the key is the PID of the nearest ancestor process that is
Claude Code: it is the common parent of the MCP server and of hook/command subprocesses.
Process names matched: claude, claude.exe; else the nearest node/node.exe ancestor
(npm install of Claude Code). Outside Claude Code (plain terminal) the key is "local".

Limits: PIDs can be reused after Claude exits (stale entries expire after 7 days);
`claude --resume` gets a new PID, so /call on must be typed again; the node fallback can
match an unrelated node ancestor. CALLMAIBE_SESSION overrides everything.
"""

import os
import subprocess
import sys
from pathlib import Path


def _parents_linux() -> dict[int, tuple[int, str]]:
    out = {}
    for d in os.listdir("/proc"):
        if d.isdigit():
            try:
                raw = Path(f"/proc/{d}/stat").read_text(encoding="utf-8", errors="replace")
                name = raw[raw.index("(") + 1:raw.rindex(")")]
                out[int(d)] = (int(raw[raw.rindex(")") + 2:].split()[1]), name)
            except (OSError, ValueError, IndexError):
                pass
    return out


def _parents_windows() -> dict[int, tuple[int, str]]:
    import ctypes
    from ctypes import wintypes

    class PE(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]

    k32 = ctypes.windll.kernel32
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snap = k32.CreateToolhelp32Snapshot(2, 0)  # TH32CS_SNAPPROCESS
    out = {}
    try:
        pe = PE()
        pe.dwSize = ctypes.sizeof(PE)
        ok = k32.Process32FirstW(snap, ctypes.byref(pe))
        while ok:
            out[pe.th32ProcessID] = (pe.th32ParentProcessID, pe.szExeFile)
            ok = k32.Process32NextW(snap, ctypes.byref(pe))
    finally:
        k32.CloseHandle(snap)
    return out


def _parents_ps() -> dict[int, tuple[int, str]]:
    out = {}
    text = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,comm="], capture_output=True,
                          text=True, check=False).stdout
    for line in text.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            out[int(parts[0])] = (int(parts[1]), os.path.basename(parts[2]))
    return out


def _table() -> dict[int, tuple[int, str]]:
    if sys.platform == "win32":
        return _parents_windows()
    if sys.platform.startswith("linux"):
        return _parents_linux()
    return _parents_ps()


def _claude_pid(table: dict[int, tuple[int, str]], start: int) -> int | None:
    node = None
    pid, seen = start, set()
    while pid in table and pid not in seen:
        seen.add(pid)
        ppid, name = table[pid]
        base = name.lower().removesuffix(".exe")
        if base == "claude":
            return pid
        if base == "node" and node is None:
            node = pid
        pid = ppid
    return node


def session_key() -> str:
    if key := os.environ.get("CALLMAIBE_SESSION"):
        return key
    try:
        pid = _claude_pid(_table(), os.getpid())
    except Exception:  # noqa: BLE001
        pid = None
    return f"claude-{pid}" if pid else "local"
