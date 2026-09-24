"""What `docker build` on the manager actually receives.

A method declares its upstream once, in `methods/<name>/.env`, and the build
picks it up as `--build-arg`. Two things can go wrong between those points and
neither shows up as an error: a key that is never forwarded leaves `ARG
SOURCE_REF` empty, and a value the remote shell splits arrives truncated. Both
end in an image built from the wrong source that reports success, so these
tests run the command GraFlag composes through a real shell against a fake
`docker` and read back the argv it was handed.
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

from graflag.docker_ops import BUILD_ARG_KEYS, DockerManager  # noqa: E402


FAKE_DOCKER = """#!/bin/sh
# One argv element per line, so a value the shell split shows up as two.
for a in "$@"; do printf '%s\\n' "$a" >> "$FAKE_DOCKER_ARGV"; done
"""


class RecordingSSH:
    """An SSHManager double: records commands, answers the .env reads."""

    def __init__(self, env_text):
        self.env_text = env_text
        self.commands = []

    def execute(self, command, **kwargs):
        self.commands.append(command)
        if command.startswith("test -f"):
            out = "exists"
        elif command.startswith("cat "):
            out = self.env_text
        else:
            out = ""
        return SimpleNamespace(returncode=0, stdout=out, stderr="")


class BuildArgsReachDocker(unittest.TestCase):
    ENV = (
        "METHOD_NAME=gady\n"
        "SOURCE_CODE=https://github.com/mufeng-74/GADY\n"
        "SOURCE_REF=1e8e4503e238f0a6c093c3aba946085aa3959410\n"
        "_LR=0.0001\n"
    )

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

    def docker_argv(self, env_text=None, method="gady"):
        """Build `method`, run the composed command, return docker's argv."""
        ssh = RecordingSSH(self.ENV if env_text is None else env_text)
        config = SimpleNamespace(remote_shared_dir="/shared", manager_ip="10.0.0.1")
        docker = DockerManager(ssh, config)
        docker.build_method_image(method)

        build_cmd = next(c for c in ssh.commands if c.startswith("docker build"))
        subprocess.run(["sh", "-c", build_cmd], check=True)
        return Path(self.argv_file).read_text().splitlines()

    #: Passed by the orchestrator, not read from the method's .env, so it is
    #: not one of the keys these tests are about. It is covered by
    #: test_local_libs.py.
    NOT_FROM_ENV = {"GRAFLAG_LIBS"}

    def from_env(self, argv):
        """The `--build-arg` keys that came out of the method's .env."""
        keys = {argv[i + 1].partition("=")[0]
                for i, a in enumerate(argv) if a == "--build-arg"}
        return keys - self.NOT_FROM_ENV

    def test_the_pinned_commit_is_forwarded(self):
        """Without this the ARG is empty and `checkout --detach` takes HEAD."""
        self.assertIn(
            "--build-arg", self.docker_argv())
        self.assertIn(
            "SOURCE_REF=1e8e4503e238f0a6c093c3aba946085aa3959410",
            self.docker_argv())

    def test_the_repository_is_forwarded(self):
        self.assertIn(
            "SOURCE_CODE=https://github.com/mufeng-74/GADY", self.docker_argv())

    def test_a_value_with_a_fragment_arrives_whole(self):
        """bond methods point SOURCE_CODE at a docs anchor.

        `https://docs.pygod.org/...#pygod.detector.CoLA` has to survive two
        readers that both treat `#` as the start of a comment: the .env parser
        (utils.parse_env_line only strips a comment introduced by whitespace,
        which is why this key keeps its fragment) and, if it were ever written
        into a command at the start of a word, the manager's shell.
        """
        url = ("https://docs.pygod.org/en/latest/generated/"
               "pygod.detector.CoLA.html#pygod.detector.CoLA")
        argv = self.docker_argv(
            f"METHOD_NAME=bond_cola\nSOURCE_CODE={url}\n", method="bond_cola")
        self.assertIn(f"SOURCE_CODE={url}", argv)
        self.assertTrue(any(a.endswith("/shared/") for a in argv),
                        f"the build context is missing; argv was {argv}")

    def test_a_value_cannot_run_a_command_on_the_manager(self):
        """The .env is hand-edited text that reaches a root shell.

        ssh.execute() hands the whole build command to the manager as one word
        and the *remote* shell parses it, so an unquoted value is an injection
        point, not a formatting detail -- the rule tests/test_ssh.py pins down
        for every other remote command. A `;` in SOURCE_CODE must arrive as
        four characters of an argument, not as a second command.
        """
        marker = Path(self.tmp) / "executed"
        env = (f"METHOD_NAME=gady\n"
               f"SOURCE_CODE=https://example.invalid/x.git; touch {marker}\n")
        argv = self.docker_argv(env)
        self.assertFalse(marker.exists(),
                         "a value from the .env ran as a command on the manager")
        self.assertIn(f"SOURCE_CODE=https://example.invalid/x.git; touch {marker}",
                      argv)

    def test_a_method_declaring_nothing_gets_no_build_args(self):
        """An empty or absent key must not become `--build-arg KEY=`.

        Docker reads that as "take KEY from the environment", which on the
        manager is whatever happens to be exported -- a worse failure than no
        value at all.
        """
        argv = self.docker_argv("METHOD_NAME=dynwalk\nSOURCE_CODE=\n", method="dynwalk")
        self.assertNotIn("SOURCE_CODE=", argv)
        self.assertEqual(self.from_env(argv), set())

    def test_the_forwarded_keys_are_the_declared_ones(self):
        """Method parameters are `_`-prefixed and belong to the run.

        Forwarding them too would put every hyperparameter in the image's build
        cache key, so a change of learning rate would rebuild from scratch.
        """
        forwarded = self.from_env(self.docker_argv())
        self.assertNotIn("_LR", forwarded)
        self.assertLessEqual(forwarded, set(BUILD_ARG_KEYS))


