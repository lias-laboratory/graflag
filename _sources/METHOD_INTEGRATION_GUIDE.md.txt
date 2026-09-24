# GraFlag Method Integration Guide

Complete guide for integrating new graph anomaly detection methods into GraFlag.

---

## Overview

Every method runs the same way. The container's entry point is always
`CMD ["python3", "-m", "graflag_runner"]`: the runner sets `DATA` and `EXP`,
starts the resource monitor, runs whatever `COMMAND` in `.env` names, verifies
that `results.json` parses, and records the status. The two integration
patterns differ only in what `COMMAND` points at:

- **An integration script** -- `COMMAND=python3 train_graflag.py`. The script
  declares a `Config` dataclass and builds it with `Config(**params(Config))`,
  which reads the `_`-prefixed environment variables. This is the default: all
  sixteen methods outside the PyGOD family have a script, and fourteen of them
  build their `Config` this way. `generaldyg` and `ada_gad` adopt upstream's
  argparse namespace with `apply_params()` instead (see Advanced Features).
- **A library entry point** -- `COMMAND=python3 -m graflag_bond.train`. The
  library reads the same variables and selects its detector from `METHOD_NAME`.
  Use it only when a library already covers the method family, as
  `graflag_bond` does for the PyGOD detectors; all seventeen `bond_*` methods
  are a `.env` and nothing else.

There is a third option, `--pass-env-args`, which appends the parameters to
`COMMAND` as CLI flags (`_BATCH_SIZE=128` becomes `--batch_size 128`). It is
still supported and **no method uses it**: it lowercases names, coerces
nothing, supplies no defaults, and lets argparse's abbreviation matching
quietly bind `--gpu` to an upstream `--gpus`. Reach for it only when `COMMAND`
is upstream's own entry point and you are not writing a script.

Integrating a new method means creating a directory with 2-3 files:

1. **`.env`** - Method configuration and parameters (required)
2. **`Dockerfile`** - Container environment setup (required unless `IMAGE=` names a shared one)
3. **`train_graflag.py`** - the integration script, when `COMMAND` names one

---

## Directory Structure

```
methods/
+-- your_method_name/            # Lowercase, alphanumeric + underscore
    +-- .env                     # Method configuration (REQUIRED)
    +-- Dockerfile               # Container setup (REQUIRED unless .env sets IMAGE=)
    +-- train_graflag.py         # Integration script, when COMMAND names one
    +-- patches/*.patch          # Fixes to the clone, applied with `git apply`
    +-- *.py                     # Additional helper files (optional)
```

The upstream clone is **not** a directory here. It is created inside the image
at build time (`git clone` into `/app/src` from `SOURCE_CODE` at `SOURCE_REF`),
so it never enters the build context and is never committed.

**Example: a method with an integration script (generaldyg)**
```
methods/generaldyg/
+-- .env                         # Configuration with _* parameters
+-- Dockerfile                   # CUDA 12.1 + PyTorch 2.1.2, pinned clone
+-- train_graflag.py             # Integration script, on upstream's own argparse namespace
+-- README.md                    # What upstream does, what the integration changes
```
Seven methods (`anograph`, `midas`, `streamspot` among them) also carry a
`patches/` directory, applied to the clone with `git apply`.

**Example: a method on the shared bond image (bond_dominant)**
```
methods/bond_dominant/
+-- .env                         # IMAGE=bond_base, plus the _* parameters
+-- README.md                    # What is specific to this detector
```
No Dockerfile: `IMAGE=bond_base` points the build at
`graflag-shared/images/bond_base/Dockerfile`, shared by all seventeen.

---

## Step 1: Create `.env` Configuration File

The `.env` file defines method metadata and configurable parameters.

### Template (integration script)
```bash
METHOD_NAME=your_method_name
DESCRIPTION=Brief description of the method
SOURCE_CODE=https://github.com/author/repo
SOURCE_REF=0000000000000000000000000000000000000000  # git ls-remote <url> HEAD
INTEGRATION=upstream
SUPPORTED_DATASETS=dataset1,dataset2

COMMAND=python3 train_graflag.py

# Method-specific parameters (prefix with underscore). They are:
# 1. set as environment variables in the container
# 2. read by params() into the script's Config -- underscore stripped, name
#    lowercased, value coerced to the field's annotation
# 3. listed in the GUI, and overridable with `--params BATCH_SIZE=256`
# Booleans are spelled out: _USE_FEATURE=true, never a bare _USE_FEATURE=

_BATCH_SIZE=128
_N_EPOCHS=200
_LEARNING_RATE=0.0001
_HIDDEN_DIM=256
_DROPOUT=0.4
_SEED=42
```

