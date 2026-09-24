# Graph Anomaly Detection Result Types

This document defines the standardized result types used in GraFlag for graph anomaly detection methods.

## Static Graph Results

### NODE_ANOMALY_SCORES

**Description:** Anomaly scores for each individual node in a static graph.

**Format:** Array of numerical scores, one per node

```json
{
  "result_type": "NODE_ANOMALY_SCORES",
  "scores": [0.12, 0.89, 0.03, 0.76, 0.15],
  "ground_truth": [0, 1, 0, 1, 0],
  "node_ids": [0, 1, 2, 3, 4]
}
```

---

### EDGE_ANOMALY_SCORES

**Description:** Anomaly scores for each individual edge in a static graph.

**Format:** Array of numerical scores, one per edge

```json
{
  "result_type": "EDGE_ANOMALY_SCORES",
  "scores": [0.05, 0.92, 0.17, 0.68],
  "ground_truth": [0, 1, 0, 1],
  "edges": [[0,1], [1,2], [2,3], [0,3]]
}
```

---

### GRAPH_ANOMALY_SCORES

**Description:** Anomaly scores for entire graphs in a graph classification setting.

**Format:** Array of numerical scores, one per graph

```json
{
  "result_type": "GRAPH_ANOMALY_SCORES",
  "scores": [0.23, 0.87, 0.11, 0.94],
  "ground_truth": [0, 1, 0, 1],
  "graph_ids": ["graph_001", "graph_002", "graph_003", "graph_004"]
}
```

---

## Temporal/Dynamic Graph Results

### TEMPORAL_NODE_ANOMALY_SCORES

**Description:** Time-series of anomaly scores for nodes as the graph evolves over time.

**Format:** 2D array where each row represents a time step, each column a node

```json
{
  "result_type": "TEMPORAL_NODE_ANOMALY_SCORES",
  "scores": [
    [0.12, 0.25, 0.08, 0.19],
    [0.15, 0.89, 0.12, 0.22],
    [0.18, 0.93, 0.15, 0.25]
  ],
  "ground_truth": [
    [0, 0, 0, 0],
    [0, 1, 0, 0],
    [0, 1, 0, 0]
  ],
  "timestamps": [0, 1, 2],
  "node_ids": [0, 1, 2, 3]
}
```

---

### TEMPORAL_EDGE_ANOMALY_SCORES

**Description:** Time-series of anomaly scores for edges as new connections appear/disappear.

**Format:** 2D array where each row represents a time step, each column an edge

```json
{
  "result_type": "TEMPORAL_EDGE_ANOMALY_SCORES",
  "scores": [
    [0.05, 0.32, 0.17],
    [0.08, 0.91, 0.20],
    [0.12, 0.95, 0.23]
  ],
  "ground_truth": [
    [0, 0, 0],
    [0, 1, 0],
    [0, 1, 0]
  ],
  "timestamps": [0, 1, 2],
  "edges": [[0,1], [1,2], [2,3]]
}
```

---

### TEMPORAL_GRAPH_ANOMALY_SCORES

**Description:** Time-series of anomaly scores for entire graphs in a dynamic setting.

**Format:** 2D array where each row represents a time iteration, each column a graph

```json
{
  "result_type": "TEMPORAL_GRAPH_ANOMALY_SCORES",
  "scores": [
    [0.086, 0.056, 0.062, 0.044],
    [0.089, 0.061, 0.065, 0.047],
    [0.092, 0.358, 0.068, 0.050]
  ],
  "ground_truth": [
    [0, 0, 0, 0],
    [0, 0, 0, 0],
    [0, 1, 0, 0]
  ],
  "timestamps": [0, 1, 2],
  "graph_ids": [478, 338, 337, 318]
}
```

---

## Streaming Graph Results

### NODE_STREAM_ANOMALY_SCORES

**Description:** Anomaly scores for nodes in a streaming setting where each node activity appears at a specific timestamp. Used for node stream anomaly detection where node events arrive sequentially over time.

**Format:** 1D array of scores with corresponding node IDs and timestamps (one score per node occurrence)

```json
{
  "result_type": "NODE_STREAM_ANOMALY_SCORES",
  "scores": [0.12, 0.89, 0.15, 0.76, 0.23],
  "ground_truth": [0, 1, 0, 1, 0],
  "node_ids": [0, 1, 0, 2, 1],
  "timestamps": [0, 0, 1, 1, 2]
}
```

**Notes:**

