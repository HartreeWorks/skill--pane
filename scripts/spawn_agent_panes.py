#!/usr/bin/env python3
"""Reserve Warp destinations now, then launch agents when prompts are ready.

Used by the pane, tab, and window skills:

    spawn_agent_panes.py reserve --mode pane|tab|window --count N
    spawn_agent_panes.py fulfil <reservation-directory> <manifest.json|->
    spawn_agent_panes.py cancel <reservation-directory>

The previous one-shot interface remains available:

    spawn_agent_panes.py --mode pane|tab|window <manifest.json|->
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path


ACCESSIBILITY_HINT = (
    "spawn_agent_panes: couldn't drive Warp - grant Accessibility to Warp in "
    "System Settings > Privacy & Security > Accessibility, then retry."
)
DEFAULT_TIMEOUT_SECONDS = 600
START_TIMEOUT_SECONDS = 5.0
MODES = ("pane", "tab", "window")


def default_agent() -> str:
    return "claude" if os.environ.get("CLAUDECODE") == "1" else "codex"


def build_launch_line(agent: str, model: str, branch: bool, promptfile: str) -> str:
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
    if branch:
        return f'codex fork --last "$(cat {pf})"'
    return f'codex "$(cat {pf})"'


def _path(root: str | Path, kind: str, index: int | None = None) -> Path:
    name = kind if index is None else f"{kind}.{index}"
    return Path(root) / name


def _atomic_write(path: Path, content: str, mode: int = 0o600) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(content)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        _safe_unlink(temporary)
        raise


def _state_writer(state: Path) -> str:
    quoted = shlex.quote(str(state))
    return (
        "write_state() {\n"
        f"    local state_path={quoted}\n"
        '    local state_tmp="${state_path}.tmp.$$"\n'
        '    print -r -- "$1" > "$state_tmp"\n'
        '    mv -f "$state_tmp" "$state_path"\n'
        "}\n"
    )


def write_waiter(root: str | Path, index: int, timeout_seconds: int) -> Path:
    waiter = _path(root, "waiter", index)
    ready = shlex.quote(str(_path(root, "ready", index)))
    claimed = shlex.quote(str(_path(root, "claimed", index)))
    cancel = shlex.quote(str(_path(root, "cancel")))
    body = (
        "#!/bin/zsh\n"
        f"{_state_writer(_path(root, 'state', index))}"
        "write_state waiting\n"
        'print -r -- "Preparing agent handoff..."\n'
        f"deadline=$((SECONDS + {timeout_seconds}))\n"
        "while (( SECONDS < deadline )); do\n"
        f"    if [[ -e {cancel} ]]; then\n"
        "        write_state cancelled\n"
        '        print -r -- "Agent handoff cancelled."\n'
        "        exit 2\n"
        "    fi\n"
        f"    if [[ -e {ready} ]] && mv -f {ready} {claimed}; then\n"
        "        write_state claimed\n"
        f"        exec {claimed}\n"
        "    fi\n"
        "    sleep 0.2\n"
        "done\n"
        "write_state timed_out\n"
        'print -r -- "Agent handoff timed out."\n'
        "exit 1\n"
    )
    _atomic_write(waiter, body, mode=0o700)
    return waiter


def create_reservation(mode: str, count: int, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS) -> dict:
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}")
    if count < 1:
        raise ValueError("count must be at least 1")
    if timeout_seconds < 1:
        raise ValueError("timeout must be at least 1 second")

    root = tempfile.mkdtemp(prefix="pane-reservation.", dir="/tmp")
    reservation = {
        "version": 1,
        "path": root,
        "mode": mode,
        "count": count,
        "timeout_seconds": timeout_seconds,
    }
    try:
        for index in range(count):
            write_waiter(root, index, timeout_seconds)
        _atomic_write(_path(root, "reservation.json"), json.dumps(reservation) + "\n")
        return reservation
    except BaseException:
        cleanup_reservation(root)
        raise


def read_reservation(root: str | Path) -> dict:
    reservation = json.loads(_path(root, "reservation.json").read_text())
    if (
        reservation.get("version") != 1
        or reservation.get("mode") not in MODES
        or not isinstance(reservation.get("count"), int)
        or reservation["count"] < 1
    ):
        raise ValueError("invalid reservation")
    reservation["path"] = str(root)
    return reservation


def build_applescript(mode: str) -> str:
    if mode == "pane":
        open_action = 'keystroke "d" using command down'
        return_action = "key code 33 using control down"
    elif mode == "tab":
        open_action = 'keystroke "t" using command down'
        return_action = 'keystroke "[" using {command down, shift down}'
    elif mode == "window":
        open_action = 'keystroke "n" using command down'
        return_action = "key code 50 using command down"
    else:
        raise ValueError(f"unknown mode {mode!r}")

    return f'''on run argv
    tell application "Warp" to activate
    tell application "System Events"
        repeat 50 times
            if exists process "Warp" then
                set frontmost of process "Warp" to true
                if frontmost of process "Warp" then exit repeat
            end if
            delay 0.1
        end repeat
        if not frontmost of process "Warp" then error "Warp did not become frontmost."
        repeat with waiterPath in argv
            {open_action}
            delay 1.0
            keystroke ((waiterPath as string) & return)
            delay 0.3
            {return_action}
            delay 0.3
        end repeat
    end tell
end run'''


def read_state(root: str | Path, index: int) -> str:
    try:
        return _path(root, "state", index).read_text().strip()
    except OSError:
        return ""


def open_reserved_sessions(reservation: dict) -> bool:
    root = reservation["path"]
    waiters = [str(_path(root, "waiter", i)) for i in range(reservation["count"])]
    result = subprocess.run(
        ["osascript", "-e", build_applescript(reservation["mode"]), *waiters],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(ACCESSIBILITY_HINT, file=sys.stderr)
        if result.stderr.strip():
            print(result.stderr.strip(), file=sys.stderr)
        return False

    pending = set(range(reservation["count"]))
    deadline = time.monotonic() + START_TIMEOUT_SECONDS
    while pending and time.monotonic() < deadline:
        pending = {index for index in pending if read_state(root, index) != "waiting"}
        if pending:
            time.sleep(0.05)
    return not pending


def _load_tasks(manifest: str) -> list[dict]:
    raw = sys.stdin.read() if manifest == "-" else Path(manifest).read_text()
    tasks = json.loads(raw)
    if not isinstance(tasks, list) or not tasks or not all(isinstance(task, dict) for task in tasks):
        raise ValueError("manifest must be a non-empty JSON array of task objects")
    return tasks


def _normalise_task(task: dict) -> dict:
    agent = task.get("agent") or default_agent()
    if not isinstance(agent, str) or agent.lower() not in ("claude", "codex"):
        raise ValueError("task 'agent' must be 'claude' or 'codex'")
    prompt = task.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("task is missing a non-empty 'prompt'")
    directory = task.get("dir") or os.getcwd()
    if not isinstance(directory, str) or not os.path.isdir(directory):
        raise ValueError(f"task directory does not exist: {directory}")
    model = task.get("model") or "opus"
    if not isinstance(model, str):
        raise ValueError("task 'model' must be a string")
    return {
        "agent": agent.lower(),
        "model": model,
        "dir": os.path.abspath(directory),
        "branch": bool(task.get("branch")),
        "prompt": prompt,
    }


def _prepare_payload(root: str, index: int, task: dict) -> tuple[Path, Path]:
    prompt = _path(root, "prompt", index)
    temporary = _path(root, "payload", index)
    claimed = _path(root, "claimed", index)
    waiter = _path(root, "waiter", index)
    try:
        _atomic_write(prompt, task["prompt"])
        launch = build_launch_line(
            task["agent"], task["model"], task["branch"], str(prompt)
        )
        cleanup = " ".join(shlex.quote(str(path)) for path in (prompt, claimed, waiter))
        body = (
            "#!/bin/zsh\n"
            f"{_state_writer(_path(root, 'state', index))}"
            "finish() {\n"
            "    local status=$?\n"
            '    write_state "completed:$status"\n'
            f"    rm -f {cleanup}\n"
            "    return $status\n"
            "}\n"
            "trap finish EXIT\n"
            "write_state started\n"
            f"cd {shlex.quote(task['dir'])} || exit 1\n"
            f"{launch}\n"
            "exit $?\n"
        )
        _atomic_write(temporary, body, mode=0o700)
        return temporary, prompt
    except BaseException:
        _safe_unlink(temporary)
        _safe_unlink(prompt)
        raise


def fulfil_reservation(root: str, tasks: list[dict]) -> dict:
    reservation = read_reservation(root)
    if len(tasks) != reservation["count"]:
        raise ValueError(
            f"reservation has {reservation['count']} sessions but {len(tasks)} tasks"
        )
    tasks = [_normalise_task(task) for task in tasks]
    if _path(root, "cancel").exists():
        raise ValueError("reservation was cancelled")
    for index in range(reservation["count"]):
        if read_state(root, index) != "waiting":
            raise ValueError(f"reserved session {index + 1} is no longer waiting")

    prepared: list[tuple[Path, Path]] = []
    try:
        for index, task in enumerate(tasks):
            prepared.append(_prepare_payload(root, index, task))
        for index, (temporary, _) in enumerate(prepared):
            os.replace(temporary, _path(root, "ready", index))
    except BaseException:
        for temporary, prompt in prepared:
            if temporary.exists():
                _safe_unlink(temporary)
                _safe_unlink(prompt)
        raise
    return {"reservation": root, "published": len(prepared)}


def cancel_reservation(root: str) -> dict:
    reservation = read_reservation(root)
    states = [read_state(root, index) for index in range(reservation["count"])]
    if any(state in ("claimed", "started") or state.startswith("completed:") for state in states):
        raise ValueError("cannot cancel after fulfilment has started")
    _atomic_write(_path(root, "cancel"), "cancel\n")
    return {"reservation": root, "cancelled": states.count("waiting")}


def cleanup_reservation(root: str | Path) -> None:
    path = Path(root)
    if not path.is_dir():
        return
    for child in path.iterdir():
        if child.is_file() or child.is_symlink():
            _safe_unlink(child)
    try:
        path.rmdir()
    except OSError:
        pass


def reserve_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="spawn_agent_panes.py reserve")
    parser.add_argument("--mode", choices=MODES, required=True)
    parser.add_argument("--count", type=int, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    args = parser.parse_args(argv)
    try:
        reservation = create_reservation(args.mode, args.count, args.timeout_seconds)
    except (OSError, ValueError) as exc:
        print(f"spawn_agent_panes: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"reservation": reservation["path"]}))
    if not open_reserved_sessions(reservation):
        print("spawn_agent_panes: one or more waiters did not start.", file=sys.stderr)
        return 1
    print(f"spawn_agent_panes: reserved {args.count} Warp {args.mode}(s).", file=sys.stderr)
    return 0


def fulfil_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="spawn_agent_panes.py fulfil")
    parser.add_argument("reservation")
    parser.add_argument("manifest")
    args = parser.parse_args(argv)
    try:
        result = fulfil_reservation(args.reservation, _load_tasks(args.manifest))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"spawn_agent_panes: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result))
    return 0


def cancel_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="spawn_agent_panes.py cancel")
    parser.add_argument("reservation")
    args = parser.parse_args(argv)
    try:
        result = cancel_reservation(args.reservation)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"spawn_agent_panes: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result))
    return 0


def legacy_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="spawn_agent_panes.py")
    parser.add_argument("--mode", choices=MODES, required=True)
    parser.add_argument("manifest")
    args = parser.parse_args(argv)
    try:
        tasks = _load_tasks(args.manifest)
        reservation = create_reservation(args.mode, len(tasks))
        print(json.dumps({"reservation": reservation["path"]}))
        if not open_reserved_sessions(reservation):
            return 1
        print(json.dumps(fulfil_reservation(reservation["path"], tasks)))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"spawn_agent_panes: {exc}", file=sys.stderr)
        return 2


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "reserve":
        return reserve_main(arguments[1:])
    if arguments and arguments[0] == "fulfil":
        return fulfil_main(arguments[1:])
    if arguments and arguments[0] == "cancel":
        return cancel_main(arguments[1:])
    return legacy_main(arguments)


def _safe_unlink(path: str | Path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


if __name__ == "__main__":
    sys.exit(main())
