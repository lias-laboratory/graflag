# AI Agent Instructions: GraFlag Method Integration

This document provides precise instructions for an AI agent to integrate new graph anomaly detection methods into the GraFlag benchmarking framework.

An agent that supports skills can use the [Method Integration Skill](AGENT_SKILL.md) instead: the same procedure, packaged with its four gates and a result checker, in graflag-shared.

---

## Quick Reference

| Item | Location |
|------|----------|
| Methods directory | `graflag-shared/methods/{method_name}/` |
| Datasets directory | `graflag-shared/datasets/{dataset_name}/` |
| Libraries directory | `graflag-shared/libs/` |
| Start from | `methods/example/` -- the annotated template |
| Entry point script | `train_graflag.py`, named by `COMMAND` in `.env` |
| Container entry point | always `CMD ["python3", "-m", "graflag_runner"]` |
| CLI command | `graflag run -m METHOD -d DATASET [--build]` |

---

## Two Integration Patterns

Every method runs the same way -- `CMD ["python3", "-m", "graflag_runner"]` --
and the two patterns differ only in what `COMMAND` in `.env` points at.

### Pattern A: An integration script

```bash
COMMAND=python3 train_graflag.py
```

The script declares a `Config` dataclass and fills it from the `_`-prefixed
env vars with one call:

```python
from dataclasses import dataclass
from graflag_runner import params

@dataclass
class Config:
    batch_size: int = 128
    learning_rate: float = 0.001
    use_memory: bool = True

config = Config(**params(Config))   # _BATCH_SIZE=256 -> config.batch_size == 256 (an int)
```

`params()` strips the underscore, lowercases the name, coerces to the
annotated type, and leaves the dataclass default in place for anything the
`.env` does not set. Every method with a script uses this pattern -- all
sixteen outside the PyGOD family: `ada_gad`, `addgraph`, `ad_gcl`, `anograph`,
`diffgad`, `dynwalk`, `f_fade`, `gady`, `generaldyg`, `huge_gad`, `midas`,
`rare`, `slade`, `streamspot`, `strgnn` and `taddy`, plus the `example`
template. Fourteen build their `Config` with `params()` as above; `generaldyg`
and `ada_gad` adopt upstream's argparse namespace with `apply_params()` instead
(see File 3).

### Pattern B: A library entry point

```bash
COMMAND=python3 -m graflag_bond.train
```

`graflag_bond` selects the PyGOD detector from `METHOD_NAME` and builds its
constructor arguments from the same env vars, dropping any the detector does
not accept. Used by all 17 `bond_*` methods -- which is what lets them share
one image (`IMAGE=bond_base`) and still run different detectors.

### `--pass-env-args`: supported, and used by nothing

```dockerfile
CMD ["python3", "-m", "graflag_runner", "--pass-env-args"]
```

The runner rewrites `_BATCH_SIZE=128` into `--batch_size 128` and appends it
to `COMMAND`. Every method that once used this was migrated to `params()`:
the rewrite lowercases names (a repo whose flag is `--lr_G` needs it renamed),
coerces nothing, supplies no defaults, and offers no way to accept a parameter
while recording that it does nothing. Reach for it only when `COMMAND` is
upstream's own entry point and you are not writing a script at all.

---

## Integration Checklist

For each method, create/verify the following files:

```
graflag-shared/methods/{method_name}/
├── .env                     # REQUIRED: Configuration
├── Dockerfile               # REQUIRED unless .env sets IMAGE=<shared image>
├── train_graflag.py         # REQUIRED unless COMMAND is a library entry point
├── patches/*.patch          # OPTIONAL: fixes to the clone, via `git apply`
└── requirements.txt         # OPTIONAL: Python dependencies
```

The upstream clone is **not** a directory here. It is created inside the image
at build time (`git clone ... src` in `/app`) from `SOURCE_CODE` at
`SOURCE_REF`, so it never enters the build context and never gets committed.

---

## File 1: `.env` Configuration

### Rules

1. **`METHOD_NAME`**: Lowercase, alphanumeric and underscores only
2. **`COMMAND`**: Entry point command (e.g., `python3 train_graflag.py` or `python3 -m graflag_bond.train`)
3. **`SUPPORTED_DATASETS`**: Comma-separated list of compatible dataset names (supports wildcards like `bond_*`)
4. **Hyperparameters**: ALL must be prefixed with `_` (underscore)
5. **Parameter naming**: `params()` strips the underscore and **lowercases** the rest
   - `_BATCH_SIZE=128` becomes `batch_size=128`
   - `_LR_G=0.001` becomes `lr_g=0.001`, so the field in your `Config` is `lr_g`, not `lr_G`
6. **Boolean parameters**: spell the value out -- `_USE_MEMORY=true` or `_USE_MEMORY=false`
   - `params()` reads `true`/`false`/`1`/`0`/`yes`/`no`, case-insensitively
   - **NEVER** write a bare `_USE_MEMORY=`. Under `--pass-env-args` an empty value
     became a bare `--use_memory` flag that argparse read as **True**; `params()`
     reads the same empty string as **False**. A `.env` carrying the old spelling
     silently inverts the flag, which is how `gady` ran with memory disabled.
