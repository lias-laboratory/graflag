"""Tests for remote command construction.

Every GraFlag operation ends up as a string interpreted by a root shell on the
swarm manager, so these tests pin down exactly what that shell receives. They
use a fake ``ssh`` on PATH that records its final argument -- the command word
ssh would hand to the remote shell -- and then run that string through a real
``sh`` to assert on the effect.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from graflag.ssh import SSHManager, remote_path  # noqa: E402


FAKE_SSH = """#!/bin/sh
# Records the last argument: what ssh sends to the remote shell.
eval "last=\\${$#}"
printf '%s' "$last" > "$FAKE_SSH_CAPTURE"
"""


class RemoteCommandTestCase(unittest.TestCase):
    """Base class providing a fake ssh and a capture file."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)

        bindir = Path(self.tmp) / "bin"
        bindir.mkdir()
        fake = bindir / "ssh"
        fake.write_text(FAKE_SSH)
        fake.chmod(0o755)

        capdir = Path(self.tmp) / "cap"
        capdir.mkdir()
        self.capture = str(capdir / "capture")
        self._old_path = os.environ.get("PATH", "")
        self._old_cap = os.environ.get("FAKE_SSH_CAPTURE")
        os.environ["PATH"] = f"{bindir}:{self._old_path}"
        os.environ["FAKE_SSH_CAPTURE"] = self.capture
        self.addCleanup(self._restore_env)

        self.ssh = SSHManager(manager_ip="10.0.0.1", ssh_port="22", ssh_key=None)

    def _restore_env(self):
        os.environ["PATH"] = self._old_path
        if self._old_cap is None:
            os.environ.pop("FAKE_SSH_CAPTURE", None)
        else:
            os.environ["FAKE_SSH_CAPTURE"] = self._old_cap

    def remote_sees(self, command):
        """Run `command` through SSHManager, return what the manager receives."""
        self.ssh.execute(command)
        return Path(self.capture).read_text()


class ExecutePassesCommandVerbatim(RemoteCommandTestCase):
    """execute() must not let a local shell reinterpret the command."""

    def test_command_reaches_remote_unchanged(self):
        cmd = "echo 'single' \"double\" $VAR `backtick` \\backslash"
        self.assertEqual(self.remote_sees(cmd), cmd)

    def test_newlines_and_heredoc_survive(self):
        cmd = "cat > /tmp/x << 'EOF'\nline one\nline two\nEOF"
        self.assertEqual(self.remote_sees(cmd), cmd)

    def test_heredoc_delimiter_keeps_its_quotes(self):
        """A quoted delimiter is what stops the remote shell expanding the body.

        Regression: the command used to be wrapped in single quotes locally,
        which turned ``<< 'EOF'`` into ``<< EOF`` and made the manager expand
        ``$VAR`` and run ``$(...)`` found in build output.
        """
        received = self.remote_sees("cat > /tmp/x << 'BUILDEOF'\nbody\nBUILDEOF")
        self.assertIn("<< 'BUILDEOF'", received)
        self.assertNotIn("<< BUILDEOF", received)


class HeredocBodyIsLiteral(RemoteCommandTestCase):
    """Content written via heredoc must land on disk byte-for-byte."""

    def _write_through_remote_shell(self, body):
        target = Path(self.tmp) / "out.txt"
        cmd = f"cat > {remote_path(str(target))} << 'BUILDEOF'\n{body}\nBUILDEOF"
        received = self.remote_sees(cmd)
        subprocess.run(["sh", "-c", received], check=True)
        return target.read_text()

    def test_build_log_with_quotes_is_not_truncated(self):
        """Regression: `pip install 'foo>=1.0'` truncated the whole log."""
        body = "Step 3/7 : RUN pip install 'foo>=1.0'\n ---> done"
        self.assertEqual(self._write_through_remote_shell(body), body + "\n")

    def test_build_log_shell_metacharacters_are_not_expanded(self):
        """Regression: $VAR was expanded and `cmd` executed on the manager."""
        body = "cost $5 for `id -un` in $HOME and $(hostname)"
        self.assertEqual(self._write_through_remote_shell(body), body + "\n")

    def test_no_stray_files_created_locally(self):
        """Regression: a leaked `>` redirect created files on the client."""
        workdir = Path(self.tmp) / "work"
        workdir.mkdir()
        os.chdir(workdir)
        before = set(os.listdir(workdir))
        self.remote_sees("cat << 'EOF'\nRUN pip install 'foo>=1.0'\nEOF")
        self.assertEqual(set(os.listdir(workdir)) - before, set())


