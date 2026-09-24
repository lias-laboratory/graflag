"""Check that a finished experiment published a result worth believing.

`status.json` saying `completed` means the process exited 0 and wrote a
`results.json` that parses. It does not mean the numbers are the method's, that
they cover the test split, or that the scores are the ones whose AUC the method
printed. Every scientific-validity defect recorded in graflag-shared passed
`completed`.

The checks here are the generic half of that -- the part that does not need to
know what the method computes:

  * the sample is usable at all: scores and ground truth the same length, both
    classes present, something left after the evaluator's filtering
  * the scores vary (a constant column scores AUC 0.5 and looks like a method)
  * what the method says it measured is what it published: every `*auc*` in
    `metadata.summary` is compared against `eval/evaluation.json`, and
    `scored_samples` against the actual count
  * the split is declared, and declared to be the test one

What it cannot check is whether the method's own number is right -- only that
the published scores reproduce it. A method that measures the wrong thing
consistently passes every check below. Read the run's log as well.

This is the last of the method-integration skill's four gates, after
`graflag evaluate`. It began as the skill's `scripts/verify_run.py`, which is
now a shim over :func:`main`; `graflag verify -e EXP` and the MCP server's
`verify_experiment` tool reach the same checks through
:meth:`graflag.core.GraFlag.verify`.
"""

import argparse
import inspect
import json
import sys

# Mirrors graflag_evaluator.preprocessing.SENTINELS. The evaluator drops these
# before computing anything, so a count taken without dropping them would not
# be the count the AUC was computed over.
SENTINEL_UNKNOWN = -1.0
SENTINEL_INACTIVE = -2.0

AUC_TOLERANCE = 1e-4  # evaluation.json rounds to 4 decimals

VALID_RESULT_TYPES = {
    "NODE_ANOMALY_SCORES", "EDGE_ANOMALY_SCORES", "GRAPH_ANOMALY_SCORES",
    "TEMPORAL_NODE_ANOMALY_SCORES", "TEMPORAL_EDGE_ANOMALY_SCORES",
    "TEMPORAL_GRAPH_ANOMALY_SCORES",
    "NODE_STREAM_ANOMALY_SCORES", "EDGE_STREAM_ANOMALY_SCORES",
    "GRAPH_STREAM_ANOMALY_SCORES",
}


# --- functions shipped to the manager -------------------------------------
# These are sent to the manager verbatim by inspect.getsource(), because the
# manager has no numpy and cannot import graflag_evaluator.preprocessing. They
# are plain Python for that reason, and graflag-shared's tests/test_verify_run.py
# cross-checks them against the real prepare_pairs() to keep the two from
# drifting.

def _flatten(arr):
    """Flatten one level of nesting, as flatten_ragged does for a list of rows."""
    out = []
    for item in arr:
        if isinstance(item, (list, tuple)):
            out.extend(_flatten(item))
        else:
            out.append(item)
    return out


def _is_finite(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) \
        and x == x and x not in (float("inf"), float("-inf"))


def _summarise_pairs(scores, truth):
    """Count what the evaluator would have kept, and describe it."""
    scores = _flatten(scores)
    truth = _flatten(truth)
    report = {
        "n_scores": len(scores),
        "n_truth": len(truth),
        "kept": 0,
        "dropped_unknown": 0,
        "dropped_inactive": 0,
        "dropped_non_finite": 0,
        "n_positive": 0,
        "distinct_scores": 0,
        "score_min": None,
        "score_max": None,
    }
    if len(scores) != len(truth):
        return report

    seen = set()
    lo = hi = None
    for s, t in zip(scores, truth):
        if not _is_finite(s):
            report["dropped_non_finite"] += 1
            continue
        if s == SENTINEL_UNKNOWN:
            report["dropped_unknown"] += 1
            continue
        if s == SENTINEL_INACTIVE:
            report["dropped_inactive"] += 1
            continue
        report["kept"] += 1
        if t:
            report["n_positive"] += 1
        if len(seen) < 64:        # enough to tell "constant" from "varies"
            seen.add(s)
        lo = s if lo is None or s < lo else lo
        hi = s if hi is None or s > hi else hi
    report["distinct_scores"] = len(seen)
    report["score_min"] = lo
    report["score_max"] = hi
    return report