7. **Reserved variables** (set by the orchestrator; a parameter with one of these names is silently dropped): `DATA`, `EXP`, `METHOD_NAME`, `COMMAND`, `MONITOR_INTERVAL`
8. **`SOURCE_REF`**: the 40-character commit your Dockerfile checks out. Required whenever the image clones anything. An unpinned clone is a method whose numbers change when upstream does, with nothing in the experiment saying so -- and the failure arrives as a `TypeError` about a signature, which reads like a GraFlag bug.
9. **`INTEGRATION`**: `upstream` or `reimplementation`. `SOURCE_CODE` names the paper's repository either way; this says whether that is the code the image actually runs. Write `reimplementation` if you wrote the model yourself, even faithfully.
10. **`IMAGE`** (optional): the name of a shared image under `images/<name>/`, used instead of a Dockerfile in your method directory. Declare it only when your image would be byte-identical to an existing one -- the seventeen `bond_*` methods share `bond_base`. Do not copy a Dockerfile that already exists; the test suite fails on duplicates and names the file you duplicated.

`graflag-shared/tests/test_methods.py` checks every one of these. It is faster to read the failure than to discover the same thing on the cluster.

### Template (integration script)

```bash
METHOD_NAME={method_name}
DESCRIPTION={Brief description from paper}
SOURCE_CODE={GitHub URL}
SOURCE_REF={40-char commit sha, from `git ls-remote {GitHub URL} HEAD`}
INTEGRATION=upstream
SUPPORTED_DATASETS={dataset1},{dataset2}

COMMAND=python3 train_graflag.py

# === HYPERPARAMETERS ===
_EPOCHS=100
_BATCH_SIZE=128
_LEARNING_RATE=0.001
_HIDDEN_DIM=64
_SEED=42
```

### Template (shared bond image)

```bash
METHOD_NAME=bond_{detector}
DESCRIPTION={Detector description}
SOURCE_CODE={PyGOD documentation page of the detector}
INTEGRATION=upstream
# Seventeen bond methods share one image; there is no Dockerfile in this
# directory. They stay distinct at run time because METHOD_NAME is set per
# service and graflag_bond.train selects the detector from it.
IMAGE=bond_base
SUPPORTED_DATASETS=bond_*

COMMAND=python3 -m graflag_bond.train

_HID_DIM=64
_NUM_LAYERS=4
_DROPOUT=0
_WEIGHT_DECAY=0
_LR=0.004
_EPOCH=100
_GPU=0
_BATCH_SIZE=0
```

### Real Example: TADDY

Copied from `methods/taddy/.env`, comments stripped:

```bash
METHOD_NAME=taddy
DESCRIPTION=Temporal Anomaly Detection in Dynamic Networks via Transformer
SOURCE_CODE=https://github.com/yuetan031/TADDY_pytorch
SOURCE_REF=dfe15ddbbc179ed0ca9a96482fe1696dfbcb5bcd
INTEGRATION=upstream
SUPPORTED_DATASETS=uci,btc_alpha,btc_otc,digg

COMMAND=python3 train_graflag.py

_ANOMALY_PER=0.1
_TRAIN_PER=0.5
_NEIGHBOR_NUM=5
_WINDOW_SIZE=2
_EMBEDDING_DIM=32
_NUM_HIDDEN_LAYERS=2
_NUM_ATTENTION_HEADS=2
_MAX_EPOCH=200
_LEARNING_RATE=0.001
_WEIGHT_DECAY=0.0005
_SEED=1
_PRINT_FREQ=10
_GPU=0
```

---

## File 2: `Dockerfile`

### Template (integration script)

```dockerfile
FROM nvidia/cuda:12.1.0-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive

WORKDIR /app

# System dependencies
RUN apt-get update && apt-get install -y \
    python3 python3-pip git \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --upgrade pip

# PyTorch (adjust version based on method requirements)
RUN pip install --no-cache-dir \
    torch torchvision torchaudio \
    --index-url https://download.pytorch.org/whl/cu121

# Common dependencies
RUN pip install --no-cache-dir numpy scipy scikit-learn pandas networkx tqdm

# PyTorch Geometric (if needed)
# RUN pip install --no-cache-dir torch-geometric
# RUN pip install --no-cache-dir \
#     torch-scatter torch-sparse \
#     -f https://data.pyg.org/whl/torch-2.1.0+cu121.html

# Clone the pinned upstream. SOURCE_CODE and SOURCE_REF come from this
# method's .env; graflag forwards them as --build-arg (BUILD_ARG_KEYS in
# docker_ops.py), so the repository and the commit are named in one place.
#
# `test -n` is not ceremony: with SOURCE_REF empty, `git checkout --detach`
# still exits 0 and leaves the clone on whatever the default branch holds
# that day -- exactly the silent float the pin exists to remove.
ARG SOURCE_CODE
ARG SOURCE_REF
RUN test -n "${SOURCE_CODE}" && test -n "${SOURCE_REF}" \
    && git clone ${SOURCE_CODE} src \
    && git -C src checkout --detach ${SOURCE_REF}

# Copy GraFlag integration files
COPY methods/{method_name}/train_graflag.py ./
COPY methods/{method_name}/*.py ./

# Where the GraFlag libraries come from: `local` (the default) installs the
# copy on the share, `pypi` the published wheel. graflag passes the choice
# as --build-arg from GRAFLAG_LIBS in its config. Copy this stanza verbatim.
#
# The unknown arm exits non-zero on purpose. An if/else would read a typo as
# "pypi" and build an image whose libraries are not the ones that were asked
# for -- and the build would report success.
ARG GRAFLAG_LIBS=local
COPY libs/graflag_runner /tmp/graflag_libs/graflag_runner
RUN case "${GRAFLAG_LIBS}" in \
      local) pip install --no-cache-dir /tmp/graflag_libs/graflag_runner ;; \
      pypi)  pip install --no-cache-dir graflag-runner ;; \
      *) echo "[ERROR] GRAFLAG_LIBS must be local or pypi, got '${GRAFLAG_LIBS}'" >&2; exit 1 ;; \
    esac && rm -rf /tmp/graflag_libs

# The entry point is always the runner. It sets DATA/EXP, starts the resource
# monitor, runs COMMAND from .env, and records the status -- your script is
# what COMMAND names, not what CMD names.
CMD ["python3", "-m", "graflag_runner"]
```

