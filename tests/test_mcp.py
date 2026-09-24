"""Tests for the MCP server (`graflag mcp`).

Most tests drive the server through the SDK's in-memory client against a fake
GraFlag, so they check the protocol surface -- tool list, annotations, argument
screening, errors -- without a cluster. The stdio tests spawn a real server
process, because the failure they guard against only exists there: a
subprocess or a print() writing into stdout, which is the protocol, or reading
stdin, which is the client's requests.
"""

import asyncio
import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mcp.shared.memory import create_connected_server_and_client_session
    HAVE_MCP = True
except ImportError:                                       # Python < 3.10, or no extra
    HAVE_MCP = False

from graflag.core import GraFlagError  # noqa: E402
from graflag.models import (  # noqa: E402
    ClearReport, DatasetInfo, EvaluationResults, ExperimentInfo,
    ExperimentResults, MethodInfo, ServiceCleanupResult, VerificationReport,
)

if HAVE_MCP:
    import logging
    from graflag import mcp_server  # noqa: E402
    logging.getLogger("mcp").setLevel(logging.WARNING)

EXPECTED_TOOLS = {
    "cluster_status", "list_methods", "method_details", "list_datasets",
    "list_experiments", "get_experiment", "run_experiment", "wait_for_experiment",
    "get_logs", "stop_experiment", "evaluate_experiment", "get_results",
    "get_evaluation", "get_plot", "verify_experiment", "cleanup_services",
    "storage_report",
}
READ_ONLY = {
    "cluster_status", "list_methods", "method_details", "list_datasets",
    "list_experiments", "get_experiment", "wait_for_experiment", "get_logs",
    "get_results", "get_evaluation", "get_plot", "verify_experiment",
    "storage_report",
}
DESTRUCTIVE = {"stop_experiment", "cleanup_services"}

INJECTIONS = ["../x", "a;rm -rf /", "$(id)", "`id`", "a b", "x'y", "", "x" * 201]


def info(name="exp__m__d__20260924_120000", status="running", service=True):
    return ExperimentInfo(name=name, method="m", dataset="d", timestamp="",
                          status=status, service_name=name if service else None)


def fake_backend():
    """A GraFlag whose remote calls are canned."""
    gf = mock.Mock(name="GraFlag")
    gf.config.remote_shared_dir = "/shared"
    gf.ssh.path_exists.return_value = True
    gf.ssh.read_file.return_value = "# m\nWhat upstream does.\n"
    gf.list_methods.return_value = [
        MethodInfo(name="m", description="a method", integration="upstream",
                   supported_data="d*", parameters={"_EPOCHS": "100"}),
        MethodInfo(name="example", description="the template"),
    ]
    gf.list_datasets.return_value = [DatasetInfo(name="d", size_mb=1.5, file_count=2)]
    gf.list_experiments.return_value = [info(status="completed", service=False)]
    gf.get_experiment.return_value = info(status="completed", service=False)
    gf.get_logs.return_value = ["line 1", "x" * 5000]
    gf.get_experiment_results.return_value = ExperimentResults(
        experiment_name="e", method_name="m", dataset="d",
        metadata={"summary": {"test_auc": 0.9}}, result_type="NODE_ANOMALY_SCORES",
        scores_available=True)
    gf.get_evaluation_results.return_value = EvaluationResults(
        experiment_name="e", metrics={"auc_roc": 0.948}, plots_available=["roc_curve.png"])
    gf.verify.return_value = VerificationReport(
        experiment_name="e", findings=[{"level": "OK", "message": "fine"}], passed=1)
    gf.cleanup_services.return_value = [ServiceCleanupResult(
        experiment="e", removed=False, reason="dry run")]
    gf.clear.return_value = ClearReport(applied=False)
    return gf


@unittest.skipUnless(HAVE_MCP, "the mcp extra is not installed")
class MCPTestCase(unittest.TestCase):
    def setUp(self):
        self.gf = fake_backend()
        self.factory_calls = 0

        def factory():
            self.factory_calls += 1
            return self.gf
        self.server = mcp_server.build_server(factory)
        patcher = mock.patch.object(mcp_server, "WAIT_POLL_SECONDS", 0.01)
        patcher.start()
        self.addCleanup(patcher.stop)

    def session(self, coro_fn):
        async def go():
            async with create_connected_server_and_client_session(
                    self.server._mcp_server) as client:
                return await coro_fn(client)
        return asyncio.run(go())

    def call(self, name, arguments=None):
        return self.session(lambda c: c.call_tool(name, arguments or {}))

    def payload(self, result):
        self.assertFalse(result.isError, result.content)
        return json.loads(result.content[0].text)

    def error_text(self, result):
        self.assertTrue(result.isError, f"expected an error, got {result.content}")
        return result.content[0].text

    def jobs(self):
        return self.server.graflag_state.jobs

    def join_jobs(self, experiment):
        for job in self.jobs().for_experiment(experiment):
            job.thread.join(timeout=10)