class RemotePathQuoting(unittest.TestCase):
    """remote_path() must neutralise names that reach a root shell."""

    def test_plain_path_is_unchanged(self):
        self.assertEqual(
            remote_path("/shared", "experiments", "exp__a__b__1"),
            "/shared/experiments/exp__a__b__1",
        )

    def test_absolute_prefix_preserved(self):
        self.assertTrue(remote_path("/shared", "datasets").startswith("/shared"))

    def test_relative_path_stays_relative(self):
        self.assertEqual(remote_path("experiments", "x"), "experiments/x")

    def test_empty_components_are_dropped(self):
        self.assertEqual(remote_path("/shared", ""), "/shared")

    def test_injection_is_quoted(self):
        quoted = remote_path("/shared/experiments", "x; rm -rf /")
        self.assertTrue(quoted.startswith("'"))
        self.assertIn("rm -rf /", quoted)


class InjectionIsNeutralised(RemoteCommandTestCase):
    """An experiment name from a URL must not become a second command."""

    def test_rm_rf_does_not_execute_injected_command(self):
        canary = Path(self.tmp) / "CANARY"
        evil = f"exp__a__b__1; touch {canary}; echo "

        victim = Path(self.tmp) / "victim"
        victim.mkdir()
        cmd = f"rm -rf {remote_path(str(victim), evil)}"
        received = self.remote_sees(cmd)
        subprocess.run(["sh", "-c", received], check=True)

        self.assertFalse(canary.exists(), "injected command executed on manager")
        self.assertTrue(victim.exists(), "unrelated path was destroyed")


class PathHelpersQuote(RemoteCommandTestCase):
    """The convenience wrappers must quote the paths they build."""

    def test_path_exists_quotes_name(self):
        self.ssh.path_exists("/shared", "experiments/a b; rm -rf /")
        received = Path(self.capture).read_text()
        self.assertTrue(received.startswith("test -e '"))

    def test_read_file_quotes_name(self):
        self.ssh.read_file("/shared", "x'y")
        self.assertIn("'", Path(self.capture).read_text())

    def test_mkdir_quotes_name(self):
        self.ssh.mkdir("/shared", "a b")
        self.assertIn("'/shared/a b'", Path(self.capture).read_text())


