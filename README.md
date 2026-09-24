# GraFlag

Distributed benchmarking framework for Graph Anomaly Detection (GAD). Orchestrates experiments on Docker Swarm clusters with NFS-based shared storage.

## Installation

```bash
pip install graflag
```

Or from source:

```bash
git clone https://github.com/lias-laboratory/graflag.git
cd graflag
pip install -e .
```

This installs the `graflag` command (includes CLI, web GUI, and devcluster).

## Related Repositories

- [graflag-shared](https://github.com/lias-laboratory/graflag-shared) -- Methods, datasets, and shared libraries (NFS-mounted storage)
- [Documentation](https://lias-laboratory.github.io/graflag/) -- Full documentation (Sphinx)

## Quick Start

### 1. Set up the shared directory

Clone the shared storage repository on your NFS mount:

```bash
cd /shared  # or your NFS mount point
git clone https://github.com/lias-laboratory/graflag-shared.git .
# Dataset files are not committed; they download on demand when you run
# `graflag run` (via the graflag_data library). Optional prefetch:
#   pip install graflag-shared/libs/graflag_data
#   graflag-data fetch         # all datasets
```

### 2. Configure

Run the interactive setup wizard:

```bash
graflag setup
```

This stores configuration in `~/.config/graflag/config.env`. To reconfigure later:

```bash
graflag setup --reconfigure
```

Or place a `.env` file in the working directory:

```
MANAGER_IP=192.168.100.10
SSH_PORT=22
SSH_KEY=~/.ssh/id_ed25519
SHARED_DIR=/shared
HOSTS_FILE=hosts.yml
```

### 3. Run an experiment

```bash
graflag run -m bond_dominant -d bond_inj_cora --build
graflag run -m taddy -d uci --params MAX_EPOCH=100 LEARNING_RATE=0.001
```

### 4. Evaluate and verify

```bash
graflag evaluate -e exp__bond_dominant__bond_inj_cora__20260309_120000
graflag verify -e exp__bond_dominant__bond_inj_cora__20260309_120000
```

`verify` checks that the published scores reproduce the AUC the method reported
and exits 1 when they do not.

## Commands

| Command | Description |
|---------|-------------|
| `graflag setup` | Interactive cluster configuration |
| `graflag run -m METHOD -d DATASET` | Run an experiment |
| `graflag status` | Show cluster status |
| `graflag list methods\|datasets\|experiments\|services` | List resources |
| `graflag logs -e EXP [-f]` | View experiment logs |
| `graflag stop -e EXP [--rm]` | Stop an experiment |
| `graflag evaluate -e EXP` | Evaluate experiment results |
| `graflag verify -e EXP [--json]` | Check the published result holds up (exit 1 on failure) |
| `graflag cleanup [--dry-run]` | Remove Swarm services for finished experiments |
| `graflag copy -s SRC --dest DST [-r]` | Copy files to/from remote |
| `graflag sync [--lib] [--path PATH]` | Sync method or library |
| `graflag gui [--port PORT]` | Start web dashboard (binds `0.0.0.0`, no auth -- see note below) |
| `graflag mcp [--config FILE]` | Serve GraFlag to an AI agent over MCP (stdio) |
| `graflag devcluster --hosts FILE` | Deploy virtual cluster |
| `graflag devcluster --down` | Stop virtual cluster |

> **Note on the dashboard.** `graflag gui` binds `0.0.0.0:5000` by default and
> has no authentication. Everything it exposes runs on the swarm manager as
> `root`, including deleting experiment directories. Run it on a trusted
> network or bind it explicitly with `graflag gui --host 127.0.0.1`.

## AI Agents (MCP)

`graflag mcp` exposes GraFlag to an MCP client -- Claude Code, Claude Desktop or
any other -- as tools to list methods and datasets, run, wait for, stop,
evaluate and verify experiments, and read their results and plots. It needs the
optional SDK (Python 3.10+):

```bash
pip install "graflag[mcp]"
claude mcp add graflag -- graflag mcp
```

Runs start in the background and `wait_for_experiment` waits for them, so no
tool call has to outlast a benchmark. Nothing that deletes data, sets up the
cluster or writes code to it is offered. See the
[MCP page](https://lias-laboratory.github.io/graflag/MCP.html) of the
documentation.

## Development Cluster

For local development without a physical cluster, describe the virtual nodes in
a `hosts.yml`:

```yaml
subnet: 192.168.100.0/24
manager: 192.168.100.10
workers:
  - 192.168.100.11
  - 192.168.100.12
```

then deploy it and point GraFlag at it:

```bash
graflag devcluster --hosts hosts.yml
graflag setup
```

## Development

Install in editable mode and run the test suite (no cluster or network needed):

```bash
pip install -e .
python3 -m unittest discover -s tests -v
```

The suite covers remote-command construction, experiment naming, and the GUI's
input validation. `tests/test_ssh.py` puts a fake `ssh` on `PATH` that records
the command the swarm manager would receive, then runs that string through a
real shell -- so it asserts on the actual effect rather than on a string shape.
Use that harness for any change that builds a remote command.

Two rules the tests enforce, worth knowing before editing `ssh.py`:

- `execute()` passes the command to `ssh` as a single argv element with no local
  shell. Reintroducing `shell=True` or wrapping the command in quotes breaks
  heredocs and lets Docker build output execute on the manager.
- The manager's shell still parses the command, so interpolated values are
  quoted at the point of use -- `remote_path()` for paths, `shlex.quote()` for
  bare values.

The shared libraries have their own suites:

```bash
cd ../graflag-shared/libs
PYTHONPATH=. python3 -m unittest discover -s graflag_data/tests -v
PYTHONPATH=. python3 -m unittest discover -s graflag_runner/tests -v
```

The documentation sources are in `docs/` (Sphinx, published at
https://lias-laboratory.github.io/graflag/):

```bash
pip install sphinx sphinx-rtd-theme myst-parser
cd docs && make html        # output in docs/_build/html
```

Publishing a GitHub release runs `.github/workflows/publish.yml`: the test
suite, then a build whose version must match the release tag, then the upload
to PyPI through Trusted Publishing.

## Dependencies

- `pyyaml` -- hosts.yml parsing for cluster setup
- `docker` -- Docker SDK for Python (service management via SSH tunnel)
- `flask`, `flask-socketio` -- Web GUI backend

## Module Structure

```
graflag/
    __init__.py      Package exports
    cli.py           Argument parsing, command dispatch, output formatting
    core.py          GraFlag orchestration class (returns structured data)
    models.py        Dataclass models (ClusterInfo, MethodInfo, ExperimentInfo, ...)
    config.py        Configuration loading (~/.config/graflag/config.env or .env)
    ssh.py           SSH and rsync file operations
    docker_ops.py    Docker Swarm operations via Docker SDK (SSH tunnel)
    api.py           Python API for GUI integration (wraps core)
    utils.py         Shared utility functions
    gui/             Web dashboard subpackage (Flask + Vue.js)
    devcluster/      Virtual cluster subpackage (Docker Compose)
tests/               Unit tests (unittest, no cluster required)
```
