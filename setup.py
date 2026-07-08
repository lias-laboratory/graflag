from setuptools import setup, find_packages

setup(
    name="graflag",
    version="1.0.0",
    description="Distributed benchmarking framework for Graph Anomaly Detection",
    author="gbay7",
    packages=find_packages(),
    include_package_data=True,
    package_data={
        "graflag.gui": [
            "templates/*.html",
            "static/css/*.css",
            "static/js/*.js",
            "static/js/components/*.js",
        ],
        "graflag.devcluster": [
            "deploy.sh",
            "hosts.yml",
            "docker-compose.yml",
            "manager/*",
            "worker/*",
        ],
    },
    python_requires=">=3.7",
    install_requires=[
        "pyyaml>=5.0",
        "docker>=6.0",
        "Flask>=2.0.0",
        "flask-socketio>=5.3.0",
        "python-socketio>=5.9.0",
    ],
    entry_points={
        "console_scripts": [
            "graflag=graflag.cli:main",
        ],
    },
)