- Each index corresponds to one node activity occurrence in the stream
- `scores[i]` is the anomaly score for node `node_ids[i]` at time `timestamps[i]`
- Same node can appear multiple times at different timestamps
- More memory-efficient than 2D format when node activities are sparse over time

**Comparison with TEMPORAL_NODE_ANOMALY_SCORES:**

| Feature | TEMPORAL_NODE_ANOMALY_SCORES | NODE_STREAM_ANOMALY_SCORES |
|---------|------------------------------|----------------------------|
| Format | 2D array `[T x N]` | 1D array `[M]` |
| Use Case | Fixed node set, scores over time | Streaming node events |
| Memory | `O(T x N)` - can be large | `O(M)` - efficient |
| Inactive Nodes | Use `-2` for missing nodes | Not represented |

---

### EDGE_STREAM_ANOMALY_SCORES

**Description:** Anomaly scores for edges in a streaming setting where each edge appears at a specific timestamp. Used for edge stream anomaly detection where edges arrive sequentially over time.

**Format:** 1D array of scores with corresponding edge pairs and timestamps (one score per edge occurrence)

```json
{
  "result_type": "EDGE_STREAM_ANOMALY_SCORES",
  "scores": [0.05, 0.91, 0.23, 0.68, 0.12],
  "ground_truth": [0, 1, 0, 1, 0],
  "edges": [[0,1], [1,2], [2,3], [0,3], [1,3]],
  "timestamps": [0, 0, 1, 1, 2]
}
```

**Notes:**

- Each index corresponds to one edge occurrence in the stream
- `scores[i]` is the anomaly score for edge `edges[i]` at time `timestamps[i]`
- Same edge can appear multiple times at different timestamps
- More memory-efficient than 2D format when edges are sparse over time

**Comparison with TEMPORAL_EDGE_ANOMALY_SCORES:**

| Feature | TEMPORAL_EDGE_ANOMALY_SCORES | EDGE_STREAM_ANOMALY_SCORES |
|---------|------------------------------|----------------------------|
| Format | 2D array `[T x E]` | 1D array `[N]` |
| Use Case | Fixed edge set, scores over time | Streaming edges |
| Memory | `O(T x E)` - can be large | `O(N)` - efficient |
| Inactive Edges | Use `-2` for missing edges | Not represented |

---

### GRAPH_STREAM_ANOMALY_SCORES

**Description:** Anomaly scores for graphs in a streaming setting where each graph snapshot appears at a specific timestamp.

**Format:** 1D array of scores with corresponding graph IDs and timestamps (one score per graph occurrence)

```json
{
  "result_type": "GRAPH_STREAM_ANOMALY_SCORES",
  "scores": [0.23, 0.87, 0.11, 0.94, 0.35],
  "ground_truth": [0, 1, 0, 1, 0],
  "graph_ids": ["graph_001", "graph_002", "graph_001", "graph_003", "graph_002"],
  "timestamps": [0, 0, 1, 1, 2]
}
```

**Notes:**

- Each index corresponds to one graph occurrence in the stream
- `scores[i]` is the anomaly score for graph `graph_ids[i]` at time `timestamps[i]`
- Same graph can appear multiple times at different timestamps
- More memory-efficient than 2D format when graph events are sparse over time

**Comparison with TEMPORAL_GRAPH_ANOMALY_SCORES:**

| Feature | TEMPORAL_GRAPH_ANOMALY_SCORES | GRAPH_STREAM_ANOMALY_SCORES |
|---------|------------------------------|----------------------------|
| Format | 2D array `[T x G]` | 1D array `[M]` |
| Use Case | Fixed graph set, scores over time | Streaming graph snapshots |
| Memory | `O(T x G)` - can be large | `O(M)` - efficient |
| Inactive Graphs | Use `-2` for missing graphs | Not represented |

---

## Special Score Values

- `-1`: Unknown/unassigned
- `-2`: Inactive/unseen at this time step (temporal/streaming types only)

## Writing Results with ResultWriter

Methods should use `ResultWriter` from `graflag_runner` to produce `results.json`:

```python
from graflag_runner import ResultWriter

writer = ResultWriter()

# Save scores with ground truth
writer.save_scores(
    result_type="NODE_ANOMALY_SCORES",
    scores=anomaly_scores.tolist(),
    ground_truth=labels.tolist(),
    node_ids=list(range(len(anomaly_scores)))  # optional
)

# Add metadata
writer.add_metadata(
    method_name="your_method",
    dataset="cora"
)

# Finalize: writes results.json to the EXP directory, atomically
writer.finalize()
```

`graflag evaluate` reads `results.json` from the experiment directory (the
`EXP` environment variable) to compute the metrics and plots.

