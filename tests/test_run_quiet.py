"""Tests for waiting on a run or an evaluation without streaming it.

`run()` and `evaluate()` used to follow the service's logs to stdout, always.
That is what the CLI wants, and what nothing else does: the dashboard runs them
in a background thread, where the stream reached only the server's console,
and the MCP server's stdout is its protocol, where one log line corrupts the
session. With `follow=False` they wait by polling instead.

The same path lost the Docker SDK connection in the dashboard: only the service
listing reconnected, so the first call after a dropped tunnel killed the
background thread and left the finished service behind. The last classes here
pin the reconnect onto every call a run makes.
"""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import docker
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from graflag.api import GraFlagAPI  # noqa: E402
from graflag.core import GraFlag  # noqa: E402
from graflag.docker_ops import DockerManager  # noqa: E402
from graflag.models import ExperimentInfo  # noqa: E402
from graflag.ssh import SSHManager  # noqa: E402

EXP = "exp__m__d__20260924_120000"


class QuietFixture(unittest.TestCase):
    """A real shared directory, probed through a local sh, and a mock Docker."""

    def setUp(self):
        self.shared = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.shared, ignore_errors=True)
        for d in ("methods/m", "datasets/d", "experiments"):
            (self.shared / d).mkdir(parents=True)
        self.exp_dir = self.shared / "experiments" / EXP

        self.gf = GraFlag.__new__(GraFlag)
        self.gf.config = mock.Mock(remote_shared_dir=str(self.shared))
        self.gf.ssh = SSHManager("10.0.0.1")
        self.gf.ssh.execute = lambda cmd, **kw: subprocess.run(
            ["sh", "-c", cmd], capture_output=True, text=True)
        self.gf.docker = mock.Mock()
        self.gf.docker.is_service_failed.return_value = False
        self.live = set()
        self.gf.docker.service_exists.side_effect = lambda name: name in self.live
        self.gf.docker.get_service_names.side_effect = lambda: set(self.live)
        self.gf.docker.cleanup_finished_service.side_effect = self.live.discard

    def write_status(self, status):
        (self.exp_dir / "status.json").write_text(
            json.dumps({"status": status}, indent=2))


class RunWithoutFollowing(QuietFixture):
    def start_service(self, *args, **kwargs):
        """What the runner does once the service is up."""
        self.live.add(EXP)
        self.write_status("running")
        (self.exp_dir / "method_output.txt").write_text("epoch 1\n")

    def finish_on_first_sleep(self, _seconds):
        self.write_status("completed")
        (self.exp_dir / "results.json").write_text('{"scores": []}')

    def run_quietly(self):
        self.gf.docker.create_service.side_effect = self.start_service
        with mock.patch("graflag.core.time.sleep",
                        side_effect=self.finish_on_first_sleep) as sleep:
            name = self.gf.run("m", "d", exp_name=EXP, follow=False)
        return name, sleep

    def test_nothing_is_streamed(self):
        self.run_quietly()
        self.gf.docker.follow_service_logs.assert_not_called()

    def test_it_waits_for_the_end_of_the_run(self):
        name, sleep = self.run_quietly()
        self.assertEqual(name, EXP)
        self.assertTrue(sleep.called, "returned before the run had ended")
        self.assertEqual(json.loads((self.exp_dir / "status.json").read_text()),
                         {"status": "completed"})

    def test_the_finished_service_is_still_removed(self):
        self.run_quietly()
        self.assertNotIn(EXP, self.live)

    def test_following_is_still_the_default(self):
        """The CLI's behaviour does not change."""
        self.gf.docker.create_service.side_effect = self.start_service

        def follow(name):
            self.finish_on_first_sleep(0)
        self.gf.docker.follow_service_logs.side_effect = follow
        self.gf.run("m", "d", exp_name=EXP)
        self.gf.docker.follow_service_logs.assert_called_once_with(EXP)


class EvaluateWithoutFollowing(QuietFixture):
    def setUp(self):
        super().setUp()
        self.exp_dir.mkdir()
        (self.exp_dir / "results.json").write_text('{"scores": []}')
        self.gf.docker.create_evaluation_service.return_value = f"eval__{EXP}"

        def evaluator_ran(name, **kwargs):
            (self.exp_dir / "eval").mkdir()
            (self.exp_dir / "eval" / "evaluation.json").write_text("{}")
            return "complete"
        self.gf.docker.wait_for_service.side_effect = evaluator_ran

    def test_evaluate_waits_silently(self):
        self.gf.evaluate(EXP, follow=False)
        self.gf.docker.follow_service_logs.assert_not_called()
        self.gf.docker.wait_for_service.assert_called_once_with(f"eval__{EXP}")
        self.gf.docker.remove_evaluation_service.assert_called_once_with(EXP)


