#!/usr/bin/env python3
"""Generate llm-d-benchmark baseline scenario YAML from config.md markdown tables.

Parses the vLLM configuration table in config.md, applies lookup tables and
default rules, and writes a scenario YAML with provenance comments showing
where each value originated.

Usage:
    python3 generate_from_config.py [config.md] [-o baselines/] [-n name] [--dry-run]
"""

import argparse
import os
import re
import sys
from dataclasses import dataclass, field
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

# Maps canonical field name -> set of recognized aliases (lowercased)
PARAMETER_ALIASES = {
    "model": {"model", "--model"},
    "hardware": {"gpu", "hardware"},
    "tensor_parallel_size": {"tensor_parallel_size", "--tensor-parallel-size", "tp"},
    "max_num_seqs": {"max_num_seqs", "--max-num-seqs"},
    "max_num_batched_tokens": {"max_num_batched_tokens", "--max-num-batched-tokens"},
    "block_size": {"block_size", "--block-size", "block_size_in_tokens"},
    "gpu_memory_utilization": {"gpu_memory_utilization", "--gpu-memory-utilization"},
    "max_model_len": {"max_model_len", "--max-model-len", "max_seq_len"},
    "enable_chunked_prefill": {"enable_chunked_prefill", "--enable-chunked-prefill"},
    "enable_prefix_caching": {"enable_prefix_caching", "--enable-prefix-caching"},
    "replicas": {"number of pods", "instances", "replicas", "num_instances"},
    "dtype": {"dtype", "--dtype"},
    "pipeline_parallel_size": {"pipeline_parallel_size", "--pipeline-parallel-size"},
    "data_parallel_size": {"data_parallel_size", "--data-parallel-size"},
    "swap_space": {"swap_space", "--swap-space"},
    "enforce_eager": {"enforce_eager", "--enforce-eager"},
}

# Section heading keywords that indicate a vLLM configuration table
VLLM_SECTION_KEYWORDS = [
    "vllm pod configuration",
    "vllm server arguments",
    "real deployment",
    "pod configuration",
    "vllm configuration",
]

# Fields whose presence in a table signals it's the vLLM config table
VLLM_INDICATOR_FIELDS = {"model", "max_num_seqs", "hardware", "replicas", "gpu_memory_utilization"}


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class ProvenanceValue:
    value: object
    source: str
    raw_param: str = ""


@dataclass
class TableSection:
    heading: str
    rows: list = field(default_factory=list)
    line_number: int = 0


# ---------------------------------------------------------------------------
# Markdown table parsing
# ---------------------------------------------------------------------------

def normalize_cell(raw: str) -> str:
    """Strip whitespace, backticks, and surrounding quotes from a table cell."""
    s = raw.strip()
    if s.startswith("`") and s.endswith("`"):
        s = s[1:-1]
    if s.startswith('"') and s.endswith('"'):
        s = s[1:-1]
    if s.startswith("'") and s.endswith("'"):
        s = s[1:-1]
    return s.strip()


def is_separator_row(line: str) -> bool:
    """Check if a line is a markdown table separator (|---|---|)."""
    return bool(re.match(r"^\s*\|[\s\-:|]+\|\s*$", line))


def parse_table_row(line: str) -> list[str]:
    """Split a pipe-delimited row into cells."""
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [cell.strip() for cell in line.split("|")]


def parse_md_tables(lines: list[str]) -> list[TableSection]:
    """Find all markdown tables in the file, grouped by their nearest heading."""
    tables = []
    current_heading = ""
    i = 0

    while i < len(lines):
        line = lines[i]

        # Track headings
        heading_match = re.match(r"^(#{1,6})\s+(.+)$", line)
        if heading_match:
            current_heading = heading_match.group(2).strip()
            i += 1
            continue

        # Detect table start: a line with pipes that's followed by a separator
        if "|" in line and not is_separator_row(line):
            if i + 1 < len(lines) and is_separator_row(lines[i + 1]):
                # Parse header
                headers = [normalize_cell(c) for c in parse_table_row(line)]
                i += 2  # skip header and separator

                rows = []
                while i < len(lines) and "|" in lines[i] and not is_separator_row(lines[i]):
                    cells = [normalize_cell(c) for c in parse_table_row(lines[i])]
                    if len(cells) >= len(headers):
                        cells = cells[: len(headers)]
                    else:
                        cells.extend([""] * (len(headers) - len(cells)))
                    row = dict(zip(headers, cells))
                    rows.append(row)
                    i += 1

                tables.append(TableSection(heading=current_heading, rows=rows, line_number=i))
                continue

        i += 1

    return tables


