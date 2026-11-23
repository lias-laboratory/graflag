"""What the client assumes about the machine it runs on.

A reviewer ran the suite on native Windows: most of it passed, and what
failed was Unix-specific. Two of those assumptions were defects in the client
itself, not in the tests: the ssh tunnel to the manager's Docker socket and
the log follower passed `preexec_fn`, which Python rejects outright on
Windows, so no service operation could start there; and a copy or sync without
a local rsync died with a bare FileNotFoundError.
"""

import re
import unittest
from pathlib import Path
from unittest import mock

from graflag import docker_ops, ssh

DOCKER_OPS = Path(docker_ops.__file__).read_text()


class ChildOptions(unittest.TestCase):
    def test_no_preexec_fn_off_posix(self):
        with mock.patch.object(docker_ops.os, "name", "nt"):
            self.assertEqual(docker_ops._child_options(), {})

    def test_posix_children_die_with_the_parent(self):
        with mock.patch.object(docker_ops.os, "name", "posix"):
            self.assertEqual(docker_ops._child_options(),
                             {"preexec_fn": docker_ops._die_with_parent})

    def test_no_call_site_passes_preexec_fn_itself(self):
        """Every Popen goes through _child_options, so a new one cannot regress."""
        self.assertEqual(re.findall(r"preexec_fn\s*=", DOCKER_OPS), [])


class RsyncIsRequiredLoudly(unittest.TestCase):
    def setUp(self):
        self.mgr = ssh.SSHManager("192.0.2.1")

    def test_missing_rsync_names_itself_and_a_way_out(self):
        with mock.patch.object(ssh.shutil, "which", return_value=None), \
             mock.patch.object(ssh.subprocess, "run") as run:
            with self.assertRaises(RuntimeError) as ctx:
                self.mgr.copy_files(["a"], "datasets")
        self.assertIn("rsync", str(ctx.exception))
        self.assertIn("WSL", str(ctx.exception))
        run.assert_not_called()

    def test_both_directions_are_checked(self):
        with mock.patch.object(ssh.shutil, "which", return_value=None), \
             mock.patch.object(ssh.subprocess, "run") as run:
            with self.assertRaises(RuntimeError):
                self.mgr.copy_files(["experiments/x"], "/tmp/x", from_remote=True)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
