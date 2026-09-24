"""Docker operations for GraFlag using Docker SDK."""

import json
import subprocess
import shlex
import signal
import socket
import threading
import time
import yaml
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional
from enum import Enum
from datetime import datetime
import logging

import re

import requests

import docker
from docker.types import ServiceMode, RestartPolicy, Resources, Mount, EndpointSpec

from .utils import load_method_env
from .ssh import remote_path, NON_INTERACTIVE_OPTS

logger = logging.getLogger(__name__)


#: ``.env`` keys forwarded to ``docker build`` as ``--build-arg``.
#:
#: A method declares the value once, in its ``.env``, and its Dockerfile reads
#: it through an ``ARG`` of the same name. The alternative -- writing the value
#: into the Dockerfile -- is what let a ``.env`` advertising one ``SOURCE_CODE``
#: sit next to an image cloning another, and it is why an upstream commit could
#: not be pinned without editing two files in step.
#:
#: Only keys named here cross the boundary. A method's parameters are
#: ``_``-prefixed and belong to the run, not to the build.
BUILD_ARG_KEYS = ("SOURCE_CODE", "SOURCE_REF")

#: Where a method image gets the GraFlag libraries.
#:
#: Not a ``BUILD_ARG_KEYS`` entry, because it is not a property of the method.
#: Whether the published wheel is current is a property of the deployment, and
#: a method that could answer it differently from its neighbours would be a
#: benchmark whose runs are not comparable.
GRAFLAG_LIBS_CHOICES = ("local", "pypi")

#: What a shared ``IMAGE=`` may be called.
#:
#: The value is both a directory under ``images/`` and half of an image
#: reference, and Docker rejects an uppercase repository name -- at push time,
#: after the build has already run for minutes.
IMAGE_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


class BuildFailed(RuntimeError):
    """A ``docker build`` that exited non-zero, with everything it printed.

    The legacy builder prints each step's output on stdout and leaves stderr
    the deprecation banner and "returned a non-zero code: 128" -- which step
    failed, never why. The why was on stdout: for the rare method, git's
    ``fatal: could not read Username for 'https://github.com'`` from cloning
    a private repository. An error built from stderr alone dropped it, and
    build.log was written only after a successful build, so a failed build
    kept no record of its cause. ``log`` is what build.log would have held.
    """

    def __init__(self, message: str, log: str):
        super().__init__(message)
        self.log = log


class ImageNames(NamedTuple):
    """Every name one method's image is known by, resolved together.

    Built and deployed from the same call, so the tag that ``docker build``
    produces and the one Swarm pulls cannot come apart.
    """

    #: ``<owner>:<tag>`` -- the local tag on the manager.
    local: str
    #: ``<manager>:5000/<owner>:<tag>`` -- what Swarm pulls.
    registry: str
    #: The method that owns the image: the method itself, or the shared image
    #: named by ``IMAGE=``. Never the same thing as ``METHOD_NAME`` at run time.
    owner: str
    #: Remote path of the Dockerfile that builds it, already quoted.
    dockerfile: str
    #: True when the image is shared, i.e. ``IMAGE=`` was declared.
    shared: bool



class ReservedEnvVars(Enum):
    """Reserved environment variable names that should not be overridden by method parameters."""
    DATA = 'DATA'
    EXP = 'EXP'
    METHOD_NAME = 'METHOD_NAME'
    COMMAND = 'COMMAND'
    MONITOR_INTERVAL = 'MONITOR_INTERVAL'
    GRAFLAG_PARAMS = 'GRAFLAG_PARAMS'

    @classmethod
    def get_names(cls):
        return {var.value for var in cls}


def _die_with_parent():
    """preexec_fn that makes a child die when its parent does.

    The log follower is an `ssh ... docker service logs -f` subprocess. Ctrl-C
    is handled -- KeyboardInterrupt terminates it -- but SIGKILL cannot be
    caught, so a `timeout`-killed or `kill -9`-ed graflag leaves the ssh
    running forever, following a service that is later removed. Measured on
    this workstation: 162 such orphans, the oldest 22 hours old, all following
    services that no longer existed.

    PR_SET_PDEATHSIG asks the kernel to signal this process when its parent
    dies, which covers the case a Python handler cannot. Linux-only and
    best-effort: anywhere else, or if ctypes cannot reach prctl, the child
    behaves exactly as before.
    """
    try:
        import ctypes
        PR_SET_PDEATHSIG = 1
        ctypes.CDLL("libc.so.6").prctl(PR_SET_PDEATHSIG, signal.SIGTERM)
    except Exception:
        pass


#: Swarm task states after which a restart_policy=none task never runs again.
TERMINAL_TASK_STATES = ('complete', 'failed', 'shutdown', 'rejected', 'orphaned')


def _service_status(running: int, tasks: List[Dict]) -> str:
    """What a Swarm service is actually doing, from its tasks.

    Every experiment service is one replica with `restart_policy=none`, so
    "no task is running" is the normal end state, not a symptom. Reporting
    it as 'pending' made a finished run indistinguishable from one that had
    never been scheduled.
    """
    if running > 0:
        return 'running'
    if not tasks:
        return 'pending'
    # Newest task first: a service that was updated has older ones behind it.
    latest = max(tasks, key=lambda t: t.get('CreatedAt') or '')
    state = (latest.get('Status', {}) or {}).get('State', '')
    return {
        'complete': 'completed',
        'failed': 'failed',
        'rejected': 'failed',
        'orphaned': 'failed',
        'shutdown': 'stopped',
        'remove': 'stopped',
    }.get(state, 'pending')