### Template (shared bond image)
```bash
METHOD_NAME=bond_dominant
DESCRIPTION=Deep Anomaly Detection on Attributed Networks
SOURCE_CODE=https://docs.pygod.org/en/latest/generated/pygod.detector.DOMINANT.html#pygod.detector.DOMINANT
INTEGRATION=upstream
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

### Key Fields

| Field | Required | Description | Example |
|-------|----------|-------------|---------|
| `METHOD_NAME` | Yes | Unique method identifier (lowercase) | `generaldyg` |
| `DESCRIPTION` | Yes | Short description | `A Generalizable Anomaly Detection Method` |
| `SOURCE_CODE` | Yes | GitHub repo or paper link | `https://github.com/...` |
| `SOURCE_REF` | If the image clones | The 40-char commit checked out after the clone | `c84dcca...` |
| `INTEGRATION` | Yes | `upstream` if the image runs the cited code, `reimplementation` if you wrote it | `upstream` |
| `COMMAND` | Yes | Entry point command | `python3 train_graflag.py` |
| `IMAGE` | No | A shared image under `images/<name>/`, used instead of a local Dockerfile | `bond_base` |
| `SUPPORTED_DATASETS` | No | Compatible datasets (comma-separated, wildcards ok); the runner refuses any other | `bond_*` |
| `_PARAMETER` | No | User-configurable parameters (prefix with `_`) | `_BATCH_SIZE=128` |

Three of these are about being able to believe a result months later:

- **`SOURCE_REF`** freezes the clone. Without it every rebuild takes whatever
  the default branch holds that day, so two experiments with the same method
  name may not have run the same code -- and when upstream changes a
  signature, the run dies with a `TypeError` that reads like a GraFlag bug.
- **`INTEGRATION`** separates "we ran the authors' code" from "we wrote this
  from the paper". `SOURCE_CODE` names the paper's repository either way, and
  two methods here (`dynwalk`, `addgraph`) used to name one they never ran.
- **`IMAGE`** lets methods share a build. Declare it only when your image
  would be byte-identical to an existing one; `graflag-shared/tests/test_methods.py`
  fails on a duplicated Dockerfile and names the file you duplicated.

### Parameter Naming Convention
- **Prefix with `_`**: all configurable parameters start with an underscore
- **Uppercase**: `_LEARNING_RATE`, `_BATCH_SIZE`. `params()` strips the
  underscore and lowercases the rest, so `_LR_G` becomes the field `lr_g` --
  a repository whose own flag is `--lr_G` needs the field named `lr_g` here
- **Booleans spelled out**: `_USE_MEMORY=true` or `_USE_MEMORY=false`.
  A bare `_USE_MEMORY=` is read as **False**; under the old `--pass-env-args`
  path the same spelling became a bare flag that argparse read as **True**, so
  a `.env` carrying it silently inverts
- **Reserved names**: set by the orchestrator and silently dropped if you
  define them: `DATA`, `EXP`, `METHOD_NAME`, `COMMAND`, `MONITOR_INTERVAL`
- **A parameter that reaches nothing stays in the `.env`**: declare it inert
  and warn when it is set off its default, rather than deleting the key. A
  removed key makes a `--params` sweep look like it worked while every run
  comes back identical

---

## Step 2: Create Dockerfile

The Dockerfile defines the containerized execution environment.

