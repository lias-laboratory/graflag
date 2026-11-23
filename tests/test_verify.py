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


class ABatchMeanAucIsRecomputed(unittest.TestCase):
    """A method whose own AUC is a mean over batches is checked against that.

    GADY reports upstream's metric, the mean of per-batch AUCs over batches of
    200 edges; the evaluator computes one AUC over every published score. The
    two differ by construction (0.9810 against 0.9788 on a real run), so the
    plain cross-check failed a run whose scores were exactly the ones the
    method measured.
    """

    def test_rank_auc_is_sklearns(self):
        import random
        try:
            from sklearn.metrics import roc_auc_score
        except ImportError:
            self.skipTest("sklearn is not installed")
        rng = random.Random(0)
        scores = [round(rng.random(), 1) for _ in range(300)]       # many ties
        labels = [rng.random() < 0.2 for _ in range(300)]
        self.assertAlmostEqual(checks._rank_auc(scores, labels),
                               roc_auc_score(labels, scores), places=12)

    def test_one_class_has_no_auc(self):
        self.assertIsNone(checks._rank_auc([0.1, 0.2], [False, False]))

    def test_batches_are_consecutive_and_one_class_batches_skipped(self):
        scores = [0.1, 0.9, 0.2, 0.8,   0.5, 0.6, 0.3, 0.7]
        truth = [0, 1, 1, 0,             0, 0, 1, 0]
        # Batches of 4: the first ranks 3 of its 4 positive/negative pairs
        # correctly (0.75), the second puts its one positive last (0.0).
        self.assertAlmostEqual(checks._batch_mean_auc(scores, truth, 4), 0.375)
        # Batches of 2: 1.0, 0.0, one class only (skipped), 0.0.
        self.assertAlmostEqual(checks._batch_mean_auc(scores, truth, 2), 1 / 3)

    def test_a_matching_batch_mean_passes(self):
        findings = checks.check(probe(
            reported_aucs={"training_info.best_auc": 0.9810},
            eval_metrics={"auc_roc": 0.9788},
            auc_batch_size=200, batch_mean_auc=0.98101))
        self.assertEqual(levels(findings, "ERROR"), [])
        self.assertTrue(any("mean over batches of 200" in m for m in levels(findings, "OK")))

    def test_a_batch_mean_that_disagrees_still_fails(self):
        findings = checks.check(probe(
            reported_aucs={"training_info.best_auc": 0.9810},
            eval_metrics={"auc_roc": 0.9788},
            auc_batch_size=200, batch_mean_auc=0.9650))
        self.assertTrue(any("not the ones the method measured" in m
                            for m in levels(findings, "ERROR")))

    def test_without_a_declared_batch_size_nothing_changes(self):
        findings = checks.check(probe(
            reported_aucs={"training_info.best_auc": 0.9810},
            eval_metrics={"auc_roc": 0.9788}))
        self.assertTrue(levels(findings, "ERROR"))

    def test_the_remote_probe_computes_it(self):
        import json
        import subprocess
        import tempfile
        scores = [0.1, 0.9, 0.2, 0.8, 0.3, 0.7]
        truth = [0, 1, 1, 0, 1, 0]
        with tempfile.TemporaryDirectory() as tmp:
            exp = pathlib.Path(tmp)
            (exp / "status.json").write_text('{"status": "completed"}')
            (exp / "results.json").write_text(json.dumps({
                "result_type": "EDGE_STREAM_ANOMALY_SCORES",
                "scores": scores, "ground_truth": truth,
                "metadata": {"summary": {"auc_batch_size": 2,
                                         "training_info": {"best_auc": 0.0}}},
            }))
            done = subprocess.run(["sh", "-c", checks.remote_script(str(exp))],
                                  capture_output=True, text=True, timeout=60)
            self.assertEqual(done.returncode, 0, done.stderr)
            out = json.loads(done.stdout.strip().splitlines()[-1])
        self.assertEqual(out["auc_batch_size"], 2)
        self.assertAlmostEqual(out["batch_mean_auc"],
                               checks._batch_mean_auc(scores, truth, 2))


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



class TheProbeIsTheCodeThatWasLoaded(unittest.TestCase):
    """An edit to verify.py on disk must not change what a running client sends.

    A validation campaign that had imported graflag.verify hours before the
    file was edited sent the manager a probe without _probe's definition
    (`NameError: name '_probe' is not defined`, 2026-11-17). `graflag gui` and
    `graflag mcp` are as long-lived. The probe's source is now read at import,
    so a running client sends the code it loaded.
    """

    def test_an_edit_on_disk_after_import_changes_nothing(self):
        import importlib.util
        import shutil
        import tempfile

        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        copy = tmp / "verify_loaded_copy.py"
        shutil.copy(checks.__file__, copy)
        spec = importlib.util.spec_from_file_location("verify_loaded_copy", copy)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        before = module.remote_script("/shared/experiments/exp__m__d__t")

        # what an editor does to the file: lines added above every helper
        copy.write_text("# edited\n" * 7 + copy.read_text())
        after = module.remote_script("/shared/experiments/exp__m__d__t")
        self.assertEqual(after, before)
        compile(after.split("\n", 1)[1].rsplit("GRAFLAG_PROBE_EOF", 1)[0],
                "probe", "exec")


if __name__ == "__main__":
    unittest.main()
