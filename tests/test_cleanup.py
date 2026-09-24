"""Tests for finished-service cleanup.

Removing a Swarm service is not reversible and it is what `graflag logs` reads
from until the run's output has been saved to disk, so most of these tests are
about what cleanup must refuse to touch.
"""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from graflag.core import GraFlag, GraFlagError  # noqa: E402


class CleanupFixture(unittest.TestCase):
    """A shared dir of real experiment directories, probed for real."""

    def setUp(self):
        self.shared = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.shared, ignore_errors=True)
        self.root = Path(self.shared) / "experiments"
        self.root.mkdir()

        self.gf = GraFlag.__new__(GraFlag)
        self.gf.config = mock.Mock(remote_shared_dir=self.shared)
        self.gf.ssh = mock.Mock()
        self.gf.ssh.execute.side_effect = lambda cmd, **kw: subprocess.run(
            ["sh", "-c", cmd], capture_output=True, text=True
        )
        self.gf.docker = mock.Mock()
        self.gf.docker.is_service_failed.return_value = False
        self.removed = []
        self.gf.docker.cleanup_finished_service.side_effect = self.removed.append

    def make_experiment(self, name, status=None, output=True, results=True):
        d = self.root / name
        d.mkdir()
        if status:
            (d / "status.json").write_text(json.dumps({"status": status}, indent=2))
        if results:
            (d / "results.json").write_text('{"scores": []}')
        if output:
            (d / "method_output.txt").write_text("epoch 1\nepoch 2\n")
        return d

    def set_live_services(self, *names):
        self.gf.docker.get_service_names.return_value = set(names)

    def outcome_for(self, results, name):
        return next(r for r in results if r.experiment == name)


class RemovesFinishedServices(CleanupFixture):
    def test_completed_experiment_service_is_removed(self):
        self.make_experiment("exp__a__b__1", status="completed")
        self.set_live_services("exp__a__b__1")

        results = self.gf.cleanup_services()

        self.assertEqual(self.removed, ["exp__a__b__1"])
        self.assertTrue(self.outcome_for(results, "exp__a__b__1").removed)

    def test_failed_experiment_service_is_removed(self):
        self.make_experiment("exp__a__b__1", status="failed")
        self.set_live_services("exp__a__b__1")

        self.gf.cleanup_services()
        self.assertEqual(self.removed, ["exp__a__b__1"])

    def test_sweeps_every_finished_experiment(self):
        for i in range(3):
            self.make_experiment(f"exp__a__b__{i}", status="completed")
        self.set_live_services("exp__a__b__0", "exp__a__b__1", "exp__a__b__2")

        self.gf.cleanup_services()
        self.assertEqual(sorted(self.removed),
                         ["exp__a__b__0", "exp__a__b__1", "exp__a__b__2"])

    def test_experiments_without_a_service_are_not_reported(self):
        self.make_experiment("exp__a__b__1", status="completed")
        self.set_live_services()  # service already gone

        self.assertEqual(self.gf.cleanup_services(), [])
        self.assertEqual(self.removed, [])


class RefusesToRemove(CleanupFixture):
    """The cases where removing the service would lose something."""

    def test_running_experiment_is_left_alone(self):
        self.make_experiment("exp__a__b__1", status="running")
        self.set_live_services("exp__a__b__1")

        results = self.gf.cleanup_services()

        self.assertEqual(self.removed, [])
        self.assertIn("still running", self.outcome_for(results, "exp__a__b__1").reason)

    def test_building_experiment_is_left_alone(self):
        self.make_experiment("exp__a__b__1", status="building", results=False)
        self.set_live_services("exp__a__b__1")

        self.gf.cleanup_services()
        self.assertEqual(self.removed, [])

    def test_service_is_kept_when_output_was_never_saved(self):
        """A run that died before writing method_output.txt has its Docker
        logs as the only record of why. Removing the service would destroy
        exactly the evidence needed for the failure worth investigating."""
        self.make_experiment("exp__a__b__1", status="failed", output=False)
        self.set_live_services("exp__a__b__1")

        results = self.gf.cleanup_services()

        self.assertEqual(self.removed, [])
        self.assertIn("only record", self.outcome_for(results, "exp__a__b__1").reason)

    def test_a_recorded_error_is_enough_to_release_the_service(self):
        """A run that died during startup has no method_output.txt, but the
        runner records the reason in status.json. Without this the service
        would be kept forever."""
        d = self.make_experiment("exp__a__b__1", status=None, output=False, results=False)
        (d / "status.json").write_text(json.dumps(
            {"status": "failed", "stage": "startup", "error": "bad MONITOR_INTERVAL"},
            indent=2,
        ))
        self.set_live_services("exp__a__b__1")

        self.gf.cleanup_services()
        self.assertEqual(self.removed, ["exp__a__b__1"])

    def test_a_status_without_an_error_is_not_enough(self):
        d = self.make_experiment("exp__a__b__1", status=None, output=False, results=False)
        (d / "status.json").write_text(json.dumps({"status": "failed"}, indent=2))
        self.set_live_services("exp__a__b__1")

        self.gf.cleanup_services()
        self.assertEqual(self.removed, [])

    def test_empty_output_file_counts_as_no_output(self):
        d = self.make_experiment("exp__a__b__1", status="failed", output=False)
        (d / "method_output.txt").write_text("")
        self.set_live_services("exp__a__b__1")

        self.gf.cleanup_services()
        self.assertEqual(self.removed, [])

    def test_unknown_status_is_left_alone(self):
        self.make_experiment("exp__a__b__1", status=None, results=False)
        self.set_live_services("exp__a__b__1")

        self.gf.cleanup_services()
        self.assertEqual(self.removed, [])

    def test_mixed_sweep_removes_only_the_safe_ones(self):
        self.make_experiment("exp__done__b__1", status="completed")
        self.make_experiment("exp__live__b__2", status="running")
        self.make_experiment("exp__nolog__b__3", status="failed", output=False)
        self.set_live_services("exp__done__b__1", "exp__live__b__2", "exp__nolog__b__3")

        self.gf.cleanup_services()
        self.assertEqual(self.removed, ["exp__done__b__1"])