### Template (integration script)
```dockerfile
FROM nvidia/cuda:12.1.0-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive

# Install Python and system dependencies
RUN apt-get update && apt-get install -y \
    python3 python3-pip git \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --upgrade pip

# Install Python dependencies specific to your method
RUN pip install --no-cache-dir \
    numpy scipy scikit-learn networkx pandas tqdm

# Install PyTorch with CUDA support (adjust version as needed)
RUN pip install --no-cache-dir \
    torch torchvision torchaudio \
    --index-url https://download.pytorch.org/whl/cu121

# Install PyTorch Geometric (if needed)
# RUN pip install --no-cache-dir torch-geometric
# RUN pip install --no-cache-dir \
#     torch-scatter torch-sparse \
#     -f https://data.pyg.org/whl/torch-2.1.0+cu121.html

WORKDIR /app

# Clone the pinned upstream. SOURCE_CODE and SOURCE_REF are declared in the
# .env and forwarded by graflag as --build-arg, so the repository and the
# commit are named in one place.
#
# `test -n` is not ceremony: with SOURCE_REF empty -- a build run by hand, a
# typo in the key -- `git checkout --detach` still exits 0 and leaves the
# clone on whatever the default branch holds that day.
ARG SOURCE_CODE
ARG SOURCE_REF
RUN test -n "${SOURCE_CODE}" && test -n "${SOURCE_REF}" \
    && git clone ${SOURCE_CODE} src \
    && git -C src checkout --detach ${SOURCE_REF}

# Copy GraFlag integration files
COPY methods/your_method/train_graflag.py ./train_graflag.py

# Where the GraFlag libraries come from: `local` (the default) installs the
# copy on the share, `pypi` the published wheel. graflag passes the choice as
# --build-arg from GRAFLAG_LIBS in its config; copy this stanza verbatim.
#
# The unknown arm exits non-zero on purpose. An if/else would read a typo as
# "pypi" and build an image whose libraries are not the ones that were asked
# for -- and report success.
ARG GRAFLAG_LIBS=local
COPY libs/graflag_runner /tmp/graflag_libs/graflag_runner
RUN case "${GRAFLAG_LIBS}" in \
      local) pip install --no-cache-dir /tmp/graflag_libs/graflag_runner ;; \
      pypi)  pip install --no-cache-dir graflag-runner ;; \
      *) echo "[ERROR] GRAFLAG_LIBS must be local or pypi, got '${GRAFLAG_LIBS}'" >&2; exit 1 ;; \
    esac && rm -rf /tmp/graflag_libs

# The entry point is always the runner. It sets DATA/EXP, starts the resource
# monitor, runs COMMAND from .env, verifies results.json, records the status.
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

**In practice you will not write this file.** All seventeen `bond_*` methods
declare `IMAGE=bond_base` and share it; adding a PyGOD detector is a `.env` and
nothing else. They were seventeen byte-identical Dockerfiles, and seventeen
multi-gigabyte images, until the image was extracted -- which is why the whole
method tree did not fit on the cluster's disk.

### Key Components

1. **Base Image**: Choose the base the method's own requirements call for. Most
   use an `nvidia/cuda` runtime image (from `11.1.1-runtime-ubuntu20.04` for
   older methods to `13.0.2-runtime-ubuntu24.04`); CPU-only methods use
   `ubuntu` or `python` images; `rare` uses `python:3.12.4-slim` with
   pip-installed CUDA wheels, to reproduce the environment it was developed in.
   A base without CUDA must set `NVIDIA_VISIBLE_DEVICES=all` and
   `NVIDIA_DRIVER_CAPABILITIES=compute,utility` itself, or the nvidia runtime
   hands the container no GPU.

2. **Dependencies**: Install all required Python packages

3. **Source Code**: Either clone from GitHub -- pinned to `SOURCE_REF`, always --
   or copy local files

4. **GraFlag Libraries**: Copy the `ARG GRAFLAG_LIBS` stanza verbatim; do not
   write your own install line. Which libraries a build uses is the cluster's
   choice (`GRAFLAG_LIBS` in `~/.config/graflag/config.env`), not the method's,
   and a method that hardcodes its own install silently opts out of a change
   the operator asked for.

   The same fact explains a trap: `graflag sync --lib` updates `/shared/libs`
   but nothing inside an already-built image. Re-run the method with `--build`
   afterwards, which is what picks the new copy up.

5. **Entry Point**: always `CMD ["python3", "-m", "graflag_runner"]`. The
   runner sets `DATA`/`EXP`, starts the monitor, runs `COMMAND` from `.env`,
   verifies that `results.json` parses, and records the status -- exit code 0
   alone is not taken as proof of success. What varies between methods is
   `COMMAND`, not `CMD`.

**Note**: The build context is the entire `graflag-shared/` directory, so `COPY libs/` and `COPY methods/` paths work relative to it.

---

## Step 3: Create Training Wrapper (`train_graflag.py`)

Needed when `COMMAND` names a script -- which is every method except the
seventeen that run `graflag_bond.train`.

**Start from `methods/example/train_graflag.py`.** It is the annotated
template, it is 172 lines, and it uses every helper below.

### Core Requirements

1. **Take the parameters with one call**
   ```python
   from dataclasses import dataclass
   from graflag_runner import params

   @dataclass
   class Config:
       learning_rate: float = 0.001
       epochs: int = 100
       use_feature: bool = False
       seed: int = 42
       gpu: int = 0

   config = Config(**params(Config))
   ```
   `params()` strips the underscore, lowercases the name, coerces against the
   annotation, and drops any `_FOO` the dataclass has no field for. The
   dataclass default applies when `.env` does not set the key, so a run never
   depends on a key being present.

   If the method brings its own argparse and you want upstream's defaults,
   take its namespace instead of re-declaring them:
   ```python
   from option import args                  # upstream's parser, with its defaults
   from graflag_runner import apply_params

   applied = apply_params(args, ignore={"gpu"})   # _GPU is read by device()
   ```

2. **Take the directories, device, seed and dataset from the SDK**
   ```python
   from graflag_runner import device, load_dataset, paths, seed_all, upstream

   run = paths()                   # run.data, run.exp, run.dataset
   seed_all(config.seed)           # random, numpy and torch together
   dev = device()                  # cpu when _GPU=-1, which is what --no-gpu sets
   upstream("src")                 # the clone on sys.path; raises if absent
   edges, labels = load_dataset()  # (edges, labels) from any dataset layout
   ```
   Not `os.environ.get("DATA")` and not `f"cuda:{config.gpu}"`. Three methods
   built the device string themselves and died on
   `Invalid device string: 'cuda:-1'`.

3. **Initialize ResultWriter**
   ```python
   from graflag_runner import ResultWriter
   writer = ResultWriter()         # reads EXP itself
   ```

4. **Add metadata**
   ```python
   from dataclasses import asdict
   writer.add_metadata(dataset=run.dataset, **asdict(config), summary={
       # required by the contract tests, read by `graflag verify`
       "dataset_info": {"scored_split": "test", "scored_samples": len(scores)},
       "results": {"test_auc": test_auc},  # the method's own AUC, same scores
   })
   ```

5. **Save results using the standardized format**
   ```python
   writer.save_scores(
       result_type="NODE_ANOMALY_SCORES",  # or TEMPORAL_*, EDGE_*, *_STREAM_*
       scores=anomaly_scores,              # list or numpy array
       ground_truth=labels,                # always include it
   )
   writer.finalize()                       # atomic; writes results.json
   ```
   Score the **test** split. The training split usually has no anomalies at
   all, and an evaluation over one class produces a null AUC rather than an
   error.

6. **Track training progress** (optional)
   ```python
   writer.spot("training", epoch=i, loss=loss)
   ```
   The schema locks after the first call per key, so pass the same fields
   every epoch. `graflag evaluate` turns each spot CSV into a `_curves.png`.

7. **Do not measure yourself.** No `time.time()` around `main()`, no `psutil`
   sampling, no `torch.cuda.max_memory_allocated()`. `graflag_runner` measures
   all three from outside, over the whole process tree, and **its numbers are
   the ones reported**; whatever the method records is preserved beside them as
   `method_reported_<key>`. One method's own sampler reported 409 MB against
   the monitor's 907 MB.

8. **Never skip a missing input.** A `try/except FileNotFoundError: continue`
   around a file the method needs turns a broken preprocessing step into an
   epoch that trains on nothing and a run that reports `completed`. Let it
   raise, with a message saying what writes the file.

### Result Types

Choose the appropriate result type for your method:

| Result Type | Description | Data Format |
|------------|-------------|-------------|
| `NODE_ANOMALY_SCORES` | Static graph, node-level | 1D array: `[0.1, 0.2, ...]` |
| `EDGE_ANOMALY_SCORES` | Static graph, edge-level | 1D array per edge |
| `GRAPH_ANOMALY_SCORES` | Graph classification | 1D array per graph |
| `TEMPORAL_NODE_ANOMALY_SCORES` | Dynamic graphs, node snapshots | 2D array: `[[t0_scores], [t1_scores]]` |
| `TEMPORAL_EDGE_ANOMALY_SCORES` | Dynamic graphs, edge snapshots | 2D array per timestamp |
| `TEMPORAL_GRAPH_ANOMALY_SCORES` | Temporal graph classification | 2D array |
| `NODE_STREAM_ANOMALY_SCORES` | Streaming nodes | 1D array with timestamps |
| `EDGE_STREAM_ANOMALY_SCORES` | Streaming edges | 1D array with timestamps |
| `GRAPH_STREAM_ANOMALY_SCORES` | Streaming graphs | 1D array with timestamps |

### Template

`methods/example/train_graflag.py` in outline. Copy that directory rather than
this block -- the real file is annotated and stays current.

```python
"""
GraFlag integration for YourMethod.
Source: https://github.com/author/repo at <SOURCE_REF>
"""
from dataclasses import asdict, dataclass

