#!/usr/bin/env python3
"""Unit tests for the transcript idle-gate in inject_compact.py.

Run: python3 test_inject_compact.py   (exits non-zero on failure)
Synthetic transcripts only; no live session or sudo needed.
"""
import importlib.util
import json
import os
import tempfile

_spec = importlib.util.spec_from_file_location(
    "ic", os.path.join(os.path.dirname(os.path.abspath(__file__)), "inject_compact.py")
)
ic = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ic)


def _write(entries):
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd)
    with open(path, "w") as f:
        for o in entries:
            f.write(json.dumps(o) + "\n")
    return path


A_END = {"type": "assistant", "message": {"stop_reason": "end_turn", "content": [{"type": "text"}]}}
A_MAX = {"type": "assistant", "message": {"stop_reason": "max_tokens"}}
A_TOOL = {"type": "assistant", "message": {"stop_reason": "tool_use", "content": [{"type": "tool_use"}]}}
U_RES = {"type": "user", "message": {"content": [{"type": "tool_result"}]}}
ATTACH = {"type": "attachment"}

CASES = [
    ("completed turn ends in end_turn", [A_TOOL, U_RES, A_END], True),
    ("terminal max_tokens counts as done", [A_TOOL, U_RES, A_MAX], True),
    ("mid tool call (tool_use tail)", [A_END, A_TOOL], False),
    ("tool result pending (user tail)", [A_TOOL, U_RES], False),
    ("attachment after tool result", [A_TOOL, U_RES, ATTACH], False),
    ("attachment newer than end_turn", [A_END, ATTACH], False),
    ("empty transcript", [], False),
]


def main():
    failures = 0
    for name, entries, want in CASES:
        path = _write(entries)
        try:
            got = ic.turn_complete(path)
        finally:
            os.unlink(path)
        ok = got == want
        failures += not ok
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: got={got} want={want}")
    print("OK" if not failures else f"{failures} FAILED")
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    main()
