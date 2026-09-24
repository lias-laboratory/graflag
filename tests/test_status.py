"""Tests for experiment status reporting.

`status.json` is documented as the source of truth for an experiment's state.
These tests run the real remote command through a local shell against a real
fixture directory, so they exercise the shell quoting, the transport encoding
and the state machine together -- which is where the reporting broke.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from graflag.core import GraFlag  # noqa: E402

EXP_NAME = "exp__taddy__uci__20260101_000000"


class StatusFixture(unittest.TestCase):
    """A real experiment directory, probed through the real remote command."""

    def setUp(self):
        self.shared = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.shared, ignore_errors=True)
        self.exp_dir = Path(self.shared) / "experiments" / EXP_NAME
        self.exp_dir.mkdir(parents=True)

        self.gf = GraFlag.__new__(GraFlag)
        self.gf.config = mock.Mock(remote_shared_dir=self.shared)
        self.gf.docker = mock.Mock()
        # A bare Mock returns a truthy object, which would read as "all tasks
        # failed"; the default here is a healthy service.
        self.gf.docker.is_service_failed.return_value = False
        self.gf.ssh = mock.Mock()
        # Run the command the orchestrator builds, locally.
        self.gf.ssh.execute.side_effect = lambda cmd, **kw: subprocess.run(
            ["sh", "-c", cmd], capture_output=True, text=True
        )

    def write_runner_status(self, **payload):
        """Write status.json the way graflag_runner does -- indent=2."""
        (self.exp_dir / "status.json").write_text(json.dumps(payload, indent=2))

    def write_orchestrator_status(self, **payload):
        """Write status.json the way core.py does -- single line."""
        (self.exp_dir / "status.json").write_text(json.dumps(payload))

    def info(self, running=()):
        return self.gf._get_experiment_info(EXP_NAME, running_services=set(running))


class RunnerStatusIsRead(StatusFixture):
    def test_completed_is_reported_while_the_service_still_exists(self):
        """Regression: the runner writes indent=2, and the reply was parsed
        line by line, so only '{' was captured and every runner status was
        silently discarded.

        Swarm services are never auto-removed here, so the fall-through
        reported every finished experiment as 'running' forever.
        """
        self.write_runner_status(status="completed", exit_code=0, method_name="taddy")
        (self.exp_dir / "results.json").write_text('{"scores": []}')

        self.assertEqual(self.info(running=[EXP_NAME]).status, "completed")

    def test_failed_is_reported_even_with_a_stale_results_file(self):
        """The dangerous case: a leftover results.json used to make a failed
        run report 'completed'."""
        self.write_runner_status(status="failed", exit_code=7)
        (self.exp_dir / "results.json").write_text('{"scores": []}')

        self.assertEqual(self.info(running=[]).status, "failed")

    def test_running_with_no_service_is_stopped(self):
        self.write_runner_status(status="running")
        self.assertEqual(self.info(running=[]).status, "stopped")

    def test_orchestrator_written_status_still_parses(self):
        """core.py writes single-line JSON; both formats must work."""
        self.write_orchestrator_status(status="building", timestamp="2026-01-01")
        (self.exp_dir / "build.log").write_text("step 1")
        self.assertEqual(self.info(running=[]).status, "failed")


class StatusContentIsNotInterpreted(StatusFixture):
    """The status file travels through a shell; its content must not matter."""

    def test_payload_with_shell_metacharacters(self):
        self.write_runner_status(
            status="completed",
            error="cost $5 for `id -un` in $HOME and $(hostname)",
        )
        self.assertEqual(self.info(running=[EXP_NAME]).status, "completed")

    def test_payload_with_quotes_and_newlines(self):
        self.write_runner_status(
            status="failed",
            error="Traceback:\n  File \"x.py\", line 1\n    it's broken",
        )
        self.assertEqual(self.info(running=[]).status, "failed")

    def test_non_utf8_bytes_do_not_crash_the_listing(self):
        (self.exp_dir / "status.json").write_bytes(b'{"status": "compl\xffeted"}')
        # Must not raise; falls back to service state.
        self.assertIsNotNone(self.info(running=[EXP_NAME]).status)


class DegradedStatusFiles(StatusFixture):
    def test_absent_status_falls_back_to_service_state(self):
        self.assertEqual(self.info(running=[EXP_NAME]).status, "running")

    def test_absent_status_with_results_is_completed(self):
        (self.exp_dir / "results.json").write_text('{"scores": []}')
        self.assertEqual(self.info(running=[]).status, "completed")

    def test_malformed_status_falls_back_rather_than_crashing(self):
        (self.exp_dir / "status.json").write_text("{not json")
        self.assertEqual(self.info(running=[EXP_NAME]).status, "running")

    def test_missing_experiment_returns_none(self):
        self.assertIsNone(
            self.gf._get_experiment_info("exp__nope__nope__1", running_services=set())
        )


class EvaluateChecksItsOutcome(unittest.TestCase):
    def setUp(self):
        self.gf = GraFlag.__new__(GraFlag)
        self.gf.config = mock.Mock(remote_shared_dir="/shared")
        self.gf.ssh = mock.Mock()
        self.gf.docker = mock.Mock()
        self.gf.docker.create_evaluation_service.return_value = "eval__x"

    def _paths(self, *, results=True, evaluation=True):
        def exists(_shared, path):
            if path.endswith("eval/evaluation.json"):
                return evaluation
            if path.endswith("results.json"):
                return results
            return True
        return exists

    def test_failed_evaluator_raises_instead_of_reporting_success(self):
        """Regression: follow_service_logs returns for a failed task exactly as
        for a successful one, and nothing checked the outcome, so a crashed
        evaluation still logged 'Evaluation results saved to: ...'."""
        from graflag.core import GraFlagError

        self.gf.ssh.path_exists.side_effect = self._paths(evaluation=False)
        self.gf.docker.follow_service_logs.return_value = "failed"

        with self.assertRaises(GraFlagError) as ctx:
            self.gf.evaluate("exp__a__b__1")
        self.assertIn("did not produce", str(ctx.exception))

    def test_missing_output_raises_even_on_a_clean_exit(self):
        from graflag.core import GraFlagError

        self.gf.ssh.path_exists.side_effect = self._paths(evaluation=False)
        self.gf.docker.follow_service_logs.return_value = "complete"

        with self.assertRaises(GraFlagError):
            self.gf.evaluate("exp__a__b__1")

    def test_successful_evaluation_returns_normally(self):
        self.gf.ssh.path_exists.side_effect = self._paths()
        self.gf.docker.follow_service_logs.return_value = "complete"
        self.gf.evaluate("exp__a__b__1")  # must not raise


if __name__ == "__main__":
    unittest.main()
