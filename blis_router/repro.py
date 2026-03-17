#!/usr/bin/env python3
"""
BLIS Router Sim2Real Reproduction Script

Runs each routing program (baselines + evolved best) against both workloads
and prints a comparison table.

Usage:
    cd sim2real/blis_router
    python repro.py [--seeds 42,456] [--num-instances 4]

Prerequisites:
    - inference-sim cloned into this directory (see README.md)
    - Go installed (go build must work)
"""

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths (all relative to this directory — fully self-contained)
# ---------------------------------------------------------------------------
SIM2REAL_DIR = Path(__file__).parent
INFERENCE_SIM_DIR = SIM2REAL_DIR / "inference-sim"
ROUTING_GO_PATH = INFERENCE_SIM_DIR / "sim" / "routing.go"
POLICY_CONFIG = SIM2REAL_DIR / "routing_config" / "routing_policy.yaml"
WORKLOADS_DIR = SIM2REAL_DIR / "workloads"

# Programs to evaluate: (display_name, path_to_go_file, is_control)
PROGRAMS = [
    ("LLQ",          SIM2REAL_DIR / "baselines" / "baseline_llq.go",  False),
    ("Glia",         SIM2REAL_DIR / "baselines" / "baseline_glia.go", False),
    ("1:1 (default)",SIM2REAL_DIR / "baselines" / "baseline_1_1.go",  True),
    ("Evolved (best)",SIM2REAL_DIR / "best" / "best_program.go",      False),
]

# Workloads: (display_name, yaml_file)
WORKLOADS = [
    ("glia_40qps",    "workload_glia_40qps.yaml"),
    ("prefix_heavy",  "workload_glia_prefix_heavy.yaml"),
]

MODEL_ID = "qwen/qwen2.5-7b-instruct"
MODEL_EXTRA_ARGS = ["--hardware", "H100", "--tp", "1"]
SNAPSHOT_REFRESH = "5000000"  # 5 seconds in microseconds


# ---------------------------------------------------------------------------
# Simulation helpers
# ---------------------------------------------------------------------------

