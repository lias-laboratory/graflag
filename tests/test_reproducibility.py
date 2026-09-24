"""Tests that `run --from-config` reproduces the run it was recorded from.

`service_config.json` is the artifact GraFlag offers as the reproducible record
of an experiment, so a value that does not survive the round trip is a silent
loss of the thing the file exists to preserve.
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from graflag.docker_ops import DockerManager  # noqa: E402


def record_env_contents(method_env, user_params):
    """Run the recording half of the round trip (_save_service_config)."""
    mgr = DockerManager.__new__(DockerManager)
    mgr.ssh = mock.Mock()
    mgr.config = mock.Mock(remote_shared_dir="/shared", manager_ip="10.0.0.1")

    with mock.patch("graflag.docker_ops.load_method_env", return_value=dict(method_env)):
        mgr._save_service_config(
            exp_name="exp__m__d__20260101_000000",
            method_name="m",
            dataset="d",
            tag="latest",
            gpu_required=True,
            method_params=dict(user_params),
            registry_image="10.0.0.1:5000/m:latest",
        )

    # The config is written through a heredoc; recover the JSON from the command.
    written = mgr.ssh.execute.call_args[0][0]
    import json
    body = written.split("\n", 1)[1].rsplit("\nEOF", 1)[0]
    return json.loads(body)["env_contents"]


def replay_params(env_contents):
    """Run the replay half (cli.py::_parse_run_args --from-config)."""
    return {
        key[1:]: str(value)
        for key, value in env_contents.items()
        if key.startswith("_")
    }


class FromConfigRoundTrip(unittest.TestCase):
    METHOD_ENV = {"_EPOCH": "100", "_LR": "0.004", "METHOD_NAME": "m"}

    def test_user_params_survive_the_round_trip(self):
        """Regression: params were recorded without the `_` prefix the run uses,
        so replay skipped them and silently fell back to the method defaults."""
        env_contents = record_env_contents(self.METHOD_ENV, {"EPOCH": "500", "LR": "0.01"})
        replayed = replay_params(env_contents)

        self.assertEqual(replayed["EPOCH"], "500")
        self.assertEqual(replayed["LR"], "0.01")

    def test_recorded_config_has_no_duplicate_unprefixed_keys(self):
        """The old behaviour left both `_EPOCH: 100` and `EPOCH: 500` in the file."""
        env_contents = record_env_contents(self.METHOD_ENV, {"EPOCH": "500"})

        self.assertNotIn("EPOCH", env_contents)
        self.assertEqual(env_contents["_EPOCH"], "500")

    def test_untouched_defaults_are_preserved(self):
        env_contents = record_env_contents(self.METHOD_ENV, {"EPOCH": "500"})
        replayed = replay_params(env_contents)
        self.assertEqual(replayed["LR"], "0.004")

    def test_replay_matches_what_the_service_actually_ran(self):
        """The recorded value must equal the one injected into the container."""
        user_params = {"EPOCH": "500"}
        mgr = DockerManager.__new__(DockerManager)
        mgr.ssh = mock.Mock()
        mgr.config = mock.Mock(remote_shared_dir="/shared")

        with mock.patch("graflag.docker_ops.load_method_env",
                        return_value=dict(self.METHOD_ENV)):
            env_list = mgr._build_service_env("m", "d", "exp", dict(user_params))

        ran = dict(pair.split("=", 1) for pair in env_list)
        replayed = replay_params(record_env_contents(self.METHOD_ENV, user_params))

        self.assertEqual(ran["_EPOCH"], "500")
        self.assertEqual(replayed["EPOCH"], ran["_EPOCH"])


class NoGpuReachesTheMethod(unittest.TestCase):
    """--no-gpu removes the Swarm reservation but the method's own `_GPU`
    index comes from its .env, so a service scheduled without a GPU could
    still be told to use cuda:0.

    Every method is switched to -1. That was once limited to graflag_bond,
    because -1 is PyGOD's contract and three methods that built a device
    string by hand turned it into the invalid "cuda:-1" -- which is what
    happened to slade. Those methods now guard on `>= 0`, and
    graflag_runner.method.device() implements the convention for the rest, so
    the restriction only stopped --no-gpu working where it was needed.
    test_methods.py::GpuConvention holds the method side of this up.
    """

    BOND = "python3 -m graflag_bond.train"
    OWN = "python3 train_graflag.py"

    def _env(self, gpu_required, params, declared="0", command=BOND):
        mgr = DockerManager.__new__(DockerManager)
        mgr.ssh = mock.Mock()
        mgr.config = mock.Mock(remote_shared_dir="/shared")
        env = {"_LR": "0.01", "COMMAND": command}
        if declared is not None:
            env["_GPU"] = declared
        with mock.patch("graflag.docker_ops.load_method_env", return_value=env):
            pairs = mgr._build_service_env("m", "d", "e", dict(params),
                                           gpu_required=gpu_required)
        return dict(p.split("=", 1) for p in pairs)

    def test_bond_method_switches_to_cpu(self):
        self.assertEqual(self._env(False, {})["_GPU"], "-1")

    def test_a_method_with_its_own_script_switches_too(self):
        """The restriction to graflag_bond left --no-gpu inert for the five
        non-bond methods that declare a `_GPU`: addgraph, gady, slade,
        strgnn and taddy."""
        self.assertEqual(self._env(False, {}, command=self.OWN)["_GPU"], "-1")

    def test_gpu_run_leaves_the_index_alone(self):
        self.assertEqual(self._env(True, {})["_GPU"], "0")

    def test_an_explicit_param_wins(self):
        self.assertEqual(self._env(False, {"GPU": "2"})["_GPU"], "2")

    def test_already_cpu_is_untouched(self):
        self.assertEqual(self._env(False, {}, declared="-1")["_GPU"], "-1")

    def test_method_without_a_gpu_index_is_unaffected(self):
        self.assertNotIn("_GPU", self._env(False, {}, declared=None))


class GpuReservation(unittest.TestCase):
    def test_gpu_services_reserve_at_least_one_gpu(self):
        """Regression: Value was 0, so Swarm reserved nothing and applied no
        placement constraint -- a 'GPU-enabled' service could land on a
        CPU-only node."""
        import inspect
        from graflag import docker_ops

        src = inspect.getsource(docker_ops.DockerManager.create_service)
        self.assertIn("'Kind': 'NVIDIA-GPU'", src)
        self.assertNotIn("'Value': 0", src)


class ParameterManifest(unittest.TestCase):
    """GraFlag names the `_FOO` variables it injected in GRAFLAG_PARAMS.

    The method side cannot infer that from the environment: a `_FOO` variable
    is not necessarily a parameter. Inside a container it nearly always is,
    but the integration guide tells people to run a method by hand too, and
    there zsh exports _P9K_TTY and conda exports _CE_CONDA -- both were being
    read as method parameters and recorded in results.json.
    """

    def _manifest(self, method_env, user_params=None):
        mgr = DockerManager.__new__(DockerManager)
        mgr.ssh = mock.Mock()
        mgr.config = mock.Mock(remote_shared_dir="/shared")
        with mock.patch("graflag.docker_ops.load_method_env",
                        return_value=dict(method_env)):
            pairs = mgr._build_service_env("m", "d", "e", dict(user_params or {}))
        env = dict(p.split("=", 1) for p in pairs)
        return env, env["GRAFLAG_PARAMS"].split(",") if env["GRAFLAG_PARAMS"] else []

    def test_it_names_every_injected_parameter(self):
        _, names = self._manifest({"_EPOCH": "100", "_LR": "0.004", "COMMAND": "x"})
        self.assertEqual(names, ["_EPOCH", "_LR"])

    def test_a_user_override_is_in_the_manifest(self):
        """--params adds a key the .env never had; it is still a parameter."""
        _, names = self._manifest({"_EPOCH": "100"}, {"DROPOUT": "0.2"})
        self.assertIn("_DROPOUT", names)

    def test_non_parameter_keys_are_excluded(self):
        env, names = self._manifest({"_EPOCH": "1", "COMMAND": "x",
                                     "DESCRIPTION": "d", "SUPPORTED_DATASETS": "*"})
        self.assertEqual(names, ["_EPOCH"])
        # They still reach the container -- the manifest classifies, it does
        # not filter.
        self.assertIn("DESCRIPTION", env)

    def test_a_method_with_no_parameters_declares_an_empty_manifest(self):
        """Empty must be distinguishable from absent: absent means an old
        image, and graflag_runner then falls back to scanning everything."""
        env, names = self._manifest({"COMMAND": "x"})
        self.assertEqual(env["GRAFLAG_PARAMS"], "")
        self.assertEqual(names, [])

    def test_the_cpu_switch_does_not_escape_the_manifest(self):
        """_GPU is rewritten after the parameter loop; it must still appear."""
        mgr = DockerManager.__new__(DockerManager)
        mgr.ssh = mock.Mock()
        mgr.config = mock.Mock(remote_shared_dir="/shared")
        env = {"_GPU": "0", "COMMAND": "python3 -m graflag_bond.train"}
        with mock.patch("graflag.docker_ops.load_method_env", return_value=env):
            pairs = mgr._build_service_env("m", "d", "e", {}, gpu_required=False)
        got = dict(p.split("=", 1) for p in pairs)
        self.assertEqual(got["_GPU"], "-1")
        self.assertIn("_GPU", got["GRAFLAG_PARAMS"].split(","))

    def test_a_method_cannot_forge_the_manifest(self):
        """GRAFLAG_PARAMS is reserved, so --params GRAFLAG_PARAMS=... cannot
        smuggle in a `_GRAFLAG_PARAMS` that shadows it, and a .env that sets
        it directly is overwritten rather than trusted."""
        from graflag.docker_ops import ReservedEnvVars
        self.assertIn("GRAFLAG_PARAMS", ReservedEnvVars.get_names())

        env, names = self._manifest({"_EPOCH": "1", "GRAFLAG_PARAMS": "_LIES"})
        self.assertEqual(names, ["_EPOCH"])


if __name__ == "__main__":
    unittest.main()


class ReplayRestoresTheRun(unittest.TestCase):
    """`run --from-config` through the real command line.

    The replay read the method, the dataset and the parameters, and nothing
    else: a run on a tagged image replayed `latest`, and a --no-gpu run
    replayed with a GPU. Passing --no-gpu again did not help either, because
    the recorded `_GPU` -- the .env default, recorded like any parameter --
    reached _build_service_env as an explicit one and blocked the switch to -1.
    """

    def replay(self, config, *extra):
        """Run `graflag run --from-config <config> <extra>` with GraFlag mocked.

        Returns the positional arguments `run()` received:
        (method, dataset, tag, build, gpu, params).
        """
        import contextlib
        import io
        import json
        import tempfile
        from graflag import cli

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "service_config.json"
            path.write_text(json.dumps(config))
            argv = ["graflag", "run", "--from-config", str(path), *extra]
            with mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(cli, "GraFlag") as graflag, \
                    contextlib.redirect_stderr(io.StringIO()):
                cli.main()
        return graflag.return_value.run.call_args.args

    CONFIG = {
        "method_name": "taddy", "dataset": "uci", "tag": "v2",
        "gpu_required": False,
        "env_contents": {"METHOD_NAME": "taddy", "_EPOCH": "50", "_GPU": "0"},
    }

    def test_the_recorded_tag_is_replayed(self):
        self.assertEqual(self.replay(self.CONFIG)[2], "v2")

    def test_a_tag_on_the_command_line_wins(self):
        self.assertEqual(self.replay(self.CONFIG, "--tag", "v3")[2], "v3")

    def test_a_run_without_a_gpu_replays_without_one(self):
        self.assertIs(self.replay(self.CONFIG)[4], False)

    def test_gpu_on_the_command_line_wins(self):
        self.assertIs(self.replay(self.CONFIG, "--gpu")[4], True)

    def test_the_replayed_service_runs_the_method_on_the_cpu(self):
        """End to end: the parameters the replay hands to run() must let
        _build_service_env switch `_GPU` to -1."""
        _, _, _, _, gpu, params = self.replay(self.CONFIG)
        mgr = DockerManager.__new__(DockerManager)
        mgr.ssh = mock.Mock()
        mgr.config = mock.Mock(remote_shared_dir="/shared")
        env = {"_EPOCH": "100", "_GPU": "0", "COMMAND": "python3 train_graflag.py"}
        with mock.patch("graflag.docker_ops.load_method_env", return_value=env):
            pairs = mgr._build_service_env("taddy", "uci", "e", dict(params),
                                           gpu_required=gpu)
        ran = dict(p.split("=", 1) for p in pairs)
        self.assertEqual(ran["_GPU"], "-1")
        self.assertEqual(ran["_EPOCH"], "50")

    def test_no_gpu_given_again_switches_the_method_too(self):
        """A run recorded with a GPU, replayed with --no-gpu: the recorded
        `_GPU` is the .env default and must not block the switch to -1."""
        config = dict(self.CONFIG, gpu_required=True)
        _, _, _, _, gpu, params = self.replay(config, "--no-gpu")
        self.assertIs(gpu, False)
        self.assertNotIn("GPU", params)

    def test_an_explicit_gpu_param_still_wins(self):
        params = self.replay(self.CONFIG, "--params", "GPU=1")[5]
        self.assertEqual(params["GPU"], "1")

    def test_a_gpu_run_keeps_its_recorded_index(self):
        config = dict(self.CONFIG, gpu_required=True)
        self.assertEqual(self.replay(config)[5]["GPU"], "0")

    def test_a_config_without_the_keys_replays_with_the_defaults(self):
        """service_config.json files written before tag and gpu_required
        were read back still replay: latest, with a GPU."""
        config = {k: v for k, v in self.CONFIG.items() if k not in ("tag", "gpu_required")}
        args = self.replay(config)
        self.assertEqual(args[2], "latest")
        self.assertIs(args[4], True)


class PlainRunDefaults(unittest.TestCase):
    """Without --from-config nothing changes: latest, with a GPU."""

    def run_args(self, *extra):
        import contextlib
        import io
        from graflag import cli
        argv = ["graflag", "run", "-m", "taddy", "-d", "uci", *extra]
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(cli, "GraFlag") as graflag, \
                contextlib.redirect_stderr(io.StringIO()):
            cli.main()
        return graflag.return_value.run.call_args.args

    def test_defaults(self):
        args = self.run_args()
        self.assertEqual(args[2], "latest")
        self.assertIs(args[4], True)

    def test_no_gpu(self):
        self.assertIs(self.run_args("--no-gpu")[4], False)

    def test_tag(self):
        self.assertEqual(self.run_args("-t", "v2")[2], "v2")
