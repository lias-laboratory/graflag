"""Tests for `graflag list methods`.

The listing is produced by one shell script run on the manager -- deliberately
one SSH call for every method rather than one per method -- and its reply is
parsed line by line. The fixture here is therefore a real `methods/` tree and
the real command, run through a local shell: the quoting, the `METHOD:`/`ENV:`
framing and the `.env` parsing are three things that only fail together, and
a double that returns canned lines would exercise none of them.
"""

import io
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from graflag.cli import _print_methods  # noqa: E402
from graflag.core import GraFlag  # noqa: E402


class MethodTree(unittest.TestCase):
    """A real methods/ directory, listed through the real remote command."""

    def setUp(self):
        self.shared = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.shared, ignore_errors=True)
        (self.shared / "methods").mkdir()

        self.gf = GraFlag.__new__(GraFlag)
        self.gf.config = mock.Mock(remote_shared_dir=str(self.shared))
        self.gf.ssh = mock.Mock()
        self.gf.ssh.execute.side_effect = lambda cmd, **kw: subprocess.run(
            ["sh", "-c", cmd], capture_output=True, text=True
        )

    def method(self, name, env, dockerfile="FROM scratch\n"):
        d = self.shared / "methods" / name
        d.mkdir()
        (d / ".env").write_text(env)
        if dockerfile is not None:
            (d / "Dockerfile").write_text(dockerfile)
        return d

    def image(self, name, dockerfile="FROM scratch\n"):
        """A shared image under images/, the way bond_base lives on the share."""
        d = self.shared / "images" / name
        d.mkdir(parents=True)
        (d / "Dockerfile").write_text(dockerfile)
        return d

    def listed(self):
        return {m.name: m for m in self.gf.list_methods()}


class WhatAMethodDeclares(MethodTree):
    def test_a_method_is_listed_with_what_it_declares(self):
        self.method("taddy", (
            "METHOD_NAME=taddy\n"
            "DESCRIPTION=Transformer-based anomaly detection\n"
            "SOURCE_CODE=https://github.com/yuetan031/TADDY_pytorch\n"
            "SUPPORTED_DATASETS=uci, btc_alpha\n"
            "COMMAND=python3 train_graflag.py\n"
            "_MAX_EPOCH=100\n"))

        m = self.listed()["taddy"]
        self.assertEqual(m.description, "Transformer-based anomaly detection")
        self.assertEqual(m.source_code, "https://github.com/yuetan031/TADDY_pytorch")
        self.assertEqual(m.supported_data, "uci, btc_alpha")
        self.assertEqual(m.parameters, {"_MAX_EPOCH": "100"})
        self.assertTrue(m.has_env)
        self.assertTrue(m.has_dockerfile)

    def test_the_declared_provenance_is_surfaced(self):
        """Whose code produced a number is the one thing a listing of methods
        cannot leave to the reader: `SOURCE_CODE` names the paper's repository
        whether or not the image builds from it, which is exactly how
        dynwalk's reimplementation came to look like the authors' work."""
        self.method("dynwalk", (
            "METHOD_NAME=dynwalk\nDESCRIPTION=NetWalk\n"
            "SOURCE_CODE=https://github.com/chengw07/NetWalk\n"
            "INTEGRATION=reimplementation\n"
            "COMMAND=python3 train_graflag.py\n"))
        self.method("taddy", (
            "METHOD_NAME=taddy\nDESCRIPTION=TADDY\n"
            "SOURCE_CODE=https://github.com/yuetan031/TADDY_pytorch\n"
            "INTEGRATION=upstream\n"
            "COMMAND=python3 train_graflag.py\n"))

        listed = self.listed()
        self.assertEqual(listed["dynwalk"].integration, "reimplementation")
        self.assertEqual(listed["taddy"].integration, "upstream")

    def test_a_method_declaring_no_provenance_claims_neither(self):
        """An .env predating the key leaves the field empty; it does not
        default to `upstream`, which would state the thing nobody wrote."""
        self.method("legacy", ("METHOD_NAME=legacy\nDESCRIPTION=old\n"
                               "SOURCE_CODE=https://example.invalid/x\n"
                               "COMMAND=python3 train_graflag.py\n"))

        self.assertEqual(self.listed()["legacy"].integration, "")


