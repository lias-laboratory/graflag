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
  "graph_ids": ["graph_001", "graph_002", "graph_003", "graph_004"]
}
```

---

## Temporal/Dynamic Graph Results

### TEMPORAL_NODE_SCORES

**Description:** Time-series of anomaly scores for nodes as the graph evolves over time.

**Format:** 2D array where each row represents a time step, each column a node

```json
{
  "result_type": "TEMPORAL_NODE_SCORES",
  "scores": [
    [0.12, 0.25, 0.08, 0.19],  // t=0
    [0.15, 0.89, 0.12, 0.22],  // t=1  
    [0.18, 0.93, 0.15, 0.25]   // t=2
  ],
  "timestamps": [0, 1, 2],
  "node_ids": [0, 1, 2, 3]
}
```

---

### TEMPORAL_EDGE_SCORES

**Description:** Time-series of anomaly scores for edges as new connections appear/disappear.

**Format:** 2D array where each row represents a time step, each column an edge

```json
{
  "result_type": "TEMPORAL_EDGE_SCORES",
  "scores": [
    [0.05, 0.32, 0.17],        // t=0
    [0.08, 0.91, 0.20],        // t=1
    [0.12, 0.95, 0.23]         // t=2  
  ],
  "timestamps": [0, 1, 2],
  "edges": [[0,1], [1,2], [2,3]]
}
```

---

### TEMPORAL_GRAPH_SCORES

**Description:** Time-series of anomaly scores for entire graphs in a streaming/dynamic setting.

**Format:** 2D array where each row represents a time iteration, each column a graph

```json
{
  "result_type": "TEMPORAL_GRAPH_SCORES",
  "scores": [
    [0.086, 0.056, 0.062, 0.044],  // iteration 1
    [0.089, 0.061, 0.065, 0.047],  // iteration 2
    [0.092, 0.358, 0.068, 0.050]   // iteration 3 (graph 1 becomes anomalous)
  ],
  "iterations": [1, 2, 3],
  "graph_ids": [478, 338, 337, 318]
}
```

---

## Scores

- **Special Values:**
  - `-1`: Unknown/unassigned
  - `-2`: Inactive/unseen at this time step

## Integration with Result Adapters

Each method's result adapter should:

0. File must be named `results.json` and saved in the current experiment directory
1. Set the appropriate `RESULT_TYPE` in the `.env` file
2. Include `metadata` field:
    - exp_name (required)
    - method_name (required)
    - dataset (required)
    - method_parameters (required)
    - exec_time (required)
    - gpu_memory (required if used)
    - memory (required)
    - threshold (required if used)
    - summary (rquired, relative to the method)
3. Structure output JSON according to the format above in `results` field
