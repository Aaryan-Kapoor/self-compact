---
name: self-compact
description: Make the current Claude Code session run /compact (or another slash command) on itself from within a tool call, when the session is a bare SSH login with no tmux. Use when you need the live TUI to compact itself.
---

# Self-compact

Runs a slash command against the current Claude Code session exactly as if you had typed it at the prompt. It works by stealing the ssh pty master file descriptor with `pidfd_getfd` and writing the keystrokes to it, so the TUI's input component parses `/compact` as real typed input rather than as a peer message (which is never slash-parsed).

## Requirements

- The session is reached over SSH (there is an sshd process holding the pty master). tmux is not needed and is not used.
- Passwordless `sudo` is available. `pidfd_getfd` needs attach permission on the sshd process, which sudo supplies.

## How to run it

Arm the injector detached, discovering this session's claude PID at arm time, then end the turn immediately and say nothing further:

```bash
CLAUDE_PID=""; p=$$
while [ "$p" -gt 1 ]; do
  [ "$(cat /proc/$p/comm 2>/dev/null)" = "claude" ] && { CLAUDE_PID=$p; break; }
  p=$(awk '/^PPid:/{print $2}' /proc/$p/status 2>/dev/null)
done
setsid sudo python3 ~/.claude/skills/self-compact/inject_compact.py "$CLAUDE_PID" >/dev/null 2>&1 &
disown
```

For a different command, pass it as the second argument, e.g. `... inject_compact.py "$CLAUDE_PID" /model`.

## Why it must be detached and idle-gated

Keystrokes that arrive while the session is busy in a tool call are queued, not executed, so a `/compact` typed mid-turn does nothing until the prompt goes idle. The script detaches with `setsid`, then watches this session's own transcript jsonl and fires a single `/compact` only once its newest entry is a finished assistant turn (`stop_reason` `end_turn`) that then stays quiet for about a second. That is a real prompt-idle signal: a turn blocked on network I/O has not written its `end_turn` yet, so it will not fire early. If the transcript cannot be located it falls back to a CPU-idle heuristic on `/proc/<pid>/stat`. It gives up after 90 seconds, and it never repeats, so it cannot spam the input line.

Because the injection lands at the idle prompt, end your turn right after arming — do not keep working, or the keystrokes will queue behind you.

## Verifying it fired

A real compaction writes a message with `isCompactSummary: true` into the session transcript at `~/.claude/projects/<project-slug>/<session-id>.jsonl`. Count those before and after; an increase means it compacted, and no change means the keystrokes queued or the idle window never opened.