import numpy as np
from sklearn.metrics import roc_auc_score

from graflag_runner import (
    ResultWriter, device, info, load_dataset, params, paths, seed_all, upstream,
)

# The clone goes on sys.path before anything is imported from it. upstream()
# anchors on __file__ and raises when the clone is missing, instead of letting
# the first import fail somewhere less obvious.
upstream("src")
# from your_module import YourModel


@dataclass
class Config:
    """One field per `_FOO` in .env. The annotation types the value; the
    default is what applies when .env does not set the key."""

    hidden_dim: int = 256
    batch_size: int = 128
    epochs: int = 200
    learning_rate: float = 0.0001
    dropout: float = 0.4
    use_feature: bool = False       # _USE_FEATURE=true / false, spelled out
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
    scores = np.zeros(len(edges))   # replace with model.predict(...)
    ground_truth = labels           # replace with the test labels
    test_auc = float(roc_auc_score(ground_truth, scores))   # the method's own number

    writer.save_scores(
        result_type="EDGE_STREAM_ANOMALY_SCORES",   # adjust per method
        scores=scores.tolist(),
        ground_truth=list(ground_truth),
    )
    # Which split the scores cover and how many there are (gate 1 checks), and
    # the method's own AUC over exactly these scores (gate 4 compares).
    writer.add_metadata(dataset=run.dataset, **asdict(config), summary={
        "dataset_info": {"scored_split": "test", "scored_samples": len(scores)},
        "results": {"test_auc": test_auc},      # replace
    })
    writer.finalize()               # atomic; writes results.json