### The shared bond image

```dockerfile
# graflag-shared/images/bond_base/Dockerfile, comments trimmed
FROM nvidia/cuda:13.0.2-runtime-ubuntu24.04
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
    python3 python3-pip git \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --break-system-packages \
    numpy scipy scikit-learn networkx
RUN pip install --no-cache-dir --break-system-packages \
    torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu121
RUN pip install --no-cache-dir --break-system-packages \
    torch-geometric pyg-lib torch-scatter torch-sparse \
    -f https://data.pyg.org/whl/torch-2.5.0+cu121.html
# PyGOD pinned: an unpinned master means a detector whose signature changed
# upstream starts silently dropping parameters, or disappears from
# pygod.detector altogether.
RUN pip install --no-cache-dir --break-system-packages \
    git+https://github.com/pygod-team/pygod.git@c84dccafa9bf587cd03cfd3e89b9be7a8f3337e8

WORKDIR /app

# GraFlag libraries (runner + bond wrapper): the same stanza, with graflag_bond.
ARG GRAFLAG_LIBS=local
COPY libs/graflag_runner /tmp/graflag_libs/graflag_runner
COPY libs/graflag_bond /tmp/graflag_libs/graflag_bond
RUN case "${GRAFLAG_LIBS}" in \
      local) pip install --no-cache-dir --break-system-packages /tmp/graflag_libs/graflag_runner /tmp/graflag_libs/graflag_bond ;; \
      pypi)  pip install --no-cache-dir --break-system-packages graflag-runner graflag-bond ;; \
      *) echo "[ERROR] GRAFLAG_LIBS must be local or pypi, got '${GRAFLAG_LIBS}'" >&2; exit 1 ;; \
    esac && rm -rf /tmp/graflag_libs

# Same entry point as any other method; what differs is COMMAND in .env.
CMD ["python3", "-m", "graflag_runner"]
```

**You will almost certainly not write this file.** All seventeen `bond_*`
methods declare `IMAGE=bond_base` and share the one at
`graflag-shared/images/bond_base/Dockerfile`; a new PyGOD detector is a `.env`
and nothing else. The template is here because adding an eighteenth image --
a second library with the same shape -- is the case where you would.

### Key Points

- **Build context**: The entire `graflag-shared/` directory is the build context, so `COPY methods/<name>/...` paths work. Note `datasets/`, `experiments/` and `.git/` are excluded by `.dockerignore` -- datasets are bind-mounted at run time, never built into the image. That file has to be on the share to do anything, since the context root is `SHARED_DIR` on the manager and not your checkout; neither `sync` nor `sync --lib` puts it there, so send it once with `graflag copy -s ./.dockerignore --dest .`. A build with no `.dockerignore` at the context root succeeds at full size rather than failing, which is why `graflag run --build` warns about it. Docker's own accounting on this cluster, for the same share: `Sending build context to Docker daemon  3.651GB` without the file, `804.4kB` with it.
- **Installing the GraFlag libraries**: copy the `ARG GRAFLAG_LIBS` stanza above; do not write your own install line. The choice is the cluster's, not the method's -- `GRAFLAG_LIBS` in `~/.config/graflag/config.env` selects it for every build, and a method that hardcodes `pip install graflag-runner` silently opts out of a library change the operator asked for. (`graflag/tests/test_local_libs.py` asserts a method `.env` cannot override it.)

  This is also why `graflag sync --lib` alone is not enough: it updates `/shared/libs`, but nothing inside an already-built image. Re-run the method with `--build` afterwards, which is what picks the new copy up.

- **Base image**: Use the base the method's requirements call for. Most methods use an `nvidia/cuda` runtime image, from `11.1.1-runtime-ubuntu20.04` for older code to `13.0.2-runtime-ubuntu24.04`; CPU-only methods use `ubuntu` or `python` images; `rare` uses `python:3.12.4-slim` with pip-installed CUDA wheels, to reproduce the environment it was developed in. A base without CUDA must set `NVIDIA_VISIBLE_DEVICES=all` and `NVIDIA_DRIVER_CAPABILITIES=compute,utility`, or the nvidia runtime hands the container no GPU.
- **graflag_runner**: Always install it -- it handles execution lifecycle, resource monitoring, and status tracking.

---

## File 3: `train_graflag.py`

Needed whenever `COMMAND` names a script. A method whose `COMMAND` is a
library entry point (`python3 -m graflag_bond.train`) has no such file.

**Read `methods/example/train_graflag.py` before writing one.** It is the
annotated template, it is 172 lines, and every helper below is used in it.

### Critical Implementation Details

#### 1. Parameters: one dataclass, one call

