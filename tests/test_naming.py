"""Tests for experiment/method naming and the GUI's input guard.

These cover the places where a name is produced in one component and consumed
in another -- the cases where a mismatch silently points at a directory that
does not exist.
"""

import re
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from graflag.core import GraFlag  # noqa: E402


class ExperimentNaming(unittest.TestCase):
    """make_experiment_name() is the single source of the directory name."""

    def test_lowercases_method_and_dataset(self):
        name = GraFlag.make_experiment_name("TADDY", "UCI")
        self.assertTrue(name.startswith("exp__taddy__uci__"), name)

    def test_shape_is_four_double_underscore_fields(self):
        name = GraFlag.make_experiment_name("taddy", "uci")
        parts = name.split("__")
        self.assertEqual(len(parts), 4)
        self.assertEqual(parts[0], "exp")
        self.assertRegex(parts[3], r"^\d{8}_\d{6}$")

    def test_run_uses_supplied_name_verbatim(self):
        """The GUI precomputes the name; run() must not invent another one."""
        gf = GraFlag.__new__(GraFlag)
        gf.config = mock.Mock(remote_shared_dir="/shared")
        gf.ssh = mock.Mock()
        gf.ssh.path_exists.return_value = True
        gf.docker = mock.Mock()

        supplied = "exp__taddy__uci__20260101_000000"
        with mock.patch.object(gf, "_ensure_dataset"):
            returned = gf.run("TADDY", "UCI", exp_name=supplied)

        self.assertEqual(returned, supplied)
        gf.ssh.mkdir.assert_called_once_with("/shared", f"experiments/{supplied}")

    def test_run_generates_a_name_when_none_supplied(self):
        gf = GraFlag.__new__(GraFlag)
        gf.config = mock.Mock(remote_shared_dir="/shared")
        gf.ssh = mock.Mock()
        gf.ssh.path_exists.return_value = True
        gf.docker = mock.Mock()

        with mock.patch.object(gf, "_ensure_dataset"):
            returned = gf.run("TADDY", "UCI")

        self.assertTrue(returned.startswith("exp__taddy__uci__"), returned)