if __name__ == "__main__":
    main()
```

Four things are absent on purpose:

- **No `time.time()` and no `psutil`.** The runner measures execution time,
  peak memory and peak GPU from outside, and its numbers are the ones reported.
- **No `os.environ.get("_EPOCHS", "100")`.** That returns the string `"100"`,
  which survives into `range()` as a `TypeError` much later. `params()` coerces.
- **No `sys.path.insert`.** `upstream()` anchors on `__file__`, so it does not
  depend on the working directory.
- **No `try/except` around `main()`.** The runner already captures the
  traceback into `method_output.txt`, writes `failed` to `status.json`, and
  exits non-zero. Catching it here only risks swallowing it.

---

## Step 4: Check the Contract, Then Test Locally (Optional)

First run the contract tests, which check the `.env`, the Dockerfile and the
script without building anything:

```bash
cd /path/to/graflag-shared
python3 -m unittest discover -s tests
```

The quickest end-to-end test is `graflag run` against the local
[devcluster](devcluster.rst) (Step 5). A build by hand is possible, but it has to
supply what `graflag run` passes: the build context is the whole
`graflag-shared/` directory, and a Dockerfile that clones needs the pin from the
`.env` as build arguments, or its `test -n` guard stops the build.

### 1. Build Docker Image
```bash
cd /path/to/graflag-shared
docker build -f methods/your_method/Dockerfile \
  --build-arg SOURCE_CODE="$(grep '^SOURCE_CODE=' methods/your_method/.env | cut -d= -f2-)" \
  --build-arg SOURCE_REF="$(grep '^SOURCE_REF=' methods/your_method/.env | cut -d= -f2-)" \
  -t your_method:latest .
```

### 2. Test Run Locally
```bash
docker run --rm --gpus all \
  -v $(pwd):/shared \
  -e DATA=/shared/datasets/your_dataset \
  -e EXP=/shared/experiments/test_exp \
  -e METHOD_NAME=your_method \
  -e COMMAND="python3 train_graflag.py" \
  -e _BATCH_SIZE=64 \
  -e _EPOCHS=10 \
  your_method:latest
```
The dataset's files must be present first (`graflag-data fetch your_dataset`),
and `EXP` must exist and be writable.

### 3. Verify Output
Check that `results.json` is created correctly:
```bash
cat experiments/test_exp/results.json
```

Expected structure:
```json
{
  "result_type": "NODE_ANOMALY_SCORES",
  "scores": [0.1, 0.2, 0.3],
  "ground_truth": [0, 0, 1],
  "metadata": {
    "method_name": "your_method"
  }
}
```

---

## Step 5: Deploy to GraFlag Cluster

Once tested, deploy to the GraFlag cluster:

### 1. Sync Method to Shared Directory
```bash
graflag sync --path methods/your_method
```
`sync` adds and overwrites files but never deletes them: a file removed from
your checkout stays on the share, inside the build context, until you delete it
there too.

### 2. Build and Run
```bash
graflag run -m your_method -d your_dataset --build
```

### 3. Run with Custom Parameters
```bash
graflag run -m your_method -d your_dataset --params EPOCHS=50 BATCH_SIZE=64
```

### 4. Monitor, Evaluate and Verify
```bash
# Follow logs in real-time
graflag logs -e exp__your_method__your_dataset__TIMESTAMP -f

# Evaluate results
graflag evaluate -e exp__your_method__your_dataset__TIMESTAMP

