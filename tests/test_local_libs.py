"""Where a method image gets graflag_runner, and who decides.

Every method Dockerfile used to run `pip install graflag-runner`, so what a
container executed was the last thing released to PyPI -- not the tree on the
share. `graflag sync --lib` pushed the libraries to the share and reported
success while changing nothing that ran, and the only way to test a library
change was to edit the Dockerfiles on the manager by hand. Eight of them were,
in three different spellings, and the manager held the only copy of the edit.

`GRAFLAG_LIBS` makes that a config value instead. These tests read the
`--build-arg` off the command GraFlag composes, so the choice is verified at
the point where it becomes an image rather than in the config object.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from graflag.config import GraflagConfig  # noqa: E402
from graflag.docker_ops import GRAFLAG_LIBS_CHOICES, DockerManager  # noqa: E402
from tests.test_build_args import FAKE_DOCKER, RecordingSSH  # noqa: E402


ENV = ("METHOD_NAME=gady\n"
       "SOURCE_CODE=https://github.com/mufeng-74/GADY\n"
       "COMMAND=python3 train_graflag.py\n")


class LibrarySource(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        bindir = Path(self.tmp) / "bin"
        bindir.mkdir()
        fake = bindir / "docker"
        fake.write_text(FAKE_DOCKER)
        fake.chmod(0o755)
        self.argv_file = str(Path(self.tmp) / "argv")

        self._old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{bindir}:{self._old_path}"
        os.environ["FAKE_DOCKER_ARGV"] = self.argv_file
        self.addCleanup(self._restore)

    def _restore(self):
        os.environ["PATH"] = self._old_path
        os.environ.pop("FAKE_DOCKER_ARGV", None)

    def manager(self, libs="local", env_text=ENV):
        ssh = RecordingSSH(env_text)
        config = SimpleNamespace(remote_shared_dir="/shared", manager_ip="10.0.0.1",
                                 graflag_libs=libs)
        return DockerManager(ssh, config), ssh

    def docker_argv(self, libs="local", env_text=ENV):
        mgr, ssh = self.manager(libs, env_text)
        mgr.build_method_image("gady")
        build_cmd = next(c for c in ssh.commands if c.startswith("docker build"))
        subprocess.run(["sh", "-c", build_cmd], check=True)
        return Path(self.argv_file).read_text().splitlines()

    def build_arg(self, argv, key):
        for i, a in enumerate(argv):
            if a == "--build-arg" and argv[i + 1].startswith(f"{key}="):
                return argv[i + 1].partition("=")[2]
        return None

    # -- the choice reaches docker ----------------------------------------

    def test_the_default_builds_from_the_share(self):
        """Local by default, because that is what the rest of the tool implies.

        `graflag sync --lib` exists to update the libraries on the share. With
        PyPI baked into every image it updated nothing that ran; with this it
        does what its name says.
        """
        self.assertEqual(self.build_arg(self.docker_argv(), "GRAFLAG_LIBS"),
                         "local")

    def test_a_release_can_be_pinned_instead(self):
        self.assertEqual(self.build_arg(self.docker_argv("pypi"), "GRAFLAG_LIBS"),
                         "pypi")

    def test_the_choice_is_always_stated(self):
        """Never omitted and left to the Dockerfile's ARG default.

        The default would then live in eleven Dockerfiles, and an image built
        by hand and one built by graflag would differ without either saying so.
        """
        for libs in GRAFLAG_LIBS_CHOICES:
            with self.subTest(libs=libs):
                self.assertIsNotNone(
                    self.build_arg(self.docker_argv(libs), "GRAFLAG_LIBS"))

    # -- what it is not ---------------------------------------------------

    def test_a_method_cannot_choose_its_own_libraries(self):
        """It is a property of the deployment, not of the method.

        A benchmark in which one method ran the released runner and its
        neighbour ran the working tree would not be comparing the two methods.
        So GRAFLAG_LIBS in a method's .env is inert -- and `.env` validation
        rejects the unknown key besides.
        """
        argv = self.docker_argv(env_text=ENV + "GRAFLAG_LIBS=pypi\n")
        self.assertEqual(self.build_arg(argv, "GRAFLAG_LIBS"), "local")

    def test_an_unknown_value_is_refused_before_the_build_starts(self):
        """A three-minute build is a bad place to learn about a typo.

        The Dockerfiles refuse it too -- the `*)` arm exits 1 -- but by then
        the clone and every pip install have already run, and the message is
        a shell exit status inside a layer rather than the name of the config
        key that is wrong.
        """
        for bad in ("locl", "PyPI ", "", "system"):
            mgr, ssh = self.manager(bad)
            with self.subTest(libs=bad):
                with self.assertRaises(ValueError) as caught:
                    mgr.build_method_image("gady")
                self.assertIn("GRAFLAG_LIBS", str(caught.exception))
                self.assertFalse(
                    [c for c in ssh.commands if c.startswith("docker build")],
                    "the build ran anyway")

    def test_the_value_arrives_as_one_argument(self):
        """Same rule as every other value crossing into a remote command."""
        argv = self.docker_argv()
        self.assertIn("GRAFLAG_LIBS=local", argv)


class WhatTheConfigSays(unittest.TestCase):
    """The default that actually ships, read through a real config file.

    The tests above hand `DockerManager` a config double, so they would pass
    unchanged if `DEFAULTS` said `pypi` -- which is the one value that must
    not be the default while the published wheel is behind the tree.
    """

    def config(self, text=""):
        path = Path(tempfile.mkdtemp()) / "config.env"
        self.addCleanup(shutil.rmtree, path.parent, ignore_errors=True)
        path.write_text("MANAGER_IP=10.0.0.1\n" + text)
        return GraflagConfig(str(path))

    def test_an_unconfigured_cluster_builds_from_the_share(self):
        """Nobody sets this key; the default is what every build will use."""
        self.assertEqual(self.config().graflag_libs, "local")

    def test_the_key_can_be_set(self):
        self.assertEqual(self.config("GRAFLAG_LIBS=pypi\n").graflag_libs, "pypi")

    def test_it_is_read_the_way_the_rest_of_the_file_is(self):
        """`.env` files are hand-written; `parse_env_line` already strips the
        quotes and the trailing comment, and the comparison is against a
        lowercase choice, so the value is normalised rather than trusted."""
        cfg = self.config('GRAFLAG_LIBS="PyPI"  # pinned for the paper\n')
        self.assertEqual(cfg.graflag_libs, "pypi")


if __name__ == "__main__":
    unittest.main()