A method does not record its own execution time or memory. `graflag_runner`
measures `exec_time_ms`, `peak_memory_mb` and `peak_gpu_mb` over the whole
process tree and merges them into `metadata` after the method exits. Values a
method passes to `writer.add_resource_metrics()` are kept beside them as
`method_reported_<key>`, but the reported numbers are the runner's.

### Required Fields

| Field | Description |
|-------|-------------|
| `result_type` | One of the valid result types listed above |
| `scores` | Anomaly scores (list or nested list) |
| `ground_truth` | Binary labels matching scores shape (0=normal, 1=anomaly); both classes must be present |

### Optional Fields

| Field | Description |
|-------|-------------|
| `metadata` | Dict with method_name, dataset, hyperparameters, etc. |
| `timestamps` | Time indices for temporal/streaming types |
| `node_ids` | Node identifiers |
| `edges` | Edge pairs as `[[src, dst], ...]` |
| `graph_ids` | Graph identifiers |

## How the Evaluator Reads Results

`graflag evaluate` flattens `scores` and `ground_truth` into one pair of arrays
whatever the result type (a ragged 2D array, with snapshots of different sizes,
is flattened row by row). Before computing anything it leaves out:

- scores equal to `-1` (unknown) or `-2` (inactive);
- scores that are not finite (NaN or infinite).

How many samples were left out, and for which reason, is reported under
`metrics.filtering` in `evaluation.json`; the dashboard shows the total. Metrics
and plots are computed from the same filtered pairs, so the AUC in a plot legend
is the one in `evaluation.json`.

| Metric | Definition |
|--------|------------|
| `auc_roc` | Area under the ROC curve |
| `auc_pr` | Average precision (`sklearn.metrics.average_precision_score`), not the trapezoidal area under the PR curve, which overstates it at low base rates |
| `precision_at_k`, `recall_at_k`, `f1_at_k` | At `k` = the number of positives, so precision and recall at `k` are equal by construction. Scores tied across the cut-off count at their positive rate (the expectation under random tie-breaking); `k` is reported alongside |
| `best_f1`, `best_f1_threshold` | The best F1 over all thresholds, and the threshold that reaches it |
| `num_samples`, `num_anomalies`, `anomaly_ratio` | Counted after filtering |

The ranking metrics need both classes. A ground truth with no anomalies -- a
training split, typically -- leaves them empty with a warning rather than
failing, which is why a method must publish the scores of its **test** split.

`python3 -m graflag_evaluator.compare_metrics <exp_dir>` recomputes an existing
experiment's metrics with the implementation before these rules and prints what
moves.

## Custom Metrics

The built-in metrics above are computed for every result type. Plugins add
more.

### Plugin Files

Place a `.py` file in one of two locations:

- **Global** (all evaluations): `libs/graflag_evaluator/plugins/`
- **Per-experiment**: `experiments/<exp_name>/custom_metrics/`

Each plugin imports `MetricCalculator` and registers a metric function at module level:

```python
# hits_at_100.py
from graflag_evaluator import MetricCalculator

def hits_at_100(scores, ground_truth, **kw):
    top = scores.argsort()[-100:]
    return {"hits@100": float(ground_truth[top].mean())}

MetricCalculator.register_metric("EDGE_STREAM_ANOMALY_SCORES", hits_at_100)
```

The function returns a `Dict[str, float]`. It receives `scores` and
`ground_truth` as NumPy arrays holding what `results.json` holds -- nested for
the temporal types, with any `-1`/`-2` sentinels still in -- plus keyword
arguments it may ignore. To measure on the same samples as the built-in metrics,
filter them the same way:

```python
from graflag_evaluator.preprocessing import prepare_pairs

def hits_at_100(scores, ground_truth, **kw):
    scores, ground_truth, _ = prepare_pairs(scores, ground_truth)
    top = scores.argsort()[-100:]
    return {"hits@100": float(ground_truth[top].mean())}
```

### Python API

The `GraFlag.register_metric()` method extracts a function's source code and writes it as a plugin file on the cluster:

```python
from graflag import GraFlag

def hits_at_100(scores, ground_truth, **kw):
    top = scores.argsort()[-100:]
    return {"hits@100": float(ground_truth[top].mean())}

gf = GraFlag()
gf.register_metric("EDGE_STREAM_ANOMALY_SCORES", hits_at_100)
gf.evaluate("exp__taddy__uci__20260309_120000")  # includes hits@100
```

Pass `experiment="exp_name"` to scope the metric to a single experiment instead of globally.