# Check the published scores reproduce the method's own number
graflag verify -e exp__your_method__your_dataset__TIMESTAMP
```
`graflag verify` checks what `completed` does not: that scores and labels are
the same length and both classes are present, that the scores vary, that the
scored split is declared as the test split, and that the evaluator's AUC
matches every AUC the method recorded under `metadata.summary`. It exits 1 when
a check fails; read each `[WARN]` it prints. Then record all four gates in the
README's `## Verification` section -- the contract tests refuse a method that
records nothing.

---

## Advanced Features

### 1. Streaming Large Results
For methods producing massive datasets, use streaming to avoid memory issues:

```python
from graflag_runner import ResultWriter, StreamableArray

def generate_scores():
    """Generator that yields scores incrementally."""
    for batch in large_dataset:
        scores = model.predict(batch)
        yield scores

writer = ResultWriter()
writer.save_scores(
    result_type="NODE_ANOMALY_SCORES",
    scores=StreamableArray(generate_scores()),  # Wrap generator
    ground_truth=test_labels,                   # still required for evaluation
)
writer.finalize()
```

### 2. Progress Tracking with `spot()`
Track arbitrary metrics during execution:

```python
# Training metrics (creates training.csv)
writer.spot("training", epoch=i, loss=loss, accuracy=acc, time_sec=t)

# Validation metrics (creates validation.csv)
writer.spot("validation", epoch=i, val_loss=val_loss, val_auc=auc)

# Custom metrics (creates preprocessing.csv)
writer.spot("preprocessing", num_nodes=n, num_edges=e)
```

Schema is locked after the first call -- subsequent calls must provide the same metric keys.

### 3. Adopting upstream's own configuration object

`params()` needs a signature to read names and types from. A method that does
`from option import args` gets an argparse namespace carrying upstream's
defaults, including keys its `.env` never mentions, and there is no signature
to consult. `apply_params()` writes onto the object instead:

```python
from graflag_runner import apply_params

applied = apply_params(args, ignore={"gpu"})   # _GPU is read by device()
```

Each `_FOO` is matched to an existing attribute `foo` and coerced to the type
of the value already there, so upstream's defaults define both what is
accepted and how it is read. An `_FOO` with no matching attribute gets a
`[WARN]` naming it, unless you listed it in `ignore`. The return value is
`{name: value}` for what was applied, ready to record in `results.json`.
`generaldyg` and `ada_gad` work this way.

There is also `--pass-env-args`, which appends the parameters to `COMMAND` as
CLI flags (`_BATCH_SIZE=128` becomes `--batch_size 128`, lowercased). It still
works and no method uses it. Besides coercing nothing and supplying no
defaults, it hands argparse's abbreviation matching a say: with upstream
declaring `--gpus` and no `--gpu`, `_GPU=0` arrives as `--gpu 0` and argparse
quietly sets `gpus=0`. Prefer `apply_params()`.

---

## Troubleshooting

### Common Issues

**1. Import Errors**
```
ModuleNotFoundError: No module named 'your_module'
```
**Solution**: call `upstream("src")` before importing anything from the clone.
It anchors on `__file__` rather than the working directory, and raises with the
path it looked in when the clone is missing.

**2. CUDA/GPU Issues**
```
RuntimeError: CUDA out of memory
```
**Solution**: Reduce batch size, use `_BATCH_SIZE` parameter, or disable GPU with `--no-gpu` flag

**3. Dataset Not Found**
```
FileNotFoundError: Dataset not found
```
**Solution**: Check the dataset name matches a directory in `datasets/`
(lowercase), and that its files have been fetched: `graflag-data status` lists
what is missing, and `graflag-data fetch <name>` downloads it.

**4. Results Not Saving**
```
results.json empty or missing
```
**Solution**: Ensure `writer.finalize()` is called, check permissions on experiment directory

**5. Container Crashes Silently**
**Solution**: Check logs with:
```bash
graflag logs -e exp__your_method__dataset__TIMESTAMP -f
```

**6. A boolean is the opposite of what `.env` says**

**Cause**: the old empty spelling, `_USE_MEMORY=`. Under `--pass-env-args` that
became a bare `--use_memory` flag, which argparse read as **True**; `params()`
reads the same empty string as **False**.

**Solution**: spell every boolean out -- `_USE_MEMORY=true` or
`_USE_MEMORY=false` -- including the commented-out ones, which are what the
next person copies.

**7. `RuntimeError: Invalid device string: 'cuda:-1'`**

**Cause**: the script builds `f"cuda:{config.gpu}"` itself, and
`graflag run --no-gpu` sets `_GPU=-1`, which means CPU.