class TheToolCatalogue(MCPTestCase):
    def tools(self):
        return {t.name: t for t in self.session(lambda c: c.list_tools()).tools}

    def test_exactly_the_planned_tools(self):
        self.assertEqual(set(self.tools()), EXPECTED_TOOLS)

    def test_nothing_that_deletes_or_sets_up_is_offered(self):
        """clear --apply, stop --rm, setup and arbitrary metric code stay human-only."""
        for name in self.tools():
            self.assertNotRegex(name, r"clear|delete|remove|setup|register|sync|copy")

    def test_read_only_tools_say_so(self):
        for name, tool in self.tools().items():
            with self.subTest(tool=name):
                self.assertEqual(tool.annotations.readOnlyHint, name in READ_ONLY)
                self.assertEqual(bool(tool.annotations.destructiveHint),
                                 name in DESTRUCTIVE)

    def test_every_tool_is_described(self):
        for name, tool in self.tools().items():
            self.assertGreater(len(tool.description or ""), 40, name)

    def test_listing_needs_no_configuration(self):
        """A server with no config can still be listed, and says what to fix."""
        self.tools()
        self.assertEqual(self.factory_calls, 0)


class NamesAreScreened(MCPTestCase):
    """What an LLM sends is checked before anything builds a remote command."""

    def test_experiment_names(self):
        for bad in INJECTIONS:
            for tool in ("get_experiment", "get_logs", "stop_experiment",
                         "evaluate_experiment", "verify_experiment", "get_results"):
                with self.subTest(tool=tool, name=bad):
                    self.assertIn("Invalid experiment name",
                                  self.error_text(self.call(tool, {"experiment": bad})))
        self.assertEqual(self.factory_calls, 0, "a bad name reached the backend")

    def test_run_arguments(self):
        for bad in INJECTIONS:
            for field in ("method", "dataset", "tag"):
                args = {"method": "m", "dataset": "d", field: bad}
                with self.subTest(field=field, name=bad):
                    self.error_text(self.call("run_experiment", args))
        self.assertEqual(self.factory_calls, 0)
        self.gf.run.assert_not_called()

    def test_plot_names(self):
        for bad in INJECTIONS:
            with self.subTest(name=bad):
                self.error_text(self.call("get_plot", {"experiment": "e", "plot": bad}))
        self.gf.ssh.execute.assert_not_called()


class ParametersAreCleaned(MCPTestCase):
    def run_with(self, params):
        release = threading.Event()
        self.gf.run.side_effect = lambda *a, **k: release.wait(5)
        result = self.call("run_experiment", {"method": "m", "dataset": "d",
                                              "params": params})
        release.set()
        return result

    def test_names_are_upper_cased_and_values_stringified(self):
        result = self.run_with({"_epochs": 5, "lr": 0.01, "Use_X": True})
        experiment = self.payload(result)["experiment"]
        self.join_jobs(experiment)
        self.assertEqual(self.gf.run.call_args.args[5],
                         {"EPOCHS": "5", "LR": "0.01", "USE_X": "true"})

    def test_reserved_names_are_refused_not_dropped(self):
        for name in ("DATA", "exp", "_METHOD_NAME", "command", "MONITOR_INTERVAL"):
            with self.subTest(name=name):
                self.assertIn("set by GraFlag itself",
                              self.error_text(self.run_with({name: "x"})))

    def test_malformed_parameters_are_refused(self):
        for params in ({"a-b": 1}, {"1x": 1}, {"x y": 1}, {"X": [1]},
                       {"X": {"a": 1}}, {"X": "a\nb"}):
            with self.subTest(params=params):
                self.error_text(self.run_with(params))
        self.gf.run.assert_not_called()


