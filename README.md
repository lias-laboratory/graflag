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
git lfs pull  # download dataset files
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

### 4. Evaluate

```bash
graflag evaluate -e exp__bond_dominant__bond_inj_cora__20260309_120000
```

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
| `graflag copy -s SRC -d DST [-r]` | Copy files to/from remote |
| `graflag sync [--lib] [--path PATH]` | Sync method or library |
| `graflag gui [--port PORT]` | Start web dashboard |
| `graflag devcluster --hosts FILE` | Deploy virtual cluster |
| `graflag devcluster --down` | Stop virtual cluster |

## Development Cluster

For local development without a physical cluster:

```bash
graflag devcluster --hosts hosts.yml
graflag setup
```

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
```