class BuildLogRecordsWhatWasBuilt(unittest.TestCase):
    """The experiment directory has to say which source went into the image.

    `docker build` never echoes a `--build-arg` value, so a build.log holding
    only docker's output records `git -C src checkout --detach ${SOURCE_REF}`
    with the variable unexpanded, and the RUN step that installs the libraries
    prints *both* branches of its `case`. A reader of the experiment therefore
    could not name the commit or say whether the runner came from the share or
    from PyPI -- the two facts `SOURCE_REF` and `GRAFLAG_LIBS` exist to fix.
    Both lines went to the operator's terminal alone until this was added; 37
    build.log files on the cluster contain neither.
    """

    ENV = BuildArgsReachDocker.ENV

    def build_log(self, env_text=None, libs=None, method="gady"):
        ssh = RecordingSSH(self.ENV if env_text is None else env_text)
        config = SimpleNamespace(remote_shared_dir="/shared",
                                 manager_ip="10.0.0.1")
        if libs is not None:
            config.graflag_libs = libs
        docker = DockerManager(ssh, config)
        return docker.build_method_image(method)

    def test_the_log_names_the_commit_the_image_was_built_from(self):
        self.assertIn("1e8e4503e238f0a6c093c3aba946085aa3959410",
                      self.build_log())

    def test_the_log_says_where_the_graflag_libraries_came_from(self):
        self.assertIn("/shared/libs", self.build_log(libs="local"))
        self.assertIn("PyPI", self.build_log(libs="pypi"))

    def test_a_method_with_no_pin_still_records_its_libraries(self):
        """An unpinned method must not take the libs line down with it."""
        log = self.build_log("METHOD_NAME=dynwalk\n", method="dynwalk")
        self.assertNotIn("Upstream pinned at", log)
        self.assertIn("GraFlag libraries from", log)

    def test_the_provenance_precedes_docker_own_output(self):
        """It is a header, not a line buried in a few thousand of docker's."""
        log = self.build_log()
        self.assertLess(log.index("Upstream pinned at"),
                        log.index("=== BUILD:"))


class ShareWithoutDockerignore(RecordingSSH):
    """The manager's share as it actually was: no .dockerignore at its root."""

    def execute(self, command, **kwargs):
        result = super().execute(command, **kwargs)
        if command.startswith("test -f") and ".dockerignore" in command:
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        return result


class TheBuildContextIsFiltered(unittest.TestCase):
    """`.dockerignore` lives in git and has to reach the share to do anything.

    The build context is SHARED_DIR itself, so that one file is the only thing
    keeping `datasets/` and `experiments/` out of the tarball docker streams
    before the first instruction runs. Nothing in GraFlag puts it there --
    `sync` copies a method directory, `sync --lib` copies a library, neither
    touches the root -- so a share populated by either has the file in the
    checkout and not on the cluster.

    The build then succeeds anyway, at full context size, which is why this
    went unnoticed through every run of the method matrix. Docker's own
    accounting on this cluster, for the same share: `Sending build context to
    Docker daemon  3.651GB` without the file, `804.4kB` with it.
    """

    ENV = BuildArgsReachDocker.ENV

    def build_log(self, ssh):
        config = SimpleNamespace(remote_shared_dir="/shared",
                                 manager_ip="10.0.0.1")
        return DockerManager(ssh, config).build_method_image("gady")

    def test_a_share_missing_it_is_called_out(self):
        self.assertIn("No .dockerignore",
                      self.build_log(ShareWithoutDockerignore(self.ENV)))

    def test_the_warning_says_how_to_fix_it(self):
        """A warning naming no remedy is one more line nobody acts on."""
        self.assertIn("graflag copy -s ./.dockerignore --dest .",
                      self.build_log(ShareWithoutDockerignore(self.ENV)))

    def test_it_lands_in_build_log_not_only_the_terminal(self):
        """Same reason as the provenance lines: the experiment keeps a record.

        `build_method_image` returns exactly what is written to build.log, so
        reading the warning back out of it is the assertion that it persists.
        """
        log = self.build_log(ShareWithoutDockerignore(self.ENV))
        self.assertLess(log.index("No .dockerignore"), log.index("=== BUILD:"))

    def test_a_share_that_has_it_stays_quiet(self):
        self.assertNotIn("dockerignore", self.build_log(RecordingSSH(self.ENV)))

    def test_the_probe_asks_about_the_context_root(self):
        """Not some other path that happens to have one."""
        ssh = RecordingSSH(self.ENV)
        self.build_log(ssh)
        self.assertEqual([c for c in ssh.commands if ".dockerignore" in c],
                         ["test -f /shared/.dockerignore"])


if __name__ == "__main__":
    unittest.main()
