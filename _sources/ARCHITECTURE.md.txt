# GraFlag Architecture

Technical architecture reference for the GraFlag distributed benchmarking platform for Graph Anomaly Detection (GAD).

---

## Table of Contents

1. [System Overview](#system-overview)
2. [Design Principles](#design-principles)
3. [Component Diagram](#component-diagram)
4. [Component Descriptions](#component-descriptions)
5. [Execution Flow](#execution-flow)
6. [Shared Libraries](#shared-libraries)
7. [Data Flow](#data-flow)
8. [Experiment Lifecycle](#experiment-lifecycle)
9. [Docker Swarm Service Model](#docker-swarm-service-model)
10. [Housekeeping](#housekeeping)
11. [Configuration](#configuration)
12. [Experiment Output Structure](#experiment-output-structure)

---

## System Overview

GraFlag is a distributed benchmarking platform that runs Graph Anomaly Detection methods across a Docker Swarm cluster. The platform provides:

- **Distributed execution** of GAD methods on GPU-equipped worker nodes via Docker Swarm.
- **Standardized result collection** through the `graflag_runner` library, which wraps method execution with resource monitoring and result serialization.
- **Automated evaluation** via `graflag_evaluator`, which computes metrics (AUC-ROC, AUC-PR, etc.) and generates plots.
- **Multiple client interfaces**: a CLI for scripting, a Python API for programmatic use, a web GUI for interactive management, and an MCP server through which AI agents manage runs.
- **Result verification**: `graflag verify` checks that a run's published scores reproduce the AUC the method reported, the last gate of method integration.
- **NFS-based shared storage** for methods, datasets, experiments, and shared libraries, accessible by all cluster nodes.

---

## Design Principles

- **Container isolation**: Each method runs in its own Docker container with a defined Dockerfile, ensuring reproducibility and dependency isolation.
- **One-shot execution**: Services use `--restart-condition none`, meaning each experiment runs exactly once. There is no automatic retry on failure.
- **Environment variable injection**: Method parameters flow from the CLI through Docker environment variables into the container, using an underscore prefix convention (`_PARAM_NAME=value`).
- **Result standardization**: All methods produce a `results.json` file conforming to a fixed schema, enabling uniform evaluation across heterogeneous methods.
- **Separation of concerns**: The orchestration layer (CLI/API) never executes method code directly. It builds images, deploys services, and reads results from shared storage.
- **Shared storage as the integration point**: NFS provides a single namespace (`/shared` or configurable) visible to all nodes, used for datasets, method source, experiment output, and shared libraries.
- **One shell, not two**: a remote command is parsed by exactly one shell -- the manager's. The client never runs a shell of its own, and every value interpolated into a remote command is quoted at the point of interpolation.

---

## Component Diagram

```
                          +-------------------------------------------+
                          |              Client Machine                |
                          |                                            |
                          |  +------+ +--------+ +---------+ +-----+  |
                          |  | CLI  | |  GUI   | | Python  | | MCP |  |
                          |  |cli.py| |Flask + | | API     | |serv-|  |
                          |  |      | |Vue.js  | | api.py  | |er   |  |
                          |  +--+---+ +---+----+ +----+----+ +--+--+  |
                          |     |         |           |         |     |
                          |     v         v           v         v     |
                          |  +--------------------------------------+  |
                          |  |          GraFlag Core (core.py)      |  |
                          |  |         returns dataclasses          |  |
                          |  +--+-------------------------------+---+  |
                          |     |                               |      |
                          |  +--v-----------+  +----------------v--+   |
                          |  | SSHManager   |  | DockerManager     |   |
                          |  | (ssh.py)     |  | (docker_ops.py)   |   |
                          |  | file ops,    |  | SDK via SSH       |   |
                          |  | rsync        |  | tunnel; builds    |   |
                          |  |              |  | and logs over SSH |   |
                          |  +------+-------+  +--------+----------+   |
                          +---------|-------------------|---------------+
                                    |                   |
                              SSH / rsync          SSH tunnel to
                                    |            Docker socket
                                    |                   |
                    +---------------------------v-----------------------------+
                    |                    Manager Node                         |
                    |                                                         |
                    |  +----------------+   +-----------------------------+   |
                    |  | Docker Swarm   |   |  Local Registry             |   |
                    |  | Manager        |   |  (registry:2 on port 5000)  |   |
                    |  +-------+--------+   +-----------------------------+   |
                    |          |                                               |
                    |          |  NFS Export: /shared                          |
                    |          |  +---------------------------------------+   |
                    |          |  | methods/ | datasets/ | experiments/   |   |
                    |          |  | libs/    |           |                |   |
                    |          |  +---------------------------------------+   |
                    +----------|----------------------------------------------+
                               |
                  Docker Swarm scheduling
                               |
          +--------------------+---------------------+
          |                    |                      |
    +-----v------+     +------v-----+     +----------v--+
    | Worker 1   |     | Worker 2   |     | Worker N    |
    |            |     |            |     |             |
    | GPU(s)     |     | GPU(s)     |     | GPU(s)      |
    | NFS mount  |     | NFS mount  |     | NFS mount   |
    | /shared    |     | /shared    |     | /shared     |
    +------------+     +------------+     +-------------+
```

---

## Component Descriptions

### graflag (CLI, GUI, DevCluster, and Orchestration)

The main Python package located in `graflag/graflag/`. It provides the command-line interface, core orchestration logic, a Python API, a web GUI, and a development cluster tool -- all installable as a single package via `pip install -e .` and accessible through the `graflag` command.

#### cli.py -- Command Line Interface

Entry point for all user-facing commands. Uses `argparse` with the following commands:

| Command      | Description                                      |
|--------------|--------------------------------------------------|
| `setup`      | Initialize Docker Swarm and join worker nodes    |
| `run`        | Build image and deploy experiment service        |
| `status`     | Show cluster status and shared directory contents|
| `list`       | List methods, datasets, experiments, or services |
| `logs`       | View experiment logs (supports `--follow`, `--tee`) |
| `stop`       | Stop a running experiment (optional `--rm`)      |
| `evaluate`   | Run evaluation on a completed experiment         |
| `verify`     | Check the published result against the method's own AUC (exit 1 on failure) |
| `cleanup`    | Remove the Swarm services of finished runs       |
| `clear`      | Report, or with `--apply` remove, storage nothing owns |
| `copy`       | Transfer files between local and remote (rsync)  |
| `sync`       | Sync a method or library directory to remote     |
| `gui`        | Start the web dashboard                          |
| `mcp`        | Serve GraFlag to an AI agent over MCP (stdio)    |
| `devcluster` | Deploy or tear down a local Docker Compose development cluster|

Key flags for `run`:
- `--build`: Build (or rebuild) the Docker image before deploying; `--force-rm` also removes the build's containers when it fails.
- `--params KEY=VALUE`: Override method parameters at runtime.
- `--no-gpu`: Deploy without a GPU reservation, and set the method's `_GPU` to `-1` (CPU).
- `--keep-service`: Leave the finished service in place instead of removing it.
- `--from-config CONFIG_FILE`: Replay an experiment from a saved `service_config.json`.
- `--tag TAG`: Image tag (default `latest`).

#### core.py -- GraFlag Orchestration Class

Central orchestration class (`GraFlag`) that coordinates all operations. All public methods return structured data (dataclasses from `models.py`) rather than printing to stdout. The CLI formats the returned data for terminal display.

Key responsibilities:

- **Initialization**: Loads `GraflagConfig`, creates `SSHManager` and `DockerManager` instances.
- **`status()`**: Returns a `ClusterInfo` dataclass with nodes, services, and shared directory contents.
- **`run()`**: Validates that the method and dataset exist, downloads the dataset's files if they are missing (`graflag_data ... fetch`, run on the manager), creates the experiment directory, optionally builds the image, deploys the Swarm service, follows its logs, removes the finished service (unless `keep_service`), and returns the experiment name.
- **`list_methods()`**: Returns `List[MethodInfo]` with method metadata parsed from `.env` files.
- **`list_datasets()`**: Returns `List[DatasetInfo]` with size and file count.
- **`list_experiments()`**: Returns `List[ExperimentInfo]` with status, sorted by timestamp.
- **`get_experiment_results()`**: Returns `ExperimentResults` parsed from `results.json`.
- **`get_evaluation_results()`**: Returns `EvaluationResults` parsed from `eval/evaluation.json`.
- **`evaluate()`**: Deploys a `graflag-evaluator` service against a completed experiment.
- **`stop()`**: Stops a running experiment's service, optionally deleting its directory.
- **`cleanup_services()`**: Removes the services of finished runs -- only terminal ones, and never one whose run cannot be diagnosed from disk (see [Housekeeping](#housekeeping)).
- **`clear()`**: Reports, or removes, storage and images that nothing owns any more, returning a `ClearReport`.
- **`register_metric()`**: Saves a custom metric function as a plugin file on the cluster (global or per-experiment) so the evaluator can load it at runtime.
- **`copy_files()`**: Bidirectional file transfer using rsync over SSH.
- **`sync()`**: Sync a local method or library directory to the remote shared storage.
- **Status management**: Writes `status.json` to experiment directories during build and on failure.

#### models.py -- Data Models

Shared dataclass definitions used by core, api, and GUI:

| Dataclass           | Description                                      |
|---------------------|--------------------------------------------------|
| `ClusterInfo`       | Cluster status, nodes, services, shared dir      |
| `MethodInfo`        | Method name, description, parameters, files      |
| `DatasetInfo`       | Dataset name, path, size, file count             |
| `ExperimentInfo`    | Experiment status, results/eval availability     |
| `ExperimentResults` | Parsed results.json with execution metrics       |
| `EvaluationResults` | Parsed evaluation.json with metrics and plots    |
| `ServiceCleanupResult` | What `cleanup_services()` did, per service    |
| `ClearItem`, `ClearReport` | What `clear()` found, and what it removed |
| `VerificationReport` | What `verify()` found: one finding per check, and the counts |
| `RunProgress`       | Progress tracking for experiment execution       |

All dataclasses implement `to_dict()` for JSON serialization.

#### config.py -- Configuration Management

`GraflagConfig` loads configuration from a `.env` file and exposes typed properties.

**Config resolution order**:

1. Explicit path passed via `--config` flag (an error if the file does not exist).
2. `.env` file in the current working directory, **only if it defines `MANAGER_IP`** -- so an unrelated project's `.env` is not mistaken for the GraFlag configuration.
3. Standard location: `~/.config/graflag/config.env`.

`graflag setup` runs an interactive wizard (`init_config()`) that prompts for values and stores them in the standard location.

| Property           | Env Variable  | Default              | Description                          |
|--------------------|---------------|----------------------|--------------------------------------|
| `manager_ip`       | `MANAGER_IP`  | (required)           | IP address of the Swarm manager node |
| `ssh_port`         | `SSH_PORT`    | `22`                 | SSH port on the manager              |
| `ssh_key`          | `SSH_KEY`     | `~/.ssh/id_ed25519`  | Path to SSH private key              |
| `remote_shared_dir`| `SHARED_DIR`  | `/shared`            | Path to shared storage on remote     |
| `nfs_port`         | `NFS_PORT`    | `2049`               | NFS port for mounting                |
| `hosts_file`       | `HOSTS_FILE`  | `hosts.yml`          | Path to hosts.yml with worker IPs    |
| `graflag_libs`     | `GRAFLAG_LIBS`| `local`              | Where method images get `graflag_runner`/`graflag_bond`: `local` (the share's `libs/`) or `pypi` (the release) |

#### ssh.py -- SSH Operations

`SSHManager` provides remote command execution and file transfer:

- **`execute(command)`**: Runs a command on the manager. Always connects as `root@MANAGER_IP`.
- **`copy_files()`**: Bidirectional rsync over SSH. Handles both local-to-remote and remote-to-local transfers.
- **`path_exists()`, `read_file()`, `mkdir()`, `list_dir()`**: File system operations on the remote via SSH.
- **`remote_path(*parts)`**: Joins path components and quotes the result for the remote shell.

All SSH commands use `-o StrictHostKeyChecking=no` and authenticate with the configured SSH key.
Every ssh and rsync the client starts has its stdin closed: ssh forwards what it
can read from its stdin to the remote command, so an inherited stdin let it
consume the rest of a shell loop's input -- or, under the MCP server, the
client's requests. `GraFlag(interactive=False)` adds `BatchMode=yes` and a
15-second `ConnectTimeout`, so a caller with nobody at a terminal gets an error
instead of a prompt nobody will answer.

**Command construction contract.** `execute()` passes its argument to `ssh` as a
single element of an argv list; `subprocess` is called without `shell=True`, so
no shell on the client ever sees the command. This is what allows the command to
carry newlines, embedded quotes and heredocs:

```python
ssh.execute(f"cat > {remote_path(exp_dir, 'build.log')} << 'BUILDEOF'\n{log}\nBUILDEOF")
```

The quoted delimiter `<< 'BUILDEOF'` is what tells the manager's shell to treat
the heredoc body as literal text. Wrapping the command in quotes on the client
(as an earlier version did) collapses it to `<< BUILDEOF`, after which the
manager expands `$VAR` and executes `$(...)` and backticks appearing anywhere in
Docker build output -- corrupting the saved log and running arbitrary commands as
root.

The manager's shell *does* parse the command string, so callers quote every
interpolated value:

| Interpolating | Use |
|---|---|
| a path built from user input | `remote_path(shared_dir, "experiments", name)` |
| a bare value (image tag, service name) | `shlex.quote(value)` |
| a glob or redirect | leave **outside** the quoted part: `f"ls {remote_path(d)}/*.png"` |

Values that arrive over HTTP are additionally screened by `_valid_name()` in
`gui/server.py` before reaching this layer. `graflag/tests/test_ssh.py` pins the
contract down by capturing what a fake `ssh` receives and executing it with a
real shell.

#### docker_ops.py -- Docker Swarm Operations

`DockerManager` handles Docker Swarm interactions using the **Docker SDK for Python** (`docker-py`) connected via an SSH tunnel to the remote Docker daemon.

**Connection model**: An SSH tunnel forwards a local TCP port to the remote Docker socket (`/var/run/docker.sock`). The Docker SDK connects to `tcp://localhost:<tunnel_port>`, providing native Python access to the Docker API. The tunnel is established lazily on first use and reused across operations.

**Operations using Docker SDK** (native Python API):

- **Swarm management**: `setup_swarm_manager()` via `client.swarm.init()`, `get_swarm_token()` via `client.swarm.attrs`, `get_nodes()` via `client.nodes.list()`.
- **Registry**: `setup_local_registry()` via `client.services.create()`.
- **Service creation**: `create_service()` via `client.services.create()` with `ServiceMode`, `RestartPolicy`, `Resources`, `Mount`, and network configuration.
- **Service lifecycle**: `list_services()`, `stop_service()`, `service_exists()`, `is_service_failed()` via `client.services.*`.
- **Cluster status**: `get_cluster_status()` via `client.info()` and node/service listing.

**Operations using SSH** (build context is on the remote host, or the SDK is unreliable):

- **Image building**: `build_method_image()` -- runs `docker build` and `docker push` via SSH because the build context (shared directory with methods and libs) resides on the remote host. It passes the method's `SOURCE_CODE` and `SOURCE_REF` and the cluster's `GRAFLAG_LIBS` as `--build-arg`, and returns the log that is saved as `build.log`; a failed build raises `BuildFailed`, which carries the same log.
- **Evaluator image**: `build_evaluator_image()` -- same rationale. Its tag is a hash of the `libs/graflag_evaluator` sources, so a source change produces a new tag and a real rebuild, while an unchanged tree reuses the image already in the registry.
- **Log retrieval**: `get_service_logs()` and `follow_service_logs()` shell out to `docker service logs` over SSH. The SDK's `service.logs(follow=True, stream=True)` does not stream reliably for swarm services.

**Worker setup**: `setup_workers()` uses SSH to execute `docker swarm join` on each worker node, since the Docker SDK connection only reaches the manager.

**Reserved environment variables** (defined in `ReservedEnvVars` enum): `DATA`, `EXP`, `METHOD_NAME`, `COMMAND`, `MONITOR_INTERVAL`. A `--params` key that collides with one is silently dropped.

#### api.py -- Python API

`GraFlagAPI` is a thin error-safe wrapper around `GraFlag` core, designed for GUI integration. Since core now returns structured data directly, the API layer:

- Delegates all operations to `self.core` (no duplicated logic).
- Wraps each call in try/except, returning empty lists or `None` on error instead of raising (prevents GUI crashes).
- Maintains the same public interface used by the GUI (backward-compatible).
- All returned dataclasses implement `to_dict()` for JSON serialization.

#### mcp_server.py -- MCP Server

`graflag mcp` serves GraFlag's operations as Model Context Protocol tools over
stdio (see the MCP page). Like the CLI, it sits directly on `GraFlag` core
rather than on `api.py`, whose error-swallowing would hide from the agent why a
call failed. Three rules shape it:

- **stdout is the protocol.** The server gives the protocol private copies of
  its stdin and stdout and points file descriptors 0 and 1 at `/dev/null` and
  stderr, so no subprocess or stray `print()` can corrupt a reply or read a
  request; every core call it makes uses `follow=False`.
- **Long operations are background jobs.** `run_experiment` and
  `evaluate_experiment` return at once; `wait_for_experiment` blocks for a
  bounded time. A job that fails before anything reaches the share is reported
  from the job itself.
- **Untrusted input.** Names go through `utils.valid_name()`, the same rule the
  dashboard applies to HTTP input; nothing that deletes data, sets up the
  cluster or writes code to it is offered.

#### verify.py -- Result Verification

The checks behind `graflag verify`, `GraFlag.verify()` and the MCP
`verify_experiment` tool. A probe runs on the manager in one SSH call -- plain
Python, since the manager has no numpy -- and returns counts, the AUCs the
method recorded and the evaluator's; `check()` turns that summary into
findings. The scores never leave the manager.

#### gui/ -- Web Interface (subpackage)

A Flask application with a Vue.js frontend, located in `graflag/graflag/gui/`. Accessible via `graflag gui [--host HOST] [--port PORT] [--debug]`.

**Backend** (`server.py`):
- Built on Flask with Flask-SocketIO for real-time updates.
- Uses `GraFlagAPI` as its data layer.
- REST endpoints under `/api/` for methods, datasets, experiments, services, run, evaluation, logs, and plot serving.
- Run, evaluation, stop, and delete operations execute in background threads to avoid blocking HTTP responses. Runs and evaluations wait with `follow=False`, polling status rather than streaming logs into the server's console.
- Plot images are streamed from the remote via SSH + base64 encoding.
- Server-side caching for methods and datasets (30-second TTL).

**Real-time updates**:
- WebSocket connection via Socket.IO.
- A `background_updater` thread polls experiments and services every 2 seconds.
- Updates are broadcast to all connected clients only when state changes (change detection via comparison with `last_state`).

**Frontend**:
- Vue.js single-page application served from Flask templates.
- Communicates with the backend via REST API and WebSocket events.

#### devcluster/ -- Development Cluster (subpackage)

A Docker Compose-based virtual cluster for local development and testing, located in `graflag/graflag/devcluster/`. Accessible via `graflag devcluster --hosts hosts.yml [--pubkey KEY]`. Use `graflag devcluster --down` to stop and remove the cluster.

**Components**:
- `deploy.sh`: Automated deployment script that generates SSH keys, writes `docker-compose.yml` from `hosts.yml` (one service per worker listed there, on the subnet it names) and starts the cluster. The generated files live in `$XDG_STATE_HOME/graflag/devcluster`, not in the installed package.
- `manager/`: Dockerfile and configuration for the manager container (Docker-in-Docker with SSH, NFS server).
- `worker/`: Dockerfile and configuration for worker containers (Docker-in-Docker with SSH, NFS client).

**Key characteristics**:
- All containers run in privileged mode (required for Docker-in-Docker and NFS).
- GPU passthrough is configured via `deploy.resources.reservations.devices`.
- Static IP assignment on a custom bridge network.
- SSH key exchange is handled during build time via build arguments.

### graflag-shared (NFS Shared Storage)

The shared storage directory is NFS-exported by the manager node and mounted on all worker nodes. Structure:

```
graflag-shared/
+-- .dockerignore         # Keeps datasets/ and experiments/ out of image builds
+-- methods/              # GAD method implementations
|   +-- <method_name>/
|       +-- .env          # Method metadata and default parameters
|       +-- Dockerfile    # Container definition (absent when .env sets IMAGE=)
|       +-- train_graflag.py  # GraFlag integration script
|       +-- README.md     # What the integration does, and what it changes
|       +-- ...
+-- images/               # Dockerfiles shared by several methods
|   +-- bond_base/        # The 17 PyGOD detectors
+-- datasets/             # Benchmark datasets
|   +-- <dataset_name>/
|       +-- metadata.json # Source URLs and checksums; files are fetched on demand
|       +-- ...
+-- experiments/          # Experiment results (one directory per run)
|   +-- exp__<method>__<dataset>__<timestamp>/
|       +-- results.json
|       +-- status.json
|       +-- service_config.json
|       +-- service_details.json
|       +-- method_output.txt
|       +-- build.log
|       +-- training.csv
|       +-- resources.csv
|       +-- eval/
+-- libs/                 # Shared Python libraries
    +-- graflag_runner/
    +-- graflag_evaluator/
    |   +-- plugins/      # Global custom metric plugins
    +-- graflag_bond/
    +-- graflag_data/     # Dataset download and build (graflag-data)
```

---

## Execution Flow

The complete lifecycle of an experiment:

### 1. Command Parsing

```
graflag run -m taddy -d uci --build --params MAX_EPOCH=100
```

The CLI parses arguments, instantiates `GraFlag`, and calls `run()`.

### 2. Validation

- SSH to manager to verify `methods/taddy/` exists.
- SSH to manager to verify `datasets/uci/` exists.
- Download the dataset's files if they are missing: `graflag_data ... fetch uci` runs on the manager, so the files land directly on the share.
- Create experiment directory: `experiments/exp__taddy__uci__20260309_143000/`.

### 3. Image Build (if `--build`)

```
status.json <- {"status": "building"}

docker build --network=host [--force-rm] \
  --build-arg SOURCE_CODE=https://github.com/yuetan031/TADDY_pytorch \
  --build-arg SOURCE_REF=dfe15ddbbc179ed0ca9a96482fe1696dfbcb5bcd \
  --build-arg GRAFLAG_LIBS=local \
  -f /shared/methods/taddy/Dockerfile \
  -t taddy:latest \
  -t MANAGER_IP:5000/taddy:latest \
  /shared/

docker push MANAGER_IP:5000/taddy:latest
```

The build context is the entire shared directory, giving the Dockerfile access to `libs/` for installing `graflag_runner` and other shared libraries; the `.dockerignore` at its root keeps `datasets/` and `experiments/` out, and the build warns when that file is missing. `SOURCE_CODE` and `SOURCE_REF` come from the method's `.env`, so the Dockerfile clones the one commit the `.env` pins. A method whose `.env` sets `IMAGE=` builds `images/<name>/Dockerfile` instead, tagged with that name.

Build output is saved to `experiments/exp__*/build.log`, for a failed build as well as a successful one.

### 4. Service Deployment

Using the Docker SDK via SSH tunnel, the equivalent of:

```
docker service create --quiet -d \
  --name exp__taddy__uci__20260309_143000 \
  --restart-condition none \
  --network host \
  --generic-resource NVIDIA-GPU=1 \
  --env METHOD_NAME=taddy \
  --env DATA=/shared/datasets/uci/ \
  --env EXP=/shared/experiments/exp__taddy__uci__20260309_143000/ \
  --env _MAX_EPOCH=100 \
  --mount type=bind,source=/shared,target=/shared \
  MANAGER_IP:5000/taddy:latest
```

is performed via `client.services.create()` with `ServiceMode`, `RestartPolicy`, `Resources`, `Mount`, and `env` parameters. The method's `.env` is read and merged into the env list (rather than using `--env-file`), giving the orchestrator full control over parameter precedence. `GRAFLAG_PARAMS` lists which `_`-prefixed variables are parameters, and with `--no-gpu` the GPU reservation is dropped and the method's `_GPU` is set to `-1`.

Service configuration is saved to `service_config.json` before deployment. Service details are saved to `service_details.json` right after creation: the service ID and image, and the task's state at that moment -- usually `pending`, before Swarm has placed it, so the node ID is often empty.

### 5. Method Execution (inside container)

Every method image has the same entry point:

```
CMD ["python3", "-m", "graflag_runner"]
```

`MethodRunner.from_env()` reads the environment and:
1. Checks the dataset against `SUPPORTED_DATASETS`, when the `.env` defines it, and refuses to start if no pattern matches.
2. Writes `status.json` with status `running`.
3. Starts `ResourceMonitor` in a background thread, sampling CPU memory (`psutil`) and GPU memory (`nvidia-smi`) over the whole process tree.
4. Runs the `COMMAND` from the `.env` in a subprocess, with real-time output capture.
5. Checks that `results.json` exists and parses -- exit code 0 alone is not taken as success -- and merges its own `exec_time_ms`, `peak_memory_mb` and `peak_gpu_mb` into the metadata, where they override anything the method recorded.
6. Writes the final `status.json` (`completed` or `failed`) and saves the captured output to `method_output.txt`.

The method reads its parameters with `graflag_runner.params()` and writes `results.json` with `ResultWriter`:

```python
from dataclasses import dataclass
from graflag_runner import ResultWriter, params

@dataclass
class Config:
    max_epoch: int = 200

config = Config(**params(Config))   # _MAX_EPOCH=100 -> config.max_epoch == 100
# ... run the detector, score the test split ...
writer = ResultWriter()
writer.save_scores(result_type="TEMPORAL_EDGE_ANOMALY_SCORES", scores=scores, ground_truth=gt)
writer.add_metadata(method_name="taddy", dataset="uci")
writer.finalize()  # writes results.json atomically
```

`python3 -m graflag_runner --pass-env-args` is also supported: it appends the parameters to `COMMAND` as CLI flags (`_MAX_EPOCH=100` becomes `--max_epoch 100`). No method uses it.

### 6. Evaluation (optional, triggered separately)

```
graflag evaluate -e exp__taddy__uci__20260309_143000
```

This deploys a separate `graflag-evaluator` Docker service that:
1. Loads `results.json` from the experiment directory.
2. Flattens scores and labels and leaves out sentinel (`-1`, `-2`) and non-finite scores, counting them (`preprocessing.py`).
3. Computes metrics (AUC-ROC, AUC-PR, etc.) via `MetricCalculator`.
4. Generates plots (ROC curve, PR curve, score distribution, one curve plot per spot CSV) via `PlotGenerator`, from the same filtered pairs.
5. Saves `evaluation.json` and plot PNGs to the `eval/` subdirectory.

The service is removed when it finishes.

---

## Shared Libraries

### graflag_runner

**Location**: `graflag-shared/libs/graflag_runner/`

The execution framework installed inside method containers. Provides:

| Module             | Description                                                    |
|--------------------|----------------------------------------------------------------|
| `runner.py`        | `MethodRunner` -- main execution wrapper. Manages lifecycle, resource monitoring, status tracking, output capture, and the check that `results.json` parses. |
| `method.py`        | The SDK for integration scripts: `params()` and `apply_params()` (parameters from `_FOO` variables), `device()` (`_GPU`, with `-1` meaning CPU), `paths()` (`DATA`/`EXP`), `upstream()` (the clone on `sys.path`), `seed_all()`, and the dataset loaders `load_dataset()`, `load_snapshots()`, `load_edge_list()`, `load_attributed_graph()`, `split_test_edges()`, `write_mat()`/`read_mat()`. |
| `results.py`       | `ResultWriter` -- standardized result serialization. Supports `save_scores()`, `add_metadata()`, `add_resource_metrics()`, `spot()` (CSV-based metric tracking), and `finalize()`, which writes to a temporary file and renames it into place. |
| `serialization.py` | JSON encoding of NumPy types for `results.json`. |
| `monitor.py`       | `ResourceMonitor` -- background thread tracking CPU memory (via `psutil`), GPU memory (via `nvidia-smi`), with periodic CSV logging through `ResultWriter.spot()`. |
| `streaming.py`     | `StreamableArray` and `stream_write_json` -- memory-efficient streaming for large score arrays (writes JSON row-by-row without loading entire arrays into memory). |
| `subprocess_utils.py` | `run_with_realtime_output()` -- subprocess execution with live stdout/stderr capture and forwarding. |
| `logging_utils.py` | Simple logging functions (`debug`, `info`, `warning`, `error`, `critical`, `exception`). |
| `__main__.py`      | Module entry point for `python -m graflag_runner`.             |

**ResultWriter.spot()** method: Tracks real-time metrics to CSV files during training. Each metric group (e.g., `"training"`, `"validation"`, `"resources"`) gets its own CSV file. Schema is locked after the first call -- subsequent calls must provide the same metric keys. The first column is always a Unix timestamp.

```python
writer.spot("training", epoch=1, loss=0.5, auc=0.85)
writer.spot("training", epoch=2, loss=0.3, auc=0.90)
```

### graflag_bond

**Location**: `graflag-shared/libs/graflag_bond/`

A wrapper library for running PyGOD anomaly detection methods through GraFlag. Provides:

| Module         | Description                                                   |
|----------------|---------------------------------------------------------------|
| `detectors.py` | `BondDetector` -- dynamic PyGOD detector registry. Discovers all detector classes from `pygod.detector` via introspection. Maps method names (with optional `bond_` prefix) to detector classes. |
| `train.py`     | Training logic for PyGOD detectors within the GraFlag framework. |
| `utils.py`     | Utility functions including `get_all_parameters()` for detector parameter discovery. |

Usage pattern: methods prefixed with `bond_` (e.g., `bond_dominant`) use this library to instantiate and train PyGOD detectors without writing boilerplate code. Their `.env` sets `COMMAND=python3 -m graflag_bond.train` and `IMAGE=bond_base`: all 17 run the one image built from `images/bond_base/Dockerfile`, and stay distinct because `METHOD_NAME` is set per service.

### graflag_evaluator

**Location**: `graflag-shared/libs/graflag_evaluator/`

The evaluation framework that computes metrics and generates plots. Runs as a Docker service.

| Module              | Description                                                 |
|---------------------|-------------------------------------------------------------|
| `evaluator.py`      | `Evaluator` -- orchestrator that loads results, computes metrics, generates plots, and saves `evaluation.json`. Loads custom metric plugins on init. |
| `preprocessing.py`  | `prepare_pairs()` -- flattens scores and labels and drops sentinel and non-finite scores, reporting the counts. Metrics and plots both go through it, so they are computed on the same samples. |
| `compare_metrics.py`| Recomputes an existing experiment with the previous metric implementation and prints what changes. |
| `metrics.py`        | `MetricCalculator` -- computes AUC-ROC, AUC-PR, and other metrics based on result type. Handles ragged arrays (e.g., temporal edge scores with varying snapshot sizes). Supports plugin-based custom metrics via `register_metric()` and `load_plugins()`. |
| `plots.py`          | `PlotGenerator` -- generates ROC curves, PR curves, score distributions, and spot curves from CSV files. |
| `run_evaluation.py` | Entry point for the evaluation Docker container.            |
| `plugins/`          | Directory for global custom metric plugins (`.py` files loaded at evaluation time). |

**Custom metric plugins**: The evaluator automatically loads `.py` files from two directories before computing metrics:

1. **Global plugins**: `graflag-shared/libs/graflag_evaluator/plugins/` -- applied to all evaluations.
2. **Per-experiment plugins**: `experiments/<exp_name>/custom_metrics/` -- scoped to a single experiment.

Each plugin file imports `MetricCalculator` and calls `register_metric()` at module level. Plugins can be created manually or via the Python API (`GraFlag.register_metric()`), which extracts the function source and writes it as a plugin file on the cluster.

### graflag_data

**Location**: `graflag-shared/libs/graflag_data/`

Downloads dataset files from their original sources. Each dataset directory holds only a `metadata.json` naming its files, their URLs, how to extract them and, optionally, their sha256; a derived dataset instead names the dataset it is built from and the command that builds it. The `graflag-data` command (`list`, `status`, `fetch`, `verify`) runs on the manager, and `graflag run` calls `fetch` for its dataset before deploying.

---

## Data Flow

### Environment Variable Injection

Parameters flow through three layers:

```
Method .env file         CLI --params            Docker Service Env
+------------------+    +------------------+    +------------------+
| METHOD_NAME=taddy|    | MAX_EPOCH=100    | -> | METHOD_NAME=taddy|
| COMMAND=python...|    | LEARNING_RATE=.01|    | COMMAND=python...|
| _BATCH_SIZE=128  |    +------------------+    | DATA=/shared/... |
| _MAX_EPOCH=50    |                            | EXP=/shared/...  |
+------------------+                            | _BATCH_SIZE=128  |
                                                | _MAX_EPOCH=100   | (overridden)
                                                | _LEARNING_RATE=.01| (added)
                                                +------------------+
```

1. The method's `.env` file defines metadata (`METHOD_NAME`, `COMMAND`, `DESCRIPTION`) and default parameters (underscore-prefixed: `_BATCH_SIZE=128`).
2. The method's `.env` is read by `DockerManager._build_service_env()` and parsed into a dict.
3. Reserved variables (`DATA`, `EXP`, `METHOD_NAME`, `COMMAND`, `MONITOR_INTERVAL`) are set by the orchestrator and cannot be overridden by `--params`.
4. User `--params` are merged into the dict as `_KEY=VALUE`, overriding any defaults from the `.env` file. With `--no-gpu`, `_GPU` becomes `-1` unless `--params` sets it.
5. `GRAFLAG_PARAMS` is added, listing the `_`-prefixed keys, and the merged dict is passed as the `env` parameter to `client.services.create()`.
6. Inside the container, the integration script reads them with `params()`: the underscore is stripped, the name lowercased and the value coerced to the type its `Config` field declares (`_MAX_EPOCH=100` becomes `max_epoch=100`, an `int`).

### Result Standardization

All methods must produce a `results.json` via `ResultWriter` with this schema:

```json
{
    "result_type": "EDGE_STREAM_ANOMALY_SCORES",
    "scores": [0.1, 0.9, 0.3],
    "ground_truth": [0, 1, 0],
    "metadata": {
        "method_name": "taddy",
        "dataset": "uci"
    }
}
```

Valid result types:

| Category   | Node                          | Edge                          | Graph                          |
|------------|-------------------------------|-------------------------------|--------------------------------|
| Static     | `NODE_ANOMALY_SCORES`         | `EDGE_ANOMALY_SCORES`         | `GRAPH_ANOMALY_SCORES`         |
| Temporal   | `TEMPORAL_NODE_ANOMALY_SCORES`| `TEMPORAL_EDGE_ANOMALY_SCORES`| `TEMPORAL_GRAPH_ANOMALY_SCORES`|
| Streaming  | `NODE_STREAM_ANOMALY_SCORES`  | `EDGE_STREAM_ANOMALY_SCORES`  | `GRAPH_STREAM_ANOMALY_SCORES`  |

Optional additional fields: `timestamps`, `node_ids`, `edges`, `graph_ids`.

### Evaluation Pipeline

```
                   plugins/*.py ------+
                   custom_metrics/*.py-+-> MetricCalculator (load_plugins)
                                       |
results.json --> Evaluator ----------->+--> MetricCalculator --> evaluation.json
                           --> PlotGenerator    --> roc_curve.png
                                                --> pr_curve.png
                                                --> score_distribution.png

*.csv (spot files) --> PlotGenerator --> <key>_curves.png (one per spot file)
```

On initialization the `Evaluator` loads custom metric plugins from the global `plugins/` directory and the experiment's `custom_metrics/` directory. Each plugin registers additional metric functions that are then included in the evaluation alongside the built-in metrics (AUC-ROC, AUC-PR, etc.). The built-in metrics flatten scores and ground truth (ragged temporal data included) and leave out `-1`/`-2` sentinels and non-finite scores; the counts are reported under `metrics.filtering`. See [RESULTS_STANDARD](RESULTS_STANDARD.md).

---

## Experiment Lifecycle

Each experiment transitions through the following states, tracked in `status.json`:

```
                +----------+
                | building |  (image build in progress)
                +----+-----+
                     |
                     v
                +---------+
     +--------->| running |  (service deployed, method executing)
     |          +----+----+
     |               |
     |     +---------+---------+
     |     |                   |
     |     v                   v
     | +-----------+     +--------+
     | | completed |     | failed |
     | +-----------+     +--------+
     |
     | (external stop)
     |     +----------+
     +---->| stopped  |
            +----------+
```

**State determination logic** (in `core.py`, `_build_experiment_info`):

`status.json` is transferred base64-encoded, because the probe's reply is parsed
line by line while `graflag_runner` writes the file indented -- reading it with a
plain `cat` captured only the opening `{`, so every runner-written status was
silently discarded and finished experiments reported `running` indefinitely.

1. Terminal states (`completed`, `failed`) from `status.json` are definitive.
2. Otherwise, if the Docker task has failed, the status is `failed`.
3. If `status.json` says `building`: `running` once the service exists; `failed` if the service never appeared and `build.log` is present; `building` otherwise.
4. If `status.json` says `running` but no Docker service exists, the status is `stopped`.
5. If there is no usable `status.json` but the Docker service is present, the status is `running`.
6. Experiments without a status that have `results.json` or an evaluation are `completed`.
7. All other cases are `unknown`.

`status.json` is written at multiple points:
- By `core.py` when the build starts (`building`) and if the build fails.
- By `graflag_runner` when execution starts (`running`), and when it finishes (`completed` or `failed`), including execution time, resource summary, and exit code.

---

## Docker Swarm Service Model

Each experiment runs as a Docker Swarm service with specific configuration:

### Service Creation

Services are created via the Docker SDK (`client.services.create()`):

```python
client.services.create(
    image="REGISTRY:5000/method:tag",
    name="exp__method__dataset__ts",
    env=["METHOD_NAME=...", "DATA=...", "EXP=...", "_PARAM=VALUE", ...],
    mounts=[Mount(target="/shared", source="/shared", type="bind")],
    mode=ServiceMode("replicated", replicas=1),
    restart_policy=RestartPolicy(condition="none"),
    resources=Resources(generic_resources=[...]),  # GPU if enabled
    networks=["host"],
)
```

### Key Design Decisions

- **`--restart-condition none`**: Experiments are one-shot tasks. A method that exits non-zero leaves its task `Failed` and is not restarted, so a failed run stays failed. The status is tracked in `status.json`.
- **`--network host`**: Containers share the host's network stack. This provides DNS resolution and internet access (for methods that download models or data).
- **`--generic-resource NVIDIA-GPU=1`**: Reserves one GPU through Docker's generic resource model (a `DiscreteResourceSpec` named `NVIDIA-GPU`). The value is a count, not an index: `0` would reserve nothing and constrain nothing. Workers advertise their GPU as a node generic resource, and the NVIDIA runtime exposes the assigned device.
- **Bind mount**: The shared directory is bind-mounted at the same path inside the container, so all paths (DATA, EXP) are consistent between host and container.

### Local Registry

A `registry:2` service runs on the manager node (port 5000), constrained to `node.role==manager`. All method images are pushed here after building, allowing any worker node to pull them during service deployment.

### Evaluation Services

Evaluation runs as a separate service named `eval__exp__method__dataset__ts`. It uses the `graflag-evaluator` image, mounts shared storage at `/shared`, and receives the experiment path as a command argument. The evaluator service is removed after completion.

---

## Housekeeping

**Services.** `run()` removes the service when the run ends, unless `--keep-service` is given, and `graflag stop` removes the service of the run it stops; `graflag cleanup` removes the ones left otherwise, by a run the client detached from or one started with `--keep-service`. Both go through `cleanup_services()`, which touches only terminal experiments and keeps a service unless the run can be diagnosed from disk -- a `method_output.txt`, or a `status.json` recording an error -- because `graflag logs` falls back to that file once the service is gone. Cleanup never turns a completed run into a failed one.

**Storage.** `graflag clear` sweeps what `cleanup` leaves: experiments whose `methods/<name>` is gone, datasets referenced only by such experiments, images (local or in the registry) whose owner is not a method, and strays at the root of the share. It removes only on that evidence, and only with `--apply`; without it, it reports the same list.

**Registry blobs.** Deleting a repository frees no disk until the registry's blobs are garbage-collected, which `--gc` does. Collection is not safe against a live registry -- it deletes blobs the running registry still reports as present, so a later push skips layers that are gone and leaves an image workers cannot pull -- so `clear --gc` scales the registry to zero first and back up afterwards.

---

## Configuration

### Client Configuration

Run `graflag setup` to interactively configure and store settings in `~/.config/graflag/config.env`:

```bash
# Required
MANAGER_IP=192.168.100.10

# Optional (with defaults)
SSH_PORT=22
SSH_KEY=~/.ssh/id_ed25519
SHARED_DIR=/shared
HOSTS_FILE=hosts.yml
NFS_PORT=2049
GRAFLAG_LIBS=local        # or pypi
```

Config resolution order: `--config` flag > a `.env` in the working directory that defines `MANAGER_IP` > `~/.config/graflag/config.env`.

### Cluster Configuration (hosts.yml)

Used by `DockerManager` for swarm setup and by `graflag devcluster` for virtual cluster deployment. The path to this file is stored in the client configuration (`HOSTS_FILE`).

```yaml
subnet: 192.168.100.0/24
manager: 192.168.100.10
workers:
  - 192.168.100.11
  - 192.168.100.12
  - 192.168.100.13
  - 192.168.100.14
```

### Method Configuration (.env)

Each method has a `.env` file in its directory:

```bash
# Metadata (read by orchestrator)
METHOD_NAME=taddy
DESCRIPTION=Temporal Anomaly Detection in Dynamic Networks via Transformer
SOURCE_CODE=https://github.com/yuetan031/TADDY_pytorch
SOURCE_REF=dfe15ddbbc179ed0ca9a96482fe1696dfbcb5bcd   # the commit the image clones
INTEGRATION=upstream                                 # or reimplementation
SUPPORTED_DATASETS=uci,btc_alpha,btc_otc,digg        # enforced by the runner
COMMAND=python3 train_graflag.py

# Default parameters (underscore prefix, overridable via --params)
_MAX_EPOCH=200
_LEARNING_RATE=0.001
_EMBEDDING_DIM=32
_GPU=0
```

A method that shares an image declares `IMAGE=<name>` and has no Dockerfile of its own. `graflag-shared/tests/test_methods.py` enforces this contract.

---

## Experiment Output Structure

```
experiments/exp__taddy__uci__20260309_143000/
+-- status.json            # Execution status (building/running/completed/failed)
+-- service_config.json    # Full service configuration (reproducible)
+-- service_details.json   # Service and task IDs, and the task's state at creation
+-- build.log              # Pinned commit, libraries source, docker build + push output (--build only)
+-- method_output.txt      # Captured stdout/stderr from method execution
+-- results.json           # Standardized method output (scores, ground_truth)
+-- training.csv           # Training metrics from writer.spot("training", ...)
+-- validation.csv         # Validation metrics from writer.spot("validation", ...)
+-- resources.csv          # Resource usage from ResourceMonitor.spot("resources", ...)
+-- custom_metrics/        # Per-experiment custom metric plugins (optional)
+-- eval/                  # Evaluation output (created by graflag evaluate)
    +-- evaluation.json    # Computed metrics and plot references
    +-- roc_curve.png      # ROC curve plot
    +-- pr_curve.png       # Precision-Recall curve plot
    +-- score_distribution.png  # Score histogram by class
    +-- training_curves.png   # One <key>_curves.png per spot CSV
    +-- resources_curves.png  # (always present: the runner's monitor)
```

The `service_config.json` file records how the experiment was run:

```json
{
    "experiment_name": "exp__taddy__uci__20260309_143000",
    "method_name": "taddy",
    "dataset": "uci",
    "tag": "latest",
    "gpu_required": true,
    "registry_image": "192.168.100.10:5000/taddy:latest",
    "manager_ip": "192.168.100.10",
    "timestamp": "2026-03-09T14:30:00.000000",
    "data_path": "/shared/datasets/uci/",
    "exp_path": "/shared/experiments/exp__taddy__uci__20260309_143000/",
    "env_contents": {
        "METHOD_NAME": "taddy",
        "COMMAND": "python3 train_graflag.py",
        "_MAX_EPOCH": "100",
        "_BATCH_SIZE": "128"
    }
}
```

To replay an experiment, copy the file to the client and pass it to `run`:

```bash
graflag copy --from-remote -s experiments/exp__taddy__uci__20260309_143000/service_config.json --dest .
graflag run --from-config service_config.json
```

The replay takes the method, the dataset, the `_`-prefixed parameters, the `tag` and `gpu_required` from the file; options given on the command line override them. A replay without a GPU switches the method's `_GPU` to `-1` like any `--no-gpu` run, since the recorded `_GPU` is the `.env` default rather than a choice. The image is whatever the registry holds under that tag now: a method rebuilt since the original run replays with the new build.
