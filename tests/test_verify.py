"""The result verifier: does it fail the runs that should fail?

`graflag verify` is the last of the four integration gates, the one between
"the run finished" and "the number is real". A gate that passes everything is
worse than none, so each check here is paired with input that must trip it --
remove the check and the test fails.

These cases moved here with the checks from the skill's `verify_run.py`. The
one that needs numpy -- the probe's plain-Python counting against the
evaluator's -- stays in graflag-shared's `tests/test_verify_run.py`, beside
`graflag_evaluator`.
"""

import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from graflag import verify as checks  # noqa: E402
from graflag.core import GraFlag, GraFlagError  # noqa: E402
from graflag.models import VerificationReport  # noqa: E402


def probe(**overrides):
    """A probe summary of a run that passes everything, before overrides."""
    base = {
        "status": "completed",
        "results_present": True,
        "result_type": "EDGE_STREAM_ANOMALY_SCORES",
        "n_scores": 100, "n_truth": 100, "kept": 100,
        "dropped_unknown": 0, "dropped_inactive": 0, "dropped_non_finite": 0,
        "n_positive": 10, "distinct_scores": 50,
        "score_min": 0.0, "score_max": 1.0,
        "scored_split": "test", "declared_samples": 100,
        "reported_aucs": {"training_info.test_auc": 0.7806},
        "eval_metrics": {"auc_roc": 0.7806},
    }
    base.update(overrides)
    return base


def levels(findings, level):
    return [m for lvl, m in findings if lvl == level]


class AHealthyRunPasses(unittest.TestCase):
    def test_the_baseline_raises_nothing(self):
        findings = checks.check(probe())
        self.assertEqual(levels(findings, "ERROR"), [])
        self.assertEqual(levels(findings, "WARN"), [])


class TheSampleHasToBeUsable(unittest.TestCase):
    def test_a_run_that_did_not_complete_fails(self):
        findings = checks.check(probe(status="failed", status_error="OOM"))
        self.assertTrue(any("not 'completed'" in m for m in levels(findings, "ERROR")))

    def test_a_missing_results_file_fails(self):
        findings = checks.check({"status": "completed", "results_present": False})
        self.assertTrue(any("nothing was published" in m
                            for m in levels(findings, "ERROR")))

    def test_an_invalid_result_type_fails(self):
        findings = checks.check(probe(result_type="EDGE_SCORES"))
        self.assertTrue(any("not one of the nine" in m
                            for m in levels(findings, "ERROR")))

    def test_scores_and_labels_of_different_length_fail(self):
        """The slade defect: the publish sliced one array and not the other."""
        findings = checks.check(probe(n_scores=3618, n_truth=24186))
        self.assertTrue(any("not the same sample" in m
                            for m in levels(findings, "ERROR")))

    def test_one_class_ground_truth_fails(self):
        """Scoring the train split: the anomalies are all in the test half."""
        findings = checks.check(probe(n_positive=0))
        self.assertTrue(any("one class only" in m
                            for m in levels(findings, "ERROR")))

    def test_a_constant_score_column_fails(self):
        findings = checks.check(probe(distinct_scores=1, score_min=0.5))
        self.assertTrue(any("ranked nothing" in m
                            for m in levels(findings, "ERROR")))

    def test_filtering_away_most_of_the_sample_fails(self):
        findings = checks.check(
            probe(kept=10, dropped_non_finite=90, n_positive=3))
        self.assertTrue(any("dropped before scoring" in m
                            for m in levels(findings, "ERROR")))

    def test_filtering_away_a_little_only_warns(self):
        findings = checks.check(probe(kept=95, dropped_inactive=5, n_positive=10))
        self.assertEqual(levels(findings, "ERROR"), [])
        self.assertTrue(any("dropped before scoring" in m
                            for m in levels(findings, "WARN")))


