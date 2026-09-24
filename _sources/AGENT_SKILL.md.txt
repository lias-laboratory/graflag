# Method Integration Skill

GraFlag ships an agent skill for integrating methods: the procedure, the
reference material and the checks the integrations in `graflag-shared` follow,
packaged so that an AI coding agent can work through them. It lives in the
graflag-shared repository:

```
graflag-shared/.claude/skills/method-integration/
├── SKILL.md               # the procedure and its four gates
├── reference/sdk.md       # the graflag_runner API for integration scripts
├── reference/traps.md     # failures that reported success, and their fixes
└── scripts/verify_run.py  # gate 4 under its old name, a shim over graflag verify
```

The layout is Claude Code's: a `SKILL.md` whose front matter names the skill and
says when to use it. The content is plain Markdown and Python, so any agent, or
a person, can follow it.

## Using it

**Claude Code** picks up the skills in a project's `.claude/skills/` directory.
Start it in a graflag-shared checkout and ask for an integration, or invoke the
skill with the upstream repository:

```
/method-integration https://github.com/author/method
```

To have it available from any directory, link it into your user skills:

```bash
ln -s "$PWD/.claude/skills/method-integration" ~/.claude/skills/method-integration
```

**Other agents**: give the agent `SKILL.md` and the two reference files. The
[AI Agent Instructions](AGENT_METHOD_INTEGRATION.md) are the long form of the
same procedure, with complete templates and a worked integration.

## The four gates

A method is not reported as integrated until all four pass, in order. Each one
catches what the one before it cannot, and each needs the one before it: gate 4
compares the AUC the method reported with the one gate 3 computed.

| Gate | Command | Catches |
|---|---|---|
| 1. Contract | `python3 -m unittest discover -s tests`, in graflag-shared | The `.env` and Dockerfile schema, unpinned clones, `sed -i` on cloned source, `COPY` paths, GPU conventions, provenance, a README with no `## Verification` section |
| 2. Build and run | `graflag sync`, then `graflag run -m METHOD -d DATASET --build` | Anything that only exists on the share |
| 3. Evaluation | `graflag evaluate -e EXP` | Metrics and plots, from the scores exactly as published |
| 4. Result integrity | `graflag verify -e EXP` | Empty, one-class or constant scores, a length mismatch, an undeclared or non-test split, published scores that disagree with the AUC the method reported |

## graflag verify

`status.json` saying `completed` means the method exited 0 and wrote a
`results.json` that parses. It does not mean the numbers are the method's, that
they cover the test split, or that they are the scores whose AUC the method
printed. `graflag verify` (graflag 1.2.0 or later) tests the generic half of
that:

- the sample is usable: scores and ground truth have the same length, both
  classes are present, and something is left after the evaluator's filtering;
- the scores vary, since a constant column scores an AUC of 0.5 and looks like
  a method;
- every `*auc*` the method recorded under `metadata.summary` matches
  `eval/evaluation.json`, and `scored_samples` matches the actual count;
- the scored split is declared, and declared to be the test split.

It exits 1 when a check fails; warnings do not fail it, and each one should be
read. It needs no agent: it runs on any finished experiment, reading it on the
manager in one SSH call, and the scores never leave the manager. What it cannot
check is whether the method's own number is right, only that the published
scores reproduce it. The skill's `scripts/verify_run.py EXP` is the same check
under its old name. graflag's `tests/test_verify.py` covers the checks, and
graflag-shared's `tests/test_verify_run.py` holds the probe's plain-Python
counting to the evaluator's.

## Recording the result

The last step of an integration writes the gates down: the method's README gets
a `## Verification` section with, per dataset, the parameters, gate 2 (status,
run time, peak memory), gate 3 (`auc_roc`) and gate 4 (failed / warned /
passed). A method that cannot run on the cluster gets the same section saying
so, with the traceback and what was tried, and one never run says that too.
Gate 1 refuses a method with no such section and no row in `VERIFICATION.md`,
so "integrated" always means something a reader can check.

## Through MCP

With the {doc}`MCP server <MCP>` configured, an agent can run gates 2 to 4 as
tools: `run_experiment` (with `build: true`) and `wait_for_experiment`,
`get_logs` while it runs, `evaluate_experiment` and `wait_for_experiment`
again, then `verify_experiment`. Gate 1 and `graflag sync` stay on the command
line, because they read the local checkout.

## The rules it enforces

- **Fail loudly.** A missing input, a patch that does not apply or an empty pin
  raises; it is never skipped, substituted or reported with a warning that lets
  the run continue.
- **Publish what the method computed.** If upstream writes a score file, the
  integration publishes that file's contents. A method whose scores are computed
  by the integration is declared `INTEGRATION=reimplementation`, and its README
  says so first.
- **Never report a run that was not verified.** A method that cannot run on the
  cluster is recorded as an environment limit, with the traceback.