class RunsAreBackgroundJobs(MCPTestCase):
    def test_run_returns_before_the_run_ends(self):
        release = threading.Event()
        started = threading.Event()

        def slow_run(*args, **kwargs):
            started.set()
            release.wait(10)
        self.gf.run.side_effect = slow_run
        self.gf.get_experiment.return_value = info(status="running")

        result = self.payload(self.call("run_experiment", {"method": "M", "dataset": "d"}))
        experiment = result["experiment"]
        self.assertRegex(experiment, r"^exp__m__d__\d{8}_\d{6}$")
        self.assertTrue(started.wait(5))
        self.assertEqual(self.jobs().for_experiment(experiment)[0].state, "running")

        release.set()
        self.join_jobs(experiment)
        kwargs = self.gf.run.call_args.kwargs
        self.assertEqual(kwargs["exp_name"], experiment)
        self.assertIs(kwargs["follow"], False, "a run must never stream to stdout")
        self.assertEqual(self.jobs().for_experiment(experiment)[0].state, "done")

    def test_a_missing_method_is_refused_up_front(self):
        self.gf.ssh.path_exists.side_effect = lambda shared, rel: not rel.startswith("methods/")
        self.assertIn("Method m not found", self.error_text(
            self.call("run_experiment", {"method": "m", "dataset": "d"})))
        self.gf.run.assert_not_called()

    def test_a_failure_before_anything_was_written_is_reported(self):
        self.gf.run.side_effect = GraFlagError("Build failed: no space left on device")
        self.gf.get_experiment.return_value = None
        experiment = self.payload(self.call(
            "run_experiment", {"method": "m", "dataset": "d", "build": True}))["experiment"]
        self.join_jobs(experiment)

        waited = self.payload(self.call("wait_for_experiment", {"experiment": experiment}))
        self.assertTrue(waited["finished"])
        self.assertEqual(waited["status"], "failed")
        self.assertIn("no space left on device", waited["jobs"][0]["error"])

    def test_one_evaluation_at_a_time(self):
        release = threading.Event()
        self.gf.evaluate.side_effect = lambda *a, **k: release.wait(10)
        self.payload(self.call("evaluate_experiment", {"experiment": "e"}))
        self.assertIn("already in progress",
                      self.error_text(self.call("evaluate_experiment", {"experiment": "e"})))
        release.set()
        self.join_jobs("e")
        self.assertIs(self.gf.evaluate.call_args.kwargs["follow"], False)


class Waiting(MCPTestCase):
    def test_returns_when_the_experiment_finishes(self):
        self.gf.get_experiment.side_effect = [info(status="running"),
                                              info(status="completed", service=False)]
        waited = self.payload(self.call("wait_for_experiment",
                                        {"experiment": "e", "timeout_seconds": 60}))
        self.assertTrue(waited["finished"])
        self.assertEqual(waited["status"], "completed")

    def test_honours_its_timeout(self):
        self.gf.get_experiment.return_value = info(status="running")
        waited = self.payload(self.call("wait_for_experiment",
                                        {"experiment": "e", "timeout_seconds": 0}))
        self.assertFalse(waited["finished"])
        self.assertEqual(waited["status"], "running")

    def test_the_timeout_is_bounded(self):
        self.error_text(self.call("wait_for_experiment",
                                  {"experiment": "e", "timeout_seconds": 100000}))

    def test_waits_for_a_job_after_the_status_is_final(self):
        """A run whose status says completed is still cleaning up its service."""
        release = threading.Event()
        self.gf.evaluate.side_effect = lambda *a, **k: release.wait(10)
        self.gf.get_experiment.return_value = info(status="completed", service=False)
        self.payload(self.call("evaluate_experiment", {"experiment": "e"}))
        waited = self.payload(self.call("wait_for_experiment",
                                        {"experiment": "e", "timeout_seconds": 0}))
        self.assertFalse(waited["finished"], "returned while the evaluation ran")
        release.set()
        self.join_jobs("e")


class RepliesAndErrors(MCPTestCase):
    def test_an_error_carries_its_reason(self):
        self.gf.verify.side_effect = GraFlagError("Experiment e not found")
        self.assertIn("Experiment e not found",
                      self.error_text(self.call("verify_experiment", {"experiment": "e"})))

    def test_a_missing_configuration_says_how_to_fix_it(self):
        def factory():
            raise GraFlagError("Configuration file not found: /nope")
        self.server = mcp_server.build_server(factory)
        text = self.error_text(self.call("cluster_status"))
        self.assertIn("graflag setup", text)
        self.assertIn("--config", text)

    def test_lists_come_back_as_one_object(self):
        result = self.call("list_methods")
        self.assertEqual(len(result.content), 1)
        self.assertEqual(self.payload(result)["count"], 2)

    def test_method_details_include_the_readme(self):
        details = self.payload(self.call("method_details", {"method": "m"}))
        self.assertEqual(details["parameters"], {"_EPOCHS": "100"})
        self.assertIn("What upstream does", details["readme"])

    def test_log_lines_are_capped(self):
        lines = self.payload(self.call("get_logs", {"experiment": "e"}))["lines"]
        self.assertEqual(len(lines[1]), mcp_server.MAX_LINE_CHARS)

    def test_results_never_include_scores(self):
        results = self.payload(self.call("get_results", {"experiment": "e"}))
        self.assertNotIn("scores", results)
        self.assertEqual(results["metadata"]["summary"]["test_auc"], 0.9)

    def test_filters_on_the_experiment_list(self):
        listed = self.payload(self.call("list_experiments", {"status": "failed"}))
        self.assertEqual(listed["count"], 0)
        self.assertEqual(self.gf.list_experiments.call_args.kwargs["limit"],
                         mcp_server.MAX_LIST)

    def test_storage_report_never_applies(self):
        self.payload(self.call("storage_report"))
        self.gf.clear.assert_called_once_with(apply=False)

    def test_stop_keeps_the_directory(self):
        self.payload(self.call("stop_experiment", {"experiment": "e"}))
        self.gf.stop.assert_called_once_with("e", remove=False)

    def test_a_plot_is_an_image(self):
        png = b"\x89PNG\r\n\x1a\nfake"
        self.gf.ssh.execute.return_value = subprocess.CompletedProcess(
            [], 0, stdout=base64.b64encode(png).decode(), stderr="")
        result = self.call("get_plot", {"experiment": "e", "plot": "roc_curve"})
        self.assertFalse(result.isError, result.content)
        self.assertEqual(result.content[0].type, "image")
        self.assertEqual(result.content[0].mimeType, "image/png")
        self.assertEqual(base64.b64decode(result.content[0].data), png)
        command = self.gf.ssh.execute.call_args.args[0]
        self.assertIn("/shared/experiments/e/eval/roc_curve.png", command)

    def test_a_missing_plot_is_named(self):
        self.gf.ssh.execute.return_value = subprocess.CompletedProcess(
            [], 3, stdout="", stderr="")
        self.assertIn("no plot pr_curve.png", self.error_text(
            self.call("get_plot", {"experiment": "e", "plot": "pr_curve"})))


