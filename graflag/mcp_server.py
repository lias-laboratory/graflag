"""GraFlag as an MCP server: the platform's operations as tools for AI agents.

``graflag mcp`` serves the Model Context Protocol over stdio, so an MCP client
-- Claude Code (``claude mcp add graflag -- graflag mcp``), Claude Desktop or
any other -- can list methods and datasets, launch and watch runs, evaluate and
verify them, and read what they produced.

Three rules shape this module.

**stdout is the protocol.** Nothing else may write to it, and nothing may read
the client's requests off stdin. :func:`_claim_stdio` gives the protocol private
copies of both pipes and points file descriptors 0 and 1 elsewhere, and every
core call made here waits quietly (``follow=False``) instead of streaming logs.

**Long operations do not hold a tool call.** ``run_experiment`` and
``evaluate_experiment`` start a background job and return at once;
``wait_for_experiment`` blocks for a bounded time, so an agent can wait without
polling in a loop.

**What an LLM sends is screened like what a browser sends.** Every name goes
through :func:`graflag.utils.valid_name` and every parameter through
:func:`_clean_params` before anything builds a remote command. Operations that
delete data, set up the cluster or run arbitrary code (``clear --apply``,
``stop --rm``, ``setup``, ``register_metric``) are not offered at all.
"""

import base64
import io
import logging
import os
import re
import sys
import threading
import time
import traceback
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Annotated, Any, Callable, Dict, List, Optional

import anyio
from mcp.server.fastmcp import FastMCP, Image
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from . import __version__
from .core import GraFlag, GraFlagError
from .docker_ops import ReservedEnvVars
from .ssh import remote_path
from .utils import valid_name

logger = logging.getLogger(__name__)

#: Ceilings on what one tool call returns or waits for. An agent reads every
#: byte of a reply, and a client gives up on a call that takes too long.
MAX_LIST = 200
MAX_LOG_LINES = 1000
MAX_LINE_CHARS = 2000
MAX_README_CHARS = 20000
MAX_PLOT_BYTES = 5 * 1024 * 1024
MAX_WAIT_SECONDS = 600
WAIT_POLL_SECONDS = 5.0

TERMINAL_STATUSES = GraFlag.TERMINAL_STATUSES

_PARAM_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")

INSTRUCTIONS = """\
GraFlag benchmarks graph anomaly detection (GAD) methods on a Docker Swarm
cluster. A typical session: list_methods and list_datasets to choose a pair
(a method's supported_datasets says which datasets it accepts, and
method_details has its parameters and README, which states what the published
number means); run_experiment, which returns an experiment name at once;
wait_for_experiment until it has finished; evaluate_experiment, then
wait_for_experiment again; get_evaluation for AUC-ROC, AUC-PR and F1; and
verify_experiment, which checks that the published scores reproduce the AUC
the method reported. A run can take minutes or hours: wait_for_experiment
returns after its timeout with the current status, so call it again. On a
development cluster run one method at a time. No tool here deletes data.
"""


def _annotations(title: str, read_only: bool, destructive: bool = False,
                 idempotent: bool = None) -> ToolAnnotations:
    return ToolAnnotations(
        title=title,
        readOnlyHint=read_only,
        destructiveHint=destructive,
        idempotentHint=read_only if idempotent is None else idempotent,
        # The cluster is the user's own, not the open world.
        openWorldHint=False,
    )


# ============================================================================
# Input screening
# ============================================================================

def _name(value: Any, what: str) -> str:
    """Return `value` if it is safe to put in a remote command, else refuse."""
    if not valid_name(value):
        raise ToolError(
            f"Invalid {what} name {value!r}: names use letters, digits, "
            "'.', '_' and '-' only")
    return value


