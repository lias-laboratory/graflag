from pathlib import Path

from setuptools import setup, find_packages

HERE = Path(__file__).parent

setup(
    name="graflag",
    version="1.2.0",
    description="Distributed benchmarking framework for Graph Anomaly Detection",
    long_description=(HERE / "README.md").read_text(encoding="utf-8"),
    long_description_content_type="text/markdown",
    author="gbay7",
    url="https://github.com/lias-laboratory/graflag",
    project_urls={
        "Documentation": "https://lias-laboratory.github.io/graflag/",
        "Source": "https://github.com/lias-laboratory/graflag",
        "Issues": "https://github.com/lias-laboratory/graflag/issues",
        "Methods and datasets": "https://github.com/lias-laboratory/graflag-shared",
    },
    license="MIT",
    # tests/ is a package so unittest can discover it; it is not part of the
    # distribution, and shipped it would install a top-level `tests` module.
    packages=find_packages(exclude=("tests", "tests.*")),
    include_package_data=True,
    # Every file the dashboard and the development cluster read at run time.
    # tests/test_packaging.py checks that each one on disk matches a pattern
    # here: the vendored Vue and Socket.IO and the images were once left out,
    # and an installed dashboard could not start.
    package_data={
        "graflag.gui": [
            "templates/*.html",
            "static/css/*.css",
            "static/img/*",
            "static/js/*.js",
            "static/js/components/*.js",
            "static/js/composables/*.js",
            "static/js/vendor/*",
        ],
        "graflag.devcluster": [
            "deploy.sh",
            "hosts.yml",
            "docker-compose.yml",
            "manager/*",
            "worker/*",
        ],
    },
    python_requires=">=3.8",
    install_requires=[
        "pyyaml>=5.0",
        "docker>=6.0",
        "Flask>=2.0.0",
        "flask-socketio>=5.3.0",
        "python-socketio>=5.9.0",
    ],
    # `graflag mcp`, the server that exposes GraFlag to AI agents. Optional:
    # the SDK needs Python 3.10+, and the rest of GraFlag still runs on 3.8.
    # Kept below 2: mcp 2.0 removed the FastMCP API the server is built on
    # (and that other MCP servers in the same environment may still import).
    extras_require={
        "mcp": ["mcp>=1.8,<2; python_version >= '3.10'"],
    },
    entry_points={
        "console_scripts": [
            "graflag=graflag.cli:main",
        ],
    },
)
