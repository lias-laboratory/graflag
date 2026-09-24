"""A failed build keeps the output that says why it failed.

build.log was written only after a successful build, and the exception a
failed one raised carried docker's stderr alone. With the legacy builder that
is the deprecation banner and "The command ... returned a non-zero code: 128":
which step failed, never why. The why is in the step output on stdout. The
first build of the rare method failed exactly like this -- git could not read
a username because the repository is private -- and the experiment directory
held a status.json naming `git clone` and nothing else.

The core half runs `run()` against a share that is a local directory, pushing
every remote command through a real `sh`, so the assertions read the files
that actually land there rather than the shape of a command string.
"""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from graflag.core import GraFlag, GraFlagError  # noqa: E402
from graflag.docker_ops import BuildFailed, DockerManager  # noqa: E402


EXP = "exp__rare__bond_inj_cora__20260923_202531"

# What the legacy builder printed for the failed clone, split the way docker
# splits it: the reason on stdout, the verdict on stderr.
STEP_OUTPUT = (
    "Step 7/15 : RUN test -n \"${SOURCE_CODE}\" && git clone ${SOURCE_CODE} src\n"
    " ---> Running in 5f0c2a9e1b7d\n"
    "Cloning into 'src'...\n"
    "fatal: could not read Username for 'https://github.com': "
    "No such device or address\n"
)
VERDICT = (
    "DEPRECATED: The legacy builder is deprecated and will be removed in a "
    "future release.\n"
    "The command '/bin/sh -c test -n \"${SOURCE_CODE}\" && git clone "
    "${SOURCE_CODE} src' returned a non-zero code: 128\n"
)
REASON = "fatal: could not read Username for 'https://github.com'"


class ManagerWhoseBuildFails:
    """An SSHManager double: answers the .env reads, fails `docker build`."""

    ENV = (
        "METHOD_NAME=rare\n"
        "SOURCE_CODE=https://github.com/author/method\n"
        "SOURCE_REF=b2de442a9b76e90fd13dd9fe715a1fe1e33618ef\n"
    )

    def execute(self, command, **kwargs):
        if command.startswith("docker build"):
            return SimpleNamespace(returncode=1, stdout=STEP_OUTPUT, stderr=VERDICT)
        if command.startswith("test -f"):
            return SimpleNamespace(returncode=0, stdout="exists", stderr="")
        if command.startswith("cat "):
            return SimpleNamespace(returncode=0, stdout=self.ENV, stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")


class TheFailureCarriesTheBuildOutput(unittest.TestCase):

    def failed_build(self):
        config = SimpleNamespace(remote_shared_dir="/shared", manager_ip="10.0.0.1")
        with self.assertRaises(BuildFailed) as caught:
            DockerManager(ManagerWhoseBuildFails(), config).build_method_image("rare")
        return caught.exception

    def test_the_reason_is_in_the_log(self):
        self.assertIn(REASON, self.failed_build().log)

    def test_the_log_is_the_one_a_success_would_have_written(self):
        """Provenance first, then the build section -- same layout as build.log."""
        log = self.failed_build().log
        self.assertIn("b2de442a9b76e90fd13dd9fe715a1fe1e33618ef", log)
        self.assertLess(log.index("b2de442a9b76e90fd13dd9fe715a1fe1e33618ef"),
                        log.index("=== BUILD:"))

    def test_the_message_still_names_the_failed_step(self):
        self.assertIn("returned a non-zero code: 128", str(self.failed_build()))

    def test_callers_catching_runtimeerror_still_catch_it(self):
        self.assertIsInstance(self.failed_build(), RuntimeError)


class ShareOnLocalDisk:
    """A manager whose share is a local directory; commands run through sh."""

    def __init__(self):
        self.commands = []

    def path_exists(self, shared, rel):
        return True

    def mkdir(self, shared, rel):
        Path(shared, rel).mkdir(parents=True, exist_ok=True)

    def execute(self, command, **kwargs):
        self.commands.append(command)
        done = subprocess.run(["sh", "-c", command], capture_output=True, text=True)
        return SimpleNamespace(returncode=done.returncode, stdout=done.stdout,
                               stderr=done.stderr)


class RunKeepsTheLogOfAFailedBuild(unittest.TestCase):

    def setUp(self):
        self.share = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.share, ignore_errors=True)
        self.exp_dir = Path(self.share, "experiments", EXP)

    def run_with(self, build_error):
        gf = GraFlag.__new__(GraFlag)
        gf.config = SimpleNamespace(remote_shared_dir=self.share)
        gf.ssh = ShareOnLocalDisk()
        gf.docker = mock.Mock()
        gf.docker.build_method_image.side_effect = build_error
        with mock.patch.object(gf, "_ensure_dataset"), \
                self.assertRaises(GraFlagError) as caught:
            gf.run("rare", "bond_inj_cora", build=True, exp_name=EXP)
        gf.docker.create_service.assert_not_called()
        return caught.exception

    def test_build_log_holds_the_reason(self):
        log = "=== BUILD: rare:latest ===\n" + STEP_OUTPUT + VERDICT
        self.run_with(BuildFailed("Failed to build image rare:latest: " + VERDICT, log))
        self.assertIn(REASON, (self.exp_dir / "build.log").read_text())

    def test_status_says_failed_and_where_the_output_is(self):
        log = "=== BUILD: rare:latest ===\n" + STEP_OUTPUT + VERDICT
        raised = self.run_with(
            BuildFailed("Failed to build image rare:latest: " + VERDICT, log))
        status = json.loads((self.exp_dir / "status.json").read_text())
        self.assertEqual(status["status"], "failed")
        self.assertIn("build.log", status["error"])
        self.assertIn("build.log", str(raised))

    def test_a_failure_with_no_output_writes_no_build_log(self):
        """Refused before docker ran (a bad GRAFLAG_LIBS, say): nothing to keep,
        and an empty build.log would read as a build that printed nothing."""
        raised = self.run_with(ValueError("GRAFLAG_LIBS must be local or pypi"))
        self.assertFalse((self.exp_dir / "build.log").exists())
        self.assertNotIn("build.log", str(raised))
        status = json.loads((self.exp_dir / "status.json").read_text())
        self.assertEqual(status["status"], "failed")


if __name__ == "__main__":
    unittest.main()