**Solution**: `dev = device()`.

**8. A `--params` sweep produces identical runs**

**Cause**: the parameter reaches nothing -- `params()` drops a `_FOO` the
`Config` has no field for, and `.env` keys outlive the code that read them.

**Solution**: add the field, or declare the parameter inert and `[WARN]` when
it is set off its default. Do not delete the key: that makes the sweep look
like it worked.

---

## Best Practices

1. **Version Control**: Pin all dependency versions in Dockerfile
2. **Logging**: Use `info()`, `warning()`, `error()` from graflag_runner for consistent logging
3. **Resource Tracking**: Let graflag_runner handle monitoring, don't implement custom tracking
4. **Error Handling**: let exceptions propagate. The runner captures the
   traceback into `method_output.txt`, writes `failed` to `status.json` and
   exits non-zero; a `try/except` in the script can only lose that. Never
   swallow a missing input -- `except FileNotFoundError: continue` around a
   file the method needs turns a broken preprocessing step into an epoch that
   trains on nothing and a run that reports `completed`.
5. **Testing**: Always test locally before deploying to cluster
6. **Reproducibility**: Set random seeds, include in metadata
7. **Ground Truth**: Always include `ground_truth` in `save_scores()` for evaluation to work

---

## Quick Reference

### Environment Variables (Automatically Set)
| Variable | Description | Example |
|----------|-------------|---------|
| `DATA` | Input dataset directory | `/shared/datasets/uci` |
| `EXP` | Experiment output directory | `/shared/experiments/exp__...` |
| `METHOD_NAME` | Method identifier | `generaldyg` |
| `COMMAND` | Command from .env | `python3 train_graflag.py` |
| `GRAFLAG_PARAMS` | Which `_FOO` variables are parameters | `_EPOCHS,_GPU,_LR` |

### graflag_runner API
```python
from graflag_runner import ResultWriter, info, warning, error

# Logging
info("Message")
warning("Warning message")
error("Error message")

# Result writing
writer = ResultWriter()
writer.save_scores(result_type="...", scores=[...], ground_truth=[...])
writer.add_metadata(method_name="...", dataset="...", summary={
    "dataset_info": {"scored_split": "test", "scored_samples": ...},
    "results": {"test_auc": ...},
})
# Rarely needed. graflag_runner measures exec time, peak memory and peak GPU
# from outside the method, and its numbers win; anything passed here is kept
# beside them as method_reported_<key>.
writer.add_resource_metrics(exec_time_ms=1234.5, peak_memory_mb=512.3, peak_gpu_mb=2048.0)
writer.spot("training", epoch=1, loss=0.5, auc=0.85)
writer.finalize()
```

### CLI Commands
```bash
# Build and run
graflag run -m your_method -d your_dataset --build

# Run with custom parameters
graflag run -m your_method -d your_dataset --params EPOCHS=50 BATCH_SIZE=64

# Replay from saved config
graflag run --from-config service_config.json

# Check logs
graflag logs -e exp__your_method__dataset__timestamp -f

# Stop running experiment
graflag stop -e exp__your_method__dataset__timestamp

# Evaluate results
graflag evaluate -e exp__your_method__dataset__timestamp
```

---

## Complete Checklist

- [ ] Created `methods/your_method/` directory
- [ ] Created `.env` with METHOD_NAME, DESCRIPTION, SOURCE_CODE, COMMAND
- [ ] Added SOURCE_REF (40 hex chars) if the Dockerfile clones anything
- [ ] Added INTEGRATION (`upstream` or `reimplementation`)
- [ ] Added SUPPORTED_DATASETS if applicable
- [ ] Added configurable parameters with `_` prefix, booleans spelled out
- [ ] Created `Dockerfile` with correct base image and dependencies -- or set
      `IMAGE=` if an identical one already exists
- [ ] Copied the `ARG GRAFLAG_LIBS` stanza verbatim, rather than a bare `pip install`
- [ ] Created `train_graflag.py`, or set `COMMAND` to a library entry point
- [ ] Took parameters with `Config(**params(Config))`, the device with `device()`,
      the directories with `paths()`
- [ ] Ran `python3 -m unittest discover -s tests` in `graflag-shared/` -- it checks
      every one of the above
- [ ] Saved results with appropriate result_type and ground_truth, from the test split
- [ ] Recorded `scored_split`, `scored_samples` and the method's own AUC in `metadata.summary`
- [ ] Called `writer.finalize()`
- [ ] Wrote `README.md`: what upstream does, what the integration changes, which split is scored
- [ ] Synced, then ran with `graflag run -m ... -d ... --build`
- [ ] Evaluated results with `graflag evaluate`
- [ ] Ran `graflag verify -e EXP` on the evaluated experiment, and read every `[WARN]`
- [ ] Recorded the four gates in the README's `## Verification` section