def canonicalize_parameter(raw: str) -> str | None:
    """Resolve a raw parameter name from config.md to its canonical form."""
    cleaned = normalize_cell(raw).lower().strip()
    for canonical, aliases in PARAMETER_ALIASES.items():
        if cleaned in aliases:
            return canonical
    return None


def find_vllm_table(tables: list[TableSection]) -> TableSection | None:
    """Select the table most likely to contain vLLM pod configuration."""
    # First pass: match by section heading
    for table in tables:
        heading_lower = table.heading.lower()
        if any(kw in heading_lower for kw in VLLM_SECTION_KEYWORDS):
            return table

    # Second pass: match by content (table with most vLLM indicator fields)
    best = None
    best_score = 0
    for table in tables:
        score = 0
        for row in table.rows:
            first_col = list(row.values())[0] if row else ""
            canonical = canonicalize_parameter(first_col)
            if canonical and canonical in VLLM_INDICATOR_FIELDS:
                score += 1
        if score > best_score:
            best = table
            best_score = score

    return best


# ---------------------------------------------------------------------------
# Field extraction
# ---------------------------------------------------------------------------

def parse_boolean(raw: str) -> bool | None:
    """Parse a boolean value, handling annotations like '(true required...)'."""
    lower = raw.lower().strip()
    if lower in ("true", "yes", "1"):
        return True
    if lower in ("false", "no", "0"):
        return False
    # Check for boolean inside parenthetical
    if "true" in lower:
        return True
    if "false" in lower:
        return False
    return None


def parse_numeric(raw: str) -> int | float | None:
    """Parse a numeric value, stripping any trailing annotations."""
    # Take only the first token (before any spaces/notes)
    token = raw.split()[0] if raw.split() else raw
    try:
        if "." in token:
            return float(token)
        return int(token)
    except ValueError:
        return None


def extract_fields(table: TableSection) -> dict[str, ProvenanceValue]:
    """Extract canonical fields from a parsed table, with provenance tracking."""
    fields = {}
    # Determine which column holds the parameter name (usually first)
    if not table.rows:
        return fields

    first_row_keys = list(table.rows[0].keys())
    param_col = first_row_keys[0]
    value_col = first_row_keys[1] if len(first_row_keys) > 1 else None

    if value_col is None:
        return fields

    for row in table.rows:
        raw_param = row.get(param_col, "")
        raw_value = row.get(value_col, "")
        canonical = canonicalize_parameter(raw_param)

        if canonical is None:
            continue

        source = f'config.md row "{normalize_cell(raw_param)}"'

        # Parse value based on field type
        if canonical in ("model", "hardware", "dtype"):
            value = normalize_cell(raw_value)
        elif canonical in ("enable_chunked_prefill", "enable_prefix_caching", "enforce_eager"):
            value = parse_boolean(raw_value)
            if value is None:
                print(f"  warning: could not parse boolean for {canonical}: '{raw_value}'", file=sys.stderr)
                continue
        else:
            value = parse_numeric(raw_value)
            if value is None:
                # Try as string (may be a model name in an unusual column)
                value = normalize_cell(raw_value)

        fields[canonical] = ProvenanceValue(value=value, source=source, raw_param=raw_param)

    return fields


# ---------------------------------------------------------------------------
# Scenario building
# ---------------------------------------------------------------------------

def normalize_hardware_key(raw: str) -> str:
    """Normalize hardware string: 'H100-SXM-80GB' -> 'H100_SXM_80GB'."""
    return re.sub(r"[-\s]", "_", raw.strip())


