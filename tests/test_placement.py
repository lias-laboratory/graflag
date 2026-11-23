"""Every run records the node it ran on.

service_details.json was written once, when the service was created. A task
that then queued for a GPU had no node yet, so on a busy cluster the record
of where a run happened was an empty string -- for exactly the runs whose
placement mattered, when comparing results across nodes. Two records now
close that: the service tells the runner its node (GRAFLAG_NODE, expanded by
Swarm per task), and run() rewrites the worker block once the task is over,
before the service is removed.
"""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from graflag.docker_ops import DockerManager, ReservedEnvVars

EXP = "exp__m__d__20260101_000000"


def manager(shared=None, cls=DockerManager):
    mgr = cls.__new__(cls)
    mgr.config = SimpleNamespace(remote_shared_dir=str(shared or "/shared"))
    mgr.ssh = SimpleNamespace(execute=lambda cmd, **kw: subprocess.run(
        ["sh", "-c", cmd], capture_output=True, text=True))
    return mgr


class TheServiceTellsTheRunnerItsNode(unittest.TestCase):
    def env(self, params=None):
        with mock.patch("graflag.docker_ops.load_method_env",
                        return_value={"_EPOCHS": "1"}):
            pairs = manager()._build_service_env("m", "d", EXP, dict(params or {}))
        return dict(pair.split("=", 1) for pair in pairs)

    def test_the_swarm_template_is_set(self):
        self.assertEqual(self.env()["GRAFLAG_NODE"], "{{.Node.Hostname}}")

    def test_it_is_reserved(self):
        self.assertIn("GRAFLAG_NODE", ReservedEnvVars.get_names())
        self.assertEqual(self.env({"GRAFLAG_NODE": "x"})["GRAFLAG_NODE"],
                         "{{.Node.Hostname}}")

    def test_it_is_not_a_method_parameter(self):
        self.assertNotIn("GRAFLAG_NODE", self.env()["GRAFLAG_PARAMS"].split(","))


class RecordPlacement(unittest.TestCase):
    def setUp(self):
        self.shared = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.shared, ignore_errors=True)
        self.exp_dir = self.shared / "experiments" / EXP
        self.exp_dir.mkdir(parents=True)
        self.details = self.exp_dir / "service_details.json"
        self.details.write_text(json.dumps({
            "service_name": EXP, "created_at_docker": "2026-01-01T00:00:00Z",
            "worker": {"node_id": "", "state": "pending"}}))

        task = {"ID": "t1", "NodeID": "n2", "CreatedAt": "2026-01-01T00:00:01Z",
                "DesiredState": "shutdown", "Status": {"State": "complete"}}
        service = mock.Mock()
        service.tasks.return_value = [task]
        client = mock.Mock()
        client.services.get.return_value = service
        client.nodes.get.return_value = mock.Mock(
            attrs={"Description": {"Hostname": "node-2"}})

        class WithClient(DockerManager):        # never patch DockerManager itself
            pass
        WithClient.client = property(lambda self_: client)
        self.mgr = manager(self.shared, cls=WithClient)
        self.mgr._with_reconnect = lambda fn: fn()

    def test_the_final_node_replaces_the_pending_one(self):
        worker = self.mgr.record_placement(EXP)
        self.assertEqual(worker["node_hostname"], "node-2")
        saved = json.loads(self.details.read_text())
        self.assertEqual(saved["worker"]["node_id"], "n2")
        self.assertEqual(saved["worker"]["node_hostname"], "node-2")
        self.assertEqual(saved["worker"]["state"], "complete")

    def test_the_creation_record_is_kept(self):
        self.mgr.record_placement(EXP)
        saved = json.loads(self.details.read_text())
        self.assertEqual(saved["created_at_docker"], "2026-01-01T00:00:00Z")

    def test_it_never_raises(self):
        def broken(fn):
            raise ConnectionError("socket closed")
        self.mgr._with_reconnect = broken
        self.assertIsNone(self.mgr.record_placement(EXP))


class RunRecordsItBeforeRemovingTheService(unittest.TestCase):
    def test_order(self):
        try:
            from tests.test_run_quiet import RunWithoutFollowing
        except ImportError:                     # discover -s tests
            from test_run_quiet import RunWithoutFollowing
        case = RunWithoutFollowing("test_nothing_is_streamed")
        case.setUp()
        self.addCleanup(case.doCleanups)
        calls = []
        case.gf.docker.record_placement.side_effect = \
            lambda name: calls.append(("record", name))
        removed = case.gf.docker.cleanup_finished_service.side_effect
        case.gf.docker.cleanup_finished_service.side_effect = \
            lambda name: (calls.append(("remove", name)), removed(name))
        name, _ = case.run_quietly()
        self.assertEqual(calls, [("record", name), ("remove", name)])


if __name__ == "__main__":
    unittest.main()
