from setuptools import setup, find_packages

setup(
    name="graflag",
    version="1.0.0",
    description="Distributed benchmarking framework for Graph Anomaly Detection",
    author="gbay7",
    packages=find_packages(),
    python_requires=">=3.7",
    install_requires=[
        "pyyaml>=5.0",
    ],
    entry_points={
        "console_scripts": [
            "graflag=graflag.cli:main",
        ],
    },
)
