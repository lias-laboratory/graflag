"""Dataset sizes in `graflag list datasets` and the dashboard.

A dataset on the share is usually a descriptor stub -- one `metadata.json` of a
few hundred bytes -- until the first run that needs it downloads the payload.
The listing measured it with `du -sm`, which rounds *up* to whole megabytes, so
every stub was reported as a 1 MB dataset and a hydrated 17 MB one looked only
seventeen times larger than an empty directory.

The fixture is a real `datasets/` tree listed through the real remote command,
run in a local shell, as in test_method_listing.py.
"""

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from graflag.core import GraFlag  # noqa: E402


class DatasetSizes(unittest.TestCase):

    def setUp(self):
        self.shared = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.shared, ignore_errors=True)
        (self.shared / "datasets").mkdir()
        self.gf = GraFlag.__new__(GraFlag)
        self.gf.config = mock.Mock(remote_shared_dir=str(self.shared))
        self.gf.ssh = mock.Mock()
        self.gf.ssh.execute.side_effect = lambda cmd, **kw: subprocess.run(
            ["sh", "-c", cmd], capture_output=True, text=True)

    def dataset(self, name, payload_bytes=0):
        d = self.shared / "datasets" / name
        d.mkdir()
        (d / "metadata.json").write_text('{"name": "%s", "files": []}' % name)
        if payload_bytes:
            (d / "data.bin").write_bytes(b"\0" * payload_bytes)
        return d

    def sizes(self):
        return {d.name: d.size_mb for d in self.gf.list_datasets()}

    def test_a_descriptor_stub_is_not_reported_as_a_megabyte(self):
        """The regression: `du -sm` rounded a 40-byte stub up to 1 MB."""
        self.dataset("stub")
        self.assertLess(self.sizes()["stub"], 0.1)

    def test_a_hydrated_dataset_reports_its_size(self):
        self.dataset("hydrated", payload_bytes=3 * 1024 * 1024)
        self.assertAlmostEqual(self.sizes()["hydrated"], 3.0, delta=0.1)

    def test_stub_and_payload_are_distinguishable(self):
        self.dataset("stub")
        self.dataset("hydrated", payload_bytes=2 * 1024 * 1024)
        sizes = self.sizes()
        self.assertGreater(sizes["hydrated"], 10 * max(sizes["stub"], 0.001))


if __name__ == "__main__":
    unittest.main()