class RsyncTrailingSlashIsPreserved(unittest.TestCase):
    """sync() asks for the contents of a directory, not the directory.

    rsync distinguishes "src/" (copy what is inside src into dest) from "src"
    (copy src itself under dest), and core.sync() asks for the first by
    appending a slash. Path() normalises that slash away, so the manager
    received the second spelling: `graflag sync` in methods/bond_dmgd wrote
    methods/bond_dmgd/bond_dmgd/.env -- a path load_method_env never reads --
    and reported [OK]. These tests capture the argv the real rsync would get
    and replay it against a local destination, so they assert on where the
    file lands rather than on the shape of a string.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        bindir = Path(self.tmp) / "bin"
        bindir.mkdir()
        self.capture = str(Path(self.tmp) / "argv")
        fake = bindir / "rsync"
        fake.write_text(
            "#!/bin/sh\n"
            '# Records one argument per line: what rsync would have been given.\n'
            ': > "$FAKE_RSYNC_CAPTURE"\n'
            'for a in "$@"; do printf \'%s\\n\' "$a" >> "$FAKE_RSYNC_CAPTURE"; done\n'
        )
        fake.chmod(0o755)

        # copy_files() mkdir -p's the destination's parent before rsyncing, so
        # the fake ssh has to be here too or the test dials 10.0.0.1 for real.
        stub_ssh = bindir / "ssh"
        stub_ssh.write_text("#!/bin/sh\nexit 0\n")
        stub_ssh.chmod(0o755)

        self._old_path = os.environ.get("PATH", "")
        self._old_cap = os.environ.get("FAKE_RSYNC_CAPTURE")
        os.environ["PATH"] = f"{bindir}:{self._old_path}"
        os.environ["FAKE_RSYNC_CAPTURE"] = self.capture
        self.addCleanup(self._restore_env)

        self.src = Path(self.tmp) / "bond_dmgd"
        self.src.mkdir()
        (self.src / ".env").write_text("METHOD_NAME=bond_dmgd\n_GPU=-1\n")

        self.ssh = SSHManager(manager_ip="10.0.0.1", ssh_port="22", ssh_key=None)

    def _restore_env(self):
        os.environ["PATH"] = self._old_path
        if self._old_cap is None:
            os.environ.pop("FAKE_RSYNC_CAPTURE", None)
        else:
            os.environ["FAKE_RSYNC_CAPTURE"] = self._old_cap

    def _sources_sent(self, source):
        """Return the source arguments rsync was handed."""
        self.ssh.copy_files(
            source_paths=[source], dest_path="/shared/methods/bond_dmgd/",
            recursive=True, from_remote=False,
        )
        argv = Path(self.capture).read_text().splitlines()
        return [a for a in argv if a.startswith(str(self.tmp))]

    def _replay_locally(self, source):
        """Run the captured source spelling through the real rsync."""
        sources = self._sources_sent(source)
        dest = Path(self.tmp) / "dest"
        dest.mkdir()
        env = dict(os.environ, PATH=self._old_path)  # the real rsync, not the fake
        subprocess.run(["rsync", "-a", *sources, f"{dest}/"], check=True, env=env)
        return dest

    def test_trailing_slash_survives_to_rsync(self):
        self.assertEqual(self._sources_sent(f"{self.src}/"), [f"{self.src}/"])

    def test_contents_land_directly_in_the_destination(self):
        dest = self._replay_locally(f"{self.src}/")
        self.assertTrue(
            (dest / ".env").exists(),
            "sync wrote methods/<name>/<name>/.env, which nothing reads",
        )
        self.assertFalse((dest / "bond_dmgd").exists())

    def test_no_trailing_slash_still_nests(self):
        """copy -s ./data --dest datasets must keep creating datasets/data."""
        dest = self._replay_locally(str(self.src))
        self.assertTrue((dest / "bond_dmgd" / ".env").exists())
        self.assertFalse((dest / ".env").exists())


class StdinIsNotInherited(unittest.TestCase):
    """No ssh or rsync child may read this process's stdin.

    ssh forwards whatever it can read from its own stdin to the remote command,
    and reads eagerly. Under the MCP server stdin is the protocol, so an
    inherited stdin let a remote command swallow the client's next requests;
    under a shell loop it swallowed the rest of the loop's input. Each test runs
    GraFlag in a child whose stdin holds bytes, behind a fake that records what
    it could read.
    """

    PAYLOAD = b'{"jsonrpc": "2.0", "id": 7, "method": "tools/list"}\n'

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        bindir = Path(self.tmp) / "bin"
        bindir.mkdir()
        self.seen = Path(self.tmp) / "seen"
        for tool in ("ssh", "rsync"):
            fake = bindir / tool
            fake.write_text(f'#!/bin/sh\ncat >> "{self.seen}"\nexit 0\n')
            fake.chmod(0o755)
        self.env = dict(os.environ, PATH=f"{bindir}:{os.environ.get('PATH', '')}",
                        PYTHONPATH=str(Path(__file__).resolve().parents[1]))
        (Path(self.tmp) / "src").mkdir()

    def _run_with_stdin(self, code):
        subprocess.run([sys.executable, "-c", code], input=self.PAYLOAD,
                       env=self.env, check=True, timeout=30)
        return self.seen.read_bytes() if self.seen.exists() else b""

    def test_execute_does_not_read_stdin(self):
        seen = self._run_with_stdin(
            "from graflag.ssh import SSHManager\n"
            "SSHManager('10.0.0.1').execute('true')\n")
        self.assertEqual(seen, b"", "ssh read the caller's stdin")

    def test_rsync_does_not_read_stdin(self):
        seen = self._run_with_stdin(
            "from graflag.ssh import SSHManager\n"
            f"SSHManager('10.0.0.1').copy_files([{str(Path(self.tmp) / 'src')!r}],"
            " '/shared/x/', from_remote=False)\n"
            "SSHManager('10.0.0.1').copy_files(['/shared/x'],"
            f" {str(Path(self.tmp) / 'dest')!r}, from_remote=True)\n")
        self.assertEqual(seen, b"", "ssh or rsync read the caller's stdin")

    def test_docker_tunnel_does_not_read_stdin(self):
        from unittest import mock
        from graflag.docker_ops import DockerManager

        manager = DockerManager(SSHManager("10.0.0.1"), config=None)
        with mock.patch("graflag.docker_ops.subprocess.Popen") as popen, \
                mock.patch("graflag.docker_ops.time.sleep"):
            popen.return_value.poll.return_value = 255      # ssh exited
            popen.return_value.stderr.read.return_value = b"refused"
            with self.assertRaises(RuntimeError):
                manager._connect()
        self.assertIs(popen.call_args.kwargs.get("stdin"), subprocess.DEVNULL)
        manager._tunnel_proc = None


class NonInteractiveSsh(unittest.TestCase):
    """With nobody at a terminal, ssh must fail rather than prompt."""

    def test_batch_mode_adds_the_options(self):
        args = SSHManager("10.0.0.1", batch_mode=True)._ssh_args()
        self.assertIn("BatchMode=yes", args)
        self.assertIn("ConnectTimeout=15", args)
        self.assertEqual(args[-1], "root@10.0.0.1")

    def test_default_can_still_prompt(self):
        """A CLI user whose key has a passphrase must still be asked for it."""
        self.assertNotIn("BatchMode=yes", SSHManager("10.0.0.1")._ssh_args())

    def test_the_tunnel_gets_them_too(self):
        from unittest import mock
        from graflag.docker_ops import DockerManager

        manager = DockerManager(SSHManager("10.0.0.1", batch_mode=True), config=None)
        with mock.patch("graflag.docker_ops.subprocess.Popen") as popen, \
                mock.patch("graflag.docker_ops.time.sleep"):
            popen.return_value.poll.return_value = 255
            popen.return_value.stderr.read.return_value = b""
            with self.assertRaises(RuntimeError):
                manager._connect()
        self.assertIn("BatchMode=yes", popen.call_args.args[0])
        manager._tunnel_proc = None

    def test_graflag_non_interactive_sets_batch_mode(self):
        from graflag.core import GraFlag

        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        config = Path(tmp) / "config.env"
        config.write_text("MANAGER_IP=10.0.0.1\n")
        self.assertTrue(GraFlag(str(config), interactive=False).ssh.batch_mode)
        self.assertFalse(GraFlag(str(config)).ssh.batch_mode)


FAKE_SSH_EXEC = """#!/bin/sh
# Runs what the manager would run, here: effects, not strings, are asserted on.
eval "last=\\${$#}"
exec sh -c "$last"
"""


class MethodEnvPathIsQuoted(unittest.TestCase):
    """load_method_env() put the method name into two remote commands bare."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        bindir = Path(self.tmp) / "bin"
        bindir.mkdir()
        (bindir / "ssh").write_text(FAKE_SSH_EXEC)
        (bindir / "ssh").chmod(0o755)
        self._old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{bindir}:{self._old_path}"
        self.addCleanup(os.environ.__setitem__, "PATH", self._old_path)
        self.shared = Path(self.tmp) / "shared"
        self.ssh = SSHManager("10.0.0.1")

    def test_a_name_with_a_space_and_a_quote_is_read(self):
        from graflag.utils import load_method_env

        name = "odd name's"
        method = self.shared / "methods" / name
        method.mkdir(parents=True)
        (method / ".env").write_text("METHOD_NAME=odd\n_GPU=0\n")
        self.assertEqual(load_method_env(self.ssh, str(self.shared), name),
                         {"METHOD_NAME": "odd", "_GPU": "0"})

    def test_an_injected_command_does_not_run(self):
        from graflag.utils import load_method_env

        witness = Path(self.tmp) / "pwned"
        load_method_env(self.ssh, str(self.shared), f"x; touch {witness}; #")
        self.assertFalse(witness.exists(), "the method name ran as a command")


if __name__ == "__main__":
    unittest.main()
