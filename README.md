# GraFlag CLI

Command-line interface for orchestrating GAD benchmarks on Docker Swarm clusters.

## Installation

```bash
pip install -e .
```

This installs the `graflag` command.

## Configuration

Create a `.env` file in the working directory:

```
MANAGER_IP=192.168.100.10
SSH_PORT=22
SHARED_DIR=/shared
SSH_KEY=~/.ssh/id_ed25519
```

## Commands

### Setup cluster

```bash
graflag setup
```

### Run benchmark

```bash
graflag benchmark -m bond_dominant -d bond_inj_cora --build
graflag benchmark -m taddy -d uci --params MAX_EPOCH=100 LEARNING_RATE=0.001
graflag benchmark --from-config ./experiments/exp__method__dataset__time/service_config.json
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

## Module Structure

```
graflag/
    __init__.py      Package exports
    cli.py           Argument parsing and command dispatch
    core.py          GraFlag orchestration class
    config.py        Configuration loading from .env
    ssh.py           SSH and rsync operations
    docker_ops.py    Docker Swarm service management
    api.py           Python API for GUI integration
```