class HandWrittenEnvLines(MethodTree):
    """`.env` files are written by hand, so they contain hand-written idioms.

    The listing used to split each line on the first `=` and keep the rest
    verbatim, so `export`, surrounding quotes and trailing comments all became
    part of the value -- and unlike the same bug in the cluster config, which
    announced itself as `ssh: Bad port '22  # default'`, here it just printed
    something slightly wrong and was believed.
    """

    def test_an_inline_comment_does_not_become_part_of_the_value(self):
        self.method("taddy", (
            "METHOD_NAME=taddy\n"
            "DESCRIPTION=TADDY\n"
            "SOURCE_CODE=https://github.com/yuetan031/TADDY_pytorch\n"
            "INTEGRATION=upstream  # the authors' code, cloned and pinned\n"
            'SUPPORTED_DATASETS="uci, btc_alpha"\n'
            "export COMMAND=python3 train_graflag.py\n"))

        m = self.listed()["taddy"]
        self.assertEqual(m.integration, "upstream")
        self.assertEqual(m.supported_data, "uci, btc_alpha")

    def test_a_quoted_value_survives_a_comment_after_it(self):
        """Both idioms on one line, which is where they used to cancel out.

        Quoting and commenting are each handled, but the quote rule matched on
        "starts and ends with the same quote" -- and a trailing comment means
        the line no longer ends with one. The value kept its quotes, so a
        SOURCE_CODE annotated this way would reach `docker build --build-arg`
        as a URL wrapped in literal quote characters and clone nothing.
        """
        self.method("taddy", (
            "METHOD_NAME=taddy\n"
            'DESCRIPTION="TADDY"  # the paper\'s name for it\n'
            'SOURCE_CODE="https://github.com/yuetan031/TADDY_pytorch"  # pinned\n'
            "COMMAND=python3 train_graflag.py\n"))

        m = self.listed()["taddy"]
        self.assertEqual(m.description, "TADDY")
        self.assertEqual(m.source_code,
                         "https://github.com/yuetan031/TADDY_pytorch")

    def test_a_fragment_in_a_url_is_not_read_as_a_comment(self):
        """The other side of the same rule, and the reason it is spelled as
        "whitespace then #" rather than "any #": bond methods cite a specific
        detector by anchor, and splitting on a bare `#` truncates the link at
        the part that identifies it."""
        url = "https://docs.pygod.org/en/latest/pygod.detector.html#pygod.detector.CoLA"
        self.method("bond_cola", (
            "METHOD_NAME=bond_cola\nDESCRIPTION=CoLA\n"
            f"SOURCE_CODE={url}\n"
            "INTEGRATION=upstream\n"
            "COMMAND=python3 -m graflag_bond.train\n"))

        self.assertEqual(self.listed()["bond_cola"].source_code, url)