class ThePublishedScoresAreTheMeasuredOnes(unittest.TestCase):
    """The cross-check: a method's own AUC against the evaluator's.

    It is the only check that looks at *which* scores were written, rather
    than at their shape, and it is cheap because both numbers already exist.
    """

    def test_a_disagreeing_auc_fails(self):
        findings = checks.check(probe(
            reported_aucs={"training_info.test_auc": 0.7806},
            eval_metrics={"auc_roc": 0.6829}))
        self.assertTrue(any("not the ones the method measured" in m
                            for m in levels(findings, "ERROR")))

    def test_any_one_reported_auc_matching_is_enough(self):
        """A method may report several; the published one need only be among them."""
        findings = checks.check(probe(
            reported_aucs={"best_test_auc": 0.7806, "test_auc": 0.6829},
            eval_metrics={"auc_roc": 0.6829}))
        self.assertEqual(levels(findings, "ERROR"), [])

    def test_rounding_to_four_decimals_still_matches(self):
        findings = checks.check(probe(
            reported_aucs={"test_auc": 0.78063285920},
            eval_metrics={"auc_roc": 0.7806}))
        self.assertEqual(levels(findings, "ERROR"), [])

    def test_a_declared_sample_count_that_does_not_match_fails(self):
        findings = checks.check(probe(declared_samples=7000))
        self.assertTrue(any("scored_samples=7000" in m
                            for m in levels(findings, "ERROR")))

    def test_no_evaluation_warns_rather_than_passing_quietly(self):
        findings = checks.check(probe(eval_metrics=None))
        self.assertEqual(levels(findings, "ERROR"), [])
        self.assertTrue(any("graflag evaluate" in m for m in levels(findings, "WARN")))

    def test_a_method_reporting_no_auc_warns(self):
        findings = checks.check(probe(reported_aucs={}))
        self.assertTrue(any("cannot be cross-checked" in m
                            for m in levels(findings, "WARN")))


class TheSplitHasToBeDeclared(unittest.TestCase):
    def test_a_missing_scored_split_warns(self):
        findings = checks.check(probe(scored_split=None))
        self.assertTrue(any("scored_split" in m for m in levels(findings, "WARN")))

    def test_scoring_something_other_than_test_warns(self):
        findings = checks.check(probe(scored_split="all"))
        self.assertTrue(any("fitted nothing" in m for m in levels(findings, "WARN")))


class TheRemoteProbeIsSelfContained(unittest.TestCase):
    """It is sent to a manager with no numpy and no graflag on sys.path."""

    def test_the_script_runs_on_a_bare_interpreter(self):
        import json
        import subprocess
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            exp = pathlib.Path(tmp)
            (exp / "status.json").write_text('{"status": "completed"}')
            (exp / "results.json").write_text(json.dumps({
                "result_type": "EDGE_STREAM_ANOMALY_SCORES",
                "scores": [0.1, 0.9, -1.0],
                "ground_truth": [0, 1, 1],
                "metadata": {"summary": {"training_info": {"test_auc": 1.0},
                                         "dataset_info": {"scored_split": "test",
                                                          "scored_samples": 3}}},
            }))
            (exp / "eval").mkdir()
            (exp / "eval" / "evaluation.json").write_text(
                json.dumps({"metrics": {"auc_roc": 1.0}}))

            script = checks.remote_script(str(exp))
            # `sh -c` the way SSHManager's argument reaches the remote shell.
            done = subprocess.run(["sh", "-c", script], capture_output=True,
                                  text=True, timeout=60)
            self.assertEqual(done.returncode, 0, done.stderr)
            out = json.loads(done.stdout.strip().splitlines()[-1])

        self.assertEqual(out["status"], "completed")
        self.assertEqual(out["n_scores"], 3)
        self.assertEqual(out["kept"], 2)
        self.assertEqual(out["dropped_unknown"], 1)
        self.assertEqual(out["scored_split"], "test")
        self.assertEqual(out["declared_samples"], 3)
        self.assertEqual(out["reported_aucs"], {"training_info.test_auc": 1.0})
        self.assertEqual(out["eval_metrics"]["auc_roc"], 1.0)
        self.assertEqual(checks.check(out), checks.check(out))
        self.assertEqual(levels(checks.check(out), "ERROR"), [])


