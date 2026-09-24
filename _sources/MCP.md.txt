# AI Agents (MCP)

`graflag mcp` serves GraFlag over the [Model Context Protocol](https://modelcontextprotocol.io),
so an AI agent can manage the platform with the same operations as the CLI:
list methods and datasets, launch and watch runs, evaluate and verify them,
and read what they produced. It runs on your machine beside the client and
drives the cluster the same way the CLI does, over SSH.

## Installing

The server needs the MCP SDK, an optional dependency that needs Python 3.10 or
later (the rest of GraFlag runs on 3.8):

```bash
pip install "graflag[mcp]"
```

The extra pins the SDK below 2.0. Version 2 removed the `FastMCP` API the
server is built on, and other MCP servers installed in the same environment may
still import it.

## Connecting a client

The server speaks MCP on stdio: the client starts `graflag mcp` and talks to it
over its stdin and stdout. It uses the same configuration as the CLI
(see {doc}`quickstart`), or the file given with `--config`.

**Claude Code**:

```bash
claude mcp add graflag -- graflag mcp
claude mcp add graflag -- graflag mcp --config ~/.config/graflag/lab.env   # another cluster
```

Add `--scope user` to have it in every project rather than only the current one.

**Claude Desktop**, and other clients configured with JSON:

```json
{
  "mcpServers": {
    "graflag": {"command": "graflag", "args": ["mcp"]}
  }
}
```

The server never prompts. SSH runs in batch mode, so the key it uses must work
without a passphrase prompt (an unencrypted key, or one loaded in `ssh-agent`),
and an unreachable manager fails after 15 seconds instead of hanging a tool
call. A configuration problem is reported on the first tool call, with how to
fix it; listing the tools works without one.

## Tools

| Tool | What it does | |
|---|---|---|
| `cluster_status` | The manager, the Swarm, its nodes and services, the shared directory | read-only |
| `list_methods` | Every method: description, `upstream` or `reimplementation`, accepted datasets, image | read-only |
| `method_details` | One method's parameters with their defaults, source and README | read-only |
| `list_datasets` | Every dataset, with size and file count | read-only |
| `list_experiments` | Recent experiments, filtered by method, dataset or status | read-only |
| `get_experiment` | One experiment's status, and any run or evaluation this server started for it | read-only |
| `run_experiment` | Start a run and return its name at once | |
| `wait_for_experiment` | Wait, up to a timeout, until the experiment and its jobs have finished | read-only |
| `get_logs` | The last lines of a run's output | read-only |
| `stop_experiment` | Stop a run; its directory is kept | destructive |
| `evaluate_experiment` | Start computing metrics and plots, and return at once | |
| `get_results` | Result type, run time, peak memory, the method's own summary; never the scores | read-only |
| `get_evaluation` | The metrics, and the names of the plots | read-only |
| `get_plot` | One plot, as an image | read-only |
| `verify_experiment` | The checks of {ref}`graflag verify <cli-verify>` | read-only |
| `cleanup_services` | Remove the services of finished runs (see `graflag cleanup`) | destructive |
| `storage_report` | What `graflag clear` would reclaim; removes nothing | read-only |

The last column is sent to the client as tool annotations, which is what lets a
client ask before a destructive call.

**Deliberately not offered:** `graflag clear --apply` and `--gc`, `stop --rm`
(which deletes the experiment directory), `setup`, `copy`, `sync`, and
`register_metric`, which writes code that the evaluator runs on the cluster as
root. Those stay with a person at the CLI.

## How a run proceeds

A run takes seconds to hours, longer than a client will wait for one tool call.
`run_experiment` checks that the method and dataset exist, starts the run in the
background and returns the experiment name. `wait_for_experiment` then blocks
for up to `timeout_seconds` (at most 600, default 120) and returns the status
either way, with `finished` saying which; an agent calls it again to keep
waiting. When the run ends, its service is removed, as `graflag run` does.

`evaluate_experiment` works the same way, and `wait_for_experiment` also waits
for an evaluation this server started. A failure that happens before the run
has written anything -- a build that fails, say -- is reported by
`get_experiment` and `wait_for_experiment` from the job itself.

A session that runs AnoGraph on the ISCX stream, recorded on the development
cluster (timings from the start of the session):

```
run_experiment      {"method": "anograph", "dataset": "anograph_iscx"}
                    -> exp__anograph__anograph_iscx__20260924_132705, started      2.8 s
wait_for_experiment -> completed, finished                                      13.7 s
evaluate_experiment -> evaluating
wait_for_experiment -> completed, finished                                      30.5 s
get_evaluation      -> auc_roc 0.948, auc_pr 0.523, 2751 samples
verify_experiment   -> 0 failed, 1 warned, 3 passed
```

The session left nothing running: both services were removed when their jobs
ended.

On a development cluster, run one method at a time: its nodes share one host's
memory. The server does not enforce this.

## Parameters

`run_experiment` takes `params` as an object, e.g. `{"EPOCHS": 5}`. Names are
those of the method's `.env` with or without the leading underscore, in any
case; they are sent upper-case, as the `.env` declares them. Values must be
strings, numbers or booleans. A name GraFlag sets itself (`DATA`, `EXP`,
`METHOD_NAME`, `COMMAND`, `MONITOR_INTERVAL`) is refused with an error, where
the CLI would drop it.

## What an agent can and cannot send

Everything an agent sends is treated as untrusted, like a request to the web
dashboard. Method, dataset, experiment, tag and plot names must be plain
identifiers (letters, digits, `.`, `_`, `-`); anything else is refused before
it reaches a command that runs on the manager.

## For maintainers: stdout is the protocol

A stdio MCP server's stdout carries its replies and its stdin carries the
client's requests, so anything else that writes to one or reads the other breaks
the session. `graflag mcp` guards both: it gives the protocol private copies of
the two pipes, points file descriptor 1 at stderr and 0 at `/dev/null`, and
moves `sys.stdout` to stderr once the transport has started. Separately, every
core call it makes waits without streaming (`follow=False`), and every ssh and
rsync GraFlag starts has its stdin closed. `tests/test_mcp.py` runs a real
stdio session against a backend that prints and starts a subprocess writing to
stdout and reading stdin, and checks that the session survives. Keep new
server code off `print()`; log instead, which goes to stderr.