def _collect_aucs(node, prefix=""):
    """Every `*auc*` number anywhere in the method's summary, by path."""
    found = {}
    if isinstance(node, dict):
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(value, (dict, list)):
                found.update(_collect_aucs(value, path))
            elif "auc" in str(key).lower() and isinstance(value, (int, float)) \
                    and not isinstance(value, bool):
                found[path] = float(value)
    elif isinstance(node, list):
        for i, value in enumerate(node):
            found.update(_collect_aucs(value, f"{prefix}[{i}]"))
    return found


def _probe(exp_dir):
    """Read one experiment on the manager and print a compact JSON summary."""
    import json as _json
    import os as _os

    out = {"exp_dir": exp_dir}

    def _load(rel):
        path = _os.path.join(exp_dir, rel)
        if not _os.path.exists(path):
            return None
        try:
            with open(path) as handle:
                return _json.load(handle)
        except Exception as exc:                       # noqa: BLE001
            return {"__unreadable__": str(exc)}

    status = _load("status.json")
    out["status"] = (status or {}).get("status")
    out["status_error"] = (status or {}).get("error")

    results = _load("results.json")
    if results is None:
        out["results_present"] = False
        print(_json.dumps(out))
        return
    out["results_present"] = True
    if "__unreadable__" in results:
        out["results_unreadable"] = results["__unreadable__"]
        print(_json.dumps(out))
        return

    out["result_type"] = results.get("result_type")
    metadata = results.get("metadata") or {}
    summary = metadata.get("summary") or {}
    out["method_name"] = metadata.get("method_name")
    out["reported_aucs"] = _collect_aucs(summary)
    out["scored_split"] = _find_key(summary, "scored_split")
    out["declared_samples"] = _find_key(summary, "scored_samples")
    out.update(_summarise_pairs(results.get("scores") or [],
                                results.get("ground_truth") or []))

    evaluation = _load("eval/evaluation.json")
    if evaluation and "__unreadable__" not in evaluation:
        metrics = evaluation.get("metrics") or evaluation
        out["eval_metrics"] = {k: v for k, v in metrics.items()
                               if isinstance(v, (int, float))}
        out["eval_filtering"] = evaluation.get("filtering")
    print(_json.dumps(out))


def _find_key(node, wanted):
    """First value stored under `wanted`, at any depth."""
    if isinstance(node, dict):
        if wanted in node:
            return node[wanted]
        for value in node.values():
            found = _find_key(value, wanted)
            if found is not None:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _find_key(value, wanted)
            if found is not None:
                return found
    return None


REMOTE_HELPERS = (_flatten, _is_finite, _summarise_pairs, _collect_aucs,
                  _find_key, _probe)


def remote_script(exp_dir):
    """The probe, as a self-contained program to run on the manager."""
    body = "\n\n".join(inspect.getsource(fn) for fn in REMOTE_HELPERS)
    return (
        "python3 - <<'GRAFLAG_PROBE_EOF'\n"
        f"SENTINEL_UNKNOWN = {SENTINEL_UNKNOWN!r}\n"
        f"SENTINEL_INACTIVE = {SENTINEL_INACTIVE!r}\n\n"
        f"{body}\n\n"
        f"_probe({exp_dir!r})\n"
        "GRAFLAG_PROBE_EOF\n"
    )


# --- the checks ------------------------------------------------------------