```python
from dataclasses import dataclass
from graflag_runner import params


@dataclass
class Config:
    """The parameter contract. Every field is a `_FOO` in .env, and the
    annotation is what the value is coerced to."""
    learning_rate: float = 0.001
    max_epoch: int = 200
    batch_size: int = 128
    use_memory: bool = True
    seed: int = 42
    gpu: int = 0


config = Config(**params(Config))
# _LEARNING_RATE=0.001 -> config.learning_rate == 0.001   (a float, not "0.001")
# _USE_MEMORY=false    -> config.use_memory is False
# a _FOO with no field -> dropped, with a [WARN] naming it
```

`params()` strips the underscore, lowercases the name, coerces against the
annotation, and drops what the signature does not accept. The dataclass
default is what applies when `.env` does not set the key, so a run never
depends on a key being present.

What this replaces, and why none of it should come back:

| Old idiom | Why it is gone |
|---|---|
| `str2bool` helper | `params()` parses `true`/`false`/`1`/`0`/`yes`/`no` itself, and an empty value is `False` rather than the `True` argparse made of it |
| `parser.add_argument('--lr_g', '--lr_G', ...)` | there is no argv, so there is no case to alias -- the field is `lr_g` |
| a 30-key `env_mappings` dict (`gady`) | the dataclass *is* the mapping |
| `os.environ.get("_EPOCHS", 100)` per parameter | a string `"100"` that silently survives into `range()` |

**If the method has its own argparse and you want upstream's defaults**, do
not re-declare them. Take upstream's namespace and write onto it:

```python
from option import args                 # upstream's own parser, with its defaults
from graflag_runner import apply_params

applied = apply_params(args, ignore={"gpu"})   # _GPU is read by device()
```

Each `_FOO` is matched to an existing attribute and coerced to the type of the
value already there, so upstream's defaults define both what is accepted and
how it is read. `generaldyg` and `ada_gad` work this way.

This is also the safer half of the argument for not using `--pass-env-args`:
passing parameters through argv gives argparse's abbreviation matching a say.
With upstream declaring `--gpus` and no `--gpu`, `_GPU=0` arrives as
`--gpu 0` and argparse quietly sets `gpus=0`.

#### 2. Everything else the SDK provides

```python
from graflag_runner import (
    device, load_dataset, params, paths, seed_all, upstream,
)

run = paths()                  # run.data, run.exp, run.dataset -- raises if unset
seed_all(config.seed)          # random, numpy and torch, together
dev = device()                 # honours _GPU=-1 (which is what `--no-gpu` sets)
upstream("src")                # puts the cloned repo on sys.path, raises if absent
edges, labels = load_dataset() # (edges, labels) from any of the four layouts
```

Use them rather than hand-rolling the equivalent. Each one exists because the
hand-rolled version was wrong somewhere:

- `device()` -- three methods built `f"cuda:{gpu}"` themselves and died on
  `Invalid device string: 'cuda:-1'`. One place understands that `-1` is CPU.
- `paths()` -- a missing `EXP` used to surface as a `TypeError` inside
  `os.path.join`, hundreds of lines later.
- `upstream()` -- anchors on `__file__`, so it does not depend on the working
  directory, and it raises when the clone is missing instead of failing at the
  first import.
- `load_dataset()` -- the same 100-line snapshot loader had been copied into
  five methods, transposes and all.
- `seed_all()` -- before it, no method seeded numpy or torch, so no run was
  repeatable.

**Do not** time `main()`, sample `psutil` memory, or read
`torch.cuda.max_memory_allocated()`. `graflag_runner` measures all three from
outside the method, over the whole process tree, and **its numbers are the
ones reported**; anything you record yourself is kept beside them as
`method_reported_<key>`. A method that sampled its own peak memory reported
409 MB against the monitor's 907 MB.

#### 3. Environment Variables

The orchestrator sets these environment variables in every container:

| Variable | Description | Example |
|----------|-------------|---------|
| `DATA` | Input dataset directory | `/shared/datasets/uci` |
| `EXP` | Experiment output directory | `/shared/experiments/exp__taddy__uci__20260309_143000` |
| `METHOD_NAME` | Method identifier | `taddy` |
| `COMMAND` | Command from .env | `python3 train_graflag.py` |
| `GRAFLAG_PARAMS` | Which `_FOO` variables are parameters | `_GPU,_MAX_EPOCH,_SEED` |

#### 4. ResultWriter API

```python
from graflag_runner import ResultWriter

writer = ResultWriter()  # Auto-reads EXP env var

# Add metadata (call before or after save_scores)
writer.add_metadata(method_name="taddy", dataset="uci", learning_rate=0.001)

# Resource metrics: rarely needed, and not what gets reported. The runner
# measures exec time, peak memory and peak GPU from outside the method and
# merges its own numbers in afterwards; whatever you pass here is preserved
# beside them as method_reported_exec_time_ms and so on.
writer.add_resource_metrics(exec_time_ms=45230.15, peak_memory_mb=2048.5)

# Track training progress (creates training.csv)
writer.spot("training", epoch=1, loss=0.5, auc=0.85)
writer.spot("training", epoch=2, loss=0.3, auc=0.90)

# Track validation metrics (creates validation.csv)
writer.spot("validation", val_loss=0.4, val_auc=0.88)

# Save final scores
writer.save_scores(
    result_type="EDGE_STREAM_ANOMALY_SCORES",
    scores=scores_list,
    ground_truth=labels_list,
)

# Finalize (writes results.json)
writer.finalize()
```

For large results, use streaming to avoid memory issues:

```python
from graflag_runner import ResultWriter, StreamableArray

writer.save_scores(
    result_type="NODE_ANOMALY_SCORES",
    scores=StreamableArray(score_generator()),  # Writes row-by-row
    ground_truth=test_labels,                   # still required for evaluation
)
```