def build_sim(routing_go_src: Path) -> bool:
    """Copy routing.go and build simulation_worker. Returns True on success."""
    shutil.copy2(routing_go_src, ROUTING_GO_PATH)
    result = subprocess.run(
        ["go", "build", "-o", "simulation_worker", "main.go"],
        cwd=INFERENCE_SIM_DIR, capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        print(f"  [BUILD ERROR] {result.stderr.strip()[:200]}", file=sys.stderr)
        return False
    return True


def run_sim(workload_yaml: Path, seed: str, num_instances: str) -> dict | None:
    """Run a single simulation, return cluster metrics dict or None on failure."""
    cmd = [
        "./simulation_worker", "run",
        "--model", MODEL_ID,
        "--num-instances", num_instances,
        "--policy-config", str(POLICY_CONFIG),
        "--workload-spec", str(workload_yaml),
        "--snapshot-refresh-interval", SNAPSHOT_REFRESH,
        "--log", "info",
        "--seed", seed,
    ] + MODEL_EXTRA_ARGS

    try:
        result = subprocess.run(
            cmd, cwd=INFERENCE_SIM_DIR, capture_output=True, text=True, timeout=120,
        )
    except subprocess.TimeoutExpired:
        print(f"  [TIMEOUT] seed={seed}", file=sys.stderr)
        return None

    if result.returncode != 0:
        print(f"  [SIM ERROR rc={result.returncode}] {result.stderr.strip()[:200]}", file=sys.stderr)
        return None

    return _parse_cluster_metrics(result.stdout + result.stderr)


def _parse_cluster_metrics(output: str) -> dict | None:
    """Extract the cluster-level JSON block from simulator output."""
    in_json = False
    buf = ""
    brace_count = 0
    for line in output.split("\n"):
        stripped = line.strip()
        if stripped.startswith("{"):
            in_json = True
            brace_count = 0
        if in_json:
            buf += line + "\n"
            brace_count += stripped.count("{") - stripped.count("}")
            if brace_count == 0 and buf.strip():
                try:
                    block = json.loads(buf)
                    if block.get("instance_id") == "cluster":
                        return block
                except json.JSONDecodeError:
                    pass
                buf = ""
                in_json = False
    return None


# ---------------------------------------------------------------------------
# Main evaluation loop
# ---------------------------------------------------------------------------

def evaluate_program(
    name: str, routing_go: Path, seeds: list[str], num_instances: str,
) -> dict | None:
    """Evaluate one program across all workloads × seeds. Returns per-workload averages."""
    original = ROUTING_GO_PATH.read_text()
    try:
        if not build_sim(routing_go):
            return None

        results = {}
        for wl_name, wl_file in WORKLOADS:
            wl_path = WORKLOADS_DIR / wl_file
            e2e_list, p95_list = [], []
            for seed in seeds:
                metrics = run_sim(wl_path, seed, num_instances)
                if metrics:
                    e2e_list.append(float(metrics.get("e2e_mean_ms", 0)))
                    p95_list.append(float(metrics.get("e2e_p95_ms", 0)))
                else:
                    print(f"  [WARN] {name} / {wl_name} / seed={seed}: no metrics", file=sys.stderr)

            if e2e_list:
                results[wl_name] = {
                    "e2e_ms": sum(e2e_list) / len(e2e_list),
                    "p95_ms": sum(p95_list) / len(p95_list),
                }
            else:
                results[wl_name] = None
        return results
    finally:
        ROUTING_GO_PATH.write_text(original)


# ---------------------------------------------------------------------------
# Scoring (matches evaluator.py formula)
# ---------------------------------------------------------------------------

def compute_improvement_vs_control(program_results: dict, control_results: dict) -> float | None:
    """Compute combined % improvement vs the control (1:1) program."""
    improvements = []
    for wl_name, _ in WORKLOADS:
        cand = program_results.get(wl_name)
        ctrl = control_results.get(wl_name)
        if cand is None or ctrl is None:
            continue
        cand_val = 0.5 * cand["e2e_ms"] + 0.5 * cand["p95_ms"]
        ctrl_val = 0.5 * ctrl["e2e_ms"] + 0.5 * ctrl["p95_ms"]
        if ctrl_val > 0:
            improvements.append(1.0 - cand_val / ctrl_val)
    if not improvements:
        return None
    return sum(improvements) / len(improvements) * 100.0


# ---------------------------------------------------------------------------
# Table printing
# ---------------------------------------------------------------------------

def fmt_ms(v: float | None) -> str:
    return f"{v:>8.0f} ms" if v is not None else "   N/A    "


def fmt_pct(v: float | None) -> str:
    if v is None:
        return "   N/A  "
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:>+6.1f}%"