def check(probe):
    """Turn a probe summary into findings. Pure, so it is testable offline.

    Returns a list of (level, message); level is "ERROR", "WARN" or "OK".
    """
    out = []
    add = lambda level, msg: out.append((level, msg))          # noqa: E731

    if probe.get("status") != "completed":
        add("ERROR", f"status is {probe.get('status')!r}, not 'completed'"
                     + (f": {probe['status_error']}" if probe.get("status_error") else ""))
    if not probe.get("results_present"):
        add("ERROR", "no results.json -- nothing was published")
        return out
    if probe.get("results_unreadable"):
        add("ERROR", f"results.json does not parse: {probe['results_unreadable']}")
        return out

    result_type = probe.get("result_type")
    if result_type not in VALID_RESULT_TYPES:
        add("ERROR", f"result_type {result_type!r} is not one of the nine valid types")
    else:
        add("OK", f"result_type {result_type}")

    n_scores, n_truth = probe.get("n_scores", 0), probe.get("n_truth", 0)
    if n_scores != n_truth:
        add("ERROR", f"{n_scores} scores against {n_truth} ground-truth labels -- "
                     "they are not the same sample")
        return out
    if n_scores == 0:
        add("ERROR", "no scores were published")
        return out

    kept = probe.get("kept", 0)
    dropped = n_scores - kept
    if kept == 0:
        add("ERROR", f"the evaluator keeps none of the {n_scores} scores "
                     "(all sentinel or non-finite)")
        return out
    if dropped:
        level = "WARN" if kept * 2 >= n_scores else "ERROR"
        add(level, f"{dropped} of {n_scores} scores are dropped before scoring "
                   f"({probe.get('dropped_unknown', 0)} unknown, "
                   f"{probe.get('dropped_inactive', 0)} inactive, "
                   f"{probe.get('dropped_non_finite', 0)} non-finite); "
                   f"the AUC is over {kept}")

    positives = probe.get("n_positive", 0)
    if positives == 0 or positives == kept:
        add("ERROR", f"ground truth has one class only ({positives} positive of "
                     f"{kept}) -- AUC is undefined. Scores must come from the "
                     "test split, which is where the anomalies are.")
    else:
        add("OK", f"{kept} scored samples, {positives} positive "
                  f"({positives / kept:.2%})")

    if probe.get("distinct_scores", 0) <= 1:
        add("ERROR", f"every score is the same value ({probe.get('score_min')}) -- "
                     "the method ranked nothing")

    declared = probe.get("declared_samples")
    if declared is not None and declared != n_scores:
        add("ERROR", f"summary declares scored_samples={declared} but {n_scores} "
                     "scores were published")

    split = probe.get("scored_split")
    if split is None:
        add("WARN", "summary does not record scored_split -- record it, so a "
                    "reader can tell a test-split AUC from a whole-stream one")
    elif str(split).lower() != "test":
        add("WARN", f"scored_split is {split!r}, not 'test'. Scoring everything "
                    "is only sound when the method fitted nothing; if it "
                    "trained, this AUC is partly over its own training data. "
                    "Either way the README has to say which it is.")
    else:
        add("OK", f"scored_split 'test' over {n_scores} samples")

    reported = probe.get("reported_aucs") or {}
    metrics = probe.get("eval_metrics") or {}
    evaluated = metrics.get("auc_roc")
    if evaluated is None:
        add("WARN", "no eval/evaluation.json -- run `graflag evaluate -e <exp>` "
                    "so the published scores can be checked against the "
                    "method's own number")
    elif not reported:
        add("WARN", f"evaluator AUC {evaluated}, but the method records no AUC in "
                    "metadata.summary, so the two cannot be cross-checked")
    else:
        agreeing = {k: v for k, v in reported.items()
                    if abs(v - evaluated) <= AUC_TOLERANCE}
        if agreeing:
            add("OK", f"evaluator AUC {evaluated} matches the method's "
                      f"{', '.join(sorted(agreeing))}")
        else:
            listed = ", ".join(f"{k}={v:.4f}" for k, v in sorted(reported.items()))
            add("ERROR", f"the evaluator scores {evaluated:.4f} and the method "
                         f"reports {listed} -- the published scores are not the "
                         "ones the method measured")
    return out


# --- driver ----------------------------------------------------------------

def probe(gf, experiment):
    """Run the probe on the manager through GraFlag's SSH layer.

    Returns the probe summary, or raises RuntimeError with what the manager
    said when it could not be produced.
    """
    exp_dir = f"{gf.config.remote_shared_dir}/experiments/{experiment}"
    result = gf.ssh.execute(remote_script(exp_dir))
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError(
            "probe failed on the manager: "
            + (result.stderr or result.stdout or "").strip()[:400])
    return json.loads(result.stdout.strip().splitlines()[-1])


def main(argv=None):
    """The original command line: ``verify_run.py EXP [--config F] [--json]``."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("experiment", help="exp__method__dataset__timestamp")
    parser.add_argument("--config", help="graflag config file (default: the usual resolution)")
    parser.add_argument("--json", action="store_true", help="print the raw probe summary")
    args = parser.parse_args(argv)

    from .core import GraFlag, GraFlagError
    try:
        report = GraFlag(config_file=args.config).verify(args.experiment)
    except GraFlagError as exc:
        print(f"[ERROR] {exc}")
        return 2
    if args.json:
        print(json.dumps(report.probe, indent=2, sort_keys=True))
    for line in format_report(report):
        print(line)
    return 1 if report.failed else 0


def format_report(report):
    """The report as the lines `graflag verify` prints."""
    lines = [f"[INFO] {report.experiment_name}"]
    lines += [f"[{f['level']}] {f['message']}" for f in report.findings]
    lines.append(f"[INFO] {report.failed} failed, {report.warned} warned, "
                 f"{report.passed} passed")
    return lines


if __name__ == "__main__":
    sys.exit(main())