class TheTreeItself(MethodTree):
    def test_every_method_is_listed_in_one_ssh_call(self):
        """The GUI polls this listing, and a round trip per method is what the
        batched script exists to avoid. 27 methods, one call."""
        for i in range(27):
            self.method(f"m{i:02d}", (f"METHOD_NAME=m{i:02d}\nDESCRIPTION=d\n"
                                      "SOURCE_CODE=https://example.invalid/x\n"
                                      "COMMAND=python3 train_graflag.py\n"))

        self.assertEqual(len(self.listed()), 27)
        self.assertEqual(self.gf.ssh.execute.call_count, 1)

    def test_a_file_beside_the_method_directories_is_not_a_method(self):
        """methods/ holds prose too -- BOND.md documents the 17 bond methods
        as a family. A file picked up as a method would be listed with no .env
        and offered for a run that cannot start."""
        self.method("taddy", ("METHOD_NAME=taddy\nDESCRIPTION=TADDY\n"
                              "SOURCE_CODE=https://example.invalid/x\n"
                              "COMMAND=python3 train_graflag.py\n"))
        (self.shared / "methods" / "BOND.md").write_text("# The bond family\n")

        self.assertEqual(sorted(self.listed()), ["taddy"])

    def test_a_method_sharing_an_image_is_not_reported_as_incomplete(self):
        """Seventeen bond methods have no Dockerfile of their own by design.

        They build from images/bond_base/Dockerfile, so resolving only
        methods/<name>/Dockerfile would show two thirds of the catalogue as
        "No Dockerfile" -- a listing that reads as broken for the methods
        that work, which is worse than no column at all.
        """
        self.image("bond_base")
        self.method("bond_cola", ("METHOD_NAME=bond_cola\nDESCRIPTION=CoLA\n"
                                  "SOURCE_CODE=https://example.invalid/x\n"
                                  "IMAGE=bond_base\n"
                                  "COMMAND=python3 -m graflag_bond.train\n"),
                    dockerfile=None)

        m = self.listed()["bond_cola"]
        self.assertEqual(m.image, "bond_base")
        self.assertTrue(m.has_dockerfile)

    def test_a_method_naming_an_image_that_is_not_there_is_reported(self):
        """The same honesty the missing-Dockerfile case gets.

        A typo in IMAGE= is a method that cannot build, and the listing is
        where that should be visible rather than at the end of a build.
        """
        self.image("bond_base")
        self.method("bond_typo", ("METHOD_NAME=bond_typo\nDESCRIPTION=typo\n"
                                  "SOURCE_CODE=https://example.invalid/x\n"
                                  "IMAGE=bond_bass\n"
                                  "COMMAND=python3 -m graflag_bond.train\n"),
                    dockerfile=None)

        self.assertFalse(self.listed()["bond_typo"].has_dockerfile)

    def test_a_method_with_no_shared_image_reports_its_own(self):
        """Declaring nothing must keep meaning "build methods/<name>/"."""
        self.image("bond_base")
        self.method("taddy", ("METHOD_NAME=taddy\nDESCRIPTION=TADDY\n"
                              "SOURCE_CODE=https://example.invalid/x\n"
                              "COMMAND=python3 train_graflag.py\n"))

        m = self.listed()["taddy"]
        self.assertEqual(m.image, "")
        self.assertTrue(m.has_dockerfile)

    def test_the_images_sweep_shares_the_one_ssh_call(self):
        """The GUI polls this every 2 seconds; a second call is a second
        round trip per poll, for a directory with one entry in it."""
        self.image("bond_base")
        for i in range(5):
            self.method(f"bond_{i}", (f"METHOD_NAME=bond_{i}\nDESCRIPTION=d\n"
                                      "SOURCE_CODE=https://example.invalid/x\n"
                                      "IMAGE=bond_base\n"
                                      "COMMAND=python3 -m graflag_bond.train\n"),
                        dockerfile=None)

        self.gf.list_methods()
        self.assertEqual(self.gf.ssh.execute.call_count, 1)

    def test_a_method_missing_its_dockerfile_is_listed_as_such(self):
        """Reported, not hidden: a half-finished method directory is a thing
        the listing is supposed to show."""
        self.method("halfdone", ("METHOD_NAME=halfdone\nDESCRIPTION=wip\n"
                                 "SOURCE_CODE=https://example.invalid/x\n"
                                 "COMMAND=python3 train_graflag.py\n"),
                    dockerfile=None)

        self.assertFalse(self.listed()["halfdone"].has_dockerfile)


class Formatting(unittest.TestCase):
    """cli.py owns the output; core.py returns the data."""

    def render(self, *methods):
        out = io.StringIO()
        with redirect_stdout(out):
            _print_methods(list(methods))
        return out.getvalue()

    def method(self, name, integration="", supported="uci"):
        from graflag.models import MethodInfo
        return MethodInfo(name=name, integration=integration,
                          supported_data=supported)

    def test_the_provenance_is_shown_beside_the_name(self):
        text = self.render(self.method("dynwalk", "reimplementation"),
                           self.method("taddy", "upstream"))
        self.assertIn("reimplementation", text)
        self.assertIn("upstream", text)

    def test_an_undeclared_provenance_reads_as_unstated(self):
        """Not blank. A blank column is read as "upstream, presumably" by the
        same reflex that made SOURCE_CODE look like a statement of what ran."""
        text = self.render(self.method("legacy", ""))
        self.assertIn("unstated", text)

    def test_the_columns_line_up(self):
        """Names differ in length by ten characters across the real tree; an
        unaligned provenance column is a column nobody reads."""
        text = self.render(self.method("bond_anomalydae", "upstream"),
                           self.method("gady", "upstream"))
        starts = [line.index("upstream") for line in text.splitlines()
                  if "upstream" in line]
        self.assertEqual(len(set(starts)), 1, text)


if __name__ == "__main__":
    unittest.main()