class DockerManager:
    """Handle Docker Swarm operations via Docker SDK with SSH tunnel."""

    def __init__(self, ssh_manager, config, hosts_file: str = "hosts.yml"):
        self.ssh = ssh_manager
        self.config = config
        self.hosts_file = hosts_file
        self._client = None
        self._tunnel_proc = None
        self._tunnel_port = None
        # The GUI shares one DockerManager across socketio (async_mode
        # 'threading'), Flask request threads, the background updater and the
        # threads spawned for run/evaluate/stop/delete. Without this, two
        # threads could both observe _client is None and both enter _connect(),
        # whose first act is close() -- tearing down the tunnel the other had
        # just built. Reentrant because _connect() calls close().
        self._lock = threading.RLock()

    @property
    def client(self) -> docker.DockerClient:
        """Lazy-initialize Docker client via SSH tunnel."""
        with self._lock:
            if self._client is None or (
                self._tunnel_proc and self._tunnel_proc.poll() is not None
            ):
                self._connect()
            return self._client

    def _with_reconnect(self, call):
        """Run a Docker SDK call, rebuilding the tunnel once if it has died.

        `client` above rebuilds only when the ssh process itself has exited.
        This cluster drops the far end of a live tunnel about once a minute
        ("Connection aborted / RemoteDisconnected"), and that leaves the
        process running and the pooled socket dead -- so the property hands
        back a client whose next call raises. Swallowed upstream, that became
        an empty service list, which is indistinguishable from a cluster with
        nothing running: the dashboard's Services panel emptied itself at
        random and filled back in two seconds later.
        """
        try:
            return call()
        except requests.exceptions.ConnectionError:
            logger.warning("[WARN] Docker tunnel dropped; reconnecting")
            with self._lock:
                self.close()
            return call()          # `client` rebuilds on the way through

    def _connect(self):
        """Establish SSH tunnel and create Docker client.

        Callers hold self._lock.
        """
        self.close()

        # Find free local port
        with socket.socket() as s:
            s.bind(('', 0))
            self._tunnel_port = s.getsockname()[1]

        # Build SSH tunnel command
        ssh_args = [
            'ssh', '-N',
            '-L', f'{self._tunnel_port}:/var/run/docker.sock',
            '-o', 'StrictHostKeyChecking=no',
            '-o', 'ExitOnForwardFailure=yes',
        ]
        if getattr(self.ssh, "batch_mode", False):
            ssh_args.extend(NON_INTERACTIVE_OPTS)
        if self.ssh.ssh_key:
            ssh_args.extend(['-i', str(Path(self.ssh.ssh_key).expanduser())])
        ssh_args.extend(['-p', str(self.ssh.ssh_port)])
        ssh_args.append(f'root@{self.ssh.manager_ip}')

        logger.debug(f"Starting SSH tunnel on port {self._tunnel_port}")
        # Guarded for the same reason as the log follower: the tunnel is a
        # long-lived `ssh -N -L .../docker.sock`, and a SIGKILL-ed graflag
        # leaves it connected to the manager indefinitely. Nine such orphans
        # were alive on this workstation, the oldest 22 hours old.
        # stdin closed for the reason SSHManager.execute gives: the tunnel
        # lives as long as the client, and under the MCP server this
        # process's stdin is the protocol.
        self._tunnel_proc = subprocess.Popen(
            ssh_args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, preexec_fn=_die_with_parent,
        )

        # Wait for the tunnel to be usable. ssh binds the local port as soon
        # as it starts, so a successful TCP connect only proves ssh is
        # listening -- not that the forward reaches the manager's Docker
        # socket. Ping the Docker API instead, otherwise the first real call
        # fails with an opaque connection error further down the stack.
        client = None
        last_error = None
        for _ in range(20):
            time.sleep(0.3)
            if self._tunnel_proc.poll() is not None:
                stderr = self._tunnel_proc.stderr.read().decode()
                raise RuntimeError(f"SSH tunnel failed: {stderr}")
            try:
                client = docker.DockerClient(
                    base_url=f'tcp://localhost:{self._tunnel_port}',
                    timeout=30,
                )
                client.ping()
                break
            except Exception as exc:
                last_error = exc
                if client is not None:
                    try:
                        client.close()
                    except Exception:
                        pass
                    client = None
                continue
        else:
            self._tunnel_proc.terminate()
            raise RuntimeError(
                f"SSH tunnel to {self.ssh.manager_ip} came up but the Docker "
                f"API did not respond: {last_error}"
            )

        self._client = client
        logger.debug("Docker client connected via SSH tunnel")

    def close(self):
        """Close Docker client and SSH tunnel.

        Tolerates a partially-constructed instance: __del__ calls this, and if
        __init__ raised before setting these attributes the resulting
        AttributeError would surface as noise during garbage collection.
        """
        lock = getattr(self, "_lock", None)
        if lock is not None:
            lock.acquire()
        try:
            self._close_locked()
        finally:
            if lock is not None:
                lock.release()

    def _close_locked(self):
        if getattr(self, "_client", None):
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None
        if getattr(self, "_tunnel_proc", None):
            self._tunnel_proc.terminate()
            try:
                self._tunnel_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._tunnel_proc.kill()
            self._tunnel_proc = None

    def __del__(self):
        self.close()

    def _load_hosts(self) -> Dict:
        """Load hosts configuration from YAML file.

        Expands `~`: HOSTS_FILE is a path a human types into config.env, so it
        routinely starts with `~`. Path() does not expand that, so the file was
        reported missing, this returned {}, and setup_workers() logged "No
        workers defined in hosts.yml" for a file that plainly defined four --
        leaving a single-node swarm that `graflag setup` called a success.
        """
        if not self.hosts_file:
            return {}

        hosts_path = Path(self.hosts_file).expanduser()
        if not hosts_path.exists():
            # Say which path was tried; "no workers" gave no clue that the
            # file had not been read at all.
            logger.warning(f"[WARN] hosts file not found: {hosts_path}")
            return {}
        with open(hosts_path, "r") as f:
            return yaml.safe_load(f) or {}

    # ========================================================================
    # Swarm Management
    # ========================================================================

    def setup_swarm_manager(self):
        """Initialize Docker Swarm on manager node."""
        logger.info("[SETUP] Initializing Docker Swarm on manager...")

        info = self.client.info()
        if info.get('Swarm', {}).get('LocalNodeState') == 'active':
            logger.info("[OK] Docker Swarm already initialized")
            return

        hosts = self._load_hosts()
        advertise_addr = hosts.get("manager", self.config.manager_ip)

        self.client.swarm.init(advertise_addr=advertise_addr)
        logger.info("[OK] Docker Swarm initialized")

    def get_swarm_token(self) -> str:
        """Get Docker Swarm worker join token."""
        swarm_attrs = self.client.swarm.attrs
        return swarm_attrs['JoinTokens']['Worker']

    def setup_workers(self, token: str):
        """Setup worker nodes to join the swarm (requires SSH to each worker)."""
        hosts = self._load_hosts()
        manager_ip = hosts.get("manager", self.config.manager_ip)
        workers = hosts.get("workers", [])

        if not workers:
            logger.warning(
                f"[WARN] No workers defined in {self.hosts_file}; "
                f"the swarm will have the manager only"
            )
            return

        logger.info(f"[SETUP] Setting up {len(workers)} worker nodes...")

        for worker_ip in workers:
            check_cmd = (
                f"ssh -o StrictHostKeyChecking=no root@{worker_ip} "
                f"'docker info --format \"{{{{.Swarm.LocalNodeState}}}}\"'"
            )
            result = self.ssh.execute(check_cmd)

            if result.stdout.strip() == "active":
                logger.info(f"[OK] Worker {worker_ip} already in swarm")
                continue

            join_cmd = (
                f"ssh -o StrictHostKeyChecking=no root@{worker_ip} "
                f"'docker swarm join --token {token} {manager_ip}:2377'"
            )
            result = self.ssh.execute(join_cmd)

            if result.returncode == 0:
                logger.info(f"[OK] Worker {worker_ip} joined swarm")
            else:
                logger.error(f"[ERROR] Failed to join worker {worker_ip}: {result.stderr}")

    def get_nodes(self) -> List[Dict]:
        """Get list of swarm nodes."""
        nodes = []
        for node in self.client.nodes.list():
            attrs = node.attrs
            spec = attrs.get('Spec', {})
            status = attrs.get('Status', {})
            manager_status = attrs.get('ManagerStatus', {})

            nodes.append({
                'id': attrs.get('ID', ''),
                'hostname': attrs.get('Description', {}).get('Hostname', ''),
                'status': status.get('State', ''),
                'availability': spec.get('Availability', ''),
                'role': spec.get('Role', ''),
                'is_manager': bool(manager_status),
                'leader': manager_status.get('Leader', False),
            })
        return nodes

    # ========================================================================
    # Registry
    # ========================================================================

    def setup_local_registry(self):
        """Setup local Docker registry service on manager."""
        logger.info("[BUILD] Setting up local Docker registry...")

        existing = self.client.services.list(filters={'name': 'registry'})
        if existing:
            logger.info("[OK] Local registry already running")
            return

        self.client.services.create(
            image='registry:2',
            name='registry',
            mode=ServiceMode('replicated', replicas=1),
            endpoint_spec=EndpointSpec(ports={5000: (5000, 'tcp')}),
            mounts=[Mount(target='/var/lib/registry', source='registry-data', type='volume')],
            constraints=['node.role==manager'],
        )
        logger.info("[OK] Local registry started on port 5000")

    # ========================================================================
    # Image Build (SSH — build context is on remote host)
    # ========================================================================

    def _graflag_libs(self) -> str:
        """Validate GRAFLAG_LIBS before it reaches a three-minute build.

        The Dockerfiles refuse an unknown value too -- a `case` with an
        explicit error arm, not an `if` whose else branch quietly means PyPI.
        Catching it here means the message names the config key rather than
        arriving as a shell exit status from inside a build layer.
        """
        value = getattr(self.config, "graflag_libs", "local")
        if value not in GRAFLAG_LIBS_CHOICES:
            raise ValueError(
                f"GRAFLAG_LIBS={value!r} is not one of "
                f"{', '.join(GRAFLAG_LIBS_CHOICES)}")
        return value

    def _image_names(self, method_name: str, tag: str = "latest",
                     method_env: Optional[Dict[str, str]] = None) -> ImageNames:
        """Resolve every name a method's image is known by, in one place.

        The image name used to be built from an f-string in
        ``build_method_image`` and again from an identical one in
        ``create_service``. Identical is the problem: the two are written
        apart, and a method that builds one image and runs another fails at
        deploy time with an image-not-found from the registry, long after the
        build said ``[OK]``. Resolving here means the two cannot disagree --
        which is what makes a shared ``IMAGE=`` safe to introduce at all.

        A method builds its own image unless its ``.env`` declares ``IMAGE=``,
        in which case it shares one: seventeen ``bond_*`` methods have
        byte-identical Dockerfiles, and building seventeen copies of the same
        ~9 GB image is why building the whole tree did not fit on the cluster.
        They stay distinct at run time -- ``_build_service_env`` sets
        ``METHOD_NAME`` per service and ``graflag_bond.train`` selects the
        PyGOD detector from it -- so nothing about what runs changes.

        Args:
            method_name: The method being built or deployed.
            tag: Image tag.
            method_env: The method's parsed ``.env``, when the caller has it.
                Omitted, it is read from the manager. Callers that already
                hold it pass it, following the same "one SSH call, not one per
                item" rule as ``list_experiments()``.

        Returns:
            An :class:`ImageNames`, including the Dockerfile to build it from.

        Raises:
            ValueError: ``IMAGE=`` is not a usable Docker repository name.
                Deliberately not a silent fall back to the method's own image:
                that would build one image, deploy another, and report
                success -- the fail-open shape this resolver exists to remove.
        """
        method_name = method_name.lower()
        tag = tag.lower()
        env = method_env if method_env is not None else load_method_env(
            self.ssh, self.config.remote_shared_dir, method_name)

        shared = (env.get("IMAGE") or "").strip().lower()
        if shared and not IMAGE_NAME.match(shared):
            raise ValueError(
                f"{method_name}: IMAGE={shared!r} is not a usable Docker "
                f"repository name (expected {IMAGE_NAME.pattern})")

        owner = shared or method_name
        dockerfile = (remote_path(self.config.remote_shared_dir, "images", owner,
                                  "Dockerfile") if shared else
                      remote_path(self.config.remote_shared_dir, "methods", owner,
                                  "Dockerfile"))
        return ImageNames(
            local=f"{owner}:{tag}",
            registry=f"{self.config.manager_ip}:5000/{owner}:{tag}",
            owner=owner,
            dockerfile=dockerfile,
            shared=bool(shared),
        )

    def _context_is_filtered(self) -> bool:
        """Whether the build context root carries a .dockerignore.

        One extra SSH round trip against a build that takes minutes -- the
        same trade `build_method_image` already makes for `method_env`.
        """
        shared = remote_path(self.config.remote_shared_dir)
        return self.ssh.execute(f"test -f {shared}/.dockerignore").returncode == 0

    def build_method_image(self, method_name: str, tag: str = "latest",
                           method_env: Optional[Dict[str, str]] = None,
                           force_rm: bool = False) -> str:
        """Build method Docker image and push to local registry.

        Uses SSH because the build context resides on the remote host.

        Every ``BUILD_ARG_KEYS`` entry the method's ``.env`` defines is passed
        through as ``--build-arg``, so a Dockerfile can pin its upstream from
        the one place that already names it.

        Args:
            method_name: Method whose ``methods/<name>/Dockerfile`` to build.
            tag: Image tag.
            method_env: The method's parsed ``.env``, when the caller already
                has it. Omitted, it is read from the manager -- one extra SSH
                round trip, against a build that takes minutes.
            force_rm: Pass ``--force-rm``, so docker removes the build's
                intermediate containers even when it fails. Without it a
                failed build keeps its last container, and that container
                holds every layer the build had produced: 12.8 GB on the
                manager after one failed ``git clone`` of the rare method.

        Returns:
            Combined build and push log output.
        """
        method_name = method_name.lower()
        tag = tag.lower()

        env = method_env if method_env is not None else load_method_env(
            self.ssh, self.config.remote_shared_dir, method_name)
        names = self._image_names(method_name, tag, method_env=env)
        local_image, registry_image = names.local, names.registry

        if names.shared:
            # Worth saying out loud: the tag being built is not the method's
            # name, and `--build` on three methods that share an image
            # rebuilds it three times. The layer cache makes that near-free
            # and --build is explicit intent, so it is not deduplicated -- but
            # it should not look like a mistake in the log either.
            logger.info(f"[INFO] Rebuilding shared image {names.owner} "
                        f"(used by {method_name})")
        logger.info(f"[BUILD] Building image {local_image}...")
        # Quoted as one word: docker takes `--build-arg KEY=VALUE`, and the
        # remote shell must not split a value containing spaces or `#`.
        build_args = "".join(
            f"--build-arg {shlex.quote(f'{key}={env[key]}')} "
            for key in BUILD_ARG_KEYS if env.get(key))

        libs = self._graflag_libs()
        build_args += f"--build-arg GRAFLAG_LIBS={libs} "

        # Both of these also go into build.log, not only to the operator's
        # terminal. `docker build` never echoes a --build-arg value, so a log
        # holding only its output records `checkout --detach ${SOURCE_REF}`
        # with the variable unexpanded -- the experiment would name no commit
        # anywhere on disk once the CLI output scrolled away. The same applies
        # to which graflag_runner went into the image: the RUN step prints
        # both branches of its `case`, so the log alone does not say which one
        # ran.
        provenance = []
        if env.get("SOURCE_REF"):
            provenance.append(f"[INFO] Upstream pinned at {env['SOURCE_REF']}")
        provenance.append("[INFO] GraFlag libraries from " + (
            f"{self.config.remote_shared_dir}/libs" if libs == "local"
            else "PyPI"))
        for line in provenance:
            logger.info(line)

        build_log = [*provenance, ""]

        # The build context is the whole share, so the `.dockerignore` at its
        # root is the only thing keeping `datasets/` and `experiments/` out of
        # the tarball docker streams before the first instruction runs. And
        # nothing puts it there: `sync` copies a method directory, `sync --lib`
        # copies a library, and neither touches the root -- so a share
        # populated by either has the file in git and not on the cluster.
        #
        # The build then succeeds anyway, which is the whole problem. Docker
        # prints the context size and moves on; on this cluster it read
        # "Sending build context to Docker daemon 3.651GB" for a build that
        # needs 674 KB, on every method, for as long as the file was missing.
        # Say so where it will be read, and put it in build.log too.
        if not self._context_is_filtered():
            note = (
                f"[WARN] No .dockerignore at {self.config.remote_shared_dir}: "
                f"the build context includes datasets/ and experiments/. "
                f"Copy it with `graflag copy -s ./.dockerignore --dest .` from "
                f"the graflag-shared checkout.")
            logger.warning(note)
            build_log.append(note + "\n")

        # Build image
        force = "--force-rm " if force_rm else ""
        build_cmd = (
            f"docker build --network=host {force}{build_args}"
            f"-f {names.dockerfile} "
            f"-t {shlex.quote(local_image)} -t {shlex.quote(registry_image)} "
            f"{remote_path(self.config.remote_shared_dir)}/"
        )
        result = self.ssh.execute(build_cmd)
        build_log.append(f"=== BUILD: {local_image} ===\n")
        build_log.append(result.stdout or "")
        if result.stderr:
            build_log.append(result.stderr)

        if result.returncode != 0:
            raise BuildFailed(f"Failed to build image {local_image}: {result.stderr}",
                              "\n".join(build_log))

        # Push to registry
        logger.info(f"[INFO] Pushing {registry_image} to local registry...")
        result = self.ssh.execute(f"docker push {shlex.quote(registry_image)}")
        build_log.append(f"\n=== PUSH: {registry_image} ===\n")
        build_log.append(result.stdout or "")
        if result.stderr:
            build_log.append(result.stderr)

        if result.returncode != 0:
            logger.warning(f"[WARN] Failed to push to registry: {result.stderr}")
        else:
            logger.info("[OK] Image pushed to local registry")

        logger.info(f"[OK] Image {local_image} built successfully")
        return "\n".join(build_log)

    def _evaluator_source_tag(self) -> str:
        """Short content hash of the evaluator sources on the manager.

        Used as the image tag so a source change produces a new tag and
        therefore a real rebuild, while an unchanged tree still hits the cache.
        Falls back to "latest" if the hash cannot be computed.
        """
        lib_dir = remote_path(
            self.config.remote_shared_dir, "libs", "graflag_evaluator"
        )
        cmd = (
            f"find {lib_dir} -type f -name '*.py' -o -type f -name 'Dockerfile' "
            f"-o -type f -name 'requirements.txt' 2>/dev/null "
            f"| sort | xargs cat 2>/dev/null | sha256sum | cut -c1-12"
        )
        result = self.ssh.execute(cmd)
        digest = (result.stdout or "").strip()
        if result.returncode == 0 and len(digest) == 12:
            return digest
        logger.warning("[WARN] Could not hash evaluator sources; using 'latest'")
        return "latest"

    def build_evaluator_image(self) -> str:
        """Build graflag-evaluator image and push to registry.

        Returns:
            Registry image path.
        """
        logger.info("[BUILD] Building graflag-evaluator image...")

        # Tag by a hash of the evaluator source. The cache check below asks
        # whether *this* content has been built, not merely whether some image
        # called :latest exists -- which is why edits to graflag_evaluator
        # never reached the cluster.
        tag = self._evaluator_source_tag()
        local_image = f"graflag-evaluator:{tag}"
        registry_image = f"{self.config.manager_ip}:5000/graflag-evaluator:{tag}"

        # Check if this exact content was already built
        check_cmd = (
            f"docker manifest inspect {shlex.quote(registry_image)} "
            f"> /dev/null 2>&1 && echo 'exists'"
        )
        result = self.ssh.execute(check_cmd)

        if result.stdout.strip() == "exists":
            logger.info(f"[OK] Evaluator image {tag} already in registry")
            return registry_image

        # Build
        build_cmd = (
            f"cd {remote_path(self.config.remote_shared_dir, 'libs', 'graflag_evaluator')} && "
            f"docker build --network=host -t {shlex.quote(local_image)} "
            f"-t {shlex.quote(registry_image)} ."
        )
        result = self.ssh.execute(build_cmd)
        if result.returncode != 0:
            raise RuntimeError(f"Failed to build evaluator image: {result.stderr}")

        # Push
        result = self.ssh.execute(f"docker push {shlex.quote(registry_image)}")
        if result.returncode != 0:
            logger.warning(f"[WARN] Failed to push evaluator to registry: {result.stderr}")
        else:
            logger.info("[OK] Evaluator image pushed to local registry")

        return registry_image

    # ========================================================================
    # Service Operations (Docker SDK)
    # ========================================================================

    def create_service(self, exp_name: str, method_name: str, dataset: str,
                       tag: str = "latest", gpu_required: bool = True,
                       method_params: dict = None) -> str:
        """Create Docker service for experiment."""
        method_params = method_params or {}
        # Read once, used twice: the image name depends on `IMAGE=` and the
        # service environment on the rest of the same file. Fetching it here
        # keeps this to one remote read per service rather than two.
        method_env = load_method_env(self.ssh, self.config.remote_shared_dir, method_name)
        registry_image = self._image_names(method_name, tag, method_env=method_env).registry

        # Save service config
        self._save_service_config(exp_name, method_name, dataset, tag, gpu_required,
                                  method_params, registry_image,
                                  method_env=method_env)

        # Build environment variables
        env_vars = self._build_service_env(
            method_name, dataset, exp_name, method_params,
            gpu_required=gpu_required, method_env=method_env,
        )

        # Mount shared directory
        shared_mount = Mount(
            target=self.config.remote_shared_dir,
            source=self.config.remote_shared_dir,
            type='bind'
        )

        # GPU resources
        resources = None
        if gpu_required:
            resources = Resources(
                generic_resources=[{
                    'DiscreteResourceSpec': {
                        'Kind': 'NVIDIA-GPU',
                        # Must be >= 1. A value of 0 reserves nothing and adds no
                        # placement constraint, so a "GPU-enabled" service could be
                        # scheduled onto a CPU-only node and several jobs could land
                        # on one GPU.
                        'Value': 1
                    }
                }]
            )
            logger.info(f"[INFO] Creating GPU-enabled service {exp_name}...")
        else:
            logger.info(f"[RUN] Creating service {exp_name}...")

        # Create service
        service = self.client.services.create(
            image=registry_image,
            name=exp_name,
            env=env_vars,
            mounts=[shared_mount],
            mode=ServiceMode('replicated', replicas=1),
            restart_policy=RestartPolicy(condition='none'),
            resources=resources,
            networks=['host'],
        )

        logger.info(f"[OK] Service {exp_name} created successfully")

        # Save service details
        self._save_service_details(exp_name, service.id)

        return exp_name

    def create_evaluation_service(self, experiment_name: str) -> str:
        """Create Docker service to run evaluation."""
        eval_service_name = f"eval__{experiment_name}"

        self._remove_service_if_exists(eval_service_name)
        logger.info(f"[INFO] Creating evaluation service: {eval_service_name}")

        registry_image = self.build_evaluator_image()

        service = self.client.services.create(
            image=registry_image,
            name=eval_service_name,
            args=[f"/shared/experiments/{experiment_name}"],
            mounts=[Mount(
                target='/shared',
                source=self.config.remote_shared_dir,
                type='bind'
            )],
            mode=ServiceMode('replicated', replicas=1),
            restart_policy=RestartPolicy(condition='none'),
            networks=['host'],
        )

        logger.info(f"[OK] Evaluation service {eval_service_name} created")
        return eval_service_name

    def list_services(self) -> List[Dict]:
        """List all Docker services.

        The whole listing is the retry unit, not the first call in it: the
        drop usually lands on one of the per-service `tasks()` calls, and a
        `svc` handed out by the old client keeps pointing at the old client,
        so retrying just that call reuses the socket that already died.
        """
        return self._with_reconnect(self._list_services_once)

    def _list_services_once(self) -> List[Dict]:
        services = []
        for svc in self.client.services.list():
            attrs = svc.attrs
            spec = attrs.get('Spec', {})
            mode = spec.get('Mode', {})

            # Desired replicas
            if 'Replicated' in mode:
                desired = mode['Replicated'].get('Replicas', 0)
            else:
                desired = 'global'

            # Image (strip sha256 digest for display)
            image = spec.get('TaskTemplate', {}).get('ContainerSpec', {}).get('Image', '')
            if '@sha256:' in image:
                image = image.split('@')[0]

            # Every task, not just the ones Swarm still wants running. The
            # filter used to be `desired-state: running`, which excludes a
            # task that has finished -- so a one-shot experiment service that
            # ran to completion reported zero running tasks and fell through
            # to 'pending'. A finished run therefore sat in the dashboard's
            # "Running Services" panel labelled as about to start, for as
            # long as it took `cleanup` to sweep it.
            tasks = svc.tasks()
            running = sum(1 for t in tasks
                          if t.get('Status', {}).get('State') == 'running')

            services.append({
                'name': spec.get('Name', ''),
                'id': attrs.get('ID', ''),
                'image': image,
                'replicas': f"{running}/{desired}" if isinstance(desired, int) else desired,
                'status': _service_status(running, tasks),
            })
        return services

    def get_service_names(self) -> set:
        """Get set of all service names."""
        return {svc.name for svc in self.client.services.list()}

    # Every SDK call on the path of a run goes through _with_reconnect. Only
    # the service listing did, so a run launched from the dashboard lost the
    # tunnel right after its service was created, the background thread died
    # on the next call, and the finished service was never removed. A retried
    # removal can find the service already gone -- the first attempt went
    # through and only its reply was lost -- which each caller below already
    # treats as "nothing to remove".

    def stop_service(self, service_name: str):
        """Stop and remove a service."""
        def once():
            try:
                service = self.client.services.get(service_name)
                service.remove()
                logger.info(f"[OK] Service {service_name} stopped and removed")
            except docker.errors.NotFound:
                raise ValueError(f"Service {service_name} not found")
            except docker.errors.APIError as e:
                raise RuntimeError(f"Failed to stop service {service_name}: {e}")
        self._with_reconnect(once)

    def _remove_service_if_exists(self, service_name: str) -> bool:
        """Remove a service if it exists."""
        def once():
            try:
                service = self.client.services.get(service_name)
                service.remove()
                logger.info(f"[INFO] Removed existing service: {service_name}")
                return True
            except docker.errors.NotFound:
                return False
        return self._with_reconnect(once)

    def cleanup_finished_service(self, service_name: str):
        """Remove a finished service (safe if it doesn't exist)."""
        def once():
            try:
                service = self.client.services.get(service_name)
                service.remove()
                logger.info(f"[INFO] Cleaned up finished service: {service_name}")
            except docker.errors.NotFound:
                pass
            except docker.errors.APIError as e:
                logger.warning(f"[WARN] Failed to clean up service {service_name}: {e}")
        self._with_reconnect(once)

    def remove_evaluation_service(self, experiment_name: str):
        """Remove evaluation service for an experiment."""
        eval_service_name = f"eval__{experiment_name}"
        if self._remove_service_if_exists(eval_service_name):
            logger.info(f"[OK] Evaluation service {eval_service_name} removed")

    def get_service_logs(self, service_name: str, tail: int = 100) -> List[str]:
        """Get recent logs for a service.

        Uses SSH + Docker CLI because the Docker SDK log streaming
        is unreliable for swarm services.
        """
        if not self.service_exists(service_name):
            return []

        result = self.ssh.execute(
            f"docker service logs --tail {int(tail)} {shlex.quote(service_name)} 2>&1"
        )
        if result.returncode == 0 and result.stdout.strip():
            return [line for line in result.stdout.strip().split('\n') if line.strip()]
        return []

    def follow_service_logs(self, service_name: str, on_line=None):
        """Follow service logs in real-time until the task finishes.

        Uses SSH + Docker CLI subprocess because the Docker SDK's
        follow mode does not stream reliably for swarm services.

        Args:
            on_line: Optional callback receiving each streamed log line. When
                given, output is read here and forwarded rather than inherited,
                so callers can capture what the user sees.

        Returns:
            The terminal task state ('complete', 'failed', ...), or None if
            the wait ended without observing one.
        """
        import time as _time

        if not self.service_exists(service_name):
            raise ValueError(f"Service {service_name} not found")

        logger.info("[INFO] Following service logs (press Ctrl+C to stop)...")

        # Build SSH command for log following
        ssh_args = ['ssh']
        if self.ssh.ssh_key:
            ssh_args.extend(['-i', str(Path(self.ssh.ssh_key).expanduser())])
        ssh_args.extend([
            '-p', str(self.ssh.ssh_port),
            '-o', 'StrictHostKeyChecking=no',
            # -tt forces a TTY, which is what makes the *remote* command die
            # with the connection. Without it sshd sends no SIGHUP, so killing
            # the local ssh client leaves `docker service logs -f` running on
            # the manager forever: 158 of them were alive there, the oldest 22
            # hours old, every one following a service that no longer existed.
            # _die_with_parent only ever covered the local half of that.
            #
            # The cost is CRLF on the stream, stripped in _pump below.
            '-tt',
            f'root@{self.ssh.manager_ip}',
            f'docker service logs -f {shlex.quote(service_name)}',
        ])

        # When the caller wants the stream captured, read it here and forward
        # it. Previously this inherited stdout, so --tee wrote a file holding
        # only the build log -- or an empty one when --build was not used.
        if on_line is None:
            proc = subprocess.Popen(ssh_args, preexec_fn=_die_with_parent)
            reader = None
        else:
            proc = subprocess.Popen(
                ssh_args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, errors='replace', bufsize=1,
                preexec_fn=_die_with_parent,
            )

            def _pump():
                for line in proc.stdout:
                    # -tt gives the remote command a TTY, so lines arrive
                    # CRLF-terminated. Left alone, every captured log line in
                    # method_output.txt would carry a trailing \r.
                    line = line.replace('\r\n', '\n').replace('\r', '')
                    print(line, end='', flush=True)
                    on_line(line.rstrip('\n'))

            reader = threading.Thread(target=_pump, daemon=True)
            reader.start()

        try:
            # Poll task state until it finishes
            while proc.poll() is None:
                _time.sleep(3)
                tasks = []
                try:
                    svc = self.client.services.get(service_name)
                    tasks = svc.tasks(filters={'desired-state': 'shutdown'})
                except Exception:
                    pass
                for task in tasks:
                    state = task.get('Status', {}).get('State', '').lower()
                    if state in ('complete', 'failed', 'shutdown', 'rejected'):
                        _time.sleep(2)  # Let final logs flush
                        proc.terminate()
                        proc.wait(timeout=5)
                        # Returned so callers can tell a clean finish from a
                        # crash; this used to return None either way, which is
                        # why `graflag evaluate` reported success regardless.
                        return state
        except KeyboardInterrupt:
            proc.terminate()
            proc.wait(timeout=5)
            logger.info("[INFO] Log following interrupted")
            return None
        except Exception:
            proc.terminate()
            proc.wait(timeout=5)
            raise

    def service_exists(self, service_name: str) -> bool:
        """Check if a Docker service exists."""
        def once():
            try:
                self.client.services.get(service_name)
                return True
            except docker.errors.NotFound:
                return False
        return self._with_reconnect(once)

    def service_task_state(self, service_name: str) -> Optional[str]:
        """The newest task's state (``running``, ``complete``, ...), lower case.

        None while the service has no task yet. Raises ValueError when the
        service does not exist.
        """
        def once():
            try:
                svc = self.client.services.get(service_name)
            except docker.errors.NotFound:
                raise ValueError(f"Service {service_name} not found")
            tasks = svc.tasks()
            if not tasks:
                return None
            latest = max(tasks, key=lambda t: t.get('CreatedAt') or '')
            state = (latest.get('Status', {}) or {}).get('State', '')
            return state.lower() or None
        return self._with_reconnect(once)

    def wait_for_service(self, service_name: str, poll: float = 3.0,
                         timeout: Optional[float] = None) -> Optional[str]:
        """Wait until the service's task ends, printing nothing.

        The quiet counterpart of :meth:`follow_service_logs`, for callers with
        nobody reading a terminal: the dashboard, and the MCP server, whose
        stdout is its protocol. Returns the terminal task state, or None if
        `timeout` seconds passed first.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            state = self.service_task_state(service_name)
            if state in TERMINAL_TASK_STATES:
                return state
            if deadline is not None and time.monotonic() >= deadline:
                return None
            time.sleep(poll)

    def is_service_failed(self, service_name: str) -> bool:
        """True when the service exists and no task reached a good end state.

        Observed terminal states for a restart_policy=none service are
        `complete` on success and `failed` on error; `shutdown` and `orphaned`
        mean the task was stopped or its node went away, which is not a method
        failure but is not success either. This is only consulted when
        status.json is absent or unreadable -- the runner's own status wins.
        """
        def once():
            try:
                svc = self.client.services.get(service_name)
                tasks = svc.tasks()
                if not tasks:
                    return False
                # Check if every task is in a terminal failure state
                for task in tasks:
                    state = task.get('Status', {}).get('State', '').lower()
                    if state not in ('failed', 'rejected', 'shutdown', 'orphaned'):
                        return False
                return True
            except docker.errors.NotFound:
                return False
        return self._with_reconnect(once)

    # ========================================================================
    # Cluster Status
    # ========================================================================

    def get_cluster_status(self) -> Dict:
        """Get Docker Swarm cluster status."""
        info = self.client.info()
        swarm = info.get('Swarm', {})

        return {
            'swarm_active': swarm.get('LocalNodeState') == 'active',
            'nodes': self.get_nodes(),
            'services': self.list_services(),
        }

    # ========================================================================
    # Internal
    # ========================================================================

    def _build_service_env(self, method_name, dataset, exp_name, method_params,
                           gpu_required: bool = True,
                           method_env: Optional[Dict[str, str]] = None):
        """Build environment variable list for service.

        Args:
            gpu_required: False when the service is scheduled without a GPU.
                Used to switch the method's own `_GPU` index to the CPU
                sentinel, since the two were otherwise independent.
            method_env: The method's parsed `.env`, when the caller has it.
                Omitted, it is read from the manager.
        """
        if method_env is None:
            method_env = load_method_env(
                self.ssh, self.config.remote_shared_dir, method_name)

        # Build env dict (method .env values as base)
        env_dict = dict(method_env)

        # Add required env vars
        env_dict['METHOD_NAME'] = method_name
        env_dict['DATA'] = f"{self.config.remote_shared_dir}/datasets/{dataset}/"
        env_dict['EXP'] = f"{self.config.remote_shared_dir}/experiments/{exp_name}/"

        # Override with method params (prefixed with _)
        reserved = ReservedEnvVars.get_names()
        for key, value in method_params.items():
            if key.upper() not in reserved:
                env_dict[f"_{key}"] = value
                logger.info(f"   Setting parameter: _{key}={value}")

        # --no-gpu only removes the Swarm resource reservation; the method's
        # own `_GPU` index comes from its .env, so a service scheduled without
        # a GPU could still be told to use cuda:0.
        #
        # `-1 means CPU` is PyGOD's documented contract, which GraFlag adopted
        # for every method: graflag_runner.method.device() implements it, and
        # methods that build their own device string guard on `>= 0` first.
        # test_methods.py::GpuConvention checks that on the whole tree, which
        # is what lets this apply to all 27 rather than to the 17 bond methods
        # it was first limited to. An explicit --params GPU=... always wins.
        if (not gpu_required
                and "_GPU" in env_dict and "GPU" not in method_params
                and str(env_dict["_GPU"]).strip() != "-1"):
            logger.info("   Setting parameter: _GPU=-1 (running without a GPU)")
            env_dict["_GPU"] = "-1"

        # Say which `_FOO` variables are parameters. The method side cannot
        # work that out from the environment alone -- a shell exports `_`,
        # zsh exports `_P9K_TTY`, conda exports `_CE_CONDA` -- so without
        # this, anything running outside a container reads those as method
        # parameters and records them in results.json. graflag_runner reads
        # it in method.injected_params() and falls back to a full scan when
        # it is absent, so older images are unaffected.
        env_dict['GRAFLAG_PARAMS'] = ','.join(
            sorted(k for k in env_dict if k.startswith('_')))

        return [f"{k}={v}" for k, v in env_dict.items()]

    def _save_service_config(self, exp_name, method_name, dataset, tag,
                             gpu_required, method_params, registry_image,
                             method_env: Optional[Dict[str, str]] = None):
        """Save service configuration to JSON.

        Args:
            method_env: The method's parsed `.env`, when the caller has it.
                Omitted, it is read from the manager. A copy is taken either
                way: the recorded config layers the user's params over the
                defaults, and the caller's dict is the same one the service
                environment is built from.
        """
        env_contents = dict(method_env) if method_env is not None else load_method_env(
            self.ssh, self.config.remote_shared_dir, method_name)

        reserved = ReservedEnvVars.get_names()
        for key, value in method_params.items():
            if key.upper() not in reserved:
                # Same "_" prefix the service actually runs with. Recording the
                # bare key left the method default in place under "_KEY", and
                # `run --from-config` (which only reads "_"-prefixed keys) then
                # silently replayed the default instead of the user's value.
                env_contents[f"_{key}"] = value

        service_config = {
            "experiment_name": exp_name,
            "method_name": method_name,
            "dataset": dataset,
            "tag": tag,
            "gpu_required": gpu_required,
            "registry_image": registry_image,
            "manager_ip": self.config.manager_ip,
            "timestamp": datetime.now().isoformat(),
            "data_path": f"{self.config.remote_shared_dir}/datasets/{dataset}/",
            "exp_path": f"{self.config.remote_shared_dir}/experiments/{exp_name}/",
            "env_contents": env_contents,
        }

        config_file = remote_path(
            self.config.remote_shared_dir, "experiments", exp_name, "service_config.json"
        )
        config_json = json.dumps(service_config, indent=2)
        self.ssh.execute(f"cat > {config_file} << 'EOF'\n{config_json}\nEOF")
        logger.info(f"[INFO] Saved service configuration to {config_file}")

    def _save_service_details(self, exp_name, service_id):
        """Save service details to JSON after creation."""
        try:
            # Fetched and listed as one unit: `service` belongs to the client
            # that fetched it, so after a reconnect its tasks() would reuse
            # the dead socket.
            def once():
                svc = self.client.services.get(exp_name)
                return svc, svc.tasks()
            service, tasks = self._with_reconnect(once)
            attrs = service.attrs

            details = {
                "service_id": service_id,
                "service_name": exp_name,
                "created_at": datetime.now().isoformat(),
                "image": attrs.get('Spec', {}).get('TaskTemplate', {}).get('ContainerSpec', {}).get('Image'),
                "created_at_docker": attrs.get('CreatedAt'),
            }

            # Get task info
            if tasks:
                task = tasks[0]
                details["worker"] = {
                    "node_id": task.get('NodeID', ''),
                    "task_id": task.get('ID', ''),
                    "state": task.get('Status', {}).get('State', ''),
                    "desired_state": task.get('DesiredState', ''),
                }

            details_file = remote_path(
                self.config.remote_shared_dir, "experiments", exp_name, "service_details.json"
            )
            details_json = json.dumps(details, indent=2)
            self.ssh.execute(f"cat > {details_file} << 'EOF'\n{details_json}\nEOF")
            logger.info(f"[INFO] Saved service details to {details_file}")
        except Exception as e:
            logger.warning(f"[WARN] Failed to save service details: {e}")