---

## Example: Complete Integration (Minimal)

**File: `methods/mymethod/.env`**
```bash
METHOD_NAME=mymethod
DESCRIPTION=My Custom Graph Anomaly Detector
SOURCE_CODE=https://github.com/me/mymethod
SOURCE_REF=0000000000000000000000000000000000000000
INTEGRATION=upstream

COMMAND=python3 train_graflag.py

_LEARNING_RATE=0.001
_EPOCHS=100
_GPU=0
```

**File: `methods/mymethod/Dockerfile`**
```dockerfile
FROM nvidia/cuda:12.1.0-runtime-ubuntu22.04
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y python3 python3-pip git && \
    rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir numpy scipy scikit-learn torch

WORKDIR /app

ARG SOURCE_CODE
ARG SOURCE_REF
RUN test -n "${SOURCE_CODE}" && test -n "${SOURCE_REF}" \
    && git clone "${SOURCE_CODE}" src \
    && git -C src checkout --detach "${SOURCE_REF}"

COPY methods/mymethod/train_graflag.py ./train_graflag.py

ARG GRAFLAG_LIBS=local
COPY libs/graflag_runner /tmp/graflag_libs/graflag_runner
RUN case "${GRAFLAG_LIBS}" in \
      local) pip install --no-cache-dir /tmp/graflag_libs/graflag_runner ;; \
      pypi)  pip install --no-cache-dir graflag-runner ;; \
      *) echo "[ERROR] GRAFLAG_LIBS must be local or pypi, got '${GRAFLAG_LIBS}'" >&2; exit 1 ;; \
    esac && rm -rf /tmp/graflag_libs

CMD ["python3", "-m", "graflag_runner"]
```

`test -n` is not ceremony. With `SOURCE_REF` empty, `git checkout --detach`
still exits 0 and leaves the clone on whatever the default branch holds that
day -- the silent float the pin exists to remove.

**File: `methods/mymethod/train_graflag.py`**
```python
from dataclasses import asdict, dataclass

import numpy as np
from sklearn.metrics import roc_auc_score

from graflag_runner import ResultWriter, info, load_dataset, params, paths, seed_all


@dataclass
class Config:
    learning_rate: float = 0.001
    epochs: int = 100
    seed: int = 42
    gpu: int = 0


def main():
    config = Config(**params(Config))
    run = paths()
    seed_all(config.seed)

    info(f"[INFO] Scoring {run.dataset}")
    edges, labels = load_dataset()

    writer = ResultWriter()
    scores = np.random.rand(len(edges))          # replace with your detector
    test_auc = float(roc_auc_score(labels, scores))   # the method's own number

    writer.save_scores(
        result_type="EDGE_STREAM_ANOMALY_SCORES",
        scores=scores.tolist(),
        ground_truth=list(labels),
    )
    # Which split the scores cover and how many there are (gate 1 checks), and
    # the method's own AUC over exactly these scores (gate 4 compares).
    writer.add_metadata(dataset=run.dataset, **asdict(config), summary={
        "dataset_info": {"scored_split": "test", "scored_samples": len(scores)},
        "results": {"test_auc": test_auc},      # replace
    })
    writer.finalize()


if __name__ == "__main__":
    main()
```

**Test:**
```bash
graflag sync                                   # from methods/mymethod/
graflag run -m mymethod -d your_dataset --build
graflag evaluate -e exp__mymethod__your_dataset__TIMESTAMP
```

Running the image by hand is possible but reproduces little: the build context
is `graflag-shared/`, not the method directory, and `DATA`, `EXP` and the
`_FOO` variables all have to be supplied by hand. `graflag run` is the shorter
path to the same answer.

---

## Resources

- **Result Types Reference**: See [RESULTS_STANDARD](RESULTS_STANDARD.md)
- **Agent Integration Guide**: See [AGENT_METHOD_INTEGRATION](AGENT_METHOD_INTEGRATION.md) for AI-assisted integration
- **The [Method Integration Skill](AGENT_SKILL.md)**: `graflag-shared/.claude/skills/method-integration/`, with the four gates, the result check behind `graflag verify` and a catalogue of failures that reported success
- **Example Methods**: `methods/generaldyg/`, `methods/taddy/`, `methods/bond_*/`
- **graflag_runner Source**: `graflag-shared/libs/graflag_runner/`
- **graflag_bond Source**: `graflag-shared/libs/graflag_bond/` (for PyGOD integration)