def _clean_params(params: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """Method parameters as `run` takes them: upper-case names, string values.

    Names may be given with or without the leading underscore the method's
    `.env` uses (`EPOCHS` and `_EPOCHS` are the same parameter), and in any
    case: they are sent upper-case, as the `.env` declares them, because an
    override spelt `_epochs` beside the default `_EPOCHS` would reach the
    container as two variables. Names GraFlag sets itself are refused rather
    than dropped, so the agent learns its value was not used.
    """
    reserved = ReservedEnvVars.get_names()
    cleaned = {}
    for key, value in (params or {}).items():
        name = str(key).lstrip("_")
        if not _PARAM_NAME.match(name):
            raise ToolError(f"Invalid parameter name {key!r}")
        name = name.upper()
        if name in reserved:
            raise ToolError(f"{name} is set by GraFlag itself and cannot be a parameter")
        if isinstance(value, bool):
            value = "true" if value else "false"
        elif isinstance(value, (int, float, str)):
            value = str(value)
        else:
            raise ToolError(f"Parameter {name} must be a string, number or boolean")
        if "\n" in value or "\x00" in value:
            raise ToolError(f"Parameter {name} must be a single line")
        cleaned[name] = value
    return cleaned


# ============================================================================
# Background jobs
# ============================================================================

@dataclass
class Job:
    """A run or an evaluation this server started and has not forgotten."""
    experiment: str
    kind: str                       # "run" or "evaluate"
    state: str = "running"          # "running", "done" or "failed"
    error: Optional[str] = None
    started: float = field(default_factory=time.time)
    finished: Optional[float] = None
    thread: Optional[threading.Thread] = field(default=None, repr=False)

    def to_dict(self) -> dict:
        return {"kind": self.kind, "state": self.state, "error": self.error,
                "seconds": round((self.finished or time.time()) - self.started, 1)}


class JobRegistry:
    """What this server has started, so a failure is reported even when it
    happened before the experiment wrote anything to disk."""

    def __init__(self):
        self._jobs: Dict[tuple, Job] = {}
        self._lock = threading.Lock()

    def start(self, experiment: str, kind: str, work: Callable[[], Any]) -> Job:
        with self._lock:
            current = self._jobs.get((experiment, kind))
            if current is not None and current.state == "running":
                raise ToolError(f"A {kind} of {experiment} is already in progress")
            job = Job(experiment=experiment, kind=kind)
            self._jobs[(experiment, kind)] = job

        def target():
            try:
                work()
                job.state = "done"
            except Exception as exc:                      # noqa: BLE001
                job.error = str(exc) or type(exc).__name__
                job.state = "failed"
                logger.warning(f"[WARN] {kind} of {experiment} failed: {job.error}")
            finally:
                job.finished = time.time()

        job.thread = threading.Thread(target=target, daemon=True,
                                      name=f"graflag-{kind}-{experiment}")
        job.thread.start()
        return job

    def for_experiment(self, experiment: str) -> List[Job]:
        with self._lock:
            return [j for (exp, _), j in self._jobs.items() if exp == experiment]


# ============================================================================
# The server
# ============================================================================

class _State:
    """One GraFlag, created on first use, and the jobs started through it."""

    def __init__(self, factory: Callable[[], Any]):
        self._factory = factory
        self._gf = None
        self._lock = threading.Lock()
        self.jobs = JobRegistry()

    def gf(self):
        # Created lazily, so a client can list the tools of a server that has
        # no configuration yet and be told how to fix it on the first call.
        with self._lock:
            if self._gf is None:
                try:
                    self._gf = self._factory()
                except GraFlagError as exc:
                    raise ToolError(
                        f"{exc}. Configure GraFlag with `graflag setup`, or "
                        "start the server with `graflag mcp --config FILE`.")
            return self._gf


async def _blocking(fn: Callable[[], Any]) -> Any:
    """Run a blocking core call off the event loop; errors become tool errors."""
    try:
        return await anyio.to_thread.run_sync(fn)
    except ToolError:
        raise
    except (GraFlagError, ValueError) as exc:
        raise ToolError(str(exc))
    except Exception as exc:                              # noqa: BLE001
        logger.error("[ERROR] %s", traceback.format_exc())
        raise ToolError(f"{type(exc).__name__}: {exc}")


def _experiment_view(info, jobs: List[Job], name: str = None) -> dict:
    """An experiment as the tools report it, with this server's jobs for it.

    Before a run has written anything the directory does not exist yet: the
    job is then all there is, and says whether it is starting or has failed.
    """
    if info is not None:
        view = info.to_dict()
    else:
        running = any(j.state == "running" for j in jobs)
        view = {"name": name, "status": "starting" if running else "failed"}
    view["jobs"] = [j.to_dict() for j in jobs]
    return view


def build_server(factory: Callable[[], Any], guard_stdout: bool = False) -> FastMCP:
    """The MCP server, over a GraFlag made by `factory` on first use.

    `guard_stdout` moves ``sys.stdout`` to stderr once the stdio transport
    holds the protocol pipe (see :func:`_claim_stdio`); tests that drive the
    server in memory leave it off.
    """
    state = _State(factory)

    @asynccontextmanager
    async def lifespan(_server):
        if guard_stdout:
            # The stdio transport has its handle on the protocol pipe by now,
            # so from here a stray print() lands on stderr, not in a reply.
            sys.stdout = sys.stderr
        yield {}

    mcp = FastMCP("graflag", instructions=INSTRUCTIONS, lifespan=lifespan)
    mcp.graflag_state = state   # the jobs, for tests and for introspection
    # Report GraFlag's version in serverInfo rather than the SDK's.
    if hasattr(getattr(mcp, "_mcp_server", None), "version"):
        mcp._mcp_server.version = __version__

    def shared() -> str:
        return state.gf().config.remote_shared_dir

    # -- cluster and catalogue ---------------------------------------------

    @mcp.tool(annotations=_annotations("Cluster status", read_only=True))
    async def cluster_status() -> dict:
        """The manager, whether the Swarm is up, its worker nodes, the running
        services and the top of the shared directory."""
        return (await _blocking(lambda: state.gf().status())).to_dict()

    @mcp.tool(annotations=_annotations("List methods", read_only=True))
    async def list_methods() -> dict:
        """Every method on the cluster: its description, whether its image runs
        the authors' code ("upstream") or GraFlag's own ("reimplementation"),
        the datasets it accepts (shell patterns) and the image it runs in.
        `example` is the annotated template new integrations start from."""
        methods = await _blocking(lambda: state.gf().list_methods())
        return {"count": len(methods), "methods": [{
            "name": m.name,
            "description": m.description,
            "integration": m.integration or "unstated",
            "supported_datasets": m.supported_data or "*",
            "image": m.image or m.name,
        } for m in methods]}

    @mcp.tool(annotations=_annotations("Method details", read_only=True))
    async def method_details(method: str) -> dict:
        """One method's full record: its parameters with their defaults (what
        run_experiment's `params` can override), source repository and README.
        The README says what upstream does, what the integration changes and
        which split is scored -- read it before comparing numbers."""
        _name(method, "method")

        def fetch():
            gf = state.gf()
            found = next((m for m in gf.list_methods() if m.name == method), None)
            if found is None:
                raise ToolError(f"Method {method} not found")
            details = found.to_dict()
            readme = gf.ssh.read_file(gf.config.remote_shared_dir,
                                      f"methods/{method}/README.md")
            if len(readme) > MAX_README_CHARS:
                readme = readme[:MAX_README_CHARS] + "\n[... README truncated]"
            details["readme"] = readme
            return details
        return await _blocking(fetch)

    @mcp.tool(annotations=_annotations("List datasets", read_only=True))
    async def list_datasets() -> dict:
        """Every dataset on the share, with its size and file count. Files are
        downloaded on the first run that needs them, so a size of 0 is normal."""
        datasets = await _blocking(lambda: state.gf().list_datasets())
        return {"count": len(datasets), "datasets": [d.to_dict() for d in datasets]}

    # -- experiments ---------------------------------------------------------

    @mcp.tool(annotations=_annotations("List experiments", read_only=True))
    async def list_experiments(
        limit: Annotated[int, Field(ge=1, le=MAX_LIST)] = 20,
        method: Optional[str] = None,
        dataset: Optional[str] = None,
        status: Optional[str] = None,
    ) -> dict:
        """The most recent experiments, newest first, optionally filtered by
        method, dataset or status (building, running, completed, failed,
        stopped, unknown)."""
        for value, what in ((method, "method"), (dataset, "dataset")):
            if value is not None:
                _name(value, what)
        filtered = any(v is not None for v in (method, dataset, status))

        def fetch():
            found = state.gf().list_experiments(limit=MAX_LIST if filtered else limit)
            keep = [e for e in found
                    if (method is None or e.method == method.lower())
                    and (dataset is None or e.dataset == dataset.lower())
                    and (status is None or e.status == status)]
            return keep[:limit]
        experiments = await _blocking(fetch)
        return {"count": len(experiments),
                "experiments": [_experiment_view(e, state.jobs.for_experiment(e.name))
                                for e in experiments]}

    @mcp.tool(annotations=_annotations("Get experiment", read_only=True))
    async def get_experiment(experiment: str) -> dict:
        """One experiment's status, whether it has results and an evaluation,
        and any run or evaluation this server started for it (with the error,
        if it failed before writing anything to disk)."""
        _name(experiment, "experiment")
        info = await _blocking(lambda: state.gf().get_experiment(experiment))
        jobs = state.jobs.for_experiment(experiment)
        if info is None and not jobs:
            raise ToolError(f"Experiment {experiment} not found")
        return _experiment_view(info, jobs, experiment)

    @mcp.tool(annotations=_annotations("Run experiment", read_only=False,
                                       idempotent=False))
    async def run_experiment(
        method: str,
        dataset: str,
        params: Optional[Dict[str, Any]] = None,
        build: bool = False,
        gpu: bool = True,
        tag: str = "latest",
    ) -> dict:
        """Start running `method` on `dataset` and return the experiment name at
        once; the run continues in the background. `params` overrides the
        method's parameters (see method_details), e.g. {"EPOCHS": 5}. `build`
        rebuilds the image first, which takes minutes -- needed only after the
        method changed. `gpu=False` runs without reserving a GPU. Follow it with
        wait_for_experiment."""
        _name(method, "method")
        _name(dataset, "dataset")
        _name(tag, "tag")
        cleaned = _clean_params(params)

        def check():
            gf = state.gf()
            for kind, name in (("methods", method), ("datasets", dataset)):
                if not gf.ssh.path_exists(gf.config.remote_shared_dir, f"{kind}/{name}"):
                    raise ToolError(f"{kind[:-1].capitalize()} {name} not found")
            return gf
        gf = await _blocking(check)

        experiment = GraFlag.make_experiment_name(method, dataset)
        state.jobs.start(experiment, "run", lambda: gf.run(
            method, dataset, tag, build, gpu, cleaned,
            exp_name=experiment, follow=False))
        return {
            "experiment": experiment,
            "state": "started",
            "building_image": build,
            "next": "wait_for_experiment",
        }

    @mcp.tool(annotations=_annotations("Wait for experiment", read_only=True))
    async def wait_for_experiment(
        experiment: str,
        timeout_seconds: Annotated[int, Field(ge=0, le=MAX_WAIT_SECONDS)] = 120,
    ) -> dict:
        """Wait until the experiment -- and any run or evaluation this server
        started for it -- has finished, or until `timeout_seconds` pass.
        Returns the status either way; `finished` says which. Call it again to
        keep waiting."""
        _name(experiment, "experiment")
        deadline = time.monotonic() + timeout_seconds
        started = time.monotonic()
        while True:
            info = await _blocking(lambda: state.gf().get_experiment(experiment))
            jobs = state.jobs.for_experiment(experiment)
            busy = any(j.state == "running" for j in jobs)
            if not busy:
                if info is None:
                    if jobs:              # failed before writing anything
                        return {**_experiment_view(None, jobs, experiment),
                                "finished": True,
                                "waited_seconds": round(time.monotonic() - started)}
                    raise ToolError(f"Experiment {experiment} not found")
                if info.status in TERMINAL_STATUSES or (
                    info.status == "unknown" and not info.service_name
                ):
                    return {**_experiment_view(info, jobs), "finished": True,
                            "waited_seconds": round(time.monotonic() - started)}
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {**_experiment_view(info, jobs, experiment), "finished": False,
                        "waited_seconds": round(time.monotonic() - started)}
            await anyio.sleep(min(WAIT_POLL_SECONDS, remaining))

    @mcp.tool(annotations=_annotations("Experiment logs", read_only=True))
    async def get_logs(
        experiment: str,
        tail: Annotated[int, Field(ge=1, le=MAX_LOG_LINES)] = 100,
    ) -> dict:
        """The last `tail` lines of the experiment's output: the live service's
        log while it exists, else the saved method_output.txt."""
        _name(experiment, "experiment")
        lines = await _blocking(lambda: state.gf().get_logs(experiment, tail=tail))
        return {"experiment": experiment,
                "lines": [line[:MAX_LINE_CHARS] for line in lines]}

    @mcp.tool(annotations=_annotations("Stop experiment", read_only=False,
                                       destructive=True, idempotent=True))
    async def stop_experiment(experiment: str) -> dict:
        """Stop a running experiment by removing its Swarm service. Its
        directory, and whatever it has written so far, are kept."""
        _name(experiment, "experiment")

        def stop():
            gf = state.gf()
            gf.stop(experiment, remove=False)
            return gf.get_experiment(experiment)
        info = await _blocking(stop)
        return _experiment_view(info, state.jobs.for_experiment(experiment), experiment)

    # -- results -------------------------------------------------------------

    @mcp.tool(annotations=_annotations("Evaluate experiment", read_only=False,
                                       idempotent=True))
    async def evaluate_experiment(experiment: str) -> dict:
        """Start computing the experiment's metrics and plots (AUC-ROC, AUC-PR,
        best F1, precision/recall/F1 at k) in a short-lived evaluation service,
        and return at once. Follow it with wait_for_experiment, then
        get_evaluation."""
        _name(experiment, "experiment")

        def check():
            gf = state.gf()
            if not gf.ssh.path_exists(gf.config.remote_shared_dir,
                                      f"experiments/{experiment}"):
                raise ToolError(f"Experiment {experiment} not found")
            if not gf.ssh.path_exists(gf.config.remote_shared_dir,
                                      f"experiments/{experiment}/results.json"):
                raise ToolError(f"{experiment} has no results.json to evaluate yet")
            return gf
        gf = await _blocking(check)
        state.jobs.start(experiment, "evaluate",
                         lambda: gf.evaluate(experiment, follow=False))
        return {"experiment": experiment, "state": "evaluating",
                "next": "wait_for_experiment"}

    @mcp.tool(annotations=_annotations("Experiment results", read_only=True))
    async def get_results(experiment: str) -> dict:
        """What the run recorded besides its scores: the result type, run time,
        peak memory and GPU memory (measured by GraFlag's monitor), and the
        method's own metadata and summary. The scores themselves are not
        returned."""
        _name(experiment, "experiment")
        results = await _blocking(lambda: state.gf().get_experiment_results(experiment))
        if results is None:
            raise ToolError(f"{experiment} has no readable results.json yet")
        return results.to_dict()

    @mcp.tool(annotations=_annotations("Evaluation", read_only=True))
    async def get_evaluation(experiment: str) -> dict:
        """The metrics evaluate_experiment computed, and the names of its plots
        (read one with get_plot)."""
        _name(experiment, "experiment")
        evaluation = await _blocking(lambda: state.gf().get_evaluation_results(experiment))
        if evaluation is None:
            raise ToolError(f"{experiment} has not been evaluated; "
                            "run evaluate_experiment first")
        return evaluation.to_dict()

    @mcp.tool(annotations=_annotations("Evaluation plot", read_only=True))
    async def get_plot(experiment: str, plot: str) -> Image:
        """One of the evaluation's plots as an image, e.g. `roc_curve`,
        `pr_curve`, `score_distribution`, `training_curves` or
        `resources_curves` (get_evaluation lists what exists)."""
        _name(experiment, "experiment")
        _name(plot, "plot")
        filename = plot if plot.endswith(".png") else f"{plot}.png"

        def fetch():
            gf = state.gf()
            path = remote_path(gf.config.remote_shared_dir, "experiments",
                               experiment, "eval", filename)
            result = gf.ssh.execute(
                f"test -f {path} || exit 3; "
                f"size=$(wc -c < {path}); "
                f"[ \"$size\" -le {MAX_PLOT_BYTES} ] || exit 4; "
                f"base64 < {path} | tr -d '\\n'")
            if result.returncode == 3:
                raise ToolError(f"{experiment} has no plot {filename}")
            if result.returncode == 4:
                raise ToolError(f"{filename} is larger than {MAX_PLOT_BYTES} bytes")
            if result.returncode != 0:
                raise ToolError(f"Could not read {filename}: {result.stderr.strip()}")
            return base64.b64decode(result.stdout.strip())
        return Image(data=await _blocking(fetch), format="png")

    @mcp.tool(annotations=_annotations("Verify experiment", read_only=True))
    async def verify_experiment(experiment: str) -> dict:
        """Check that the published result is worth believing: scores and
        ground truth match in length, both classes are present, the scores
        vary, the scored split is declared as the test split, and the AUC the
        evaluator computes equals the one the method reported. Run
        evaluate_experiment first. `failed` > 0 means the number should not be
        used; read every WARN."""
        _name(experiment, "experiment")
        return (await _blocking(lambda: state.gf().verify(experiment))).to_dict()

    # -- housekeeping ----------------------------------------------------------

    @mcp.tool(annotations=_annotations("Clean up services", read_only=False,
                                       destructive=True, idempotent=True))
    async def cleanup_services(dry_run: bool = False) -> dict:
        """Remove the Swarm services of finished experiments. A service is kept
        whenever the run cannot be diagnosed from disk, since its log would be
        the only record. `dry_run` reports without removing."""
        results = await _blocking(lambda: state.gf().cleanup_services(dry_run=dry_run))
        return {"dry_run": dry_run, "services": [r.to_dict() for r in results]}

    @mcp.tool(annotations=_annotations("Storage report", read_only=True))
    async def storage_report() -> dict:
        """What `graflag clear` would reclaim: experiments whose method is gone,
        datasets only they used, unowned images and registry repositories, and
        strays on the share, with sizes. Report only -- nothing is removed;
        removing is left to a person, with `graflag clear --apply`."""
        return (await _blocking(lambda: state.gf().clear(apply=False))).to_dict()

    return mcp


# ============================================================================
# stdio
# ============================================================================

#: The protocol's own stdin and stdout, held here for the life of the process.
#: The lifespan rebinds sys.stdout to stderr; if nothing else referred to the
#: protocol stream, collecting it would close the buffer the SDK's transport
#: writes through, and the first reply would fail with "I/O operation on
#: closed file".
_PROTOCOL_STREAMS = ()


def _claim_stdio():
    """Give the protocol the only handles on the client's pipes.

    Duplicates file descriptors 0 and 1 for the protocol, then points fd 0 at
    /dev/null and fd 1 at stderr, so no subprocess, C extension or stray write
    can read the client's requests or write into its replies. ``sys.stdin`` and
    ``sys.stdout`` are rebound to the duplicates, which is what the SDK's stdio
    transport wraps when it starts; the server's lifespan then moves
    ``sys.stdout`` to stderr (see :func:`build_server`).
    """
    sys.stdout.flush()
    protocol_in = os.dup(0)
    protocol_out = os.dup(1)
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
    os.close(devnull)
    os.dup2(2, 1)
    global _PROTOCOL_STREAMS
    sys.stdin = io.TextIOWrapper(io.BufferedReader(io.FileIO(protocol_in, "rb")),
                                 encoding="utf-8")
    sys.stdout = io.TextIOWrapper(io.BufferedWriter(io.FileIO(protocol_out, "wb")),
                                  encoding="utf-8", write_through=True)
    _PROTOCOL_STREAMS = (sys.stdin, sys.stdout)


def serve(config_file: Optional[str] = None):
    """Serve GraFlag's tools over stdio until the client disconnects."""
    # The SDK logs every request at INFO; GraFlag's own messages are the ones
    # worth finding in the client's server log.
    logging.getLogger("mcp").setLevel(logging.WARNING)
    _claim_stdio()
    server = build_server(lambda: GraFlag(config_file, interactive=False),
                          guard_stdout=True)
    server.run("stdio")