#### 5. Result Saving (CRITICAL)

```python
# CRITICAL RULES:
# 1. Use TEST data for evaluation (contains anomalies)
# 2. Training data typically has all zeros (no anomalies)
# 3. Always include ground_truth
# 4. Choose correct result_type

writer.save_scores(
    result_type=result_type,
    scores=scores if isinstance(scores, list) else scores.tolist(),
    ground_truth=labels if isinstance(labels, list) else labels.tolist(),
)
```

### Complete Template

This is `methods/example/train_graflag.py` in outline. Copy the directory
rather than this block -- the real file is annotated and stays current.

```python
"""
GraFlag integration for {MethodName}.
{Description}
Source: {GitHub URL} at {SOURCE_REF}
"""

from dataclasses import asdict, dataclass

import numpy as np

from graflag_runner import (
    ResultWriter, device, info, load_dataset, params, paths, seed_all, upstream,
)

# Put the clone on sys.path before importing anything from it. upstream()
# anchors on __file__ and raises if the clone is missing, rather than letting
# the first import fail somewhere less obvious.
upstream("src")
# from your_module import YourModel, YourDataLoader


@dataclass
class Config:
    """The parameter contract: one field per `_FOO` in .env.

    The annotation is what the value is coerced to, and the default is what
    applies when .env does not set the key -- so the run never depends on a
    key being present, and a stale .env key cannot reach the model as a
    surprise keyword argument.
    """

    # Model
    hidden_dim: int = 64
    num_layers: int = 2

    # Training
    batch_size: int = 128
    epochs: int = 100
    learning_rate: float = 0.001
    use_feature: bool = False       # _USE_FEATURE=true / false, spelled out

    # Run
    seed: int = 42
    gpu: int = 0                    # read by device(), not by the method


def main():
    config = Config(**params(Config))
    run = paths()                   # run.data, run.exp, run.dataset
    seed_all(config.seed)
    dev = device()                  # cpu when _GPU=-1

    info(f"[INFO] {run.dataset} from {run.data} on {dev}")
    for key, value in asdict(config).items():
        info(f"[INFO]   {key}: {value}")

    edges, labels = load_dataset()  # or the method's own loader, from the clone
    writer = ResultWriter()         # reads EXP itself

    # --- train -------------------------------------------------------------
    # model = YourModel(**asdict(config)).to(dev)
    # for epoch in range(1, config.epochs + 1):
    #     loss = train_epoch(model, edges)
    #     writer.spot("training", epoch=epoch, loss=loss)

    # --- score the TEST split ----------------------------------------------
    # The training split usually has no anomalies at all, and an evaluation
    # over one class produces a null AUC rather than an error.
    scores = np.zeros(len(edges))   # replace
    ground_truth = labels           # replace

    writer.save_scores(
        result_type="EDGE_STREAM_ANOMALY_SCORES",   # adjust per method
        scores=scores.tolist(),
        ground_truth=list(ground_truth),
    )
    writer.add_metadata(dataset=run.dataset, **asdict(config))
    writer.finalize()               # atomic; writes results.json


if __name__ == "__main__":
    main()
```

What is deliberately absent, and should stay absent: `argparse`, `str2bool`,
`os.environ` lookups, a `time.time()` around `main()`, a `psutil` sampler, a
`try/except` that logs and re-raises (the runner already records the traceback
and the failed status), and `logging.basicConfig` (`info`/`warning`/`error`
from `graflag_runner` write in the house format).

---

## File 4: Dataset Directory (only for a new dataset)

Data files are never committed. A dataset directory holds a `metadata.json`
saying where its files come from, and `graflag-data` downloads them onto the
share; `graflag run` does it on demand before deploying.

### Structure

```
graflag-shared/datasets/{dataset_name}/
└── metadata.json      # the descriptor; the files are fetched next to it
```

### `metadata.json`

```json
{
  "name": "bond_inj_cora",
  "description": "Cora citation graph with injected outliers (PyGOD BOND benchmark).",
  "source": "https://github.com/pygod-team/pygod",
  "source_repo": "https://github.com/pygod-team/data",
  "compatible_methods": ["bond_*"],
  "format": "PyTorch-Geometric Data object (.pt)",
  "files": [
    {
      "name": "bond_inj_cora.pt",
      "url": "https://github.com/pygod-team/data/raw/main/inj_cora.pt.zip",
      "extract": "zip",
      "members": ["inj_cora.pt"]
    }
  ]
}
```

Each entry of `files` names the file to produce and the URL to download, and,
for an archive, how to extract it (`extract`) and which member to keep
(`members`). A `sha256` lets `graflag-data verify` check the file. A dataset
built from another one sets `derived: true`, `derived_from` and a `build`
command instead of URLs. `libs/graflag_data/README.md` has the full schema.

### Rules

1. **Naming**: lowercase and descriptive (`uci`, `btc_alpha`, `bond_inj_cora`); `-d <name>` refers to `datasets/<name>/`
2. **Never commit data files**: only `metadata.json` is versioned
3. **Fetch and check before the first run**: `graflag-data fetch {dataset_name}`, then `graflag-data status`

### Examples

Existing dataset names in the platform:

- `bond_inj_cora`, `bond_inj_amazon`, `bond_inj_flickr` (injection-based anomaly)
- `bond_books`, `bond_disney`, `bond_enron`, `bond_reddit`, `bond_weibo` (real-world)
- `bond_gen_100`, `bond_gen_500`, `bond_gen_1000`, `bond_gen_5000`, `bond_gen_10000` (synthetic)
- `btc_alpha`, `btc_otc` (cryptocurrency networks)
- `uci` (social network)

---

## Result Types Reference

| Method Output | Result Type | scores Format |
|--------------|-------------|---------------|
| Static node scores | `NODE_ANOMALY_SCORES` | `[float, ...]` |
| Static edge scores | `EDGE_ANOMALY_SCORES` | `[float, ...]` |
| Static graph scores | `GRAPH_ANOMALY_SCORES` | `[float, ...]` |
| Dynamic node (snapshots) | `TEMPORAL_NODE_ANOMALY_SCORES` | `[[t1], [t2], ...]` |
| Dynamic edge (snapshots) | `TEMPORAL_EDGE_ANOMALY_SCORES` | `[[t1], [t2], ...]` |
| Dynamic graph (snapshots) | `TEMPORAL_GRAPH_ANOMALY_SCORES` | `[[t1], [t2], ...]` |
| Streaming nodes | `NODE_STREAM_ANOMALY_SCORES` | `[float, ...]` with timestamps |
| Streaming edges | `EDGE_STREAM_ANOMALY_SCORES` | `[float, ...]` with timestamps |
| Streaming graphs | `GRAPH_STREAM_ANOMALY_SCORES` | `[float, ...]` with timestamps |

Special score values:
- `-1`: Unknown/unassigned
- `-2`: Inactive/unseen at this time step

---

## Common Errors and Fixes

### Error 1: `RuntimeError: Invalid device string: 'cuda:-1'`

**Cause**: the script builds `f"cuda:{config.gpu}"` itself. `graflag run
--no-gpu` sets `_GPU=-1`, and `-1` means CPU.

**Fix**: take the device from the SDK, which is the one place that knows:
```python
from graflag_runner import device
dev = device()                 # torch.device("cpu") when _GPU=-1
```

### Error 2: a `--params` sweep produces identical runs

**Cause**: the parameter reaches nothing. `params()` drops a `_FOO` your
`Config` has no field for, and `.env` keys outlive the code that read them --
`gady` accepted `_LR` for a model that trains two optimizers on `_LR_D` and
`_LR_G`, and upstream never read it either.

**Fix**: if the parameter is genuinely inert, keep the key and say so out
loud, rather than deleting it and leaving the sweep looking successful:
```python
INERT_PARAMS = {"lr": "GADY trains two optimizers, on _LR_D and _LR_G"}

for name, why in INERT_PARAMS.items():
    if getattr(config, name) != getattr(Config, name):
        warning(f"[WARN] _{name.upper()} is not used: {why}")
```
List it in the method's README under "Parameters that do nothing".

### Error 3: a boolean is the opposite of what `.env` says

**Cause**: the old empty spelling, `_USE_MEMORY=`. Under `--pass-env-args`
that became a bare `--use_memory` flag, which argparse read as **True**.
`params()` reads the empty string as **False**.

**Fix**: spell every boolean out -- `_USE_MEMORY=true`. Including the
commented-out ones, which are what the next person copies.

### Error 4: "File not found" for a dataset on the cluster

**Cause**: the dataset's files were never fetched onto the share. Until they
are, a dataset directory holds only its `metadata.json`.

**Fix**: on the manager, `graflag-data status` shows what is missing and
`graflag-data fetch <name>` downloads it. `graflag run` fetches automatically,
so this appears when a method is run by hand or a download failed.

### Error 5: "AUC is null"

**Cause**: Using training data (all labels = 0, no anomalies)

**Fix**: Use TEST data that contains anomalies:
```python
# WRONG: Using training snapshots
for snap in data['snap_train']:
    scores.append(predict(snap))

# RIGHT: Using test snapshots with injected anomalies
for snap in data['snap_test']:
    scores.append(predict(snap))
```

### Error 6: "ValueError: setting array element with sequence"

**Cause**: Ragged arrays (different lengths per timestamp)

**Status**: Handled by graflag_evaluator (flattens ragged arrays automatically)

---

## Testing Commands

```bash
# 1. Sync method to cluster
graflag sync --path methods/{method_name}

# 2. Build and run
graflag run -m {method_name} -d {dataset_name} --build

# 3. Check logs (follow in real-time)
graflag logs -e exp__{method_name}__{dataset_name}__TIMESTAMP -f

# 4. Stop if needed
graflag stop -e exp__{method_name}__{dataset_name}__TIMESTAMP

# 5. Evaluate results
graflag evaluate -e exp__{method_name}__{dataset_name}__TIMESTAMP

# 6. Run with custom parameters
graflag run -m {method_name} -d {dataset_name} --params EPOCHS=50 BATCH_SIZE=64
```

---

## Agent Prompt Template

Use this prompt to instruct an AI agent to integrate methods:

---

````markdown
# Task: Integrate Graph Anomaly Detection Methods into GraFlag

## Methods to Integrate

| Method | Paper | GitHub | Description |
|--------|-------|--------|-------------|
| {method1} | {paper1} | {github1} | {desc1} |
| {method2} | {paper2} | {github2} | {desc2} |

## Instructions

For EACH method in the list above, perform the following steps:

### Step 1: Analyze Original Repository
1. Examine the GitHub repository structure
2. Identify:
   - Main training script and entry point
   - Data loading code and expected data format
   - Model architecture files
   - All configurable hyperparameters (check argparse)
   - Required Python dependencies
   - CUDA/PyTorch version requirements

