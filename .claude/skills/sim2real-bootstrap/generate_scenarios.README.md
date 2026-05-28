# generate_scenarios.py — Assumptions & Decisions

## Purpose

Converts entries from `top3_selection.json` into llm-d-benchmark scenario YAML
files (overrides on top of `defaults.yaml`).

## Input fields used

Only system-under-test fields are mapped:

| Source section | Fields used |
|---|---|
| `workload` | `model`, `hardware` |
| `vllm_args` | all fields |
| `routing_config` | `strategy` (currently not emitted — see below) |

Ignored sections: `tool`, `tool_config`, `results`, `metadata`, and workload
traffic parameters (`num_requests`, `isl_*`, `osl_*`, `arrival_pattern`,
`slo_*`, `seed`, `trace_file`).

## Omission rules (when a field is NOT emitted)

These assume `defaults.yaml` from llm-d-benchmark provides the base values.

| Field | Omitted when | Rationale |
|---|---|---|
| `decode.parallelism` | `tensor_parallel_size == 1` AND `data_parallel_size == 1` | Matches the `parallelism_single` default |
| `swap_space` | value == 4 | 4 is vLLM's built-in default |
| `enforce_eager` | value == true | `defaults.yaml` sets `enforceEager: true` |
| `dtype` | value == "auto" | vLLM auto-selects dtype by default |
| `kv_cache_dtype` | value == "auto" | vLLM default; never emitted currently |
| `enable_chunked_prefill` | value == false | Only emitted as `--enable-chunked-prefill` when true |
| `pipeline_parallel_size` | value == 1 | Single-pipeline is the default |

## Lookup tables (values not in the input JSON)

### MODEL_METADATA

Maps model name → fields needed by the scenario but absent from `top3_selection.json`.

| Field | Source | Example |
|---|---|---|
| `shortName` | Derived: lowercase, slashes → hyphens | `meta-llama-llama-3-1-8b` |
| `path` | Convention: `models/<model_name>` | `models/meta-llama/Llama-3.1-8B` |
| `size` | Hardcoded estimate (PVC size hint) | `1Ti` |
| `maxModelLen` | Hardcoded from model spec (max context window) | `131072` for Llama-3.1-8B |

### HARDWARE_LABELS

Maps simulation hardware identifiers → Kubernetes node selector label values.

| Input | Output |
|---|---|
| `H100_SXM_80GB` | `NVIDIA-H100-80GB-HBM3` |
| `A100_SXM_80GB` | `NVIDIA-A100-SXM4-80GB` |
| `A100_PCIE_40GB` | `NVIDIA-A100-PCIE-40GB` |

## Field mappings

### Direct mappings (1:1)

| Input | Output location |
|---|---|
| `workload.model` | `model.name`, `model.huggingfaceId` |
| `workload.hardware` | `decode.acceleratorType.labelValue` |
| `vllm_args.num_instances` | `decode.replicas` |
| `vllm_args.tensor_parallel_size` | `decode.parallelism.tensor`, `decode.parallelism.workers` |
| `vllm_args.data_parallel_size` | `decode.parallelism.data`, `decode.parallelism.dataLocal` |
| `vllm_args.block_size` | `model.blockSize` |
| `vllm_args.gpu_memory_utilization` | `model.gpuMemoryUtilization` |
| `vllm_args.enforce_eager` | `vllmCommon.flags.enforceEager` |

### Mapped to additionalFlags

| Input | Flag |
|---|---|
| `vllm_args.max_num_seqs` | `--max-num-seqs=N` |
| `vllm_args.max_num_batched_tokens` | `--max-num-batched-tokens=N` |
| `vllm_args.enable_chunked_prefill` | `--enable-chunked-prefill` |
| `vllm_args.enable_prefix_caching` | `--no-enable-prefix-caching` (when false) |
| `vllm_args.dtype` | `--dtype=X` |
| `vllm_args.swap_space` | `--swap-space=N` |
| `vllm_args.pipeline_parallel_size` | `--pipeline-parallel-size=N` |

## Not yet mapped

| Field | Reason |
|---|---|
| `routing_config.strategy` | "round-robin" is likely default EPP behavior; custom strategies would need `inferenceExtension.pluginsCustomConfig` |
| `routing_config.scorers` | Always null in current data |
| `routing_config.picker` | Always null in current data |
