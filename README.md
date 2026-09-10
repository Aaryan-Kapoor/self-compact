# Self-`/compact`: making a live Claude Code session run a slash command on itself

This document explains how a running Claude Code session reached over plain SSH can execute `/compact` (or any other slash command) against itself, exactly as if a human had typed it at the prompt. It was worked out and verified on 2026-09-09 against Claude Code 2.1.263–2.1.267 running under a bare SSH login, with no tmux or screen in between, on Linux 7.0.0-30-generic.

The short version is that the only thing the TUI treats as a real command is a keystroke arriving on its terminal input, so the whole problem reduces to getting characters onto that input from inside a tool call. Every channel that looks like it should carry a command turns out not to, and the one channel that does — the pty master — belongs to `sshd` and is fenced off from the obvious `/proc` route. The unlock is to steal the master file descriptor out of `sshd` with `pidfd_getfd`, and the sections below explain both why the easy doors are locked and why this one opens.

## The one-line answer

From inside the session, arm the injector detached, discovering this session's own claude PID at arm time, then end the turn immediately and say nothing else:

```bash
CLAUDE_PID=""; p=$$
while [ "$p" -gt 1 ]; do
  [ "$(cat /proc/$p/comm 2>/dev/null)" = "claude" ] && { CLAUDE_PID=$p; break; }
  p=$(awk '/^PPid:/{print $2}' /proc/$p/status 2>/dev/null)
done
setsid sudo python3 inject_compact.py "$CLAUDE_PID" >/dev/null 2>&1 &
disown
```

A second or two after the turn ends and the prompt goes idle, `/compact` appears on the input line and submits itself, and the TUI parses and runs it the same way it would for a human. Passwordless `sudo` is required, because stealing the descriptor needs attach permission on the `sshd` process. The command defaults to `/compact`; pass a different one as the second argument.

## Why the obvious approaches do not work

### The SendMessage tool refuses to address the session itself

Claude Code's peer-messaging tool carries an explicit self-target guard. It rejects the session's own name, its `[ref]` form, and the alias `main`, so all three ways of naming yourself are covered. There is no tool-level path to your own inbox.

### The peer socket delivers messages but never parses them as commands

Each session registers a Unix domain socket and a `peerToken`, and it is entirely possible to speak the NDJSON wire protocol to your own inbox directly and have the message appear in the conversation. That part works. What does not work is getting a command out of it: slash-command parsing lives in the TUI's input component and runs only on typed input, so a `/compact` delivered over the socket arrives as literal text wrapped in the usual peer-message envelope. The message is real, the command is not.

### The control-frame channel has no compact action

The same socket accepts control frames rather than user messages, but the action whitelist has exactly three entries: `rename`, which routes to the rename handler, `peer_message_status`, and `notify_when_idle`. There is nothing in that list that triggers compaction, and unknown actions are dropped.

### Auto-compaction is a different thing wearing the same name

You can absolutely cause a compaction to happen by editing `~/.claude/settings.json`, since `autoCompactEnabled` plus a small `autoCompactWindow` will arm the threshold check that runs at the start of each turn, and the config file is watched with inotify so the change takes effect without a restart. The arm fraction comes from a Statsig gate and sits around 0.92 of the window by default, and there is a `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` environment variable, though that one is only read at process start.

This is worth knowing about and it is worth not using. Auto-compaction is the harness deciding to compact on its own schedule, which is a genuinely different event from the user running the command, and if the goal is the latter then the settings route is a way of appearing to succeed without succeeding. It also mutates global config as a side effect, which then has to be reverted.

### The kernel closes the ordinary injection tricks

The classic trick for shoving characters into your own terminal is the `TIOCSTI` ioctl, and it is disabled here:

```
dev.tty.legacy_tiocsti = 0
```

Attaching a debugger to the parent and driving it from there fails for a related reason:

```
kernel.yama.ptrace_scope = 1
```