### Step 2: Choose Integration Pattern
- **An integration script** (`COMMAND=python3 train_graflag.py`): the default.
  Write `train_graflag.py`, declare a `Config` dataclass, and build it with
  `Config(**params(Config))`. Use this unless the next bullet applies.
- **A library entry point** (`COMMAND=python3 -m graflag_bond.train`): only when
  a library already covers the method family, as `graflag_bond` does for the
  PyGOD detectors. Then the method is a `.env` and nothing else.

### Step 3: Create Method Directory
Create `graflag-shared/methods/{method_name}/` with these files:

#### 3.1: `.env` File
- Set METHOD_NAME (lowercase, alphanumeric, underscores)
- Set DESCRIPTION from paper abstract
- Set SOURCE_CODE to GitHub URL
- Set SOURCE_REF to the 40-char commit the Dockerfile will check out (`git ls-remote <url> HEAD`)
- Set INTEGRATION to `upstream` or `reimplementation`
- Set SUPPORTED_DATASETS to compatible dataset names (comma-separated, wildcards ok)
- Set COMMAND (e.g., `python3 train_graflag.py` or `python3 -m graflag_bond.train`)
- Add ALL hyperparameters with `_` prefix
- For booleans, spell the value out: `_USE_MEMORY=true`, never a bare `_USE_MEMORY=`
- Remember: `params()` strips the underscore and lowercases, so `_LR_G` is the field `lr_g`

#### 3.2: `Dockerfile`
- Base: `nvidia/cuda` (version matching method requirements)
- Install all Python dependencies
- Clone the pinned source: `ARG SOURCE_CODE` / `ARG SOURCE_REF`, then `test -n` both, `git clone`, `git -C src checkout --detach ${SOURCE_REF}`
- Copy train_graflag.py and helper files
- Install the GraFlag libraries with the `ARG GRAFLAG_LIBS` / `case` stanza, copied verbatim -- not a bare `pip install graflag-runner`
- Add `graflag_bond` to the same `case` stanza if the method is a PyGOD detector
- CMD is always `["python3", "-m", "graflag_runner"]`; what differs is `COMMAND` in `.env`

#### 3.3: `train_graflag.py`
- Start from `methods/example/train_graflag.py`; do not write one from scratch
- Declare a `Config` dataclass whose fields are the method's parameters, with types
  and defaults, and build it with `Config(**params(Config))`
- If the method has its own argparse you want the defaults from, import that
  namespace and call `apply_params(ns, ignore={"gpu"})` instead
- Take the dataset from `load_dataset()` and the directories from `paths()`,
  not from `os.environ` by hand
- Take the device from `device()`, and seed with `seed_all(config.seed)`
- Put the clone on the path with `upstream("src")`, then import the method's code
- Do NOT time the run or sample memory yourself -- see "Everything else the SDK provides"
- Call `writer.spot("training", ...)` during training loop
- Generate predictions on TEST data (contains anomalies)
- Call `writer.save_scores()` with correct result_type and ground_truth
- Call `writer.add_metadata()` with all hyperparameters
- Call `writer.finalize()`

### Step 4: Describe the Dataset (only if it is new)
Create `graflag-shared/datasets/{dataset_name}/metadata.json` naming the files
and their source URLs, then fetch them with `graflag-data fetch {dataset_name}`.
Never commit data files.

### Step 5: Verify Integration
Pass the four gates, in order, and report each for every method:
- [ ] Contract: `python3 -m unittest discover -s tests` in `graflag-shared/`
- [ ] Build and run: `graflag sync`, then `graflag run -m {method} -d {dataset} --build` completes
- [ ] Evaluation: `graflag evaluate -e {experiment}`
- [ ] Result integrity: `graflag verify -e {experiment}` reports no failure, and every `[WARN]` it prints is explained in the method's README
- [ ] Recorded: the README's `## Verification` section gives each gate's result (or why the method cannot run here); gate 1 refuses a method that records nothing

## Critical Requirements

1. **Parameters**: one `Config` dataclass built with `Config(**params(Config))`, or
   upstream's own namespace through `apply_params()` -- never an argparse parser
   of your own, no `str2bool`, no per-key `os.environ.get`
2. **Booleans**: `_FLAG=true` or `_FLAG=false`, never a bare `_FLAG=`
3. **No committed data**: a dataset is a `metadata.json` descriptor; its files are fetched
4. **TEST data**: Predictions must be on test data with anomalies, not training data
5. **ground_truth**: Always include ground_truth in save_scores()
6. **Reserved vars**: Never override DATA, EXP, METHOD_NAME, COMMAND, MONITOR_INTERVAL

## Reference Implementations

Study these existing integrations:
- `methods/example/` -- the annotated template; start here
- `methods/taddy/` -- an integration script over a pinned clone, `Config(**params(Config))`
- `methods/generaldyg/` -- adopts upstream's argparse namespace via `apply_params()` (as does `ada_gad`)
- `methods/gady/` -- the hard case: upstream imported, training loop reproduced, inert parameters declared
- `methods/bond_cola/` -- a `.env` and nothing else, on the shared `bond_base` image

## Workspace Paths
- Methods: `graflag-shared/methods/`
- Datasets: `graflag-shared/datasets/`
- Libraries: `graflag-shared/libs/` (graflag_runner, graflag_bond, graflag_evaluator, graflag_data)
````

---

## Example: Complete GADY Integration

