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
            self.assertIn('set frontmost of process "Warp" to true', script)

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
