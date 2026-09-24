"""Core GraFlag functionality."""

import inspect
import base64
import binascii
import json
import shlex
import subprocess
import textwrap
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional
import logging

from .config import GraflagConfig
from .ssh import SSHManager, remote_path
from .docker_ops import BuildFailed, DockerManager
from .utils import load_method_env, parse_env_line
from .models import (
    ClusterInfo, MethodInfo, DatasetInfo, ExperimentInfo,
    ExperimentResults, EvaluationResults, ServiceCleanupResult,
    ClearItem, ClearReport, VerificationReport,
)

logger = logging.getLogger(__name__)


class GraFlagError(Exception):
    """Custom exception for GraFlag errors."""
    pass


class GraFlag:
    """Main GraFlag orchestration class.

    All public methods return structured data. No direct printing to stdout
    (except follow_logs which streams in real time).
    """

    def __init__(self, config_file: Optional[str] = None, interactive: bool = True):
        """Initialize GraFlag with configuration.

        `config_file` defaults to None, meaning "resolve normally" (cwd .env
        if it is a GraFlag config, else the user config). It used to default
        to the literal ".env", which config resolution now reads as an
        explicit request for that exact file -- so `GraFlag()`, the documented
        Python API entry point, failed with "Configuration file not found".

        `interactive=False` is for callers with nobody at a terminal, such as
        the MCP server: ssh then fails instead of prompting for a password or
        passphrase, and gives up on an unreachable manager after 15 seconds.
        """
        try:
            self.config = GraflagConfig(config_file)
        except ValueError as e:
            raise GraFlagError(str(e))

        self.ssh = SSHManager(
            manager_ip=self.config.manager_ip,
            ssh_port=self.config.ssh_port,
            ssh_key=self.config.ssh_key,
            batch_mode=not interactive,
        )
        self.docker = DockerManager(self.ssh, self.config, hosts_file=self.config.hosts_file)

    # ========================================================================
    # Cluster Management
    # ========================================================================

    def setup(self):
        """Setup GraFlag cluster: initialize swarm and setup workers."""
        logger.info("[SETUP] Setting up GraFlag cluster...")
        self.docker.setup_swarm_manager()
        token = self.docker.get_swarm_token()
        self.docker.setup_workers(token)
        self.docker.setup_local_registry()
        logger.info("[OK] GraFlag cluster setup completed!")

    def status(self) -> ClusterInfo:
        """Get cluster status.

        Returns:
            ClusterInfo with nodes, services, and shared directory info.
        """
        try:
            cluster = self.docker.get_cluster_status()
            shared_contents = self.ssh.list_dir(self.config.remote_shared_dir, "")

            nodes = cluster.get('nodes', [])
            worker_nodes = [
                {
                    'hostname': n['hostname'],
                    'status': n['status'],
                    'availability': n['availability'],
                    'is_manager': n['is_manager'],
                }
                for n in nodes
            ]

            return ClusterInfo(
                manager_ip=self.config.manager_ip,
                is_connected=True,
                swarm_initialized=cluster.get('swarm_active', False),
                worker_nodes=worker_nodes,
                shared_dir=self.config.remote_shared_dir,
                shared_contents=shared_contents,
                services=cluster.get('services', []),
            )
        except Exception as e:
            # A cluster that has never been set up reports a raw Docker 503
            # ("This node is not a swarm manager"), which reads like a fault
            # rather than a missing step.
            message = str(e)
            if "not a swarm manager" in message:
                message = (
                    f"Swarm is not initialized on {self.config.manager_ip}. "
                    f"Run 'graflag setup' first."
                )
            return ClusterInfo(
                manager_ip=self.config.manager_ip,
                is_connected=False,
                swarm_initialized=False,
                error=message,
            )

    # ========================================================================
    # Run
    # ========================================================================

    @staticmethod
    def make_experiment_name(method_name: str, dataset: str) -> str:
        """Build the canonical experiment directory name.

        Callers that need the name *before* the (blocking) run starts -- the
        GUI, for instance -- must use this and pass the result to :meth:`run`,
        otherwise their name and the real directory disagree on both casing
        and timestamp.
        """
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return f"exp__{method_name.lower()}__{dataset.lower()}__{timestamp}"

    def run(
        self, method_name: str, dataset: str, tag: str = "latest",
        build: bool = False, gpu: bool = True, method_params: dict = None,
        exp_name: str = None, keep_service: bool = False,
        force_rm: bool = False, follow: bool = True
    ) -> str:
        """Run experiment.

        Blocks until the run has ended either way.

        Args:
            exp_name: Pre-computed experiment name (see
                :meth:`make_experiment_name`). Generated when omitted.
            follow: Stream the container's output to stdout while waiting, as
                the CLI does. False waits by polling the experiment's status
                instead and prints nothing -- for the dashboard and the MCP
                server, where stdout is not a terminal (for the MCP server it
                is the protocol).
            keep_service: Leave the finished Swarm service in place. Useful
                when you want `docker service ps` to inspect the task.
            force_rm: With ``build``, remove the build's intermediate
                containers even when it fails (``docker build --force-rm``).

        Returns:
            Experiment name.

        Raises:
            GraFlagError: If run fails.
        """
        method_name = method_name.lower()
        dataset = dataset.lower()
        tag = tag.lower()
        method_params = method_params or {}

        exp_name = exp_name or self.make_experiment_name(method_name, dataset)

        logger.info(f"[RUN] Starting run: {exp_name}")

        # Validate method exists
        if not self.ssh.path_exists(self.config.remote_shared_dir, f"methods/{method_name}"):
            raise GraFlagError(
                f"Method {method_name} not found in {self.config.remote_shared_dir}/methods/{method_name}"
            )

        # Validate dataset exists
        if not self.ssh.path_exists(self.config.remote_shared_dir, f"datasets/{dataset}"):
            raise GraFlagError(
                f"Dataset {dataset} not found in {self.config.remote_shared_dir}/datasets/{dataset}"
            )

        # Hydrate dataset files on demand from their original sources.
        # No-op for datasets without metadata.json or marked as derived.
        self._ensure_dataset(dataset)

        # Create experiment directory
        exp_dir = f"experiments/{exp_name}"
        self.ssh.mkdir(self.config.remote_shared_dir, exp_dir)
        logger.info(f"[INFO] Experiment directory: {self.config.remote_shared_dir}/{exp_dir}")

        # Build image if requested
        if build:
            self._write_status(exp_dir, "building")
            try:
                build_log = self.docker.build_method_image(
                    method_name, tag, force_rm=force_rm)
            except Exception as e:
                error = f"Build failed: {e}"
                # The failed build is the one whose log matters: docker's
                # stderr names the step that failed, and the reason is in the
                # step output the exception carries (see BuildFailed).
                if isinstance(e, BuildFailed):
                    self._write_build_log(exp_dir, e.log)
                    error += f"\nThe build output is in {exp_dir}/build.log."
                self._write_status(exp_dir, "failed", error=error)
                raise GraFlagError(error)
            self._write_build_log(exp_dir, build_log)

        # Create service
        self.docker.create_service(exp_name, method_name, dataset, tag, gpu, method_params)

        if follow:
            # Stream the logs to stdout until the task ends.
            self.docker.follow_service_logs(exp_name)
        else:
            self.wait_for_experiment(exp_name)

        # Remove the finished service. Without this every run leaves one behind
        # forever, since restart_policy=none services are never reaped.
        # cleanup_services() refuses to remove anything whose output is not
        # already on disk, so this cannot cost us the logs for a failed run.
        if not keep_service:
            # Housekeeping must never fail a run that already succeeded, so
            # anything going wrong here is logged and dropped.
            try:
                for outcome in self.cleanup_services(experiment=exp_name):
                    if outcome.removed:
                        logger.info(f"[INFO] Removed finished service {exp_name}")
                    else:
                        logger.debug(
                            f"[INFO] Kept service {exp_name}: {outcome.reason}"
                        )
            except Exception as e:
                logger.debug(f"[INFO] Service cleanup skipped for {exp_name}: {e}")

        logger.info(f"[INFO] View logs later: graflag logs -e {exp_name}")

        # Signal failure to the caller. Without this `graflag run` exited 0
        # for an experiment that crashed, so nothing scripting GraFlag -- a CI
        # job, a sweep over methods -- could tell a successful benchmark from a
        # failed one.
        final = self._get_experiment_info(exp_name)
        if final and final.status == "failed":
            raise GraFlagError(
                f"Experiment {exp_name} failed. "
                f"See: graflag logs -e {exp_name}"
            )

        return exp_name

    def wait_for_experiment(self, experiment_name: str, poll: float = 5.0,
                            timeout: Optional[float] = None) -> Optional[ExperimentInfo]:
        """Wait until an experiment reaches a terminal status, printing nothing.

        Polls the same probe `graflag list experiments` uses, so "finished"
        means what the listing says it means, including the task that died
        before the runner could write status.json. An experiment whose
        directory holds no status and whose service is gone ("unknown") also
        ends the wait: nothing is left that could change it.

        Returns the last ExperimentInfo, or None when the experiment does not
        exist. With `timeout`, returns the current state once it has passed,
        terminal or not.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        misses = 0
        while True:
            info = self._get_experiment_info(experiment_name)
            if info is None:
                # A failed ssh reads exactly like a missing directory. One
                # dropped connection must not end the wait of a run that is
                # still going, so only a repeated miss counts as "not there".
                misses += 1
                if misses >= self.WAIT_MISSES:
                    return None
                time.sleep(poll)
                continue
            misses = 0
            if info.status in self.TERMINAL_STATUSES or (
                info.status == "unknown" and not info.service_name
            ):
                return info
            if deadline is not None and time.monotonic() >= deadline:
                return info
            time.sleep(poll)

    def _ensure_dataset(self, dataset: str):
        """Fetch any files listed in ``datasets/<dataset>/metadata.json`` that
        are missing on the shared NFS mount.

        Runs ``graflag_data`` on the manager so the download lands directly on
        the shared volume. Silently returns for datasets that have no
        ``metadata.json`` (backwards compatibility with datasets that still
        ship as full Git-LFS blobs).
        """
        shared = self.config.remote_shared_dir
        if not self.ssh.path_exists(shared, f"datasets/{dataset}/metadata.json"):
            logger.debug(
                f"[INFO] No metadata.json for dataset {dataset}; skipping fetch."
            )
            return

        cmd = (
            f"PYTHONPATH={remote_path(shared, 'libs')} python3 -m graflag_data "
            f"--root {remote_path(shared, 'datasets')} fetch {shlex.quote(dataset)}"
        )
        logger.info(f"[INFO] Ensuring dataset {dataset} is downloaded...")
        result = self.ssh.execute(cmd)
        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            raise GraFlagError(
                f"Failed to fetch dataset {dataset}: {stderr or 'unknown error'}"
            )

    def register_metric(
        self, result_type: str, metric_func: Callable,
        experiment: str = None,
    ):
        """Register a custom metric as a plugin file on the cluster.

        The function source is extracted via ``inspect.getsource`` and written
        to a ``.py`` plugin file that the evaluator loads at runtime.

        Args:
            result_type: Result type the metric applies to
                (e.g. ``"EDGE_STREAM_ANOMALY_SCORES"``).
            metric_func: A function with signature
                ``(scores, ground_truth, **kwargs) -> Dict[str, float]``.
            experiment: If given, the plugin is scoped to that experiment
                (``custom_metrics/`` inside the experiment directory).
                Otherwise it is saved to the global plugins directory.

        Raises:
            GraFlagError: If the function source cannot be extracted or the
                file cannot be written.
        """
        func_name = metric_func.__name__
        try:
            source = textwrap.dedent(inspect.getsource(metric_func))
        except (OSError, TypeError) as e:
            raise GraFlagError(
                f"Cannot extract source of {func_name}: {e}. "
                "Create the plugin file manually instead."
            )

        plugin_content = (
            f'"""Auto-generated metric plugin: {func_name}"""\n'
            f"import numpy as np\n"
            f"from graflag_evaluator import MetricCalculator\n\n"
            f"{source}\n"
            f'MetricCalculator.register_metric("{result_type}", {func_name})\n'
        )

        if experiment:
            plugin_dir = (
                f"{self.config.remote_shared_dir}/experiments/"
                f"{experiment}/custom_metrics"
            )
        else:
            plugin_dir = (
                f"{self.config.remote_shared_dir}/libs/"
                f"graflag_evaluator/plugins"
            )

        self.ssh.execute(f"mkdir -p {remote_path(plugin_dir)}")
        plugin_path = remote_path(plugin_dir, f"{func_name}.py")
        # Write via heredoc with a delimiter unlikely to appear in source
        self.ssh.execute(
            f"cat > {plugin_path} << 'PLUGINEOF'\n{plugin_content}PLUGINEOF"
        )
        logger.info(f"[OK] Saved metric plugin: {plugin_path}")

    def evaluate(self, experiment_name: str, follow: bool = True):
        """Evaluate an experiment: compute metrics and generate plots.

        Args:
            follow: Stream the evaluator's output to stdout, as the CLI does.
                False waits for it silently (see :meth:`run`).

        Raises:
            GraFlagError: If evaluation fails.
        """
        logger.info(f"[INFO] Evaluating experiment: {experiment_name}")

        if not self.ssh.path_exists(self.config.remote_shared_dir, f"experiments/{experiment_name}"):
            raise GraFlagError(f"Experiment {experiment_name} not found")

        if not self.ssh.path_exists(self.config.remote_shared_dir, f"experiments/{experiment_name}/results.json"):
            raise GraFlagError(f"results.json not found in experiment {experiment_name}")

        try:
            eval_service_name = self.docker.create_evaluation_service(experiment_name)
            if follow:
                final_state = self.docker.follow_service_logs(eval_service_name)
            else:
                final_state = self.docker.wait_for_service(eval_service_name)
            self.docker.remove_evaluation_service(experiment_name)
        except Exception as e:
            raise GraFlagError(f"Evaluation failed: {e}")

        # The service finishing is not the same as the evaluation succeeding.
        # follow_service_logs returns for a failed task exactly as it does for
        # a successful one, so this used to report success either way.
        produced = self.ssh.path_exists(
            self.config.remote_shared_dir,
            f"experiments/{experiment_name}/eval/evaluation.json",
        )
        if final_state in ("failed", "rejected") or not produced:
            raise GraFlagError(
                f"Evaluation of {experiment_name} did not produce "
                f"eval/evaluation.json"
                + (f" (task state: {final_state})" if final_state else "")
                + f". See: graflag logs -e {eval_service_name}"
            )

        eval_dir = f"{self.config.remote_shared_dir}/experiments/{experiment_name}/eval"
        logger.info(f"[INFO] Evaluation results saved to: {eval_dir}")

    def verify(self, experiment_name: str) -> VerificationReport:
        """Check that a finished experiment published a result worth believing.

        The last of the four integration gates, after :meth:`evaluate`: it
        compares what the method says it measured with what it published.
        See :mod:`graflag.verify` for the checks. Reads the experiment in one
        remote call and never moves the scores to the client.

        Raises:
            GraFlagError: If the experiment does not exist or cannot be read.
        """
        from . import verify as checks

        if not self.ssh.path_exists(self.config.remote_shared_dir,
                                    f"experiments/{experiment_name}"):
            raise GraFlagError(f"Experiment {experiment_name} not found")
        try:
            summary = checks.probe(self, experiment_name)
        except (RuntimeError, ValueError) as e:
            raise GraFlagError(f"Cannot verify {experiment_name}: {e}")

        findings = [{"level": level, "message": message}
                    for level, message in checks.check(summary)]
        count = lambda level: sum(1 for f in findings if f["level"] == level)  # noqa: E731
        return VerificationReport(
            experiment_name=experiment_name,
            findings=findings,
            failed=count("ERROR"),
            warned=count("WARN"),
            passed=count("OK"),
            probe=summary,
        )

    # ========================================================================
    # Resource Discovery
    # ========================================================================

    def list_methods(self) -> List[MethodInfo]:
        """List available methods.

        Returns:
            List of MethodInfo objects.
        """
        methods_dir = f"{self.config.remote_shared_dir}/methods"
        images_dir = f"{self.config.remote_shared_dir}/images"

        # Single SSH call: list dirs, check files, and cat all .env files.
        # The images/ sweep rides along in the same call rather than costing
        # one round trip per method -- the GUI polls this every 2 seconds.
        cmd = (
            f'for f in {images_dir}/*/Dockerfile; do '
            f'  [ -f "$f" ] && echo "IMAGE:$(basename "$(dirname "$f")")"; '
            f'done; '
            f'for d in {methods_dir}/*/; do '
            f'  name=$(basename "$d"); '
            f'  has_env=$( [ -f "$d/.env" ] && echo 1 || echo 0 ); '
            f'  has_dockerfile=$( [ -f "$d/Dockerfile" ] && echo 1 || echo 0 ); '
            f'  echo "METHOD:$name:$has_env:$has_dockerfile"; '
            f'  if [ "$has_env" = "1" ]; then '
            f'    while IFS= read -r line || [ -n "$line" ]; do '
            f'      case "$line" in ""|\\#*) continue;; esac; '
            f'      echo "ENV:$name:$line"; '
            f'    done < "$d/.env"; '
            f'  fi; '
            f'done'
        )
        result = self.ssh.execute(cmd)
        if result.returncode != 0:
            return []

        # Parse output
        method_meta = {}  # name -> {has_env, has_dockerfile}
        method_envs = {}  # name -> {key: value}
        shared_images = set()  # names with an images/<name>/Dockerfile

        for line in result.stdout.strip().split('\n'):
            if not line:
                continue
            if line.startswith('IMAGE:'):
                shared_images.add(line.split(':', 1)[1])
            elif line.startswith('METHOD:'):
                parts = line.split(':', 3)
                if len(parts) == 4:
                    name = parts[1]
                    method_meta[name] = {
                        'has_env': parts[2] == '1',
                        'has_dockerfile': parts[3] == '1',
                    }
                    method_envs.setdefault(name, {})
            elif line.startswith('ENV:'):
                parts = line.split(':', 2)
                if len(parts) == 3:
                    name = parts[1]
                    # Through the shared parser, not a bare partition: a
                    # method .env is hand-written, so `export KEY=`, quotes and
                    # trailing comments all occur, and each one used to be
                    # carried into the value and shown to the user verbatim.
                    parsed = parse_env_line(parts[2])
                    if parsed:
                        key, value = parsed
                        method_envs.setdefault(name, {})[key] = value

        methods = []
        for name in sorted(method_meta.keys()):
            meta = method_meta[name]
            env_vars = method_envs.get(name, {})
            parameters = {k: v for k, v in env_vars.items() if k.startswith('_')}

            # A method that declares IMAGE= has no Dockerfile of its own, by
            # design -- it builds from images/<IMAGE>/Dockerfile, which is the
            # whole point of seventeen bond methods sharing one ~9 GB image.
            # Resolving it the same way DockerManager._image_names does keeps
            # the listing from reporting "No Dockerfile" for a method that
            # builds and runs perfectly well.
            image = env_vars.get("IMAGE", "").strip().lower()
            has_dockerfile = (image in shared_images if image
                              else meta['has_dockerfile'])

            methods.append(MethodInfo(
                name=name,
                description=env_vars.get("DESCRIPTION", ""),
                source_code=env_vars.get("SOURCE_CODE", ""),
                integration=env_vars.get("INTEGRATION", ""),
                image=image,
                supported_data=env_vars.get("SUPPORTED_DATASETS", "Unknown"),
                parameters=parameters,
                has_dockerfile=has_dockerfile,
                has_env=meta['has_env'],
            ))

        return methods

    def list_datasets(self) -> List[DatasetInfo]:
        """List available datasets.

        Returns:
            List of DatasetInfo objects.
        """
        datasets_dir = f"{self.config.remote_shared_dir}/datasets"

        # Single SSH call: list all datasets with size and file count
        cmd = (
            f'for d in {datasets_dir}/*/; do '
            f'  [ -d "$d" ] || continue; '
            f'  name=$(basename "$d"); '
            # KiB, converted below: `du -sm` rounds up to whole megabytes,
            # which reported every 1 KB descriptor stub as a 1 MB dataset.
            f'  size=$(du -sk "$d" 2>/dev/null | cut -f1 || echo 0); '
            f'  count=$(find "$d" -type f 2>/dev/null | wc -l); '
            f'  echo "$name:$size:$count"; '
            f'done'
        )
        result = self.ssh.execute(cmd)
        if result.returncode != 0:
            return []

        datasets = []
        for line in result.stdout.strip().split('\n'):
            if not line or ':' not in line:
                continue
            parts = line.split(':')
            if len(parts) >= 3:
                name = parts[0]
                try:
                    size_mb = float(parts[1]) / 1024.0
                except (ValueError, IndexError):
                    size_mb = 0.0
                try:
                    file_count = int(parts[2])
                except (ValueError, IndexError):
                    file_count = 0

                datasets.append(DatasetInfo(
                    name=name,
                    path=f"{datasets_dir}/{name}",
                    size_mb=size_mb,
                    file_count=file_count,
                ))

        return sorted(datasets, key=lambda d: d.name)

    def count_experiments(self) -> int:
        """How many experiment directories exist on the share.

        `list_experiments` takes a limit and gives no indication when it
        truncates, so a dashboard showing 50 of 78 looked complete. This is
        the cheap other half: one `ls | wc -l`, no per-experiment probing, so
        it can be called alongside the list without doubling its cost.

        Prefers the total captured by the most recent `list_experiments`,
        which gets it from the same remote call it already makes. Asking
        separately doubled the cold cost of the GUI's most-polled endpoint
        (measured: 437ms -> 1.06s) for a single integer.

        Returns:
            The directory count, or 0 when the experiments root is absent.
        """
        cached = getattr(self, "_last_experiment_total", None)
        if cached is not None:
            return cached

        exp_root = remote_path(self.config.remote_shared_dir, "experiments")
        result = self.ssh.execute(
            f'cd {exp_root} 2>/dev/null || exit 0; '
            f'ls -1 2>/dev/null | wc -l'
        )
        if result.returncode != 0:
            return 0
        try:
            return int((result.stdout or "0").strip() or 0)
        except ValueError:
            return 0

    def list_experiments(self, limit: int = 50,
                         offset: int = 0) -> List[ExperimentInfo]:
        """List recent experiments.

        Probes every experiment in a single remote command. The previous
        implementation issued one SSH connection per experiment directory --
        and there is no ControlMaster, so each was a fresh TCP and auth
        handshake. With 50 experiments that was 52 connections, repeated every
        two seconds by the GUI's background updater. `limit` was also applied
        only after probing everything.

        Args:
            limit: how many to return.
            offset: how many of the newest to skip. Paging was done in the
                browser over whatever the API had already capped, so the last
                page was the end of the cap rather than the end of the data.

        Returns:
            List of ExperimentInfo (most recent first).
        """
        shared = self.config.remote_shared_dir
        exp_root = remote_path(shared, "experiments")

        # One call: newest-first listing plus, for each, the file checks and
        # base64'd status.json that _get_experiment_info would have fetched.
        cmd = (
            f'cd {exp_root} 2>/dev/null || exit 0; '
            # The total comes from the same call. Asking for it separately
            # doubled the cold cost of the GUI's most-polled endpoint (437ms
            # -> 1.06s measured) for one integer.
            f'echo "TOTAL:$(ls -1 2>/dev/null | wc -l)"; '
            f'for d in $(ls -1t 2>/dev/null); do '
            f'  [ -d "$d" ] || continue; '
            f'  echo "EXP:$d"; '
            f'  echo "RESULTS:$( [ -f "$d/results.json" ] && echo 1 || echo 0 )"; '
            f'  echo "EVAL:$( [ -f "$d/eval/evaluation.json" ] && echo 1 || echo 0 )"; '
            f'  echo "BUILD_LOG:$( [ -f "$d/build.log" ] && echo 1 || echo 0 )"; '
            f'  echo "STATUS_B64:$(base64 < "$d/status.json" 2>/dev/null | tr -d \'\\n\')"; '
            f'done'
        )
        result = self.ssh.execute(cmd)
        if result.returncode != 0 or not result.stdout.strip():
            return []

        try:
            running_services = self.docker.get_service_names()
        except Exception:
            running_services = set()

        experiments = []
        current = None
        total_seen = None
        for line in result.stdout.split("\n"):
            if line.startswith("TOTAL:"):
                try:
                    total_seen = int(line[len("TOTAL:"):].strip())
                except ValueError:
                    total_seen = None
                continue
            line = line.rstrip("\n")
            if line.startswith("EXP:"):
                if current:
                    experiments.append(current)
                current = {"name": line[4:].strip()}
            elif current is None:
                continue
            elif line.startswith("STATUS_B64:"):
                current["status_json"] = self._decode_status(
                    line[len("STATUS_B64:"):].strip(), current["name"]
                )
            elif ":" in line:
                key, _, val = line.partition(":")
                current[key] = val.strip() == "1"
        if current:
            experiments.append(current)

        infos = [
            self._build_experiment_info(entry, running_services)
            for entry in experiments
        ]
        infos = [i for i in infos if i]
        infos.sort(key=lambda e: e.timestamp or "", reverse=True)
        # Captured from the same remote call, for count_experiments() to reuse.
        # The listing already enumerates every directory -- the limit is
        # applied here, in Python -- so the total costs nothing extra.
        self._last_experiment_total = (
            total_seen if total_seen is not None else len(infos))
        return infos[offset:offset + limit] if limit else infos[offset:]

    def list_services(self) -> List[Dict]:
        """List running Docker services.

        Returns:
            List of service dicts with name, replicas, image, status.
        """
        return self.docker.list_services()

    # ========================================================================
    # Logs
    # ========================================================================

    def get_logs(self, experiment_name: str, tail: int = 100) -> List[str]:
        """Get experiment logs (non-streaming).

        Tries Docker service logs first, then falls back to method_output.txt.

        Returns:
            List of log lines.
        """
        # Try Docker service logs
        logs = self.docker.get_service_logs(experiment_name, tail=tail)
        if logs:
            return logs

        # Fall back to saved output
        output_path = f"experiments/{experiment_name}/method_output.txt"
        if self.ssh.path_exists(self.config.remote_shared_dir, output_path):
            content = self.ssh.read_file(self.config.remote_shared_dir, output_path)
            if content.strip():
                lines = content.strip().split('\n')
                return lines[-tail:] if len(lines) > tail else lines

        return []

    def follow_logs(self, experiment_name: str, tee_file: str = None):
        """Follow logs for an experiment (streams to stdout).

        Shows build log (if exists) + service logs.
        Falls back to method_output.txt if the service is gone.
        """
        exp_base = f"experiments/{experiment_name}"
        output_parts = []

        # Show build log if it exists
        build_log_path = f"{exp_base}/build.log"
        if self.ssh.path_exists(self.config.remote_shared_dir, build_log_path):
            build_content = self.ssh.read_file(self.config.remote_shared_dir, build_log_path)
            if build_content.strip():
                output_parts.append(build_content)

        # Try Docker service logs (follow mode)
        if self.docker.service_exists(experiment_name):
            if output_parts:
                print("\n".join(output_parts))
                print("\n" + "=" * 60)
                print("=== SERVICE LOGS ===")
                print("=" * 60 + "\n")
            streamed = []
            self.docker.follow_service_logs(
                experiment_name,
                on_line=streamed.append if tee_file else None,
            )
            self._save_tee(tee_file, output_parts + streamed)
            return

        # Service no longer exists -- fall back to method_output.txt
        output_path = f"{exp_base}/method_output.txt"
        if self.ssh.path_exists(self.config.remote_shared_dir, output_path):
            logger.info("[INFO] Service removed. Showing saved output:")
            content = self.ssh.read_file(self.config.remote_shared_dir, output_path)
            output_parts.append(content)
            print("\n".join(output_parts))
            self._save_tee(tee_file, output_parts)
        elif output_parts:
            print("\n".join(output_parts))
            self._save_tee(tee_file, output_parts)
        else:
            raise GraFlagError(f"No logs found for experiment '{experiment_name}'")

    def show_logs(self, experiment_name: str, tee_file: str = None):
        """Show logs (non-follow mode) — prints to stdout."""
        exp_base = f"experiments/{experiment_name}"
        output_parts = []

        # Show build log if it exists
        build_log_path = f"{exp_base}/build.log"
        if self.ssh.path_exists(self.config.remote_shared_dir, build_log_path):
            build_content = self.ssh.read_file(self.config.remote_shared_dir, build_log_path)
            if build_content.strip():
                output_parts.append(build_content)

        # Try Docker service logs (non-follow)
        logs = self.docker.get_service_logs(experiment_name)
        if logs:
            if output_parts:
                output_parts.append("\n" + "=" * 60)
                output_parts.append("=== SERVICE LOGS ===")
                output_parts.append("=" * 60 + "\n")
            output_parts.extend(logs)
            print("\n".join(output_parts))
            self._save_tee(tee_file, output_parts)
            return

        # Fall back to method_output.txt
        output_path = f"{exp_base}/method_output.txt"
        if self.ssh.path_exists(self.config.remote_shared_dir, output_path):
            logger.info("[INFO] Service removed. Showing saved output:")
            content = self.ssh.read_file(self.config.remote_shared_dir, output_path)
            output_parts.append(content)
            print("\n".join(output_parts))
            self._save_tee(tee_file, output_parts)
        elif output_parts:
            print("\n".join(output_parts))
            self._save_tee(tee_file, output_parts)
        else:
            raise GraFlagError(f"No logs found for experiment '{experiment_name}'")

    # ========================================================================
    # Service Control
    # ========================================================================

    def stop(self, experiment_name: str, remove: bool = False):
        """Stop a running experiment/service.

        Args:
            experiment_name: Name of the experiment
            remove: If True, also delete the experiment directory
        """
        logger.info(f"[STOP] Stopping experiment: {experiment_name}")
        try:
            self.docker.stop_service(experiment_name)
            logger.info(f"[OK] Service {experiment_name} stopped")
        except ValueError:
            logger.info(f"[INFO] No running service for {experiment_name}")

        if remove:
            exp_path = f"{self.config.remote_shared_dir}/experiments/{experiment_name}"
            if self.ssh.path_exists(self.config.remote_shared_dir, f"experiments/{experiment_name}"):
                self.ssh.execute(f"rm -rf {remote_path(exp_path)}")
                logger.info(f"[INFO] Deleted experiment directory: {exp_path}")

    # ========================================================================
    # Service Cleanup
    # ========================================================================

    # Experiments in these states will never produce more output, so their
    # Swarm service is only holding task records.
    TERMINAL_STATUSES = ("completed", "failed", "stopped")

    #: Consecutive failed probes after which wait_for_experiment() concludes
    #: that the experiment is gone rather than that ssh blinked.
    WAIT_MISSES = 3

    def cleanup_services(
        self, experiment: str = None, dry_run: bool = False
    ) -> List[ServiceCleanupResult]:
        """Remove Swarm services for experiments that have finished.

        Swarm services are created with ``restart_policy=none`` and were never
        removed, so every run left one behind forever. They are what
        ``docker service ls`` and ``graflag list services`` show, and they hold
        task records on the manager indefinitely.

        Two safety rules, because removing a service is not reversible:

        1. Only terminal experiments are touched. Anything still running or
           building keeps its service.
        2. The service is kept unless the run's output is already on disk.
           ``graflag logs`` falls back to ``method_output.txt`` once the
           service is gone, but a run that died before the runner wrote that
           file has its Docker logs as the only diagnostic -- removing the
           service would destroy the evidence for the failure you most need to
           read.

        Args:
            experiment: Limit to one experiment. Default: sweep all.
            dry_run: Report what would be removed without removing it.

        Returns:
            One ServiceCleanupResult per candidate considered.
        """
        experiments = self.list_experiments(limit=10_000)
        if experiment:
            experiments = [e for e in experiments if e.name == experiment]
            if not experiments:
                raise GraFlagError(f"Experiment {experiment} not found")

        candidates = [e for e in experiments if e.service_name]
        if not candidates:
            return []

        have_output = self._experiments_with_saved_output(
            [e.name for e in candidates]
        )

        results = []
        for exp in candidates:
            if exp.status not in self.TERMINAL_STATUSES:
                results.append(ServiceCleanupResult(
                    exp.name, False, f"still {exp.status}"
                ))
                continue
            if exp.name not in have_output:
                results.append(ServiceCleanupResult(
                    exp.name, False,
                    "no saved output or recorded error; the service logs are "
                    "the only record",
                ))
                continue
            if dry_run:
                results.append(ServiceCleanupResult(
                    exp.name, False, f"would remove ({exp.status})"
                ))
                continue

            try:
                self.docker.cleanup_finished_service(exp.name)
                results.append(ServiceCleanupResult(
                    exp.name, True, f"({exp.status})"
                ))
            except Exception as e:
                results.append(ServiceCleanupResult(exp.name, False, str(e)))

        return results

    #: The five directories GraFlag owns at the root of the share. They are
    #: named here as well as in the survey so that a widened rule in one place
    #: cannot reach them: the survey decides what is *collected*, this decides
    #: what may be *deleted*.
    OWNED_DIRS = ("datasets", "experiments", "images", "libs", "methods")

    def clear(self, apply: bool = False, share: bool = True,
              images: bool = True, collect: bool = False) -> ClearReport:
        """Remove what is on the cluster but no longer belongs to anything.

        ``graflag cleanup`` sweeps finished *services*; this sweeps the
        storage they leave behind. An item is removed only on evidence that
        nothing owns it any more, never because its name looks disposable:

        - an **experiment** whose ``methods/<name>`` directory is gone. The
          method was deleted or renamed, so the run can no longer be
          reproduced or even read against its definition.
        - a **dataset** referenced only by such experiments. A dataset no
          experiment has used yet is kept -- it is there to be run, and "used
          by nothing" is its normal state before the first run.
        - an **image**, local or in the registry, whose owner is not a method.
          The owner is ``IMAGE=`` from the method's ``.env`` when it declares
          one, so the seventeen ``bond_*`` methods keep ``bond_base`` alive
          between them.
        - a **stray** at the root of the share: anything beside the five
          directories GraFlag owns.

        Nothing is removed unless ``apply`` is true. The dry run reports the
        same rows with ``removed`` false, so the two are diffable.

        ``collect`` additionally garbage-collects the registry's blobs, which
        is what actually reclaims the disk -- deleting a repository removes
        its manifests and leaves every layer behind. It is a separate flag
        because it cannot be done safely while the registry is serving:
        garbage collection deletes blobs the running registry still answers
        ``HEAD`` for from its cache, so the next ``docker push`` reports
        "Layer already exists", uploads nothing, and produces an image the
        manager can run and a worker cannot pull. This scales the service to
        zero first and back up afterwards, in a ``finally``, so an error
        during collection cannot leave the cluster without a registry.

        Args:
            apply: Remove. Default is to report only.
            share: Consider experiments, datasets and strays.
            images: Consider local images and registry repositories.
            collect: Garbage-collect registry blobs (implies ``images``).

        Returns:
            A ClearReport. Failures to remove an individual item are
            collected in ``errors`` rather than raised -- clearing is
            housekeeping and must not fail the caller.
        """
        report = ClearReport(applied=apply)
        facts = self._clear_survey(share=share, images=images or collect)

        items: List[ClearItem] = []
        if share:
            items.extend(self._clear_share_items(facts))
        if images or collect:
            items.extend(self._clear_image_items(facts))
        report.items = items

        if not apply:
            return report

        self._clear_apply(report, facts)
        if collect:
            self._clear_collect_registry(report, facts)
        return report

    def _clear_survey(self, share: bool, images: bool) -> dict:
        """Gather every fact the decision needs, in one remote call.

        One script rather than a call per item, for the reason
        ``list_experiments`` is written the same way: a share with fifty
        experiments and twenty images would otherwise be seventy round trips.
        The script only *reports*; every verdict is reached in Python, where
        it is testable without a cluster.
        """
        shared = self.config.remote_shared_dir
        script = textwrap.dedent(f"""
            set -u
            SHARED={remote_path(shared)}
            cd "$SHARED" 2>/dev/null || exit 0
            for d in methods/*/; do
                [ -d "$d" ] || continue
                n=${{d%/}}; n=${{n#methods/}}
                echo "M $n"
                [ -f "$d.env" ] && grep -E '^[[:space:]]*IMAGE=' "$d.env" 2>/dev/null \
                    | head -1 | sed "s|^|O $n |"
            done
            for d in experiments/*/; do
                [ -d "$d" ] || continue
                n=${{d%/}}; echo "E ${{n#experiments/}}"
            done
            for d in datasets/*/; do
                [ -d "$d" ] || continue
                n=${{d%/}}; echo "D ${{n#datasets/}}"
            done
            for e in */; do
                e=${{e%/}}
                [ -d "$e" ] || continue
                case "$e" in
                    datasets|experiments|images|libs|methods) continue ;;
                esac
                echo "S $e"
            done
            du -sbL experiments/* datasets/* 2>/dev/null | sed 's|^|SZ |'
            if [ "{int(images)}" = "1" ] && docker info >/dev/null 2>&1; then
                docker image ls --no-trunc --format 'I {{{{.Repository}}}} {{{{.Tag}}}} {{{{.ID}}}}' 2>/dev/null
                ids=$(docker image ls -q --no-trunc 2>/dev/null | sort -u | tr '\n' ' ')
                [ -n "$ids" ] && docker image inspect $ids \
                    --format 'IS {{{{.Id}}}} {{{{.Size}}}}' 2>/dev/null
                c=$(docker ps --filter name=registry --format '{{{{.ID}}}}' 2>/dev/null | head -1)
                if [ -n "$c" ]; then
                    echo "RC $c"
                    docker exec "$c" ls /var/lib/registry/docker/registry/v2/repositories \
                        2>/dev/null | sed 's|^|R |'
                    docker exec "$c" du -sb /var/lib/registry 2>/dev/null \
                        | awk '{{print "RSZ", $1}}'
                fi
                docker inspect "$c" \
                    --format 'RV {{{{range .Mounts}}}}{{{{.Name}}}}{{{{end}}}}' 2>/dev/null
            fi
            df -B1 --output=avail /var/lib/docker 2>/dev/null | tail -1 | sed 's|^|AVAIL |'
        """).strip()

        result = self.ssh.execute(script)
        facts = {
            "methods": set(), "owners": set(), "experiments": [], "datasets": set(),
            "strays": [], "sizes": {}, "images": [], "image_sizes": {},
            "registry_repos": [], "registry_container": "", "registry_volume": "",
            "registry_bytes": 0, "avail_bytes": 0,
        }
        for line in (result.stdout or "").splitlines():
            tag, _, rest = line.strip().partition(" ")
            if not rest and tag not in ("",):
                continue
            if tag == "M":
                facts["methods"].add(rest)
            elif tag == "O":
                name, _, env_line = rest.partition(" ")
                parsed = parse_env_line(env_line)
                if parsed and parsed[1]:
                    facts["owners"].add(parsed[1].lower())
            elif tag == "E":
                facts["experiments"].append(rest)
            elif tag == "D":
                facts["datasets"].add(rest)
            elif tag == "S":
                facts["strays"].append(rest)
            elif tag == "SZ":
                size, _, path = rest.partition("\t")
                if not path:
                    size, _, path = rest.partition(" ")
                try:
                    facts["sizes"][path.strip()] = int(size)
                except ValueError:
                    pass
            elif tag == "I":
                parts = rest.split()
                if len(parts) == 3:
                    facts["images"].append(tuple(parts))
            elif tag == "IS":
                image_id, _, size = rest.partition(" ")
                try:
                    facts["image_sizes"][image_id] = int(size)
                except ValueError:
                    pass
            elif tag == "R":
                facts["registry_repos"].append(rest)
            elif tag == "RC":
                facts["registry_container"] = rest
            elif tag == "RV":
                facts["registry_volume"] = rest
            elif tag == "RSZ":
                try:
                    facts["registry_bytes"] = int(rest)
                except ValueError:
                    pass
            elif tag == "AVAIL":
                try:
                    facts["avail_bytes"] = int(rest)
                except ValueError:
                    pass
        # A method always owns an image under its own name unless its .env
        # points elsewhere; the survey only reports the redirect.
        facts["owners"] |= {m.lower() for m in facts["methods"]}
        return facts

    @staticmethod
    def _experiment_parts(name: str):
        """(method, dataset) from `exp__METHOD__DATASET__TIMESTAMP`.

        Returns (None, None) for a directory that does not carry the naming,
        which is what keeps a hand-made folder from being read as an
        experiment whose method happens not to exist.
        """
        if not name.startswith("exp__"):
            return None, None
        parts = name.split("__")
        if len(parts) < 4:
            return None, None
        return parts[1], parts[2]

    def _clear_share_items(self, facts: dict) -> List[ClearItem]:
        """Decide which experiments, datasets and strays are orphaned."""
        methods = {m.lower() for m in facts["methods"]}
        dead, live_datasets, dead_datasets = [], set(), set()

        for name in facts["experiments"]:
            method, dataset = self._experiment_parts(name)
            if method is None:
                continue
            if method.lower() in methods:
                if dataset:
                    live_datasets.add(dataset.lower())
                continue
            dead.append((name, method))
            if dataset:
                dead_datasets.add(dataset.lower())

        items = [
            ClearItem(kind="experiment", name=name,
                      reason=f"methods/{method} no longer exists",
                      size_bytes=facts["sizes"].get(f"experiments/{name}", 0))
            for name, method in sorted(dead)
        ]
        for dataset in sorted(facts["datasets"]):
            key = dataset.lower()
            if key in dead_datasets and key not in live_datasets:
                items.append(ClearItem(
                    kind="dataset", name=dataset,
                    reason="used only by experiments removed here",
                    size_bytes=facts["sizes"].get(f"datasets/{dataset}", 0)))
        items.extend(
            ClearItem(kind="stray", name=name,
                      reason="a directory at the root of the share that GraFlag "
                             "does not use")
            for name in sorted(facts["strays"])
        )
        return items

    @staticmethod
    def _image_owner(repository: str) -> str:
        """The name an image is owned under, from its repository.

        Strips the cluster registry prefix so that `10.0.0.1:5000/taddy` and
        `taddy` resolve to the same owner, and normalises `-` to `_` because
        the evaluator is built as `graflag-evaluator` and named
        `graflag_evaluator` in the sources -- a difference that silently
        turned off the rule protecting it.
        """
        repo = repository.rsplit("/", 1)[-1] if ":5000/" in repository else repository
        return repo.lower().replace("-", "_")

    def _clear_image_items(self, facts: dict) -> List[ClearItem]:
        """Decide which images and registry repositories are orphaned.

        The rule is evidence of *non-use*, not absence of ownership. Those are
        not the same thing and the difference is expensive: `nvidia/cuda`,
        `python` and `ubuntu` are owned by no method and are what every method
        Dockerfile builds `FROM`, so a rule that collects whatever no method
        claims deletes the base layers of the entire cluster. A first dry run
        of this command proposed exactly that, along with both evaluator
        images.

        So an image is collected only when something says it is dead:

        - it is **dangling** -- no repository at all, so nothing can name it;
        - or its owner is an **orphan owner**: a method that no longer exists,
          evidenced by an experiment directory naming it or by a repository
          in GraFlag's own registry, which holds nothing GraFlag did not push.

        An image GraFlag never built is never a candidate, whatever its name.
        """
        known = set(facts["owners"]) | {"graflag_evaluator"}
        known = {self._image_owner(o) for o in known}

        orphan_owners = set()
        methods = {m.lower() for m in facts["methods"]}
        for name in facts["experiments"]:
            method, _ = self._experiment_parts(name)
            if method and method.lower() not in methods:
                orphan_owners.add(self._image_owner(method))
        for repo in facts["registry_repos"]:
            # The registry is GraFlag's: anything in it was pushed by a build
            # here, so a repository no method claims is a method that is gone.
            owner = self._image_owner(repo)
            if owner not in known:
                orphan_owners.add(owner)

        items = []
        for repo, tag, image_id in facts["images"]:
            if repo == "<none>":
                items.append(ClearItem(
                    kind="image", name=image_id, reason="dangling: no repository",
                    size_bytes=facts["image_sizes"].get(image_id, 0)))
            elif self._image_owner(repo) in orphan_owners:
                items.append(ClearItem(
                    kind="image", name=f"{repo}:{tag}",
                    reason="built for a method that no longer exists",
                    size_bytes=facts["image_sizes"].get(image_id, 0)))
        for repo in sorted(facts["registry_repos"]):
            if self._image_owner(repo) in orphan_owners:
                items.append(ClearItem(
                    kind="registry", name=repo,
                    reason="built for a method that no longer exists"))
        return items

    def _clear_guard(self, path: str) -> str:
        """Refuse a path that is not a removable child of the share.

        The rule is written as a whitelist of shape -- a non-empty name
        directly under SHARED, no traversal, not one of the five GraFlag owns
        -- because the failure being guarded against is a *widened* rule
        upstream, and a blacklist only stops what it was told about.
        """
        shared = self.config.remote_shared_dir.rstrip("/")
        if ".." in path:
            raise GraFlagError(f"refusing to remove {path}: contains '..'")
        if not path.startswith(shared + "/"):
            raise GraFlagError(f"refusing to remove {path}: outside {shared}")
        rest = path[len(shared) + 1:]
        if not rest:
            raise GraFlagError(f"refusing to remove {path}: it is the share itself")
        if rest in self.OWNED_DIRS:
            raise GraFlagError(f"refusing to remove {path}: GraFlag owns it")
        return path

    def _clear_apply(self, report: ClearReport, facts: dict) -> None:
        """Remove every decided item, in one remote call per kind."""
        shared = self.config.remote_shared_dir
        paths, by_path = [], {}
        for item in report.items:
            if item.kind == "experiment":
                target = f"{shared.rstrip('/')}/experiments/{item.name}"
            elif item.kind == "dataset":
                target = f"{shared.rstrip('/')}/datasets/{item.name}"
            elif item.kind == "stray":
                target = f"{shared.rstrip('/')}/{item.name}"
            else:
                continue
            try:
                paths.append(self._clear_guard(target))
                by_path[target] = item
            except GraFlagError as exc:
                report.errors.append(str(exc))

        if paths:
            quoted = " ".join(remote_path(p) for p in paths)
            result = self.ssh.execute(f"rm -rf {quoted} && echo CLEARED")
            if "CLEARED" in (result.stdout or ""):
                for item in by_path.values():
                    item.removed = True
                    report.freed_bytes += item.size_bytes
            else:
                report.errors.append(
                    f"removing share items failed: {(result.stderr or '').strip()}")

        local = [i for i in report.items if i.kind == "image"]
        if local:
            # One call, but a verdict per image rather than one for the batch:
            # `docker image rm` keeps going after a failure, and its prose
            # ("Untagged: ...", "image is being used by container ...") is not
            # something to decide a deletion from. The exit status is, so the
            # script turns each one into a line naming the image.
            script = "\n".join(
                f"docker image rm -f {shlex.quote(i.name)} >/dev/null 2>&1 "
                f"&& echo RM {shlex.quote(i.name)} || echo KEPT {shlex.quote(i.name)}"
                for i in local)
            result = self.ssh.execute(script)
            cleared = {line.split(" ", 1)[1]
                       for line in (result.stdout or "").splitlines()
                       if line.startswith("RM ")}
            for item in local:
                if item.name in cleared:
                    item.removed = True
                    report.freed_bytes += item.size_bytes
                else:
                    report.errors.append(f"could not remove image {item.name}")

        repos = [i for i in report.items if i.kind == "registry"]
        container = facts.get("registry_container")
        if repos and container:
            base = "/var/lib/registry/docker/registry/v2/repositories"
            for item in repos:
                target = f"{base}/{item.name}"
                if ".." in target:
                    report.errors.append(f"refusing to remove {target}: contains '..'")
                    continue
                result = self.ssh.execute(
                    f"docker exec {shlex.quote(container)} rm -rf {remote_path(target)} "
                    f"&& echo CLEARED")
                if "CLEARED" in (result.stdout or ""):
                    item.removed = True
                else:
                    report.errors.append(f"could not remove registry repo {item.name}")
        elif repos:
            report.errors.append("no registry container found; repositories kept")

    def _clear_collect_registry(self, report: ClearReport, facts: dict) -> None:
        """Garbage-collect registry blobs with the registry stopped.

        Deleting a repository above removed manifests and tags; every layer
        they referenced is still on disk until this runs. It has to run
        against a stopped registry -- see `clear` -- so the service is scaled
        to zero, collected in a throwaway container against the same volume,
        and scaled back in a `finally`.
        """
        volume = facts.get("registry_volume")
        if not volume:
            report.errors.append("registry volume not found; blobs not collected")
            return

        before = facts.get("registry_bytes", 0)
        try:
            self.ssh.execute("docker service scale --detach registry=0")
            # Scale returns before the task is gone; collecting against a
            # registry that is still writing is the failure mode this whole
            # dance exists to avoid.
            self.ssh.execute(
                "for i in $(seq 1 60); do "
                "  [ -z \"$(docker ps -q --filter name=registry)\" ] && break; "
                "  sleep 1; "
                "done")
            # The serving config has no `delete.enabled`, so
            # `--delete-untagged` against it refuses -- and without it the
            # collection keeps every manifest a previous build left untagged,
            # which is most of what is reclaimable. Rather than enable
            # deletion on the running service, the throwaway collector is
            # given its own config pointing at the same directory: the
            # capability exists only for the life of this container, while
            # the registry the cluster talks to is unchanged.
            gc_config = (
                "version: 0.1\n"
                "storage:\n"
                "  filesystem:\n"
                "    rootdirectory: /var/lib/registry\n"
                "  delete:\n"
                "    enabled: true\n")
            result = self.ssh.execute(
                f"docker run --rm -v {shlex.quote(volume)}:/var/lib/registry "
                f"--entrypoint sh registry:2 -c "
                f"{shlex.quote(f'printf %s {shlex.quote(gc_config)} > /tmp/gc.yml && registry garbage-collect /tmp/gc.yml --delete-untagged')}"
                f" 2>&1")
            after = self.ssh.execute(
                f"docker run --rm -v {shlex.quote(volume)}:/var/lib/registry "
                f"--entrypoint du registry:2 -sb /var/lib/registry "
                f"2>/dev/null | cut -f1")
            try:
                freed = before - int((after.stdout or "0").strip().split()[0])
            except (ValueError, IndexError):
                freed = 0
            if freed > 0:
                report.freed_bytes += freed
            report.registry_collected = True
            if result.returncode != 0:
                report.errors.append(
                    f"garbage collection reported an error: "
                    f"{(result.stdout or result.stderr or '').strip()[:200]}")
        finally:
            # Scaling back is not the same as being back: `--detach` returns
            # as soon as the desired state is recorded, so reporting success
            # on its exit status would say the registry is up while the task
            # is still being scheduled -- and say it just as confidently if
            # the task never starts. Wait for a running container and report
            # on that instead.
            self.ssh.execute("docker service scale --detach registry=1")
            restored = self.ssh.execute(
                "for i in $(seq 1 60); do "
                "  [ -n \"$(docker ps -q --filter name=registry)\" ] "
                "&& { echo REGISTRY_UP; break; }; "
                "  sleep 1; "
                "done")
            if "REGISTRY_UP" not in (restored.stdout or ""):
                report.errors.append(
                    "REGISTRY DID NOT COME BACK UP -- run "
                    "'docker service scale registry=1' on the manager. Until "
                    "it is up no node can pull a method image.")

    def _experiments_with_saved_output(self, names: List[str]) -> set:
        """Names whose failure is diagnosable without the Docker service logs.

        Either the method's captured output is on disk, or status.json records
        an error -- which is what the runner writes when it fails during
        startup, before any method output exists. Without the second case a
        run that died at startup would keep its service forever.
        """
        if not names:
            return set()

        exp_root = remote_path(self.config.remote_shared_dir, "experiments")
        checks = " ".join(shlex.quote(n) for n in names)
        cmd = (
            f'cd {exp_root} 2>/dev/null || exit 0; '
            f'for d in {checks}; do '
            f'  if [ -s "$d/method_output.txt" ] || '
            f'grep -q \'"error"\' "$d/status.json" 2>/dev/null; then '
            f'    echo "$d"; '
            f'  fi; '
            f'done'
        )
        result = self.ssh.execute(cmd)
        if result.returncode != 0:
            return set()
        return {line.strip() for line in result.stdout.split("\n") if line.strip()}

    # ========================================================================
    # Results
    # ========================================================================

    def get_experiment_results(self, experiment_name: str) -> Optional[ExperimentResults]:
        """Get experiment results from results.json."""
        results_path = remote_path(
            self.config.remote_shared_dir, "experiments", experiment_name, "results.json"
        )
        result = self.ssh.execute(f"cat {results_path} 2>/dev/null")
        if result.returncode != 0 or not result.stdout.strip():
            return None

        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            return None

        metadata = data.get("metadata", {})
        return ExperimentResults(
            experiment_name=experiment_name,
            method_name=metadata.get("method_name", ""),
            dataset=metadata.get("dataset", ""),
            metadata=metadata,
            execution_time_ms=metadata.get("exec_time_ms"),
            peak_memory_mb=metadata.get("peak_memory_mb"),
            peak_gpu_memory_mb=metadata.get("peak_gpu_mb"),
            result_type=data.get("result_type"),
            scores_available="scores" in data or "scores_file" in data,
        )

    def get_evaluation_results(self, experiment_name: str) -> Optional[EvaluationResults]:
        """Get evaluation results from eval/evaluation.json."""
        eval_path = (
            f"{self.config.remote_shared_dir}/experiments/{experiment_name}/eval"
        )
        eval_json = remote_path(eval_path, "evaluation.json")

        result = self.ssh.execute(f"cat {eval_json} 2>/dev/null")
        if result.returncode != 0 or not result.stdout.strip():
            return None

        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            return None

        # List plot files
        plots = []
        result = self.ssh.execute(
            f"ls -1 {remote_path(eval_path)}/*.png 2>/dev/null || true"
        )
        if result.returncode == 0 and result.stdout.strip():
            for line in result.stdout.strip().split('\n'):
                if line.strip():
                    plots.append(line.strip().split('/')[-1])

        return EvaluationResults(
            experiment_name=experiment_name,
            metrics=data.get("metrics", {}),
            plots_available=plots,
            evaluation_path=eval_path,
        )

    # ========================================================================
    # File Operations
    # ========================================================================

    def copy_files(self, source_paths, dest_path: str, recursive: bool = False, from_remote: bool = False):
        """Copy files/directories bidirectionally."""
        if from_remote:
            remote_sources = []
            for src in (source_paths if isinstance(source_paths, list) else [source_paths]):
                clean_src = src.lstrip('/')
                remote_sources.append(f"{self.config.remote_shared_dir}/{clean_src}")
            return self.ssh.copy_files(remote_sources, dest_path, recursive, from_remote=True)
        else:
            clean_dest = dest_path.lstrip('/')
            remote_dest = f"{self.config.remote_shared_dir}/{clean_dest}"
            return self.ssh.copy_files(source_paths, remote_dest, recursive, from_remote=False)

    def mount_nfs(self, shared_dir: str):
        """Mount NFS share on local machine."""
        mount_dir = Path(shared_dir).expanduser()
        try:
            mount_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.warning("[WARN] Stale NFS mount detected, cleaning up...")
            subprocess.run(f"sudo umount -l {mount_dir}", shell=True, capture_output=True)
            mount_dir.mkdir(parents=True, exist_ok=True)

        result = subprocess.run(f"mountpoint -q {mount_dir}", shell=True)
        if result.returncode == 0:
            logger.info(f"[OK] NFS already mounted at {mount_dir}")
            return

        mount_cmd = (
            f"sudo mount -t nfs "
            f"-o addr={self.config.manager_ip},port={self.config.nfs_port},"
            f"vers=3,hard,intr,rsize=8192,wsize=8192,timeo=30,retrans=3 "
            f"{self.config.manager_ip}:/tmp/shared {mount_dir}"
        )

        result = subprocess.run(mount_cmd, shell=True)
        if result.returncode == 0:
            logger.info(f"[OK] NFS mounted at {mount_dir}")
        else:
            raise GraFlagError(f"Failed to mount NFS at {mount_dir}")

    def sync(self, local_path: str, is_lib: bool = False):
        """Sync a local method or library directory to remote shared storage."""
        local_dir = Path(local_path).resolve()

        if not local_dir.is_dir():
            raise GraFlagError(f"Path is not a directory: {local_dir}")

        if is_lib:
            lib_name = local_dir.name
            remote_dest = f"{self.config.remote_shared_dir}/libs/{lib_name}"
            logger.info(f"Syncing library '{lib_name}' to remote...")
        else:
            env_file = local_dir / ".env"
            if not env_file.exists():
                raise GraFlagError(f"No .env file found in {local_dir}.")

            method_name = None
            with open(env_file, 'r') as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("METHOD_NAME="):
                        method_name = line.split("=", 1)[1].strip()
                        break

            if not method_name:
                raise GraFlagError(f"METHOD_NAME not found in {env_file}")

            # run() lowercases the method before looking up methods/<name>, so
            # sync must do the same or a method whose METHOD_NAME is not already
            # lowercase (e.g. TADDY) lands in a directory run() never reads.
            method_name = method_name.lower()
            if method_name != local_dir.name.lower():
                logger.warning(
                    f"[WARN] METHOD_NAME '{method_name}' does not match directory "
                    f"'{local_dir.name}'; syncing to methods/{method_name}"
                )

            remote_dest = f"{self.config.remote_shared_dir}/methods/{method_name}"
            logger.info(f"Syncing method '{method_name}' to remote...")

        self.ssh.copy_files(
            source_paths=[f"{local_dir}/"],
            dest_path=f"{remote_dest}/",
            recursive=True,
            from_remote=False,
        )

        target_type = "library" if is_lib else "method"
        target_name = lib_name if is_lib else method_name
        logger.info(f"Synced {target_type} '{target_name}' to {remote_dest}")

    # ========================================================================
    # Internal Helpers
    # ========================================================================

    def _write_build_log(self, exp_dir: str, text: str):
        """Write build.log to an experiment directory on the remote."""
        build_log_path = remote_path(self.config.remote_shared_dir, exp_dir, "build.log")
        self.ssh.execute(f"cat > {build_log_path} << 'BUILDEOF'\n{text}\nBUILDEOF")

    def _write_status(self, exp_dir: str, status: str, error: str = None):
        """Write status.json to an experiment directory on the remote."""
        data = {"status": status, "timestamp": datetime.now().isoformat()}
        if error:
            data["error"] = error
        status_data = json.dumps(data)
        status_path = remote_path(self.config.remote_shared_dir, exp_dir, "status.json")
        self.ssh.execute(f"cat > {status_path} << 'STATUSEOF'\n{status_data}\nSTATUSEOF")

    def _decode_status(self, encoded: str, exp_name: str) -> Optional[dict]:
        """Decode one base64'd status.json payload, tolerating a bad file."""
        if not encoded:
            return None
        try:
            raw = base64.b64decode(encoded).decode("utf-8", errors="replace")
        except (ValueError, binascii.Error):
            logger.debug(f"Undecodable status.json for {exp_name}")
            return None
        if not raw.strip():
            return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            logger.warning(
                f"[WARN] {exp_name}: status.json is not valid JSON; "
                f"falling back to Docker service state"
            )
            return None

    def _build_experiment_info(
        self, entry: dict, running_services: set = None
    ) -> Optional[ExperimentInfo]:
        """Turn one probe result into an ExperimentInfo.

        Shared by the batched listing and the single-experiment lookup so the
        two cannot disagree about what a status means.
        """
        exp_name = entry.get("name")
        if not exp_name:
            return None

        full_exp_path = f"{self.config.remote_shared_dir}/experiments/{exp_name}"
        has_results = entry.get("RESULTS", False)
        has_evaluation = entry.get("EVAL", False)
        has_build_log = entry.get("BUILD_LOG", False)

        status_data = entry.get("status_json") or {}
        runner_status = status_data.get("status")

        parts = exp_name.split("__")
        method = parts[1] if len(parts) > 1 else "unknown"
        dataset = parts[2] if len(parts) > 2 else "unknown"
        timestamp = parts[3] if len(parts) > 3 else ""

        if running_services is not None:
            service_exists = exp_name in running_services
        else:
            service_exists = self.docker.service_exists(exp_name)

        service_failed = service_exists and self.docker.is_service_failed(exp_name)

        if runner_status in ("completed", "failed"):
            status = runner_status
        elif service_failed:
            status = "failed"
        elif runner_status == "building":
            if service_exists:
                status = "building"
            elif has_build_log:
                status = "failed"  # build finished but service never created
            else:
                status = "building"
        elif runner_status == "running":
            status = "running" if service_exists else "stopped"
        elif service_exists:
            status = "running"
        elif has_results or has_evaluation:
            status = "completed"
        else:
            status = "unknown"

        return ExperimentInfo(
            name=exp_name,
            method=method,
            dataset=dataset,
            timestamp=timestamp,
            status=status,
            has_results=has_results,
            has_evaluation=has_evaluation,
            results_path=f"{full_exp_path}/results.json" if has_results else None,
            evaluation_path=f"{full_exp_path}/eval" if has_evaluation else None,
            service_name=exp_name if service_exists else None,
        )

    def get_experiment(self, experiment_name: str) -> Optional[ExperimentInfo]:
        """One experiment's status, from a single remote probe.

        None when the experiment does not exist (or the manager could not be
        reached -- the probe cannot tell the two apart).
        """
        return self._get_experiment_info(experiment_name)

    def _get_experiment_info(self, exp_name: str, running_services: set = None) -> Optional[ExperimentInfo]:
        """Get information for a single experiment."""
        quoted_path = remote_path(
            self.config.remote_shared_dir, "experiments", exp_name
        )

        # Single SSH call for all the checks. status.json is base64'd because
        # the reply is parsed line by line and the runner writes it indented.
        check_cmd = (
            f'echo "EXISTS:$(test -d {quoted_path} && echo 1 || echo 0)"\n'
            f'echo "RESULTS:$(test -f {quoted_path}/results.json && echo 1 || echo 0)"\n'
            f'echo "EVAL:$(test -f {quoted_path}/eval/evaluation.json && echo 1 || echo 0)"\n'
            f'echo "BUILD_LOG:$(test -f {quoted_path}/build.log && echo 1 || echo 0)"\n'
            f'echo "STATUS_B64:$(base64 < {quoted_path}/status.json 2>/dev/null'
            f' | tr -d \'\\n\')"'
        )
        result = self.ssh.execute(check_cmd)
        if result.returncode != 0:
            return None

        entry = {"name": exp_name}
        for line in result.stdout.strip().split("\n"):
            if line.startswith("STATUS_B64:"):
                entry["status_json"] = self._decode_status(
                    line[len("STATUS_B64:"):].strip(), exp_name
                )
            elif ":" in line:
                key, _, val = line.partition(":")
                entry[key] = val.strip() == "1"

        if not entry.get("EXISTS", False):
            return None

        return self._build_experiment_info(entry, running_services)

    def _save_tee(self, tee_file: str, output_parts: List[str]):
        """Save output to file if tee_file is specified."""
        if tee_file:
            tee_path = Path(tee_file).expanduser().resolve()
            tee_path.parent.mkdir(parents=True, exist_ok=True)
            tee_path.write_text("\n".join(output_parts))
            logger.info(f"[INFO] Saved to {tee_path}")
