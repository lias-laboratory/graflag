"""`graflag run --build --force-rm` reaches `docker build` as --force-rm.

A failed build without it keeps the last container it ran, and that container
holds every layer the build had produced until then. The first build of the
rare method failed at `git clone` and left 12.8 GB on the manager that way,
which neither `graflag clear` nor `docker image prune` would touch while the
container existed.

The option crosses three layers -- argparse, GraFlag.run, the composed build
command -- and dropping it at any one of them turns it into a flag that is
accepted and does nothing. Each layer gets its own test, and the last one runs
the composed command through a real `sh` against a fake `docker`, so it reads
the argv docker would actually be handed.
"""

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from graflag import cli  # noqa: E402
from graflag.core import GraFlag  # noqa: E402
from graflag.docker_ops import DockerManager  # noqa: E402


FAKE_DOCKER = """#!/bin/sh
for a in "$@"; do printf '%s\\n' "$a" >> "$FAKE_DOCKER_ARGV"; done
"""


class Manager:
    """An SSHManager double: answers the .env reads, records every command."""

    ENV = ("METHOD_NAME=rare\n"
           "SOURCE_CODE=https://github.com/author/method\n"
           "SOURCE_REF=b2de442a9b76e90fd13dd9fe715a1fe1e33618ef\n")

    def __init__(self):
        self.commands = []

    def execute(self, command, **kwargs):
        self.commands.append(command)
        out = self.ENV if command.startswith("cat ") else (
            "exists" if command.startswith("test -f") else "")
        return SimpleNamespace(returncode=0, stdout=out, stderr="")


class DockerIsHandedTheFlag(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        bindir = Path(self.tmp) / "bin"
        bindir.mkdir()
        (bindir / "docker").write_text(FAKE_DOCKER)
        (bindir / "docker").chmod(0o755)
        self.argv_file = Path(self.tmp) / "argv"
        env = {"PATH": f"{bindir}:{os.environ.get('PATH', '')}",
               "FAKE_DOCKER_ARGV": str(self.argv_file)}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def docker_argv(self, **kwargs):
        ssh = Manager()
        config = SimpleNamespace(remote_shared_dir="/shared", manager_ip="10.0.0.1")
        DockerManager(ssh, config).build_method_image("rare", **kwargs)
        build = next(c for c in ssh.commands if c.startswith("docker build"))
        subprocess.run(["sh", "-c", build], check=True)
        return self.argv_file.read_text().splitlines()

    def test_asked_for_it_arrives_as_its_own_argument(self):
        argv = self.docker_argv(force_rm=True)
        self.assertEqual(argv[0], "build")
        self.assertIn("--force-rm", argv)

    def test_not_asked_for_it_is_absent(self):
        """docker's default stays the default: nothing changes for existing runs."""
        self.assertNotIn("--force-rm", self.docker_argv())


class RunForwardsIt(unittest.TestCase):

    def run_with(self, **kwargs):
        gf = GraFlag.__new__(GraFlag)
        gf.config = SimpleNamespace(remote_shared_dir="/shared")
        gf.ssh = mock.Mock()
        gf.ssh.path_exists.return_value = True
        gf.docker = mock.Mock()
        gf.docker.build_method_image.return_value = "log"
        with mock.patch.object(gf, "_ensure_dataset"), \
                mock.patch.object(gf, "cleanup_services"):
            gf.run("rare", "bond_inj_cora", build=True, **kwargs)
        return gf.docker.build_method_image.call_args

    def test_force_rm_reaches_the_builder(self):
        self.assertTrue(self.run_with(force_rm=True).kwargs.get("force_rm"))

    def test_the_default_asks_for_nothing(self):
        self.assertFalse(self.run_with().kwargs.get("force_rm"))


class TheCommandLine(unittest.TestCase):

    def main(self, *argv):
        """Run `graflag <argv>` with GraFlag replaced; return (instance, stderr)."""
        stderr = io.StringIO()
        with mock.patch.object(sys, "argv", ["graflag", *argv]), \
                mock.patch.object(cli, "GraFlag") as graflag, \
                contextlib.redirect_stderr(stderr):
            try:
                cli.main()
            except SystemExit as exit_:
                self.exit_code = exit_.code
            else:
                self.exit_code = 0
        return graflag.return_value, stderr.getvalue()

    def test_the_flag_reaches_run(self):
        gf, _ = self.main("run", "-m", "rare", "-d", "bond_inj_cora",
                          "--build", "--force-rm")
        self.assertEqual(self.exit_code, 0)
        self.assertTrue(gf.run.call_args.kwargs.get("force_rm"))

    def test_without_it_run_is_told_false(self):
        gf, _ = self.main("run", "-m", "rare", "-d", "bond_inj_cora", "--build")
        self.assertFalse(gf.run.call_args.kwargs.get("force_rm"))

    def test_without_build_it_is_a_usage_error(self):
        """Accepted without --build it would do nothing and say nothing."""
        gf, err = self.main("run", "-m", "rare", "-d", "bond_inj_cora", "--force-rm")
        self.assertEqual(self.exit_code, 2)
        self.assertIn("--force-rm applies to the image build", err)
        gf.run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
