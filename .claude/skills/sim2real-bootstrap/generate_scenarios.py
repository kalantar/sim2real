#!/usr/bin/env python3
"""Generate llm-d-benchmark scenario (baseline.yaml) files from top3_selection.json."""

import json
import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Lookup tables
# ---------------------------------------------------------------------------

MODEL_METADATA = {
    "meta-llama/Llama-3.1-8B": {
        "shortName": "meta-llama-llama-3-1-8b",
        "path": "models/meta-llama/Llama-3.1-8B",
        "size": "1Ti",
        "maxModelLen": 131072,
    },
    "Qwen/Qwen3-14B": {
        "shortName": "qwen-qwen3-14b",
        "path": "models/Qwen/Qwen3-14B",
        "size": "1Ti",
        "maxModelLen": 40960,
    },
}

HARDWARE_LABELS = {
    "H100_SXM_80GB": "NVIDIA-H100-80GB-HBM3",
    "A100_SXM_80GB": "NVIDIA-A100-SXM4-80GB",
    "A100_PCIE_40GB": "NVIDIA-A100-PCIE-40GB",
}


# ---------------------------------------------------------------------------
# Field registry: declares which fields we handle vs intentionally ignore.
# Any field not in either set triggers a warning.
# ---------------------------------------------------------------------------

KNOWN_FIELDS = {
    "workload": {
        "mapped": {"model", "hardware"},
        "ignored": {
            "preset", "num_requests", "isl_mean", "isl_max",
            "osl_mean", "osl_max", "arrival_pattern",
            "slo_ttft_mean_ms", "seed", "trace_file",
        },
    },
    "vllm_args": {
        "mapped": {
            "tensor_parallel_size", "pipeline_parallel_size",
            "num_instances", "data_parallel_size",
            "max_num_seqs", "max_num_batched_tokens",
            "enable_chunked_prefill", "block_size",
            "gpu_memory_utilization", "dtype", "kv_cache_dtype",
            "enable_prefix_caching", "enforce_eager", "swap_space",
        },
        "ignored": set(),
    },
    "routing_config": {
        "mapped": {"strategy"},
        "ignored": {"scorers", "picker"},
    },
    "tool_config": {
        "mapped": set(),
        "ignored": {
            "scheduler", "admission_policy", "preemption_policy",
            "max_concurrency", "vidur_scheduler_type",
        },
    },
}

# Top-level keys in each entry
KNOWN_TOP_LEVEL = {"tool", "workload", "vllm_args", "routing_config", "tool_config", "results", "metadata"}


def check_unknown_fields(entry: dict, entry_name: str) -> list[str]:
    """Check for fields not in the known registry. Returns list of warnings."""
    warnings = []

    # Check top-level keys
    for key in entry:
        if key not in KNOWN_TOP_LEVEL:
            warnings.append(f"[{entry_name}] unknown top-level key: '{key}'")

    # Check each section
    for section_name, registry in KNOWN_FIELDS.items():
        section = entry.get(section_name)
        if section is None:
            continue
        all_known = registry["mapped"] | registry["ignored"]
        for key in section:
            if key not in all_known:
                warnings.append(
                    f"[{entry_name}] unknown field in {section_name}: '{key}' "
                    f"— may need mapping"
                )

    return warnings


def build_additional_flags(vllm_args: dict) -> list[str]:
    """Convert vllm_args into a list of --flag strings for additionalFlags."""
    flags = []

    if vllm_args.get("max_num_seqs") is not None:
        flags.append(f"--max-num-seqs={vllm_args['max_num_seqs']}")

    if vllm_args.get("max_num_batched_tokens") is not None:
        flags.append(f"--max-num-batched-tokens={vllm_args['max_num_batched_tokens']}")

    if vllm_args.get("enable_chunked_prefill"):
        flags.append("--enable-chunked-prefill")

    if not vllm_args.get("enable_prefix_caching", True):
        flags.append("--no-enable-prefix-caching")

    if vllm_args.get("dtype") and vllm_args["dtype"] != "auto":
        flags.append(f"--dtype={vllm_args['dtype']}")

    if vllm_args.get("swap_space") is not None and vllm_args["swap_space"] != 4:
        flags.append(f"--swap-space={vllm_args['swap_space']}")

    if vllm_args.get("pipeline_parallel_size", 1) > 1:
        flags.append(f"--pipeline-parallel-size={vllm_args['pipeline_parallel_size']}")

    return flags


def build_scenario(entry: dict, name: str) -> dict:
    """Build a scenario YAML dict from a single top3_selection entry."""
    workload = entry["workload"]
    vllm_args = entry["vllm_args"]

    model_name = workload["model"]
    hardware = workload["hardware"]

    meta = MODEL_METADATA.get(model_name, {})
    hw_label = HARDWARE_LABELS.get(hardware, hardware)

    tp = vllm_args.get("tensor_parallel_size", 1)
    replicas = vllm_args.get("num_instances", 1)
    dp = vllm_args.get("data_parallel_size", 1)

    scenario = {"name": name}

    # Model
    scenario["model"] = {
        "name": model_name,
        "shortName": meta.get("shortName", model_name.replace("/", "-").lower()),
        "path": meta.get("path", f"models/{model_name}"),
        "huggingfaceId": model_name,
        "size": meta.get("size", "1Ti"),
        "maxModelLen": meta.get("maxModelLen", 16384),
        "blockSize": vllm_args.get("block_size", 16),
        "gpuMemoryUtilization": vllm_args.get("gpu_memory_utilization", 0.9),
    }

    # Decode
    decode = {"replicas": replicas}

    if hw_label:
        decode["acceleratorType"] = {
            "labelKey": "nvidia.com/gpu.product",
            "labelValue": hw_label,
        }

    if tp > 1 or dp > 1:
        decode["parallelism"] = {
            "data": dp,
            "dataLocal": dp,
            "tensor": tp,
            "workers": tp,
        }

    flags = build_additional_flags(vllm_args)
    if flags:
        decode["vllm"] = {"additionalFlags": flags}

    # enforce_eager: defaults.yaml sets it true; only override if false
    enforce_eager = vllm_args.get("enforce_eager", True)
    if not enforce_eager:
        scenario["vllmCommon"] = {"flags": {"enforceEager": False}}

    scenario["decode"] = decode

    return scenario


