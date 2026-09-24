"""Tests for devcluster directory handling.

deploy.sh writes docker-compose.yml, hosts.yml and SSH keys next to itself.
When that is the installed package, it means writing into site-packages.
"""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from graflag.devcluster import cli as dc  # noqa: E402

HOSTS_YML = "subnet: 10.0.0.0/24\nmanager: 10.0.0.1\nworkers:\n    - 10.0.0.2\n"
COMPOSE = "services: {}\n"


class WorkDirLocation(unittest.TestCase):
    def test_work_dir_is_outside_the_package(self):
        """Regression: a root-owned or read-only install could not deploy at
        all, an editable install dirtied the git checkout, and the compose
        file was global state -- deploying a second cluster overwrote the
        first's, so --down tore down the wrong one."""
        package = Path(dc.__file__).resolve().parent
        work = dc._work_dir().resolve()

        self.assertNotEqual(work, package)
        self.assertFalse(str(work).startswith(str(package)))

    def test_respects_xdg_state_home(self):
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": "/tmp/xdg-probe"}):
            self.assertEqual(
                dc._work_dir(), Path("/tmp/xdg-probe/graflag/devcluster")
            )


class WorkDirPreparation(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.package = self.tmp / "pkg"
        (self.package / "manager").mkdir(parents=True)
        (self.package / "worker").mkdir()
        (self.package / "deploy.sh").write_text("#!/bin/bash\n")
        (self.package / "manager" / "Dockerfile.manager").write_text("FROM x\n")
        (self.package / "worker" / "Dockerfile.worker").write_text("FROM y\n")

    def _prepare(self):
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.tmp / "state")}):
            return dc._prepare_work_dir(self.package)

    def test_templates_are_copied(self):
        work = self._prepare()
        self.assertTrue((work / "deploy.sh").is_file())
        self.assertTrue((work / "manager" / "Dockerfile.manager").is_file())
        self.assertTrue((work / "worker" / "Dockerfile.worker").is_file())

    def test_deploy_script_stays_executable(self):
        work = self._prepare()
        self.assertTrue(os.access(work / "deploy.sh", os.X_OK))

    def test_preparing_twice_is_safe(self):
        self._prepare()
        work = self._prepare()
        self.assertTrue((work / "deploy.sh").is_file())

    def test_the_package_directory_is_not_written_to(self):
        before = sorted(p.name for p in self.package.rglob("*"))
        self._prepare()
        self.assertEqual(sorted(p.name for p in self.package.rglob("*")), before)


class DeployedDirResolution(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.package = self.tmp / "pkg"
        self.package.mkdir()

    def test_prefers_the_work_dir_when_a_cluster_was_deployed_there(self):
        state = self.tmp / "state"
        work = state / "graflag" / "devcluster"
        work.mkdir(parents=True)
        (work / "docker-compose.yml").write_text(COMPOSE)

        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(state)}):
            self.assertEqual(dc._deployed_dir(self.package), work)

    def test_falls_back_to_the_package_dir(self):
        """A cluster deployed by an older version must still be tearable."""
        (self.package / "docker-compose.yml").write_text(COMPOSE)

        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.tmp / "empty")}):
            self.assertEqual(dc._deployed_dir(self.package), self.package)


class MainRunsInTheWorkDir(unittest.TestCase):
    """The wiring, not just the helpers: deploy must not run in the package."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.hosts = self.tmp / "hosts.yml"
        self.hosts.write_text(HOSTS_YML)
        self.pubkey = self.tmp / "id.pub"
        self.pubkey.write_text("ssh-ed25519 AAAA test\n")
        self.state = self.tmp / "state"

    def _run_main(self, **kwargs):
        seen = {}

        def fake_run(argv, cwd=None, env=None, **kw):
            seen["argv"], seen["cwd"] = argv, cwd
            return mock.Mock(returncode=0)

        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.state)}), \
             mock.patch.object(dc.subprocess, "run", side_effect=fake_run), \
             self.assertRaises(SystemExit) as exit_ctx:
            dc.main(**kwargs)
        return seen, exit_ctx.exception.code

    def test_deploy_does_not_run_inside_the_installed_package(self):
        package = Path(dc.__file__).resolve().parent
        seen, code = self._run_main(hosts_yml=str(self.hosts), pubkey=str(self.pubkey))

        self.assertEqual(code, 0)
        work = (self.state / "graflag" / "devcluster").resolve()
        self.assertEqual(Path(seen["cwd"]).resolve(), work)
        self.assertNotEqual(Path(seen["cwd"]).resolve(), package)

    def test_deploy_leaves_the_package_directory_untouched(self):
        package = Path(dc.__file__).resolve().parent
        before = {p.name: p.stat().st_mtime for p in package.iterdir() if p.is_file()}

        self._run_main(hosts_yml=str(self.hosts), pubkey=str(self.pubkey))

        after = {p.name: p.stat().st_mtime for p in package.iterdir() if p.is_file()}
        self.assertEqual(before, after)

    def test_hosts_file_is_copied_into_the_work_dir(self):
        self._run_main(hosts_yml=str(self.hosts), pubkey=str(self.pubkey))
        work = self.state / "graflag" / "devcluster"
        self.assertTrue((work / "hosts.yml").is_file())


if __name__ == "__main__":
    unittest.main()
