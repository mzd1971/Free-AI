"""Task completion and recovery with a scripted model, real files and Python subprocesses."""

import json
from pathlib import Path
import sys
import tempfile
import unittest

from project_agent import ProjectAgent
from test_project_agent import ScriptedModel


DEVELOPMENT = Path(__file__).resolve().parents[1]
TEST_COMMAND = {"argv": [sys.executable, "-B", "-m", "unittest", "test_math"], "purpose": "check"}
VERSION_COMMAND = {"argv": [sys.executable, "-V"], "purpose": "check"}


def artifacts(run_dir):
    return {path.name: path.read_bytes() for path in Path(run_dir).iterdir()}


class TaskCompletionRegressions(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=DEVELOPMENT)
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.workspace = root / "project"
        self.workspace.mkdir()
        self.runs = root / "runs"
        self.events = []

    def agent(self, *turns, approve=lambda request: True, **options):
        model = ScriptedModel(*turns)
        if options.get("permission_mode") == "readonly":
            model.expected_tools -= {"write_file", "replace_text", "run_command"}
        return ProjectAgent(self.workspace, model, runs_root=self.runs, approve_command=approve,
                            on_event=self.events.append, **options)

    def test_denied_check_remains_open_until_the_requested_check_runs(self):
        args = [("run_command", VERSION_COMMAND)]
        first = self.agent(args, "Done.", "Done.", approve=lambda request: False).run(
            "Run the Python version check.")
        self.assertEqual(first["status"], "unfinished")
        self.assertTrue(first["continuation_available"])
        self.assertNotEqual(first["task_state"]["verification"]["status"], "passed")

        next_run = self.agent(args, "Verified.").run("Continue.", resume_from=first["run_dir"])
        self.assertEqual(next_run["status"], "completed")
        self.assertEqual(next_run["task_state"]["verification"]["status"], "passed")

    def test_check_that_cannot_start_stays_open(self):
        missing = {"argv": ["missing-test-binary-c25f3b6d.exe"], "purpose": "check"}
        result = self.agent([("run_command", missing)], "Done.", "Done.").run(
            "Run the project's acceptance test.")
        self.assertEqual(result["status"], "unfinished")
        self.assertTrue(result["continuation_available"])

    def test_explicit_unfinished_plan_blocks_completion(self):
        for status in ("pending", "in_progress"):
            with self.subTest(status=status):
                plan = {"steps": [{"text": "Inspect module", "status": status}]}
                result = self.agent([("update_plan", plan)], "Done.", "Done.").run("Inspect module.")
                self.assertEqual(result["status"], "unfinished")
                self.assertTrue(result["continuation_available"])

    def test_readonly_question_can_complete_without_checks(self):
        result = self.agent("A module is a unit of Python code.", permission_mode="readonly").run(
            "What is a Python module?")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["task_state"]["verification"]["status"], "not_run")

    def test_unrelated_check_cannot_discharge_interrupted_check_after_two_resumes(self):
        expected_command = {"argv": [sys.executable, "-B", "-c",
            "from pathlib import Path; assert Path('early.txt').read_text() == 'ready'"],
            "purpose": "check"}

        def interrupt(request):
            raise KeyboardInterrupt("simulate process exit before receiving a command result")

        interrupted = self.agent([("write_file", {"path": "early.txt", "content": "ready"})],
                                 [("run_command", expected_command)], approve=interrupt)
        with self.assertRaises(KeyboardInterrupt):
            interrupted.run("Write a file and confirm its contents.")
        original = interrupted.run_dir
        original_files = artifacts(original)
        middle = self.agent("Done.", "Done.").run("Continue.", resume_from=original)
        self.assertEqual(middle["status"], "unfinished")
        self.assertTrue(middle["task_state"]["uncertain"])
        middle_files = artifacts(middle["run_dir"])

        unrelated = self.agent([("run_command", VERSION_COMMAND)], "Done.", "Done.").run(
            "Continue.", resume_from=middle["run_dir"])
        self.assertTrue(unrelated["task_state"]["uncertain"])
        self.assertEqual(unrelated["status"], "unfinished")

        matched = self.agent([("run_command", expected_command)], "Verified.").run(
            "Continue.", resume_from=middle["run_dir"])
        self.assertEqual(matched["status"], "completed")
        self.assertFalse(matched["task_state"]["uncertain"])
        self.assertEqual(artifacts(original), original_files)
        self.assertEqual(artifacts(middle["run_dir"]), middle_files)

    def test_real_test_repair_and_repeated_handoff_preserve_evidence(self):
        source = self.workspace / "math_impl.py"
        source.write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
        (self.workspace / "test_math.py").write_text(
            "import unittest\nfrom math_impl import add\n\n"
            "class TestMath(unittest.TestCase):\n"
            "    def test_add(self):\n        self.assertEqual(add(1, 2), 3)\n", encoding="utf-8")
        check = [("run_command", TEST_COMMAND)]
        repair = {"path": source.name, "old": "return a - b", "new": "return a + b"}
        first = self.agent(check, [("read_file", {"path": source.name})],
                           [("replace_text", repair)], check, max_steps=4).run(
            "Make the addition test pass.")
        self.assertEqual(first["status"], "limit")
        commands = [e["result"] for e in self.events
                    if e["type"] == "tool_result" and e["tool"] == "run_command"]
        self.assertEqual([r["returncode"] for r in commands], [1, 0])
        self.assertIn("AssertionError", commands[0]["stderr"])
        self.assertIn("return a + b", source.read_text(encoding="utf-8"))
        first_files = artifacts(first["run_dir"])
        first_state = first["task_state"]
        self.assertEqual(first_state["verification"]["status"], "passed")
        self.assertEqual([e["path"] for e in first_state["edits"]], [source.name])
        self.assertTrue(first_state["observations"])

        middle = self.agent(check, max_steps=1).run("Continue.", resume_from=first["run_dir"])
        self.assertEqual(middle["status"], "limit")
        middle_files = artifacts(middle["run_dir"])
        last_agent = self.agent(check, "Verified.")
        last = last_agent.run("Continue.", resume_from=middle["run_dir"])
        self.assertEqual(last["status"], "completed")
        self.assertEqual(last["task_state"]["verification"]["status"], "passed")
        self.assertEqual(last["task_state"]["edits"], first_state["edits"])
        for observation in first_state["observations"]:
            self.assertIn(observation, last["task_state"]["observations"])
        self.assertIn(source.name, last_agent.model.calls[0][-1]["content"])
        self.assertEqual(last["changes"], [])
        self.assertEqual(artifacts(first["run_dir"]), first_files)
        self.assertEqual(artifacts(middle["run_dir"]), middle_files)
        history = json.loads(first_files["history.json"])
        self.assertTrue(any(message["role"] == "tool" for message in history))