`methods/gady/` is the method that exercises every rule on this page, so it is
worth reading in full. What follows is the shape; `methods/gady/README.md` is
where the reasoning lives.

### `.env`
```bash
METHOD_NAME=gady
DESCRIPTION=Unsupervised Anomaly Detection on Dynamic Graphs (WSDM 2024)
SOURCE_CODE=https://github.com/mufeng-74/GADY
SOURCE_REF=1e8e4503e238f0a6c093c3aba946085aa3959410
INTEGRATION=upstream
SUPPORTED_DATASETS=gady_*

COMMAND=python3 train_graflag.py

_BS=200
_N_EPOCH=50
_LR_G=0.000001
_LR_D=0.000001
_USE_MEMORY=true
_SEED=142
_GPU=0
# ... twenty more, among them the inert _LR (see below)
```

`_USE_MEMORY=true`, spelled out. This `.env` carried the bare `_USE_MEMORY=`
from the `--pass-env-args` era, which `params()` reads as **False** -- so the
method would have trained with memory disabled and said nothing.

### `train_graflag.py` (key parts)
```python
@dataclass
class Config:
    """Every parameter GADY accepts, with the default it takes when unset."""
    bs: int = 200
    n_epoch: int = 50
    use_memory: bool = False
    lr_g: float = 0.000001
    lr_d: float = 0.000001
    seed: int = 142
    gpu: int = 0
    # ... one field per _FOO in .env


config = Config(**params(Config))
```

This dataclass replaced three competing paths that each decided the same
value: an argparse parser fed by `--pass-env-args`, a 30-key `env_mappings`
dict, and a loop in `main()` copying the second over the first. The dict read
`ANOMALY_PER` where GraFlag injects `_ANOMALY_PER`, so all thirty lookups
returned `None` and the whole path was dead -- and two of its entries were
additionally misspelled, which nothing could reveal while it never ran.

### What `gady` still shows that the templates do not

- **Upstream's code runs, but upstream's `train.py` does not.** `train.py`
  reads `args.alpha` for a flag its parser never declares, so the module raises
  `AttributeError` before any training code. The model, generator, losses,
  samplers and data pipeline are imported from the clone; the training loop is
  reproduced. `README.md` says so, and says where the two differ.
- **Inert parameters are declared, not deleted.** `_LR` is accepted because
  removing it would make a `--params LR=...` sweep look like it worked. Setting
  it off its default prints a `[WARN]` naming it.
- **A missing input stops the run.** The positional features are written by
  upstream's `preproc_new.py`; the loader raises with that sentence in the
  message rather than skipping the file. An earlier version caught
  `FileNotFoundError` and `continue`d, which turned a missing artifact into an
  epoch that trained on nothing.

## Example: bond_cola Integration (a shared image)

### `.env`
```bash
METHOD_NAME=bond_cola
DESCRIPTION=Anomaly Detection on Attributed Networks via Contrastive Self-Supervised Learning
SOURCE_CODE=https://docs.pygod.org/en/latest/generated/pygod.detector.CoLA.html#pygod.detector.CoLA
INTEGRATION=upstream
IMAGE=bond_base
SUPPORTED_DATASETS=bond_*

COMMAND=python3 -m graflag_bond.train

_HID_DIM=64
# ... the rest of the detector's constructor arguments, _-prefixed
```

### `Dockerfile`

There isn't one. `IMAGE=bond_base` points the build at
`graflag-shared/images/bond_base/Dockerfile`, which all seventeen `bond_*`
methods share. They were seventeen byte-identical Dockerfiles, and seventeen
multi-gigabyte images, until the image was extracted -- and building the whole
method tree did not fit on the cluster's disk.

The methods stay distinct at run time because graflag sets `METHOD_NAME` per
service and `graflag_bond.train` selects the PyGOD detector from it. So a new
PyGOD detector is a `.env` file and nothing else.

No `train_graflag.py` either -- `graflag_bond.train` handles everything via
the `BondDetector` registry that discovers PyGOD detector classes
automatically.

---

## Summary

1. **Choose the shape**: an integration script (`COMMAND=python3 train_graflag.py`)
   unless a library already covers the family, as `graflag_bond` does for PyGOD
2. **`.env`**: lowercase method name matching the directory, `_` prefix for every
   parameter, booleans spelled out, `SOURCE_REF` + `INTEGRATION` for provenance,
   `SUPPORTED_DATASETS` for compatibility, `IMAGE=` only to share an existing image
3. **`Dockerfile`**: a base that matches the method (CUDA for GPU code), pinned clone guarded by `test -n`, the
   `ARG GRAFLAG_LIBS` stanza copied verbatim, `CMD ["python3", "-m", "graflag_runner"]`
   -- or no Dockerfile at all if `IMAGE=` names a shared one
4. **`train_graflag.py`**: copy `methods/example/`, declare a `Config` dataclass,
   build it with `Config(**params(Config))` (or `apply_params()` on upstream's
   namespace), take the device from `device()`, the
   dataset from `load_dataset()`, the directories from `paths()`; score the TEST
   split and write it with `ResultWriter`
5. **Dataset**: a `metadata.json` descriptor, fetched with `graflag-data`; never committed data files

**Key rule**: when the original method works and the integration does not, look
first at what the method was *given* -- a parameter that reached nothing, a
boolean that arrived inverted, a device string built from `_GPU=-1`, or a file
that was missing and skipped. Every one of those failed silently at least once
here, which is why the SDK does each of them in exactly one place.