def write_commented_yaml(scenario: dict, entry: dict, out_path: str):
    """Write scenario YAML with comments explaining the source of each field."""
    lines = []
    lines.append("scenario:")
    lines.append(f"- name: {scenario['name']}")
    lines.append("")
    lines.append("  model:")
    lines.append(f"    name: {scenario['model']['name']}  # from workload.model")
    lines.append(f"    shortName: {scenario['model']['shortName']}  # derived from model name (lookup table)")
    lines.append(f"    path: {scenario['model']['path']}  # derived from model name (lookup table)")
    lines.append(f"    huggingfaceId: {scenario['model']['huggingfaceId']}  # from workload.model")
    lines.append(f"    size: {scenario['model']['size']}  # lookup table (storage estimate)")
    lines.append(f"    maxModelLen: {scenario['model']['maxModelLen']}  # lookup table (model's max context window)")
    lines.append(f"    blockSize: {scenario['model']['blockSize']}  # from vllm_args.block_size")
    lines.append(f"    gpuMemoryUtilization: {scenario['model']['gpuMemoryUtilization']}  # from vllm_args.gpu_memory_utilization")

    if "vllmCommon" in scenario:
        lines.append("")
        lines.append("  vllmCommon:")
        lines.append("    flags:")
        lines.append(f"      enforceEager: {str(scenario['vllmCommon']['flags']['enforceEager']).lower()}  # from vllm_args.enforce_eager")

    lines.append("")
    lines.append("  decode:")
    lines.append(f"    replicas: {scenario['decode']['replicas']}  # from vllm_args.num_instances")

    if "acceleratorType" in scenario["decode"]:
        lines.append("    acceleratorType:")
        lines.append(f"      labelKey: {scenario['decode']['acceleratorType']['labelKey']}")
        lines.append(f"      labelValue: {scenario['decode']['acceleratorType']['labelValue']}  # from workload.hardware (lookup table)")

    if "parallelism" in scenario["decode"]:
        p = scenario["decode"]["parallelism"]
        lines.append("    parallelism:")
        lines.append(f"      data: {p['data']}  # from vllm_args.data_parallel_size")
        lines.append(f"      dataLocal: {p['dataLocal']}  # from vllm_args.data_parallel_size")
        lines.append(f"      tensor: {p['tensor']}  # from vllm_args.tensor_parallel_size")
        lines.append(f"      workers: {p['workers']}  # from vllm_args.tensor_parallel_size")

    if "vllm" in scenario["decode"]:
        lines.append("    vllm:")
        lines.append("      additionalFlags:")
        for flag in scenario["decode"]["vllm"]["additionalFlags"]:
            source = _flag_source(flag)
            lines.append(f"      - \"{flag}\"  # from vllm_args.{source}")

    lines.append("")

    with open(out_path, "w") as f:
        f.write("\n".join(lines) + "\n")


def _flag_source(flag: str) -> str:
    """Map a --flag back to its vllm_args field name."""
    mapping = {
        "--max-num-seqs": "max_num_seqs",
        "--max-num-batched-tokens": "max_num_batched_tokens",
        "--enable-chunked-prefill": "enable_chunked_prefill",
        "--no-enable-prefix-caching": "enable_prefix_caching",
        "--dtype": "dtype",
        "--swap-space": "swap_space",
        "--pipeline-parallel-size": "pipeline_parallel_size",
    }
    for prefix, source in mapping.items():
        if flag.startswith(prefix):
            return source
    return "unknown"


def generate(input_path: str, output_dir: str):
    with open(input_path) as f:
        data = json.load(f)

    os.makedirs(output_dir, exist_ok=True)

    all_warnings = []

    for group_name, entries in data.items():
        for i, entry in enumerate(entries):
            scenario_name = f"{group_name}-{i+1}"

            warnings = check_unknown_fields(entry, scenario_name)
            all_warnings.extend(warnings)

            scenario = build_scenario(entry, scenario_name)

            filename = f"{scenario_name}.yaml"
            out_path = os.path.join(output_dir, filename)
            write_commented_yaml(scenario, entry, out_path)

            print(f"  wrote {out_path}")

    if all_warnings:
        print(f"\n⚠ {len(all_warnings)} warning(s) — unknown fields detected:")
        for w in all_warnings:
            print(f"  {w}")
        print("\nUpdate KNOWN_FIELDS in the script to classify these as 'mapped' or 'ignored'.")


if __name__ == "__main__":
    script_dir = Path(__file__).parent

    input_file = sys.argv[1] if len(sys.argv) > 1 else str(script_dir / "top3_selection.json")
    output_dir = sys.argv[2] if len(sys.argv) > 2 else str(script_dir / "generated_scenarios")

    print(f"Reading: {input_file}")
    print(f"Output:  {output_dir}")
    generate(input_file, output_dir)
    print("Done.")
