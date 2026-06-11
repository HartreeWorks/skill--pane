#!/usr/bin/env python3
"""Spawn one or more fresh AI-agent sessions in new Warp panes, tabs, or windows.

Generalises the `c2` zsh helper (~/.zshrc): for each task it stashes the prompt in
a /tmp file, writes a self-cleaning runner script, then drives Warp via a single
AppleScript pass — splitting a pane (Cmd+D) or opening a window (Cmd+N), typing the
runner path, pressing Enter, and (pane mode) refocusing the origin pane.

Used by the `pane` and `window` skills. Reads a JSON manifest of tasks; the calling
agent composes the prompts.

Usage:
    spawn_agent_panes.py --mode pane|tab|window <manifest.json>
    spawn_agent_panes.py --mode pane -        # read manifest from stdin

Manifest: a JSON array of task objects, each:
    {
      "agent":  "claude" | "codex",   # default: $CLAUDECODE -> claude, else codex
      "model":  "opus",               # claude only; default opus. Ignored for codex.
      "dir":    "/abs/path",          # default: current working directory
      "branch": false,                # fork the current session instead of fresh
      "prompt": "..."                 # required; the seed prompt / task directive
    }

Launch line per (agent x branch):
    claude, fresh  : claude --model <model> "$(cat promptfile)"
    claude, branch : claude --resume <CLAUDE_CODE_SESSION_ID> --fork-session --model <model> "$(cat promptfile)"
    codex,  fresh  : codex "$(cat promptfile)"
    codex,  branch : codex fork --last "$(cat promptfile)"

Requires Accessibility permission for Warp (System Settings > Privacy & Security >
Accessibility) — the same permission `c2` relies on.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import tempfile

ACCESSIBILITY_HINT = (
    "spawn_agent_panes: couldn't drive Warp - grant Accessibility to Warp in "
    "System Settings > Privacy & Security > Accessibility, then retry."
)


def default_agent() -> str:
    """Default agent type from the environment of the calling agent."""
    return "claude" if os.environ.get("CLAUDECODE") == "1" else "codex"


def build_launch_line(agent: str, model: str, branch: bool, promptfile: str) -> str:
    """The command the new pane's shell runs to start the agent."""
    pf = shlex.quote(promptfile)
    if agent == "claude":
        model_arg = f"--model {shlex.quote(model)}"
        if branch:
            session_id = os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip()
            if session_id:
                return (
                    f"claude --resume {shlex.quote(session_id)} --fork-session "
                    f'{model_arg} "$(cat {pf})"'
                )
            print(
                "spawn_agent_panes: branch requested but CLAUDE_CODE_SESSION_ID is "
                "unset; falling back to a fresh claude session.",
                file=sys.stderr,
            )
        return f'claude {model_arg} "$(cat {pf})"'
    # codex
    if branch:
        return f'codex fork --last "$(cat {pf})"'
    return f'codex "$(cat {pf})"'


def write_runner(task: dict) -> str:
    """Write the prompt + self-cleaning runner script; return the runner path."""
    agent = (task.get("agent") or default_agent()).lower()
    if agent not in ("claude", "codex"):
        raise ValueError(f"unknown agent {agent!r} (expected 'claude' or 'codex')")
    model = task.get("model") or "opus"
    directory = task.get("dir") or os.getcwd()
    branch = bool(task.get("branch"))
    prompt = task.get("prompt")
    if not prompt or not prompt.strip():
        raise ValueError("task is missing a non-empty 'prompt'")

    # Use /tmp explicitly (like the c2 helper): the runner path is typed into the
    # new pane keystroke-by-keystroke, so a short path drops far fewer characters
    # than the long $TMPDIR default (/var/folders/...).
    pf_fd, promptfile = tempfile.mkstemp(prefix="pane-prompt.", dir="/tmp")
    with os.fdopen(pf_fd, "w") as fh:
        fh.write(prompt)

    launch = build_launch_line(agent, model, branch, promptfile)
    runner_fd, runner = tempfile.mkstemp(prefix="pane-runner.", dir="/tmp")
    runner_body = (
        "#!/bin/zsh\n"
        f"cd {shlex.quote(directory)}\n"
        f"{launch}\n"
        f"rm -f {shlex.quote(promptfile)} {shlex.quote(runner)}\n"
    )
    with os.fdopen(runner_fd, "w") as fh:
        fh.write(runner_body)
    os.chmod(runner, 0o755)

    label = f"{agent}{' (branch)' if branch else ''}"
    print(f"spawn_agent_panes: prepared {label} in {directory}", file=sys.stderr)
    return runner


def build_applescript(mode: str) -> str:
    """AppleScript that loops over runner paths (passed as argv) and drives Warp.

    Delays are copied from the working `c2` helper; tune the post-split delay if the
    first characters get dropped on a slow shell start.
    """
    if mode == "pane":
        open_keys = 'keystroke "d" using command down'  # Cmd+D: split right
        # Ctrl+[ = pane_group:navigate_prev -> back to the origin pane so the next
        # split originates from the same pane.
        refocus = "delay 0.2\n            key code 33 using control down"
    elif mode == "tab":
        open_keys = 'keystroke "t" using command down'  # Cmd+T: new tab
        refocus = ""
    else:  # window
        open_keys = 'keystroke "n" using command down'  # Cmd+N: new window
        refocus = ""

    return f"""on run argv
    tell application "Warp" to activate
    delay 0.3
    repeat with runnerPath in argv
        tell application "System Events"
            {open_keys}
            delay 1.0
            keystroke (runnerPath as string)
            delay 0.1
            key code 36
            {refocus}
        end tell
    end repeat
end run"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Spawn agent sessions in Warp panes/tabs/windows.")
    parser.add_argument("--mode", choices=("pane", "tab", "window"), required=True)
    parser.add_argument("manifest", help="Path to manifest JSON, or '-' for stdin.")
    args = parser.parse_args()

    raw = sys.stdin.read() if args.manifest == "-" else open(args.manifest).read()
    try:
        tasks = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"spawn_agent_panes: invalid manifest JSON: {exc}", file=sys.stderr)
        return 2
    if not isinstance(tasks, list) or not tasks:
        print("spawn_agent_panes: manifest must be a non-empty JSON array.", file=sys.stderr)
        return 2

    runners: list[str] = []
    try:
        for task in tasks:
            runners.append(write_runner(task))
    except (ValueError, KeyError) as exc:
        for runner in runners:
            _cleanup_runner(runner)
        print(f"spawn_agent_panes: {exc}", file=sys.stderr)
        return 2

    script = build_applescript(args.mode)
    result = subprocess.run(
        ["osascript", "-e", script, *runners],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(ACCESSIBILITY_HINT, file=sys.stderr)
        if result.stderr.strip():
            print(result.stderr.strip(), file=sys.stderr)
        for runner in runners:
            _cleanup_runner(runner)
        return 1

    print(
        f"spawn_agent_panes: spawned {len(runners)} {args.mode}"
        f"{'' if len(runners) == 1 else 's'}.",
        file=sys.stderr,
    )
    return 0


def _cleanup_runner(runner: str) -> None:
    """Remove a runner and the promptfile it references (best effort).

    Only reached when a pane never launches (build error / osascript failure); a
    successfully launched runner deletes itself.
    """
    try:
        with open(runner) as fh:
            body = fh.read()
        for token in body.split():
            if "pane-prompt." in token:
                _safe_unlink(token.strip('"\''))
    except OSError:
        pass
    _safe_unlink(runner)


def _safe_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


if __name__ == "__main__":
    sys.exit(main())