def derive_scenario_name(config_path: str, override: str | None = None) -> str:
    """Derive scenario name from folder basename, sanitized."""
    if override:
        sanitized = re.sub(r"[^a-z0-9]", "", override.lower())
        return sanitized[:20]
    folder = Path(config_path).resolve().parent.name
    sanitized = re.sub(r"[^a-z0-9]", "", folder.lower())
    return sanitized[:20]


def build_additional_flags(
    fields: dict[str, ProvenanceValue],
) -> list[tuple[str, str]]:
    """Build additionalFlags list with provenance. Returns (flag, source) tuples."""
    flags = []

    if "max_num_seqs" in fields:
        f = fields["max_num_seqs"]
        flags.append((f"--max-num-seqs={f.value}", f.source))

    if "max_num_batched_tokens" in fields:
        f = fields["max_num_batched_tokens"]
        flags.append((f"--max-num-batched-tokens={f.value}", f.source))

    if "enable_chunked_prefill" in fields and fields["enable_chunked_prefill"].value:
        f = fields["enable_chunked_prefill"]
        flags.append(("--enable-chunked-prefill", f.source))

    # Always emit --no-enable-prefix-caching UNLESS explicitly True
    epc = fields.get("enable_prefix_caching")
    if epc is None:
        flags.append(("--no-enable-prefix-caching", "default (not specified in config.md)"))
    elif not epc.value:
        flags.append(("--no-enable-prefix-caching", epc.source))
    # If epc.value is True: don't emit the flag (caching enabled)

    if "dtype" in fields and fields["dtype"].value != "auto":
        f = fields["dtype"]
        flags.append((f"--dtype={f.value}", f.source))

    if "swap_space" in fields and fields["swap_space"].value != 4:
        f = fields["swap_space"]
        flags.append((f"--swap-space={f.value}", f.source))

    if "pipeline_parallel_size" in fields and fields["pipeline_parallel_size"].value > 1:
        f = fields["pipeline_parallel_size"]
        flags.append((f"--pipeline-parallel-size={f.value}", f.source))

    return flags


