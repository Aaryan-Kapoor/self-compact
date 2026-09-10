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
import os
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

    time.sleep(2)  # let the arming turn's output finish rendering
    idle_run = 0
    last = cpu_ticks(claude_pid)
    deadline = time.time() + 90
    while time.time() < deadline:
        time.sleep(0.5)
        now = cpu_ticks(claude_pid)
        idle_run = idle_run + 1 if now - last <= 1 else 0
        last = now
        if idle_run >= 3:
            os.write(master, b"\x15")   # Ctrl-U: clear any partial input line
            time.sleep(0.15)
            os.write(master, command.encode())
            time.sleep(0.4)
            os.write(master, b"\r")     # Enter: submit
            log(f"injected {command!r} after idle")
            return
    log("gave up: no idle window within 90s")


if __name__ == "__main__":
    main()