# A server process whose backend misbehaves the way real code paths can: it
# prints, and it starts a child that writes to fd 1 and reads fd 0.
STDIO_SERVER = textwrap.dedent("""
    import subprocess, sys
    sys.path.insert(0, {root!r})
    from graflag import mcp_server
    from graflag.models import ClusterInfo

    class Backend:
        class config:
            remote_shared_dir = "/shared"
        def status(self):
            print("print leak")
            subprocess.run(["sh", "-c", "echo child leak; cat; echo cat saw EOF >&2"],
                           timeout=20)
            return ClusterInfo(manager_ip="10.0.0.1", is_connected=True,
                               swarm_initialized=True)
        def list_methods(self):
            return []

    mcp_server._claim_stdio()
    mcp_server.build_server(Backend, guard_stdout={guard}).run("stdio")
""")


@unittest.skipUnless(HAVE_MCP, "the mcp extra is not installed")
class StdioIsTheProtocolAlone(unittest.TestCase):
    """A real stdio session survives a backend that writes to stdout."""

    def session(self, command, args, env=None, timeout=60):
        errlog = tempfile.TemporaryFile(mode="w+")
        self.addCleanup(errlog.close)

        async def go():
            params = StdioServerParameters(command=command, args=args,
                                           env=env or dict(os.environ))
            async with stdio_client(params, errlog=errlog) as (read, write):
                async with ClientSession(read, write) as client:
                    await client.initialize()
                    tools = await client.list_tools()
                    first = await client.call_tool("cluster_status", {})
                    second = await client.call_tool("list_methods", {})
                    return tools, first, second

        async def bounded():
            return await asyncio.wait_for(go(), timeout)
        result = asyncio.run(bounded())
        errlog.seek(0)
        return result, errlog.read()

    def test_the_session_survives_stray_output(self):
        script = STDIO_SERVER.format(root=str(ROOT), guard=True)
        (tools, first, second), stderr = self.session(sys.executable, ["-c", script])
        self.assertEqual(len(tools.tools), len(EXPECTED_TOOLS))
        self.assertFalse(first.isError, first.content)
        self.assertTrue(json.loads(first.content[0].text)["is_connected"])
        self.assertFalse(second.isError, "the session did not survive the leak")
        # Everything that tried to write to stdout reached stderr instead, and
        # the child's `cat` read /dev/null, not the client's next request.
        for leak in ("print leak", "child leak", "cat saw EOF"):
            self.assertIn(leak, stderr)

    def test_graflag_mcp_starts_from_the_command_line(self):
        tmp = tempfile.mkdtemp()
        config = Path(tmp) / "config.env"
        config.write_text("MANAGER_IP=10.0.0.1\n")
        bindir = Path(tmp) / "bin"
        bindir.mkdir()
        (bindir / "ssh").write_text("#!/bin/sh\nexit 255\n")   # no manager here
        (bindir / "ssh").chmod(0o755)
        env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}")
        (tools, first, second), _ = self.session(
            sys.executable, ["-m", "graflag.cli", "mcp", "--config", str(config)],
            env=env)
        self.assertEqual({t.name for t in tools.tools}, EXPECTED_TOOLS)
        # The manager is unreachable: an answer, not a hang or a crash.
        self.assertIsNotNone(first)


if __name__ == "__main__":
    unittest.main()