class DryRun(CleanupFixture):
    def test_dry_run_removes_nothing(self):
        self.make_experiment("exp__a__b__1", status="completed")
        self.set_live_services("exp__a__b__1")

        results = self.gf.cleanup_services(dry_run=True)

        self.assertEqual(self.removed, [])
        self.assertFalse(self.outcome_for(results, "exp__a__b__1").removed)
        self.assertIn("would remove",
                      self.outcome_for(results, "exp__a__b__1").reason)

    def test_dry_run_still_reports_what_it_would_skip(self):
        self.make_experiment("exp__a__b__1", status="running")
        self.set_live_services("exp__a__b__1")

        results = self.gf.cleanup_services(dry_run=True)
        self.assertIn("still running", self.outcome_for(results, "exp__a__b__1").reason)


class SingleExperiment(CleanupFixture):
    def test_limits_to_the_named_experiment(self):
        self.make_experiment("exp__a__b__1", status="completed")
        self.make_experiment("exp__a__b__2", status="completed")
        self.set_live_services("exp__a__b__1", "exp__a__b__2")

        self.gf.cleanup_services(experiment="exp__a__b__1")
        self.assertEqual(self.removed, ["exp__a__b__1"])

    def test_unknown_experiment_raises(self):
        self.set_live_services()
        with self.assertRaises(GraFlagError):
            self.gf.cleanup_services(experiment="exp__nope__x__1")


class RunSignalsFailure(unittest.TestCase):
    """`graflag run` must exit non-zero when the experiment failed."""

    def _run(self, final_status):
        gf = GraFlag.__new__(GraFlag)
        gf.config = mock.Mock(remote_shared_dir="/shared")
        gf.ssh = mock.Mock()
        gf.ssh.path_exists.return_value = True
        gf.docker = mock.Mock()
        info = mock.Mock(status=final_status)
        with mock.patch.object(gf, "_ensure_dataset"), \
             mock.patch.object(gf, "cleanup_services", return_value=[]), \
             mock.patch.object(gf, "_get_experiment_info", return_value=info):
            return gf.run("m", "d")

    def test_failed_experiment_raises(self):
        """Regression: run() returned normally for a crashed experiment, so
        the CLI exited 0 and nothing scripting GraFlag could tell a successful
        benchmark from a failed one."""
        with self.assertRaises(GraFlagError) as ctx:
            self._run("failed")
        self.assertIn("failed", str(ctx.exception))

    def test_completed_experiment_returns_the_name(self):
        self.assertTrue(self._run("completed").startswith("exp__m__d__"))

    def test_unknown_status_is_not_treated_as_failure(self):
        self.assertTrue(self._run("unknown").startswith("exp__m__d__"))


class RunCleansUpAfterItself(unittest.TestCase):
    def _run(self, **kwargs):
        gf = GraFlag.__new__(GraFlag)
        gf.config = mock.Mock(remote_shared_dir="/shared")
        gf.ssh = mock.Mock()
        gf.ssh.path_exists.return_value = True
        gf.docker = mock.Mock()
        with mock.patch.object(gf, "_ensure_dataset"), \
             mock.patch.object(gf, "cleanup_services") as cleanup:
            cleanup.return_value = []
            gf.run("m", "d", **kwargs)
        return cleanup

    def test_run_removes_the_service_by_default(self):
        cleanup = self._run()
        cleanup.assert_called_once()
        self.assertIn("experiment", cleanup.call_args.kwargs)

    def test_keep_service_opts_out(self):
        cleanup = self._run(keep_service=True)
        cleanup.assert_not_called()

    def test_cleanup_failure_does_not_fail_a_successful_run(self):
        """Housekeeping runs after the method has already finished. A problem
        listing or removing the service must not turn a completed run into a
        command that exits non-zero."""
        gf = GraFlag.__new__(GraFlag)
        gf.config = mock.Mock(remote_shared_dir="/shared")
        gf.ssh = mock.Mock()
        gf.ssh.path_exists.return_value = True
        gf.docker = mock.Mock()

        with mock.patch.object(gf, "_ensure_dataset"), \
             mock.patch.object(gf, "cleanup_services",
                               side_effect=GraFlagError("boom")):
            name = gf.run("m", "d")          # must not raise

        self.assertTrue(name.startswith("exp__m__d__"))


if __name__ == "__main__":
    unittest.main()