class WaitForExperiment(QuietFixture):
    def info(self, status, service=True):
        return ExperimentInfo(name=EXP, method="m", dataset="d", timestamp="",
                              status=status, service_name=EXP if service else None)

    def wait(self, *probes, **kwargs):
        with mock.patch.object(self.gf, "_get_experiment_info",
                               side_effect=list(probes)), \
                mock.patch("graflag.core.time.sleep"):
            return self.gf.wait_for_experiment(EXP, **kwargs)

    def test_returns_at_a_terminal_status(self):
        for status in ("completed", "failed", "stopped"):
            with self.subTest(status=status):
                result = self.wait(self.info("running"), self.info(status))
                self.assertEqual(result.status, status)

    def test_one_failed_probe_does_not_end_the_wait(self):
        """A dropped ssh reads like a missing directory; it must not end a run."""
        result = self.wait(self.info("running"), None, None, self.info("completed"))
        self.assertEqual(result.status, "completed")

    def test_a_directory_that_stays_missing_ends_it(self):
        self.assertIsNone(self.wait(None, None, None))

    def test_an_unknown_run_with_no_service_ends_it(self):
        """Nothing is left that could change it."""
        result = self.wait(self.info("unknown", service=False))
        self.assertEqual(result.status, "unknown")

    def test_timeout_returns_the_current_state(self):
        result = self.wait(self.info("running"), timeout=0)
        self.assertEqual(result.status, "running")


class TheApiWaitsQuietly(unittest.TestCase):
    """The dashboard reaches core through GraFlagAPI; it must not follow."""

    def setUp(self):
        self.api = GraFlagAPI.__new__(GraFlagAPI)
        self.api.core = mock.Mock()

    def test_run(self):
        self.api.run("m", "d", exp_name=EXP)
        self.assertIs(self.api.core.run.call_args.kwargs["follow"], False)

    def test_evaluate(self):
        self.api.evaluate_experiment(EXP)
        self.api.core.evaluate.assert_called_once_with(EXP, follow=False)


class ReconnectOnTheRunPath(unittest.TestCase):
    """Every SDK call a run makes survives one dropped tunnel."""

    def setUp(self):
        self.manager = DockerManager(SSHManager("10.0.0.1"), config=mock.Mock(
            remote_shared_dir="/shared"))
        self.dead = mock.Mock(name="client-with-dead-socket")
        self.dead.services.get.side_effect = requests.exceptions.ConnectionError(
            "Connection aborted: RemoteDisconnected")
        self.fresh = mock.Mock(name="client-after-reconnect")
        self.service = self.fresh.services.get.return_value
        self.service.tasks.return_value = [
            {"Status": {"State": "complete"}, "CreatedAt": "2026-09-24T12:00:00"}]
        self.service.attrs = {}
        self.manager._client = self.dead

        def reconnect():
            self.manager._client = self.fresh
        patcher = mock.patch.object(self.manager, "_connect", side_effect=reconnect)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_service_exists(self):
        self.assertTrue(self.manager.service_exists(EXP))

    def test_is_service_failed(self):
        self.assertFalse(self.manager.is_service_failed(EXP))

    def test_service_task_state(self):
        self.assertEqual(self.manager.service_task_state(EXP), "complete")

    def test_stop_service(self):
        self.manager.stop_service(EXP)
        self.service.remove.assert_called_once()

    def test_cleanup_finished_service(self):
        self.manager.cleanup_finished_service(EXP)
        self.service.remove.assert_called_once()

    def test_service_details_are_still_saved(self):
        self.manager.ssh.execute = mock.Mock()
        self.manager._save_service_details(EXP, "id")
        written = self.manager.ssh.execute.call_args.args[0]
        self.assertIn('"state": "complete"', written)

    def test_a_removal_whose_reply_was_lost_is_not_an_error(self):
        """The first remove went through; the retry finds nothing to remove."""
        self.fresh.services.get.side_effect = docker.errors.NotFound("gone")
        self.assertFalse(self.manager._remove_service_if_exists(EXP))


class WaitForService(unittest.TestCase):
    def setUp(self):
        self.manager = DockerManager(SSHManager("10.0.0.1"), config=None)

    def test_returns_the_terminal_state(self):
        with mock.patch.object(self.manager, "service_task_state",
                               side_effect=[None, "running", "failed"]), \
                mock.patch("graflag.docker_ops.time.sleep"):
            self.assertEqual(self.manager.wait_for_service("s"), "failed")

    def test_timeout(self):
        with mock.patch.object(self.manager, "service_task_state",
                               return_value="running"):
            self.assertIsNone(self.manager.wait_for_service("s", timeout=0))


if __name__ == "__main__":
    unittest.main()