def build_scenario(
    fields: dict[str, ProvenanceValue], name: str
) -> tuple[dict, dict[str, str]]:
    """Build scenario dict and provenance map from extracted fields."""
    # --- Validate required fields ---
    if "model" not in fields:
        print("ERROR: required field 'model' not found in config.md", file=sys.stderr)
        sys.exit(1)
    if "hardware" not in fields:
        print("ERROR: required field 'hardware' (GPU) not found in config.md", file=sys.stderr)
        sys.exit(1)

    model_name = fields["model"].value
    hardware_raw = fields["hardware"].value
    hardware_key = normalize_hardware_key(hardware_raw)

    # --- Model metadata ---
    meta = MODEL_METADATA.get(model_name)
    if meta is None:
        print(f"  warning: model '{model_name}' not in MODEL_METADATA, deriving values", file=sys.stderr)
        short_name = model_name.replace("/", "-").lower()
        model_path = f"models/{model_name}"
        size = "1Ti"
        # max_model_len must come from config.md
        if "max_model_len" not in fields:
            print(f"ERROR: model '{model_name}' not in lookup table and max_model_len not in config.md", file=sys.stderr)
            sys.exit(1)
        max_model_len = int(fields["max_model_len"].value)
        meta_source = "derived (model not in lookup table)"
    else:
        short_name = meta["shortName"]
        model_path = meta["path"]
        size = meta["size"]
        max_model_len = meta["maxModelLen"]
        meta_source = f'lookup: MODEL_METADATA["{model_name}"]'

    # Override max_model_len from config.md if present
    if "max_model_len" in fields:
        max_model_len = int(fields["max_model_len"].value)
        max_model_len_source = fields["max_model_len"].source
    else:
        max_model_len_source = meta_source + ".maxModelLen"

    # --- Hardware ---
    hw_label = HARDWARE_LABELS.get(hardware_key)
    if hw_label is None:
        print(f"  warning: hardware '{hardware_key}' not in HARDWARE_LABELS", file=sys.stderr)
        hw_label = f"NVIDIA-{hardware_key}"
        hw_source = f"best-effort ('{hardware_key}' not in lookup table)"
    else:
        hw_source = f'lookup: HARDWARE_LABELS["{hardware_key}"]'

    # --- Numeric fields with defaults ---
    def get_int(field_name, default, default_source="default (not in config.md)"):
        if field_name in fields:
            return int(fields[field_name].value), fields[field_name].source
        return default, default_source

    def get_float(field_name, default, default_source="default (not in config.md)"):
        if field_name in fields:
            return float(fields[field_name].value), fields[field_name].source
        return default, default_source

    replicas, replicas_source = get_int("replicas", 1)
    block_size, block_size_source = get_int("block_size", 16)
    gpu_mem, gpu_mem_source = get_float("gpu_memory_utilization", 0.9)
    tp, tp_source = get_int("tensor_parallel_size", 1)
    dp, dp_source = get_int("data_parallel_size", 1)

    # --- Build scenario dict ---
    scenario = {"name": name}

    scenario["model"] = {
        "name": model_name,
        "shortName": short_name,
        "path": model_path,
        "huggingfaceId": model_name,
        "size": size,
        "maxModelLen": max_model_len,
        "blockSize": block_size,
        "gpuMemoryUtilization": gpu_mem,
    }

    decode = {"replicas": replicas}
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

    additional_flags = build_additional_flags(fields)
    if additional_flags:
        decode["vllm"] = {"additionalFlags": additional_flags}

    # enforce_eager: defaults.yaml sets true; only emit override if false
    if "enforce_eager" in fields and not fields["enforce_eager"].value:
        scenario["vllmCommon"] = {"flags": {"enforceEager": False}}

    scenario["decode"] = decode

    # --- Build provenance map ---
    provenance = {
        "model.name": fields["model"].source,
        "model.shortName": meta_source + ".shortName" if meta else "derived from model name",
        "model.path": meta_source + ".path" if meta else "derived from model name",
        "model.huggingfaceId": fields["model"].source,
        "model.size": meta_source + ".size" if meta else "default estimate",
        "model.maxModelLen": max_model_len_source,
        "model.blockSize": block_size_source,
        "model.gpuMemoryUtilization": gpu_mem_source,
        "decode.replicas": replicas_source,
        "decode.acceleratorType.labelValue": hw_source,
    }

    if tp > 1 or dp > 1:
        provenance["decode.parallelism.tensor"] = tp_source
        provenance["decode.parallelism.data"] = dp_source

    return scenario, provenance


# ---------------------------------------------------------------------------
# YAML output with provenance comments
# ---------------------------------------------------------------------------

