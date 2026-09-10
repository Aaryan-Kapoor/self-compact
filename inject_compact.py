#!/usr/bin/env python3
"""Self-compact injector for a live Claude Code session.

Types a slash command (default /compact) at the session's own idle prompt by
stealing the ssh pty master fd with pidfd_getfd and writing keystrokes to it.
Must run detached and under sudo (pidfd_getfd needs attach on the sshd process).

Usage: sudo python3 inject_compact.py <claude_pid> [command] [logfile]
The wrapper discovers <claude_pid> at arm time; the script discovers the pts and
the sshd master fd on its own.
"""
import array
import ctypes
import fcntl
import glob
import json
import os
import pwd
import sys
import time

SYS_pidfd_open = 434
SYS_pidfd_getfd = 438
TIOCGPTN = 0x80045430

libc = ctypes.CDLL("libc.so.6", use_errno=True)
libc.syscall.restype = ctypes.c_long


def syscall(n, *args):
    r = libc.syscall(ctypes.c_long(n), *[ctypes.c_long(a) for a in args])
    if r < 0:
        raise OSError(ctypes.get_errno(), os.strerror(ctypes.get_errno()))
    return r


def comm(pid):
    try:
        with open(f"/proc/{pid}/comm") as f:
            return f.read().strip()
    except OSError:
        return ""


def ppid(pid):
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("PPid:"):
                    return int(line.split()[1])
    except OSError:
        pass
    return 0


def ancestors(pid):
    out = []
    while pid and pid != 1:
        out.append(pid)
        pid = ppid(pid)
    return out


def find_claude():
    for p in ancestors(os.getpid()):
        if comm(p) == "claude":
            return p
    for d in glob.glob("/proc/[0-9]*"):
        pid = int(d.rsplit("/", 1)[1])
        if comm(pid) == "claude":
            return pid
    raise SystemExit("claude pid not found")


def pts_of(pid):
    for fd in (0, 1, 2):
        try:
            t = os.readlink(f"/proc/{pid}/fd/{fd}")
        except OSError:
            continue
        if "/pts/" in t:
            return int(t.rsplit("/", 1)[1])
    raise SystemExit("controlling pts not found")


def steal_master(claude_pid, ptsnum):
    """Find the sshd fd that is the master for /dev/pts/<ptsnum> and return it."""
    candidates = [p for p in ancestors(claude_pid) if "sshd" in comm(p)]
    for sshd in candidates:
        pidfd = syscall(SYS_pidfd_open, sshd, 0)
        try:
            for entry in os.listdir(f"/proc/{sshd}/fd"):
                try:
                    stolen = syscall(SYS_pidfd_getfd, pidfd, int(entry), 0)
                except OSError:
                    continue
                try:
                    buf = array.array("i", [0])
                    fcntl.ioctl(stolen, TIOCGPTN, buf, True)
                    if buf[0] == ptsnum:
                        return stolen
                except OSError:
                    pass
                os.close(stolen)
        finally:
            os.close(pidfd)
    raise SystemExit(f"no sshd master fd for pts {ptsnum}")


def cpu_ticks(pid):
    with open(f"/proc/{pid}/stat") as f:
        data = f.read()
    fields = data[data.rindex(")") + 2:].split()
    return int(fields[11]) + int(fields[12])  # utime + stime


# stop_reasons that mean the assistant handed the turn back and the prompt is idle.
TERMINAL_STOP = {"end_turn", "max_tokens", "stop_sequence"}


def find_transcript(pid):
    """Locate this session's transcript jsonl from the claude process, or None.

    Claude Code stores it at <owner-home>/.claude/projects/<cwd-with-/-as-->/<session>.jsonl
    and does not hold it open, so it is found by cwd-derived slug + newest mtime.
    """
    try:
        cwd = os.readlink(f"/proc/{pid}/cwd")
        home = pwd.getpwuid(os.stat(f"/proc/{pid}").st_uid).pw_dir
    except OSError:
        return None
    base = os.path.join(home, ".claude", "projects", cwd.replace("/", "-"))
    jsonls = glob.glob(os.path.join(base, "*.jsonl"))
    if not jsonls:
        return None
    return max(jsonls, key=lambda p: os.path.getmtime(p))


def turn_complete(path):
    """True when the transcript's newest substantive entry is a finished assistant turn.

    During a turn the tail is tool_use / tool_result / attachment entries; a completed
    turn ends with an assistant message whose stop_reason is terminal and nothing after
    it. A network-blocked generation has no such entry yet, so this stays False.
    """
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 65536))
            chunk = f.read()
    except OSError:
        return False
    for raw in reversed(chunk.split(b"\n")):
        raw = raw.strip()
        if not raw:
            continue
        try:
            o = json.loads(raw)
        except ValueError:
            continue  # partial first line of the chunk
        t = o.get("type")
        if t == "assistant":
            return (o.get("message") or {}).get("stop_reason") in TERMINAL_STOP
        if t in ("user", "attachment"):
            return False  # a tool result or its attachment: still mid-turn
    return False


def main():
    claude_pid = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else find_claude()
    command = sys.argv[2] if len(sys.argv) > 2 else "/compact"
    logfile = sys.argv[3] if len(sys.argv) > 3 else None

    def log(msg):
        if logfile:
            try:
                with open(logfile, "a") as f:
                    f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
            except OSError:
                pass

    ptsnum = pts_of(claude_pid)
    master = steal_master(claude_pid, ptsnum)
    log(f"master for pts {ptsnum} acquired (claude={claude_pid})")

    def inject():
        os.write(master, b"\x15")       # Ctrl-U: clear any partial input line
        time.sleep(0.15)
        os.write(master, command.encode())
        time.sleep(0.4)
        os.write(master, b"\r")         # Enter: submit
        log(f"injected {command!r} after idle")

    time.sleep(1)  # let the arming turn settle before sampling
    transcript = find_transcript(claude_pid)
    deadline = time.time() + 90

    if transcript:
        # Primary gate: fire once the session's own transcript shows a finished turn
        # that then stays quiet ~1s, i.e. the prompt is genuinely idle and waiting.
        log(f"gating on transcript {transcript}")
        while time.time() < deadline:
            time.sleep(0.5)
            if turn_complete(transcript) and time.time() - os.path.getmtime(transcript) >= 1.0:
                inject()
                return
        log("gave up: transcript never reached an idle turn within 90s")
        return

    # Fallback (transcript not found): the original CPU-idle heuristic.
    log("transcript not found; falling back to cpu-idle gate")
    idle_run = 0
    last = cpu_ticks(claude_pid)
    while time.time() < deadline:
        time.sleep(0.5)
        now = cpu_ticks(claude_pid)
        idle_run = idle_run + 1 if now - last <= 1 else 0
        last = now
        if idle_run >= 3:
            inject()
            return
    log("gave up: no idle window within 90s")


if __name__ == "__main__":
    main()
