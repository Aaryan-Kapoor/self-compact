---
name: self-compact
description: Make the current Claude Code session run /compact (or another slash command) on itself from within a tool call, when the session is a bare SSH login with no tmux. Use when you need the live TUI to compact itself.
---

# Self-compact

Runs a slash command against the current Claude Code session exactly as if you had typed it at the prompt. It works by duplicating the ssh pty master file descriptor out of `sshd` with `pidfd_getfd` and writing the keystrokes to it, so the TUI's input component parses `/compact` as real typed input rather than as a peer message (which is never slash-parsed).

## Requirements

- The session is reached over SSH (there is an sshd process holding the pty master). tmux is not needed and is not used.
- Passwordless `sudo`. `pidfd_getfd` needs attach permission on the sshd process, which sudo supplies. Root is dropped back to your user as soon as the master fd is held.
- Linux 5.6 or newer (for `pidfd_getfd`), glibc, and `python3`.
- Nobody is typing in the session. The injector clears the input line before typing, so an unsubmitted draft would be lost.

## How to run it

Arm the injector detached, passing this session's own pid and session id from the environment Claude Code exports to every tool shell, then end the turn immediately and say nothing further:

```bash
LOG="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/self-compact.log"
setsid sudo -n python3 ~/.claude/skills/self-compact/inject_compact.py \
  --pid "$CLAUDE_PID" --session "$CLAUDE_CODE_SESSION_ID" --log "$LOG" >/dev/null 2>&1 &
disown
```

For a different command, add `--command /model` (single-line slash commands only; anything else is refused). `--timeout` changes the 90 second wait for an idle prompt, and `--dry-run` does everything except write the keystrokes, which is the way to test the setup.

## Why it must be detached and idle-gated

Keystrokes that arrive while the session is busy in a tool call are queued, not executed, so a `/compact` typed mid-turn does nothing until the prompt goes idle. The wrapper detaches the injector with `setsid`, and the injector then watches this session's own transcript, located exactly by session id (honouring `CLAUDE_CONFIG_DIR` and `CLAUDE_CODE_PROJECT_DIR_NAME` from the claude process's environment), and fires a single command only once all of the following hold:

- the newest state-bearing entry is an assistant message with `stop_reason` `end_turn` or `refusal`, followed only by known bookkeeping entries, and the file has been untouched for about a second;
- the claude process is still alive and is the foreground process group on its terminal;
- no other injector holds this session's lock.

Every one of those checks is repeated immediately before each of the three writes (clear line, command, Enter), so a prompt that stops being idle mid-sequence gets at most a cleared line and never a submitted command. Anything unexpected fails closed: a transcript that cannot be found, a partial line still being appended, an entry type the injector does not recognise, or a stop reason like `max_tokens` all mean no injection. There is no CPU-idle fallback. It gives up after the timeout and never repeats, so it cannot spam the input line.

Because the injection lands at the idle prompt, end your turn right after arming. Do not keep working, or the keystrokes will wait behind you and the injector will time out.

## Verifying it fired

Read the log file. It records `armed`, `gating on <transcript>`, then one of `delivered '/compact'`, `aborted before <step>: <reason>`, or `gave up: <reason>`. For `/compact` it then watches the transcript for up to a minute and logs `compaction observed in transcript` when a `compact_boundary` entry appears. The exit status is 0 when delivered, 2 when it gave up or aborted, and 1 on a setup error, though the detached wrapper discards it, which is why the log exists.

A `delivered` line without `compaction observed` means the keystrokes reached the terminal but the TUI did not compact, for example because the conversation was too short to compact. A `gave up` line names the check that never passed.
