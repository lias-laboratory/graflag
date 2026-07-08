# GraFlag CLI

Command-line interface for orchestrating GAD experiments on Docker Swarm clusters. Includes the web GUI and development cluster as subpackages.

## Installation

```bash
pip install -e .
```

This installs the `graflag` command.

## Configuration

Run interactive setup to store configuration in `~/.config/graflag/config.env`:

```bash
graflag setup
```

Or place a `.env` file in the working directory:

```
MANAGER_IP=192.168.100.10
SSH_PORT=22
SSH_KEY=~/.ssh/id_ed25519
SHARED_DIR=/shared
HOSTS_FILE=hosts.yml
```

## Dependencies

- `pyyaml` -- hosts.yml parsing for cluster setup
- `docker` -- Docker SDK for Python (service management via SSH tunnel)
- `flask`, `flask-socketio` -- Web GUI backend

## Commands

### Setup cluster

```bash
graflag setup
```

### Run experiment

```bash
graflag run -m bond_dominant -d bond_inj_cora --build
graflag run -m taddy -d uci --params MAX_EPOCH=100 LEARNING_RATE=0.001
graflag run --from-config ./experiments/exp__method__dataset__time/service_config.json
```

### List resources

```bash
graflag list methods
graflag list datasets
graflag list experiments
graflag list services
```

### Cluster status

```bash
graflag status
```

### View logs

```bash
graflag logs -e exp__bond_dominant__bond_inj_cora__20260309_120000
graflag logs -e exp__bond_dominant__bond_inj_cora__20260309_120000 -f
graflag logs -e exp__bond_dominant__bond_inj_cora__20260309_120000 --tee ./output.log
```

### Stop experiment

```bash
graflag stop -e exp__bond_dominant__bond_inj_cora__20260309_120000
graflag stop -e exp__bond_dominant__bond_inj_cora__20260309_120000 --rm
```

### Evaluate

```bash
graflag evaluate -e exp__bond_dominant__bond_inj_cora__20260309_120000
```

### Copy files

```bash
graflag copy -s ./data -d datasets -r
graflag copy --from-remote -s experiments/exp_name -d ./local_results
```

### Sync method or library

```bash
graflag sync                        # sync current directory as method
graflag sync --path ./my-method/    # sync specific directory
graflag sync --lib --path ./my-lib/ # sync as shared library
```

### Web GUI

```bash
graflag gui
graflag gui --port 8080 --debug
```

### Development cluster

```bash
graflag devcluster --hosts hosts.yml
graflag devcluster --hosts hosts.yml --pubkey ~/.ssh/id_ed25519.pub
graflag devcluster --down
```

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