class GraFlagVerify(unittest.TestCase):
    """GraFlag.verify() over a real experiment directory, probed through sh."""

    def setUp(self):
        self.shared = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.shared, ignore_errors=True)
        self.exp = self.shared / "experiments" / "exp__m__d__1"
        self.exp.mkdir(parents=True)
        (self.exp / "status.json").write_text('{"status": "completed"}')
        self.gf = GraFlag.__new__(GraFlag)
        self.gf.config = mock.Mock(remote_shared_dir=str(self.shared))
        self.gf.ssh = mock.Mock()
        self.gf.ssh.execute.side_effect = lambda cmd, **kw: subprocess.run(
            ["sh", "-c", cmd], capture_output=True, text=True)
        self.gf.ssh.path_exists.side_effect = (
            lambda shared, rel: (pathlib.Path(shared) / rel).exists())

    def publish(self, scores, truth, reported_auc, evaluated_auc):
        (self.exp / "results.json").write_text(json.dumps({
            "result_type": "NODE_ANOMALY_SCORES",
            "scores": scores, "ground_truth": truth,
            "metadata": {"summary": {"test_auc": reported_auc,
                                     "scored_split": "test",
                                     "scored_samples": len(scores)}},
        }))
        (self.exp / "eval").mkdir()
        (self.exp / "eval" / "evaluation.json").write_text(
            json.dumps({"metrics": {"auc_roc": evaluated_auc}}))

    def test_a_consistent_run_passes(self):
        self.publish([0.1, 0.9, 0.2, 0.8], [0, 1, 0, 1], 1.0, 1.0)
        report = self.gf.verify("exp__m__d__1")
        self.assertIsInstance(report, VerificationReport)
        self.assertTrue(report.ok, report.findings)
        self.assertEqual(report.failed, 0)
        self.assertEqual(report.probe["kept"], 4)

    def test_scores_that_disagree_with_the_method_fail(self):
        self.publish([0.1, 0.9, 0.2, 0.8], [0, 1, 0, 1], 0.71, 1.0)
        report = self.gf.verify("exp__m__d__1")
        self.assertEqual(report.failed, 1)
        self.assertIn("not the ones the method measured",
                      next(f["message"] for f in report.findings
                           if f["level"] == "ERROR"))

    def test_the_scores_never_reach_the_report(self):
        """An experiment can hold millions of scores; the report holds counts."""
        self.publish([0.1, 0.9, 0.2, 0.8], [0, 1, 0, 1], 1.0, 1.0)
        report = self.gf.verify("exp__m__d__1")
        self.assertNotIn("scores", report.probe)
        self.assertNotIn("ground_truth", report.probe)

    def test_a_missing_experiment_is_an_error(self):
        with self.assertRaises(GraFlagError):
            self.gf.verify("exp__nope__d__1")


class TheCommandLine(unittest.TestCase):
    def report(self, failed):
        return VerificationReport(
            experiment_name="exp__m__d__1",
            findings=[{"level": "ERROR" if failed else "OK", "message": "x"}],
            failed=int(failed), passed=int(not failed))

    def run_cli(self, report):
        from graflag import cli
        with mock.patch.object(sys, "argv", ["graflag", "verify", "-e", "exp__m__d__1"]), \
                mock.patch("graflag.cli.GraFlag") as gf, \
                mock.patch("builtins.print"):
            gf.return_value.verify.return_value = report
            try:
                cli.main()
            except SystemExit as exit_:
                return exit_.code
        return 0

    def test_a_failed_check_exits_1(self):
        self.assertEqual(self.run_cli(self.report(failed=True)), 1)

    def test_a_passing_run_exits_0(self):
        self.assertEqual(self.run_cli(self.report(failed=False)), 0)

    def test_the_old_script_interface_is_kept(self):
        """The skill's verify_run.py is a shim over main(); same exit codes."""
        with mock.patch("graflag.core.GraFlag") as gf, mock.patch("builtins.print"):
            gf.return_value.verify.return_value = self.report(failed=True)
            self.assertEqual(checks.main(["exp__m__d__1"]), 1)
            gf.return_value.verify.return_value = self.report(failed=False)
            self.assertEqual(checks.main(["exp__m__d__1", "--json"]), 0)


if __name__ == "__main__":
    unittest.main()