class SyncTargetsTheDirectoryRunReads(unittest.TestCase):
    """sync() must write where run() looks: methods/<lowercased name>."""

    def _sync(self, tmpdir, method_name):
        env = Path(tmpdir) / ".env"
        env.write_text(f"METHOD_NAME={method_name}\nCOMMAND=python3 x.py\n")

        gf = GraFlag.__new__(GraFlag)
        gf.config = mock.Mock(remote_shared_dir="/shared")
        gf.ssh = mock.Mock()
        gf.sync(str(tmpdir), is_lib=False)
        return gf.ssh.copy_files.call_args.kwargs["dest_path"]

    def test_uppercase_method_name_is_lowercased(self):
        """Regression: METHOD_NAME=TADDY synced to methods/TADDY, unreadable
        by run(), which lowercases to methods/taddy."""
        import tempfile
        with tempfile.TemporaryDirectory(suffix="taddy") as tmp:
            dest = self._sync(tmp, "TADDY")
        self.assertEqual(dest, "/shared/methods/taddy/")

    def test_lowercase_method_name_is_unchanged(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            dest = self._sync(tmp, "generaldyg")
        self.assertEqual(dest, "/shared/methods/generaldyg/")


class GuiNameGuard(unittest.TestCase):
    """The GUI rejects names before they reach a root shell on the manager."""

    @staticmethod
    def _valid_name():
        from graflag.gui.server import _valid_name
        return _valid_name

    def test_accepts_real_experiment_names(self):
        valid = self._valid_name()
        for name in (
            "exp__taddy__uci__20260101_000000",
            "bond_cola",
            "btc_alpha_snapshot",
            "graflag-evaluator",
        ):
            self.assertTrue(valid(name), name)

    def test_rejects_shell_metacharacters(self):
        valid = self._valid_name()
        for name in (
            "exp__a__b__1; rm -rf /",
            "exp__a__b__1' ; touch /tmp/x ; '",
            "$(hostname)",
            "`id`",
            "a b",
            "a|b",
            "a&b",
            "a>b",
        ):
            self.assertFalse(valid(name), name)

    def test_rejects_path_traversal(self):
        valid = self._valid_name()
        for name in ("..", "../../etc", "a/../b", "a/b"):
            self.assertFalse(valid(name), name)

    def test_rejects_empty_and_oversized(self):
        valid = self._valid_name()
        self.assertFalse(valid(""))
        self.assertFalse(valid(None))
        self.assertFalse(valid("a" * 201))


class HostsFileResolution(unittest.TestCase):
    """HOSTS_FILE is a path a human types, so `~` must work."""

    def _manager(self, hosts_file):
        from graflag.docker_ops import DockerManager
        mgr = DockerManager.__new__(DockerManager)
        mgr.hosts_file = hosts_file
        mgr.ssh = mock.Mock()
        mgr.config = mock.Mock(manager_ip="10.0.0.1")
        return mgr

    def test_tilde_in_hosts_path_is_expanded(self):
        """Regression: Path('~/x.yml').exists() is False, so _load_hosts
        returned {} and setup_workers logged 'No workers defined' for a file
        that defined four -- leaving a single-node swarm that setup called a
        success."""
        import tempfile, os
        from pathlib import Path as P

        home = tempfile.mkdtemp()
        rel = "graflag_hosts_test.yml"
        P(home, rel).write_text(
            "subnet: 192.168.100.0/24\nmanager: 192.168.100.10\n"
            "workers:\n    - 192.168.100.11\n    - 192.168.100.12\n"
        )
        with mock.patch.dict(os.environ, {"HOME": home}):
            hosts = self._manager(f"~/{rel}")._load_hosts()

        self.assertEqual(hosts.get("manager"), "192.168.100.10")
        self.assertEqual(len(hosts.get("workers", [])), 2)

    def test_absolute_path_still_works(self):
        import tempfile
        from pathlib import Path as P

        d = tempfile.mkdtemp()
        p = P(d) / "hosts.yml"
        p.write_text("manager: 10.0.0.1\nworkers:\n    - 10.0.0.2\n")
        hosts = self._manager(str(p))._load_hosts()
        self.assertEqual(len(hosts["workers"]), 1)

    def test_missing_file_returns_empty_without_raising(self):
        self.assertEqual(self._manager("/nope/hosts.yml")._load_hosts(), {})

    def test_unset_hosts_file_returns_empty(self):
        self.assertEqual(self._manager(None)._load_hosts(), {})


class PythonApiDefaults(unittest.TestCase):
    """The documented `GraFlag()` entry point must work with no arguments."""

    def _in_empty_dir(self, factory):
        import os, tempfile
        cwd = os.getcwd()
        try:
            os.chdir(tempfile.mkdtemp())   # no .env here
            return factory()
        finally:
            os.chdir(cwd)

    def test_graflag_constructs_without_arguments(self):
        """Regression: the default was the literal ".env", which config
        resolution reads as an explicit request for that exact file, so
        `GraFlag()` raised "Configuration file not found: .env"."""
        from graflag.config import CONFIG_FILE
        if not CONFIG_FILE.exists():
            self.skipTest("no user config on this machine")
        gf = self._in_empty_dir(GraFlag)
        self.assertEqual(gf.config.config_path, CONFIG_FILE)

    def test_api_constructs_without_arguments(self):
        from graflag.config import CONFIG_FILE
        from graflag.api import GraFlagAPI
        if not CONFIG_FILE.exists():
            self.skipTest("no user config on this machine")
        api = self._in_empty_dir(GraFlagAPI)
        self.assertEqual(api.core.config.config_path, CONFIG_FILE)

    def test_explicit_missing_config_still_raises(self):
        from graflag.core import GraFlagError
        with self.assertRaises(GraFlagError):
            GraFlag(config_file="/nope/does-not-exist.env")


if __name__ == "__main__":
    unittest.main()