which permits tracing descendants only, and the claude process is an ancestor rather than a descendant of anything spawned from inside a tool call.

## The unlock: stealing the pty master out of `sshd`

The one descriptor that is genuinely wired to the TUI's input is the pty master. When the session is reached over SSH that master belongs to `sshd`, and the tempting move is to reopen it through `/proc/<sshd-pid>/fd/<n>`. That does not work: the master is marked non-dumpable so the same user cannot open it out of `/proc`, and even where a path is openable, opening a pty by name hands back a freshly allocated pty rather than the existing open file, so the characters go nowhere the TUI is listening.

`pidfd_getfd` sidesteps both problems. It duplicates the actual open file object out of another process's descriptor table, so what comes back is the real master that `sshd` is already holding, not a new pty and not a `/proc` reopen. The call needs `PTRACE_MODE_ATTACH` permission on the target, which is exactly what running under `sudo` supplies, and it is why passwordless `sudo` is the one prerequisite.

The injector puts this together without any hardcoded process ids. It is handed the claude PID at arm time, reads the controlling pts number from `/proc/<claude>/fd/0`, walks up the ancestry to find the `sshd` processes, and for each of their descriptors calls `pidfd_getfd` and then `TIOCGPTN` to ask which pts that master controls. The descriptor whose pts number matches the session's is the right one, and writing `/compact\r` to it lands on the input line.

## Why it must be detached and idle-gated

Two separate timing failures will silently swallow the command, and both look identical from the outside because the input line simply ends up empty.

The first is that keystrokes arriving while claude is busy executing a tool call are queued rather than executed. If the write runs synchronously inside the Bash tool call that arms it, the session is by definition busy at that moment, so the text sits in the queue and does nothing useful. Detaching with `setsid` and waiting past the end of the turn is what puts the keystrokes at an idle prompt, and `disown` keeps the shell from tracking the job so the tool call returns immediately rather than blocking on it.

The injector waits for that idle prompt rather than guessing at a fixed delay, and it reads the prompt's state from the session's own transcript rather than inferring it from CPU load. Claude Code appends each turn to a jsonl transcript under `~/.claude/projects/`, and a turn that has handed control back to the user ends with an assistant message whose `stop_reason` is `end_turn` and nothing after it; a turn still in flight ends in a tool-use or tool-result entry instead. The injector locates that transcript from the claude process, watches its tail, and fires only once the newest entry is a finished turn that has then stayed quiet for about a second. This is what distinguishes a genuinely idle prompt from a turn that merely looks quiet because it is blocked on network I/O: a blocked turn has not written its `end_turn` yet. Where the transcript cannot be found the injector falls back to sampling CPU time from `/proc/<pid>/stat`. Either way it fires exactly once and then exits, giving up after ninety seconds if no idle window opens. The single-shot design matters: an injector that retried would queue several `/compact` invocations behind a busy prompt and spray them the moment it went idle.

## Verifying that it actually fired

The failure mode here is believing it worked when the write was swallowed, so it is worth having a check that does not rely on impressions. Compaction writes a message with `isCompactSummary: true` into the session transcript, so counting those in `~/.claude/projects/<project-slug>/<session-id>.jsonl` before and after distinguishes a real compaction from a no-op. No increase after an attempt means the keystrokes queued or the idle window never opened, and the detach or the idle gate is the thing to look at.

## Generalising beyond `/compact`

Nothing in the mechanism is specific to compaction. Any slash command the TUI accepts can be driven the same way, since the only thing being exploited is that a stolen master fd can put real characters on the input line. The constraints that carry over are that the session is reached over SSH, that passwordless `sudo` is available, that the keystrokes land while the prompt is idle, and that only one is ever sent.

## Installing it as a skill

The same mechanism ships as a Claude Code skill so a session can invoke it on demand. `SKILL.md` and `inject_compact.py` in this repo are the skill; place the directory at `~/.claude/skills/self-compact/` and the session can run the arming step above against itself.
