"""Tests that the distribution carries what an installed graflag needs.

`pip install -e .` runs straight from the checkout, so a file missing from
`package_data` goes unnoticed until someone installs the wheel: the vendored
Vue and Socket.IO and the dashboard's images were once left out, and the
dashboard a PyPI install served could not start.
"""

import ast
import unittest
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]


def setup_arguments():
    """The keyword arguments of the setup() call in setup.py, as AST nodes.

    Read rather than run: setuptools is not installed in a fresh Python 3.12
    environment -- CI's included -- and the suite needs nothing outside the
    standard library.
    """
    tree = ast.parse((ROOT / "setup.py").read_text())
    call = next(node for node in ast.walk(tree)
                if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "setup")
    return {kw.arg: kw.value for kw in call.keywords}


def setup_value(name):
    """A literal argument of setup(), e.g. package_data or version."""
    return ast.literal_eval(setup_arguments()[name])


def data_files(package_dir):
    """Every non-Python file a package reads at run time, relative to it."""
    return sorted(
        PurePosixPath(p.relative_to(package_dir).as_posix())
        for p in package_dir.rglob("*")
        if p.is_file() and p.suffix not in (".py", ".pyc") and "__pycache__" not in p.parts
    )


class PackageData(unittest.TestCase):

    def assert_shipped(self, package, package_dir):
        patterns = setup_value("package_data")[package]
        files = data_files(package_dir)
        self.assertTrue(files, f"no data files found under {package_dir}")
        missing = [str(f) for f in files if not any(f.match(p) for p in patterns)]
        self.assertEqual(missing, [], f"{package}: not covered by package_data")

    def test_every_dashboard_file_is_shipped(self):
        self.assert_shipped("graflag.gui", ROOT / "graflag" / "gui")

    def test_every_devcluster_file_is_shipped(self):
        self.assert_shipped("graflag.devcluster", ROOT / "graflag" / "devcluster")


class Distribution(unittest.TestCase):

    def test_the_test_suite_is_not_installed(self):
        """find_packages() alone picks up tests/ and installs a top-level
        `tests` package into site-packages."""
        call = setup_arguments()["packages"]
        self.assertIsInstance(call, ast.Call)
        self.assertEqual(getattr(call.func, "id", None), "find_packages")
        exclude = next((ast.literal_eval(kw.value) for kw in call.keywords if kw.arg == "exclude"), ())
        self.assertIn("tests", exclude)
        self.assertIn("tests.*", exclude)

    def test_links_point_at_the_laboratory(self):
        urls = [setup_value("url"), *setup_value("project_urls").values()]
        for url in urls:
            self.assertTrue(
                url.startswith(("https://github.com/lias-laboratory/",
                                "https://lias-laboratory.github.io/")), url)

    def test_the_version_is_the_packages_own(self):
        """setup.py and graflag.__version__ once disagreed (1.0.1 and 1.0.0)."""
        init = (ROOT / "graflag" / "__init__.py").read_text()
        declared = next(line.split("=", 1)[1].strip().strip("\"'")
                        for line in init.splitlines() if line.startswith("__version__"))
        self.assertEqual(setup_value("version"), declared)



class TheMcpExtra(unittest.TestCase):
    """`graflag mcp` needs the SDK; the rest of GraFlag must not."""

    def requirement(self):
        (req,) = setup_value("extras_require")["mcp"]
        return req

    def test_the_sdk_is_optional(self):
        self.assertNotIn("mcp", " ".join(setup_value("install_requires")))
        self.assertTrue(self.requirement().startswith("mcp"))

    def test_it_is_held_below_2(self):
        """mcp 2.0 removed mcp.server.fastmcp, which the server imports."""
        self.assertIn("<2", self.requirement())

    def test_only_where_the_sdk_installs(self):
        self.assertIn("python_version >= '3.10'", self.requirement())
        self.assertEqual(setup_value("python_requires"), ">=3.8")

    def test_ci_installs_it_so_its_tests_run(self):
        workflow = (ROOT / ".github/workflows/publish.yml").read_text()
        self.assertIn('pip install -e ".[mcp]"', workflow)


if __name__ == "__main__":
    unittest.main()
