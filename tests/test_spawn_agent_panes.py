#!/usr/bin/env python3

import importlib.util
import json
import os
import pathlib
import subprocess
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch


SCRIPT = pathlib.Path(__file__).parents[1] / "scripts" / "spawn_agent_panes.py"
SPEC = importlib.util.spec_from_file_location("spawn_agent_panes", SCRIPT)
spawn = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(spawn)


class SpawnAgentPanesTests(unittest.TestCase):
    def test_each_mode_opens_destination_then_returns_to_origin(self):
        expected = {
            "pane": ('keystroke "d" using command down', "key code 33 using control down"),
            "tab": (
                'keystroke "t" using command down',
                'keystroke "[" using {command down, shift down}',
            ),
            "window": ('keystroke "n" using command down', "key code 50 using command down"),
        }
        for mode, (open_action, return_action) in expected.items():
            script = spawn.build_applescript(mode)
            self.assertLess(script.index(open_action), script.index(return_action))
            self.assertIn('tell application "Warp" to activate', script)
            self.assertNotIn('set frontmost of process "Warp" to true', script)

    def test_applescript_guards_focus_and_modifiers_before_typing(self):
        script = spawn.build_applescript("pane")
        open_destination = script.index('keystroke "d" using command down')
        type_waiter = script.index("keystroke ((waiterPath as string) & return)")
        guard_command = spawn._applescript_string(spawn.modifier_guard_command())

        self.assertEqual(script.count(f"do shell script {guard_command}"), 3)
        self.assertLess(script.index(f"do shell script {guard_command}"), open_destination)
        self.assertLess(
            script.index(f"do shell script {guard_command}", open_destination),
            type_waiter,
        )
        self.assertLess(
            script.index('if not frontmost of process "Warp"', open_destination),
            type_waiter,
        )
        self.assertIn("Warp lost focus before waiter command entry", script)
        return_to_origin = script.index("key code 33 using control down")
        self.assertLess(
            script.index(f"do shell script {guard_command}", type_waiter),
            return_to_origin,
        )

    def test_modifier_gate_requires_a_stable_release_interval(self):
        flags = iter(
            [
                spawn.MODIFIER_FLAGS_MASK,
                0,
                0,
                spawn.MODIFIER_FLAGS_MASK,
                0,
                0,
                0,
                0,
            ]
        )
        now = [0.0]

        def clock():
            return now[0]

        def sleep(seconds):
            now[0] += seconds

        self.assertTrue(
            spawn.wait_for_modifiers_released(
                timeout_seconds=1.0,
                stable_seconds=0.2,
                poll_interval_seconds=0.1,
                flags_reader=lambda: next(flags),
                clock=clock,
                sleeper=sleep,
            )
        )

    def test_modifier_gate_times_out_while_a_modifier_is_held(self):
        now = [0.0]

        def clock():
            return now[0]

        def sleep(seconds):
            now[0] += seconds

        self.assertFalse(
            spawn.wait_for_modifiers_released(
                timeout_seconds=0.3,
                stable_seconds=0.1,
                poll_interval_seconds=0.1,
                flags_reader=lambda: spawn.MODIFIER_FLAGS_MASK,
                clock=clock,
                sleeper=sleep,
            )
        )

    def test_reservation_creates_waiters_and_metadata(self):
        reservation = spawn.create_reservation("pane", 2, timeout_seconds=30)
        self.addCleanup(spawn.cleanup_reservation, reservation["path"])
        self.assertEqual(reservation["count"], 2)
        for index in range(2):
            waiter = spawn._path(reservation["path"], "waiter", index)
            self.assertTrue(waiter.is_file())
            self.assertTrue(os.access(waiter, os.X_OK))
            self.assertIn("Preparing agent handoff", waiter.read_text())

    def test_open_requires_every_waiter_to_report_waiting(self):
        reservation = spawn.create_reservation("tab", 2, timeout_seconds=30)
        self.addCleanup(spawn.cleanup_reservation, reservation["path"])
        for index in range(2):
            spawn._path(reservation["path"], "state", index).write_text("waiting\n")
        with patch.object(
            spawn.subprocess, "run", return_value=MagicMock(returncode=0, stderr="")
        ):
            self.assertTrue(spawn.open_reserved_sessions(reservation))

    def test_open_failure_cancels_any_waiters_that_started(self):
        reservation = spawn.create_reservation("pane", 2, timeout_seconds=30)
        self.addCleanup(spawn.cleanup_reservation, reservation["path"])
        with patch.object(
            spawn.subprocess,
            "run",
            return_value=MagicMock(returncode=1, stderr="focus changed"),
        ):
            self.assertFalse(spawn.open_reserved_sessions(reservation))
        self.assertEqual(spawn._path(reservation["path"], "cancel").read_text(), "cancel\n")

    def test_fulfil_publishes_executable_payload(self):
        reservation = spawn.create_reservation("pane", 1, timeout_seconds=30)
        self.addCleanup(spawn.cleanup_reservation, reservation["path"])
        root = reservation["path"]
        spawn._path(root, "state", 0).write_text("waiting\n")
        result = spawn.fulfil_reservation(
            root,
            [{"agent": "codex", "dir": root, "prompt": "Do the task."}],
        )
        ready = spawn._path(root, "ready", 0)
        self.assertEqual(result["published"], 1)
        self.assertTrue(ready.is_file())
        self.assertTrue(os.access(ready, os.X_OK))
        self.assertIn('codex "$(cat ', ready.read_text())

    def test_cancel_signals_waiter(self):
        reservation = spawn.create_reservation("pane", 1, timeout_seconds=30)
        self.addCleanup(spawn.cleanup_reservation, reservation["path"])
        root = reservation["path"]
        spawn._path(root, "state", 0).write_text("waiting\n")
        result = spawn.cancel_reservation(root)
        self.assertEqual(result["cancelled"], 1)
        self.assertTrue(spawn._path(root, "cancel").is_file())

    def test_waiter_claims_and_executes_published_payload(self):
        with tempfile.TemporaryDirectory() as root:
            waiter = spawn.write_waiter(root, 0, timeout_seconds=5)
            process = subprocess.Popen(
                [waiter], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
            )
            self.addCleanup(lambda: process.poll() is None and process.terminate())
            deadline = time.time() + 2
            while time.time() < deadline and spawn.read_state(root, 0) != "waiting":
                time.sleep(0.02)
            marker = pathlib.Path(root) / "ran"
            temporary = pathlib.Path(root) / "temporary"
            temporary.write_text(f"#!/bin/zsh\ntouch {marker}\n")
            temporary.chmod(0o700)
            os.replace(temporary, spawn._path(root, "ready", 0))
            _, stderr = process.communicate(timeout=3)
            self.assertEqual(process.returncode, 0, stderr)
            self.assertTrue(marker.is_file())
            self.assertEqual(spawn.read_state(root, 0), "claimed")


if __name__ == "__main__":
    unittest.main()