def print_table(all_results: dict, seeds: list[str]):
    wl_names = [wl for wl, _ in WORKLOADS]
    col_w = 20

    sep = "-" * (col_w + 2 + len(wl_names) * 26 + 10)
    header_seeds = "+".join(seeds)

    print()
    print("=" * len(sep))
    print(f"BLIS Router Repro  —  model: {MODEL_ID}  |  seeds: {header_seeds}")
    print("=" * len(sep))
    print()

    # Header row
    row = f"{'Program':<{col_w}}"
    for wl in wl_names:
        row += f"  {'E2E':>8}  {'P95':>8}  "
    row += f"  {'vs 1:1':>8}"
    print(row)

    sub = f"{'':<{col_w}}"
    for wl in wl_names:
        sub += f"  {('[' + wl + ']'):>20}  "
    print(sub)
    print(sep)

    # Find control results
    ctrl_results = None
    for prog_name, _, is_control in PROGRAMS:
        if is_control:
            ctrl_results = all_results.get(prog_name)
            break

    for prog_name, _, is_control in PROGRAMS:
        res = all_results.get(prog_name)
        marker = " *" if is_control else "  "
        row = f"{prog_name + marker:<{col_w}}"
        if res is None:
            row += "  [FAILED]"
        else:
            for wl in wl_names:
                wl_data = res.get(wl)
                e2e = wl_data["e2e_ms"] if wl_data else None
                p95 = wl_data["p95_ms"] if wl_data else None
                row += f"  {fmt_ms(e2e)}  {fmt_ms(p95)}  "
            if ctrl_results and not is_control:
                imp = compute_improvement_vs_control(res, ctrl_results)
                row += f"  {fmt_pct(imp)}"
            else:
                row += f"  {'(control)':>8}"
        print(row)

    print(sep)
    print("* = control (1:1 baseline). vs 1:1 = combined improvement using scorer formula.")
    print("  scorer: val = 0.5*E2E + 0.5*P95, improvement = (1 - evolved/baseline) * 100")
    print(f"  positive = better than 1:1, negative = worse")
    print()

    # Per-workload detail table
    print("Per-workload detail:")
    print(f"{'Program':<{col_w}}  {'glia_40qps E2E':>14}  {'glia_40qps P95':>14}  {'prefix_heavy E2E':>16}  {'prefix_heavy P95':>16}")
    print("-" * (col_w + 70))
    for prog_name, _, _ in PROGRAMS:
        res = all_results.get(prog_name)
        if res is None:
            print(f"{prog_name:<{col_w}}  FAILED")
            continue
        parts = []
        for wl in wl_names:
            wl_data = res.get(wl)
            parts.append(fmt_ms(wl_data["e2e_ms"] if wl_data else None))
            parts.append(fmt_ms(wl_data["p95_ms"] if wl_data else None))
        print(f"{prog_name:<{col_w}}  {'  '.join(parts)}")
    print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="BLIS Router Sim2Real Repro")
    parser.add_argument("--seeds", default="42,456",
                        help="Comma-separated simulation seeds (default: 42,456)")
    parser.add_argument("--num-instances", default="4",
                        help="Number of simulated LLM instances (default: 4)")
    parser.add_argument("--programs", default="all",
                        help="Comma-separated program names to run, or 'all' (default: all). "
                             "E.g. '1:1 (default),Evolved (best)'")
    args = parser.parse_args()

    seeds = [s.strip() for s in args.seeds.split(",") if s.strip()]

    # Validate prerequisites
    if not INFERENCE_SIM_DIR.exists():
        print(f"ERROR: inference-sim not found at {INFERENCE_SIM_DIR}", file=sys.stderr)
        print("  Run: git clone https://github.com/inference-sim/inference-sim.git inference-sim", file=sys.stderr)
        print("       git -C inference-sim checkout 7fd7a88d5d5005b15b142fa8e70cf5d8537ceebe", file=sys.stderr)
        sys.exit(1)
    if not ROUTING_GO_PATH.exists():
        print(f"ERROR: routing.go not found at {ROUTING_GO_PATH}", file=sys.stderr)
        sys.exit(1)
    if not WORKLOADS_DIR.exists():
        print(f"ERROR: workloads not found at {WORKLOADS_DIR}", file=sys.stderr)
        sys.exit(1)

    # Filter programs if requested
    programs_to_run = PROGRAMS
    if args.programs != "all":
        requested = {p.strip() for p in args.programs.split(",")}
        programs_to_run = [(n, p, c) for n, p, c in PROGRAMS if n in requested]
        if not programs_to_run:
            print(f"ERROR: no programs matched '{args.programs}'", file=sys.stderr)
            sys.exit(1)

    print(f"BLIS Router Repro")
    print(f"  seeds={seeds}  num_instances={args.num_instances}")
    print(f"  inference-sim: {INFERENCE_SIM_DIR}")
    print(f"  workloads: {WORKLOADS_DIR}")
    print()

    all_results = {}
    for prog_name, prog_path, is_control in programs_to_run:
        marker = " [control]" if is_control else ""
        print(f"Evaluating: {prog_name}{marker}  ({prog_path.name})")
        if not prog_path.exists():
            print(f"  [SKIP] file not found: {prog_path}", file=sys.stderr)
            all_results[prog_name] = None
            continue
        results = evaluate_program(prog_name, prog_path, seeds, args.num_instances)
        all_results[prog_name] = results
        if results:
            for wl, wl_data in results.items():
                if wl_data:
                    print(f"  {wl}: e2e={wl_data['e2e_ms']:.1f}ms  p95={wl_data['p95_ms']:.1f}ms")
        print()

    print_table(all_results, seeds)


if __name__ == "__main__":
    main()
