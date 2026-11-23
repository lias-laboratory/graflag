"""build.log fills while the build runs, and the build's verdict survives it.

The build ran as one captured ssh call, so build.log appeared only when it
ended: nothing to `tail -f` through a ten-minute build. The output is now
`tee`d into the file on the manager as it arrives. A pipe hides the exit
status of its first command in POSIX sh, which has no pipefail -- and a build
that fails but reports success is exactly what this repository's tests exist
to prevent -- so the wrapper is run here through a real `sh`, against a fake
`docker` that fails on request.
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from graflag.docker_ops import BuildFailed, DockerManager

FAKE_DOCKER = """#!/bin/sh
case "$1" in
  build)
    echo "Step 1/2 : FROM base"
    echo "fatal: could not read Username" >&2
    [ -n "$FAKE_DOCKER_SEEN" ] && cp "$FAKE_DOCKER_LOG" "$FAKE_DOCKER_SEEN"
    exit "${FAKE_DOCKER_BUILD_RC:-0}" ;;
  push) exit 0 ;;
esac
exit 0
"""


class LocalManager:
    """Runs every command through sh, as the manager would; answers .env reads."""

    ENV = ("METHOD_NAME=rare\n"
           "SOURCE_CODE=https://github.com/author/method\n"
           "SOURCE_REF=b2de442a9b76e90fd13dd9fe715a1fe1e33618ef\n")

    def execute(self, command, **kwargs):
        if command.startswith("cat ") and ".env" in command:
            return SimpleNamespace(returncode=0, stdout=self.ENV, stderr="")
        if command.startswith("test -f"):
            return SimpleNamespace(returncode=0, stdout="exists", stderr="")
        done = subprocess.run(["sh", "-c", command], capture_output=True, text=True)
        return SimpleNamespace(returncode=done.returncode, stdout=done.stdout,
                               stderr=done.stderr)


class LiveBuildLog(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        bindir = self.tmp / "bin"
        bindir.mkdir()
        (bindir / "docker").write_text(FAKE_DOCKER)
        (bindir / "docker").chmod(0o755)
        self.log = self.tmp / "build.log"
        self.seen = self.tmp / "seen-during-build"
        self.env = {"PATH": f"{bindir}:{os.environ.get('PATH', '')}",
                    "FAKE_DOCKER_LOG": str(self.log),
                    "FAKE_DOCKER_SEEN": str(self.seen)}

    def build(self, rc):
        config = SimpleNamespace(remote_shared_dir="/shared", manager_ip="10.0.0.1")
        with mock.patch.dict(os.environ, dict(self.env, FAKE_DOCKER_BUILD_RC=str(rc))):
            return DockerManager(LocalManager(), config).build_method_image(
                "rare", live_log=str(self.log))

    def test_the_log_exists_while_the_build_runs(self):
        self.build(0)
        during = self.seen.read_text()
        self.assertIn("b2de442a9b76e90fd13dd9fe715a1fe1e33618ef", during)  # provenance
        self.assertIn("=== BUILD: rare:latest ===", during)

    def test_the_output_lands_in_it(self):
        self.build(0)
        text = self.log.read_text()
        self.assertIn("Step 1/2 : FROM base", text)
        self.assertIn("fatal: could not read Username", text)   # stderr too

    def test_a_failed_build_still_fails(self):
        with self.assertRaises(BuildFailed) as caught:
            self.build(3)
        self.assertIn("could not read Username", str(caught.exception))

    def test_a_successful_build_returns_its_log(self):
        self.assertIn("Step 1/2 : FROM base", self.build(0))

    def test_without_live_log_the_command_is_unchanged(self):
        recorded = []

        class Recording(LocalManager):
            def execute(self, command, **kwargs):
                recorded.append(command)
                return super().execute(command, **kwargs)

        config = SimpleNamespace(remote_shared_dir="/shared", manager_ip="10.0.0.1")
        with mock.patch.dict(os.environ, self.env):
            DockerManager(Recording(), config).build_method_image("rare")
        self.assertTrue(any(c.startswith("docker build") for c in recorded))
        self.assertFalse(any("tee -a" in c for c in recorded))


if __name__ == "__main__":
    unittest.main()