def write_provenance_yaml(
    scenario: dict, provenance: dict[str, str], out_path: str, dry_run: bool = False
):
    """Write scenario YAML with inline provenance comments."""
    lines = []
    lines.append("scenario:")
    lines.append(f"- name: {scenario['name']}")
    lines.append("")
    lines.append("  model:")
    lines.append(f"    name: {scenario['model']['name']}  # {provenance['model.name']}")
    lines.append(f"    shortName: {scenario['model']['shortName']}  # {provenance['model.shortName']}")
    lines.append(f"    path: {scenario['model']['path']}  # {provenance['model.path']}")
    lines.append(f"    huggingfaceId: {scenario['model']['huggingfaceId']}  # {provenance['model.huggingfaceId']}")
    lines.append(f"    size: {scenario['model']['size']}  # {provenance['model.size']}")
    lines.append(f"    maxModelLen: {scenario['model']['maxModelLen']}  # {provenance['model.maxModelLen']}")
    lines.append(f"    blockSize: {scenario['model']['blockSize']}  # {provenance['model.blockSize']}")
    lines.append(f"    gpuMemoryUtilization: {scenario['model']['gpuMemoryUtilization']}  # {provenance['model.gpuMemoryUtilization']}")

    if "vllmCommon" in scenario:
        source = "config.md row \"enforce_eager\""
        if "enforce_eager" in provenance:
            source = provenance["enforce_eager"]
        lines.append("")
        lines.append("  vllmCommon:")
        lines.append("    flags:")
        lines.append(f"      enforceEager: false  # {source}")

    lines.append("")
    lines.append("  decode:")
    lines.append(f"    replicas: {scenario['decode']['replicas']}  # {provenance['decode.replicas']}")
    lines.append("    acceleratorType:")
    lines.append("      labelKey: nvidia.com/gpu.product")
    lines.append(f"      labelValue: {scenario['decode']['acceleratorType']['labelValue']}  # {provenance['decode.acceleratorType.labelValue']}")

    if "parallelism" in scenario["decode"]:
        p = scenario["decode"]["parallelism"]
        lines.append("    parallelism:")
        lines.append(f"      data: {p['data']}  # {provenance['decode.parallelism.data']}")
        lines.append(f"      dataLocal: {p['dataLocal']}  # {provenance['decode.parallelism.data']}")
        lines.append(f"      tensor: {p['tensor']}  # {provenance['decode.parallelism.tensor']}")
        lines.append(f"      workers: {p['workers']}  # {provenance['decode.parallelism.tensor']}")

    if "vllm" in scenario["decode"]:
        lines.append("    vllm:")
        lines.append("      additionalFlags:")
        for flag, source in scenario["decode"]["vllm"]["additionalFlags"]:
            lines.append(f'      - "{flag}"  # {source}')

    lines.append("")

    output = "\n".join(lines) + "\n"

    if dry_run:
        print(output)
    else:
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        with open(out_path, "w") as f:
            f.write(output)
        print(f"  wrote {out_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Generate baseline scenario YAML from config.md"
    )
    parser.add_argument(
        "config", nargs="?", default="./config.md", help="Path to config.md"
    )
    parser.add_argument(
        "-o", "--output-dir", default="./baselines", help="Output directory"
    )
    parser.add_argument(
        "-n", "--name", help="Override scenario name (default: derived from folder)"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Print YAML to stdout, don't write file"
    )
    args = parser.parse_args()

    config_path = args.config
    if not os.path.isfile(config_path):
        print(f"ERROR: config file not found: {config_path}", file=sys.stderr)
        sys.exit(1)

    print(f"Reading: {config_path}")

    with open(config_path) as f:
        lines = f.readlines()

    # Parse all tables
    tables = parse_md_tables(lines)
    if not tables:
        print("ERROR: no markdown tables found in config.md", file=sys.stderr)
        sys.exit(1)

    # Find the vLLM configuration table
    vllm_table = find_vllm_table(tables)
    if vllm_table is None:
        print("ERROR: could not find vLLM configuration table in config.md", file=sys.stderr)
        print(f"  searched {len(tables)} table(s) with headings: {[t.heading for t in tables]}", file=sys.stderr)
        sys.exit(1)

    print(f"  found table under: \"{vllm_table.heading}\" ({len(vllm_table.rows)} rows)")

    # Extract fields
    fields = extract_fields(vllm_table)
    if not fields:
        print("ERROR: no recognized fields extracted from table", file=sys.stderr)
        sys.exit(1)

    print(f"  extracted {len(fields)} field(s): {list(fields.keys())}")

    # Derive scenario name
    scenario_name = derive_scenario_name(config_path, args.name)
    print(f"  scenario name: {scenario_name}")

    # Build scenario
    scenario, provenance = build_scenario(fields, scenario_name)

    # Write output
    out_path = os.path.join(args.output_dir, f"{scenario_name}.yaml")
    write_provenance_yaml(scenario, provenance, out_path, dry_run=args.dry_run)

    # Validate output parses as YAML
    if not args.dry_run:
        try:
            import yaml
            with open(out_path) as f:
                yaml.safe_load(f)
            print("  validated: output is valid YAML")
        except ImportError:
            print("  note: PyYAML not available, skipping validation")
        except Exception as e:
            print(f"  WARNING: output YAML validation failed: {e}", file=sys.stderr)

    print("Done.")


if __name__ == "__main__":
    main()
