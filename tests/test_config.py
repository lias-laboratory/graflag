"""Which file becomes the GraFlag config, and what happens when it should not.

`get_config_path()` prefers a `.env` in the working directory over the user
config whenever that file defines MANAGER_IP. That rule is deliberate -- it is
how a per-project cluster is selected -- but it means any `.env` sitting in a
directory someone runs `graflag` from takes over the connection settings.

This repository shipped such a file. `.gitignore` listed `.env`, which reads
like protection and is not: the rule has no effect on a path that is already
tracked, so the file stayed in the tree, naming a manager and an SSH key that
no longer existed. Every command run from the repository root resolved to it
and died with "Permission denied (publickey)", while the same command one
directory up worked. The resolution logic was correct throughout and had no
tests; these are those tests.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from graflag.config import GraflagConfig, get_config_path  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
GRAFLAG_ENV = "MANAGER_IP=10.0.0.1\nSSH_KEY=~/.ssh/id_ed25519\n"
FOREIGN_ENV = "DATABASE_URL=postgres://localhost/app\nSECRET_KEY=hunter2\n"


class WorkingDirectoryEnv(unittest.TestCase):
    """The cwd `.env` rule, from both sides."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.old_cwd = os.getcwd()
        self.addCleanup(os.chdir, self.old_cwd)
        os.chdir(self.tmp)

    def _write(self, text):
        Path(self.tmp, ".env").write_text(text)

    def test_graflag_env_in_cwd_wins(self):
        self._write(GRAFLAG_ENV)
        self.assertEqual(get_config_path(), Path(self.tmp) / ".env")

    def test_foreign_env_in_cwd_is_ignored(self):
        """An unrelated project's .env must not become the cluster config.

        Without the MANAGER_IP check this returned the file, and `graflag
        setup` then refused to run because "the config exists" -- leaving no
        way forward but deleting someone else's file.
        """
        self._write(FOREIGN_ENV)
        self.assertNotEqual(get_config_path(), Path(self.tmp) / ".env")

    def test_no_env_in_cwd_falls_through_to_user_config(self):
        self.assertEqual(get_config_path().name, "config.env")

    def test_explicit_config_outranks_the_cwd_env(self):
        self._write(GRAFLAG_ENV)
        other = Path(self.tmp) / "prod.env"
        other.write_text("MANAGER_IP=10.9.9.9\n")
        self.assertEqual(get_config_path(str(other)), other)

    def test_missing_explicit_config_is_an_error(self):
        """Not a silent fallback: it surfaced as "Missing MANAGER_IP"."""
        self._write(GRAFLAG_ENV)
        with self.assertRaises(ValueError) as caught:
            GraflagConfig(str(Path(self.tmp) / "nope.env"))
        self.assertIn("not found", str(caught.exception))
        # and the cwd .env must not have been used behind the caller's back
        self.assertNotIn("10.0.0.1", str(caught.exception))


class RepositoryShipsNoEnv(unittest.TestCase):
    """The repository itself must not carry a file that hijacks resolution."""

    def test_no_env_at_the_repository_root(self):
        self.assertFalse(
            (REPO / ".env").exists(),
            ".env at the repo root silently overrides ~/.config/graflag/"
            "config.env for every command run from here. Keep the sample in "
            ".env.example, which get_config_path() never reads.",
        )

    def test_env_is_not_tracked_by_git(self):
        """`.gitignore` does not apply to a path that is already tracked."""
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", ".env"],
            cwd=REPO, capture_output=True, text=True,
        )
        if "not a git repository" in tracked.stderr:
            self.skipTest("not a git checkout")
        self.assertNotEqual(tracked.returncode, 0, "`.env` is tracked in git")

    def test_the_sample_is_shipped_instead(self):
        sample = REPO / ".env.example"
        self.assertTrue(sample.exists(), "no .env.example for users to copy")
        self.assertIn("MANAGER_IP", sample.read_text())


if __name__ == "__main__":
    unittest.main()
