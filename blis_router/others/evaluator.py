"""
BLIS Router Evaluator for SkyDiscover

Evaluates evolved routing algorithms by:
1. Extracting Go code from Python wrapper
2. Writing evolved routing.go to BLIS source (restored after evaluation)
3. Building BLIS
4. Running simulations on N seeds × N LLMs × 2 workloads (default: 2 seeds × 2 LLMs × 2 = 8 sims)
5. Computing score based on baseline-normalized per-workload ratios

Score = mean(1 - candidate/baseline) × 100  (percentage improvement over baseline)
Each workload contributes equally regardless of absolute latency magnitude.
Higher is better; 0 = baseline parity, +20 = 20% faster, -10 = 10% slower.

Multi-seed evaluation (default: seeds 42 and 456):
  Every candidate is tested against multiple simulation seeds to ensure
  robustness. Seed 42 is the normal case; seed 456 exposes baseline degradation
  under bursty traffic (load-balance scorer saturates). Set BLIS_SEED=42 for
  single-seed backward compatibility, or BLIS_SEED=42,456 for explicit multi-seed.

Multi-LLM mode (default ON, disable with BLIS_MULTI_LLM=0):
  Runs each workload against multiple LLM model configurations (qwen_7b + qwen_14b)
  to test generalizability. The combined score averages across all seeds, models,
  and workloads. Baseline metrics include per-model breakdowns.

Experiment isolation:
- routing.go is saved before and restored after every evaluation
- baseline_metrics.json is written to BLIS_OUTPUT_DIR (not benchmark dir)
- No artifacts are left in the benchmark directory between evaluations

NOTE: This evaluator is NOT safe for concurrent execution.
Multiple evaluate() calls MUST be serialized because they share routing.go.
"""

import json
import logging
import os
import re
import subprocess
import sys
import traceback
from difflib import unified_diff
from pathlib import Path

logger = logging.getLogger(__name__)

WORKLOADS = [
    ("glia_40qps", "workload_glia_40qps.yaml"),
    ("prefix_heavy", "workload_glia_prefix_heavy.yaml"),
]

# Model configs: (short_name, model_id, extra CLI args)
_MODEL_QWEN_7B = ("qwen_7b", "qwen/qwen2.5-7b-instruct", [
    "--hardware", "H100", "--tp", "1",
])
_MODEL_QWEN_14B = ("qwen_14b", "qwen/qwen3-14b", [
    "--hardware", "H100", "--tp", "1",
])

_DEFAULT_SEEDS = ["42", "456"]
SIM_SEEDS = (
    os.environ.get("BLIS_SEED", "").split(",")
    if os.environ.get("BLIS_SEED")
    else _DEFAULT_SEEDS
)
SIM_NUM_INSTANCES = os.environ.get("BLIS_NUM_INSTANCES", "4")
MULTI_LLM = os.environ.get("BLIS_MULTI_LLM", "0") == "1"
# Snapshot refresh interval in microseconds (5 seconds = realistic Prometheus scrape)
SIM_SNAPSHOT_REFRESH = os.environ.get("BLIS_SNAPSHOT_REFRESH", "5000000")


def _get_models():
    """Return list of model configs to evaluate against."""
    if MULTI_LLM:
        return [_MODEL_QWEN_7B, _MODEL_QWEN_14B]
    return [_MODEL_QWEN_7B]


def _get_output_dir() -> Path:
    """Get the experiment output directory from env, falling back to a temp location."""
    output_dir = os.environ.get("BLIS_OUTPUT_DIR")
    if output_dir:
        return Path(output_dir)
    # Fallback for standalone testing only — warn loudly
    logger.warning(
        "BLIS_OUTPUT_DIR not set. Artifacts will be written to benchmark directory. "
        "Set BLIS_OUTPUT_DIR for experiment runs to ensure isolation."
    )
    fallback = Path(__file__).parent / ".eval_artifacts"
    fallback.mkdir(exist_ok=True)
    return fallback


def _build_sim_cmd(
    inference_sim_dir: Path, policy_config_path: Path, workload_path: Path,
    model_id: str, extra_args: list[str], seed: str,
) -> list[str]:
    return [
        "./simulation_worker", "run",
        "--model", model_id,
        "--num-instances", SIM_NUM_INSTANCES,
        "--policy-config", str(policy_config_path),
        "--workload-spec", str(workload_path),
        "--snapshot-refresh-interval", SIM_SNAPSHOT_REFRESH,
        "--log", "info",
        "--seed", seed,
    ] + extra_args


def _restore_routing_go(routing_go_path: Path, original_content: str):
    """Restore routing.go to its original content. Logs and re-raises on failure."""
    try:
        routing_go_path.write_text(original_content)
    except OSError as exc:
        logger.error(
            "CRITICAL: Failed to restore routing.go after evaluation! "
            "File may be corrupted. Path: %s, Error: %s",
            routing_go_path, exc,
        )
        raise


def extract_evolve_block(code: str) -> str:
    pattern = r"// EVOLVE-BLOCK-START(.*?)// EVOLVE-BLOCK-END"
    match = re.search(pattern, code, re.DOTALL)
    return match.group(1).strip() if match else ""


def print_diff(initial_code: str, current_code: str):
    initial_block = extract_evolve_block(initial_code)
    current_block = extract_evolve_block(current_code)
    if not initial_block or not current_block:
        return
    diff = list(unified_diff(
        initial_block.splitlines(keepends=True),
        current_block.splitlines(keepends=True),
        lineterm="",
    ))
    if not diff:
        logger.info("NO DIFF — code unchanged from initial")
        return
    removed = sum(1 for l in diff if l.startswith("-") and not l.startswith("---"))
    added = sum(1 for l in diff if l.startswith("+") and not l.startswith("+++"))
    logger.info(f"Diff vs initial: -{removed} / +{added} lines")


def _parse_cluster_metrics(output_text: str) -> dict | None:
    json_blocks = []
    in_json = False
    json_buffer = ""
    brace_count = 0
    max_buffer = 1_000_000  # 1MB safety limit
    for line in output_text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("{"):
            in_json = True
            brace_count = 0
        if in_json:
            json_buffer += line + "\n"
            if len(json_buffer) > max_buffer:
                logger.warning("JSON buffer exceeded 1MB limit, resetting")
                json_buffer = ""
                in_json = False
                continue
            brace_count += stripped.count("{") - stripped.count("}")
            if brace_count == 0 and json_buffer.strip():
                try:
                    json_blocks.append(json.loads(json_buffer))
                except json.JSONDecodeError as exc:
                    logger.debug(
                        "Skipped non-JSON block in simulator output (%s): %.200s",
                        exc, json_buffer.strip(),
                    )
                json_buffer = ""
                in_json = False
    for block in json_blocks:
        if block.get("instance_id") == "cluster":
            return block
    return None


def _error_result(error_msg: str, error_type: str, suggestion: str = "", **extra_artifacts) -> dict:
    return {
        "combined_score": -100000.0,
        "avg_e2e_ms": float("inf"),
        "error": error_msg,
        "artifacts": {
            "error_type": error_type,
            "error_message": error_msg,
            "suggestion": suggestion,
            **extra_artifacts,
        },
    }


def _run_workloads(
    models, script_dir: Path, inference_sim_dir: Path, policy_config_path: Path,
    seeds: list[str],
) -> tuple[list[float], list[float], dict, dict]:
    """Run all workloads across all seeds and models.

    Returns (latencies, tail_latencies, workload_results, per_model).
    All results are averaged across seeds × models.
    """
    all_latencies = []
    all_tail_latencies = []
    per_model = {}
    # Aggregate per-workload across seeds and models (for evaluate() return value)
    workload_agg: dict[str, list[dict]] = {wl: [] for wl, _ in WORKLOADS}

    for model_name, model_id, extra_args in models:
        model_latencies = []
        model_tail_latencies = []
        model_workloads: dict[str, list[dict]] = {wl: [] for wl, _ in WORKLOADS}

        for seed in seeds:
            for workload_name, workload_file in WORKLOADS:
                workload_path = script_dir / "workloads" / workload_file
                cmd = _build_sim_cmd(
                    inference_sim_dir, policy_config_path, workload_path,
                    model_id, extra_args, seed=seed,
                )
                seed_tag = f"[seed={seed}]" if len(seeds) > 1 else ""
                model_tag = f"{model_name}/" if len(models) > 1 else ""
                log_key = f"{model_tag}{workload_name}{seed_tag}"
                try:
                    sim_result = subprocess.run(
                        cmd, cwd=inference_sim_dir, capture_output=True, text=True, timeout=120,
                    )
                    if sim_result.returncode != 0:
                        logger.warning("%s failed: %s", log_key, sim_result.stderr[:300])
                        continue
                    output_text = sim_result.stdout + (sim_result.stderr or "")
                    cluster_metrics = _parse_cluster_metrics(output_text)
                    if cluster_metrics and "e2e_mean_ms" in cluster_metrics:
                        e2e_ms = float(cluster_metrics["e2e_mean_ms"])
                        e2e_p95_ms = float(cluster_metrics.get("e2e_p95_ms", e2e_ms))
                        model_latencies.append(e2e_ms)
                        model_tail_latencies.append(e2e_p95_ms)
                        entry = {
                            "e2e_ms": e2e_ms, "e2e_p95_ms": e2e_p95_ms,
                            "completed_requests": cluster_metrics.get("completed_requests"),
                            "ttft_mean_ms": cluster_metrics.get("ttft_mean_ms"),
                            "tokens_per_sec": cluster_metrics.get("tokens_per_sec"),
                        }
                        model_workloads[workload_name].append(entry)
                        workload_agg[workload_name].append(entry)
                        logger.info(f"{log_key}: e2e_mean={e2e_ms:.2f}ms, p95={e2e_p95_ms:.2f}ms")
                    else:
                        logger.warning("%s: no cluster metrics found", log_key)
                except (subprocess.TimeoutExpired, subprocess.SubprocessError, OSError) as exc:
                    logger.warning("%s failed: %s", log_key, exc)
                except Exception as exc:
                    logger.error("Unexpected error in %s: %s", log_key, exc, exc_info=True)

        per_model_entry = {"model_id": model_id, "workloads": {}}
        for wl_name, entries in model_workloads.items():
            if entries:
                avg_e2e = sum(e["e2e_ms"] for e in entries) / len(entries)
                avg_p95 = sum(e["e2e_p95_ms"] for e in entries) / len(entries)
                per_model_entry["workloads"][wl_name] = {
                    "e2e_ms": avg_e2e, "e2e_p95_ms": avg_p95,
                    "completed_requests": entries[0].get("completed_requests"),
                    "ttft_mean_ms": entries[0].get("ttft_mean_ms"),
                    "tokens_per_sec": entries[0].get("tokens_per_sec"),
                }
            else:
                per_model_entry["workloads"][wl_name] = {"e2e_ms": None, "error": "All seeds failed"}
        if model_latencies:
            per_model_entry["avg_e2e_ms"] = sum(model_latencies) / len(model_latencies)
            per_model_entry["avg_p95_ms"] = sum(model_tail_latencies) / len(model_tail_latencies)
        per_model[model_name] = per_model_entry
        all_latencies.extend(model_latencies)
        all_tail_latencies.extend(model_tail_latencies)

    # Build per-workload aggregate (average across seeds and models)
    workload_results = {}
    for workload_name, entries in workload_agg.items():
        if entries:
            avg_e2e = sum(e["e2e_ms"] for e in entries) / len(entries)
            avg_p95 = sum(e["e2e_p95_ms"] for e in entries) / len(entries)
            workload_results[workload_name] = {
                "e2e_ms": avg_e2e, "e2e_p95_ms": avg_p95,
                "completed_requests": entries[0].get("completed_requests"),
                "ttft_mean_ms": entries[0].get("ttft_mean_ms"),
                "tokens_per_sec": entries[0].get("tokens_per_sec"),
            }
        else:
            workload_results[workload_name] = {"e2e_ms": None, "error": "All seeds/models failed"}

    return all_latencies, all_tail_latencies, workload_results, per_model


def get_or_compute_baseline(
    script_dir: Path, inference_sim_dir: Path, policy_config_path: Path
) -> dict:
    """Compute baseline metrics, caching in the experiment output directory."""
    output_dir = _get_output_dir()
    cache_path = output_dir / "baseline_metrics.json"

    # Try to load from cache; recompute if seeds changed
    try:
        with open(cache_path, "r") as f:
            cached = json.load(f)
        cached_seeds = cached.get("seeds")
        if cached_seeds == sorted(SIM_SEEDS):
            return cached
        logger.info("Baseline seeds changed (%s -> %s), recomputing", cached_seeds, sorted(SIM_SEEDS))
    except FileNotFoundError:
        pass  # No cache yet, will compute below
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to read baseline cache, recomputing: %s", exc)

    # Try .go first (native Go), fall back to .py (legacy Python wrapper)
    initial_program_path = script_dir / "initial_program.go"
    if not initial_program_path.exists():
        initial_program_path = script_dir / "initial_program.py"
    if not initial_program_path.exists():
        logger.warning("initial_program not found; cannot compute baseline")
        return {}

    with open(initial_program_path, "r") as f:
        initial_text = f.read()

    go_code = extract_go_code(initial_text)
    if not go_code:
        logger.warning("Could not extract Go code from initial program for baseline")
        return {}

    routing_go_path = inference_sim_dir / "sim" / "routing.go"
    if not routing_go_path.exists():
        logger.warning("routing.go not found at %s; initialize inference-sim submodule", routing_go_path)
        return {}

    # Save original routing.go, restore after baseline computation
    original_routing = routing_go_path.read_text()
    try:
        routing_go_path.write_text(go_code)

        build_result = subprocess.run(
            ["go", "build", "-o", "simulation_worker", "main.go"],
            cwd=inference_sim_dir, capture_output=True, text=True, timeout=60,
        )
        if build_result.returncode != 0:
            logger.warning("Baseline build failed: %s", build_result.stderr[:300])
            return {}

        models = _get_models()
        all_latencies, all_tail_latencies, workload_results, per_model = _run_workloads(
            models, script_dir, inference_sim_dir, policy_config_path,
            seeds=SIM_SEEDS,
        )

        baseline = {"seeds": sorted(SIM_SEEDS)}
        # Per-workload (averaged across seeds and models)
        for wl_name, wl_data in workload_results.items():
            if wl_data.get("e2e_ms") is not None:
                baseline[f"{wl_name}_e2e_ms"] = wl_data["e2e_ms"]
            if wl_data.get("e2e_p95_ms") is not None:
                baseline[f"{wl_name}_p95_ms"] = wl_data["e2e_p95_ms"]

        if all_latencies:
            avg_e2e = sum(all_latencies) / len(all_latencies)
            avg_p95 = sum(all_tail_latencies) / len(all_tail_latencies)
            baseline["avg_e2e_ms"] = avg_e2e
            baseline["avg_p95_ms"] = avg_p95
            baseline["combined_score"] = -0.5 * avg_e2e - 0.5 * avg_p95

        # Per-model breakdown (only in baseline, not in evaluate return)
        if len(models) > 1:
            baseline["per_model"] = per_model

        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            with open(cache_path, "w") as f:
                json.dump(baseline, f, indent=2)
            logger.info("Cached baseline metrics to %s", cache_path)
        except OSError as exc:
            logger.warning("Failed to cache baseline metrics: %s", exc)

        return baseline

    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("Baseline computation error: %s", exc)
        return {}
    finally:
        _restore_routing_go(routing_go_path, original_routing)


def extract_go_code(program_text: str) -> str:
    match = re.search(r'GO_ROUTING_CODE\s*=\s*"""(.*?)"""', program_text, re.DOTALL)
    if match:
        return match.group(1).strip()
    if "package sim" in program_text:
        return program_text
    return ""


def evaluate(program_path: str) -> dict:
    try:
        with open(program_path, "r") as f:
            program_text = f.read()
    except (OSError, IOError) as exc:
        return _error_result(
            f"Cannot read program file: {exc}", "FileReadError",
            f"Check that {program_path} exists and is readable",
        )

    script_dir = Path(__file__).parent
    inference_sim_dir = script_dir / "inference-sim"
    routing_go_path = inference_sim_dir / "sim" / "routing.go"
    policy_config_path = script_dir / "routing" / "routing_policy.yaml"

    # Verify routing.go exists (submodule must be initialized)
    if not routing_go_path.exists():
        return _error_result(
            f"routing.go not found at {routing_go_path}",
            "SetupError",
            "Initialize the inference-sim submodule: git submodule update --init",
        )

    go_code = extract_go_code(program_text)
    if not go_code:
        return _error_result(
            "Failed to extract Go code", "ExtractionError",
            'Ensure program contains GO_ROUTING_CODE = """...""" or starts with "package sim"',
        )

    logger.info(f"Extracted Go code: {len(go_code)} chars")

    try:
        initial_program_path = script_dir / "initial_program.go"
        if not initial_program_path.exists():
            initial_program_path = script_dir / "initial_program.py"
        if initial_program_path.exists():
            with open(initial_program_path, "r") as f:
                initial_go_code = extract_go_code(f.read())
            if initial_go_code:
                print_diff(initial_go_code, go_code)
    except Exception as exc:
        logger.warning("Failed to compute diff against initial program (non-fatal): %s", exc)

    baseline = get_or_compute_baseline(script_dir, inference_sim_dir, policy_config_path)
    if not baseline:
        logger.warning("Baseline metrics unavailable; score comparison will not be possible")

    # Save original routing.go before mutation
    original_routing = routing_go_path.read_text()

    try:
        try:
            routing_go_path.write_text(go_code)
        except Exception as e:
            return _error_result(
                f"Failed to write file: {e}", "FileWriteError",
                full_traceback=traceback.format_exc(),
            )

        try:
            result = subprocess.run(
                ["go", "build", "-o", "simulation_worker", "main.go"],
                cwd=inference_sim_dir, capture_output=True, text=True, timeout=60,
            )
            if result.returncode != 0:
                error_summary = result.stderr.strip()[:500] if result.stderr else "Unknown build error"
                return _error_result(
                    f"Build failed: {error_summary}", "BuildError",
                    "Check for Go syntax errors in the evolved EVOLVE-BLOCK section",
                    build_stderr=result.stderr,
                )
        except subprocess.TimeoutExpired:
            return _error_result("Build timeout (60s)", "BuildTimeout")
        except Exception as e:
            return _error_result(
                f"Build error: {e}", type(e).__name__,
                full_traceback=traceback.format_exc(),
            )

        models = _get_models()
        all_latencies, all_tail_latencies, workload_results, _ = _run_workloads(
            models, script_dir, inference_sim_dir, policy_config_path,
            seeds=SIM_SEEDS,
        )

        failed_workloads = [wl for wl, d in workload_results.items() if d.get("e2e_ms") is None]

        if len(all_latencies) == 0:
            return _error_result(
                "All workloads failed", "AllWorkloadsFailed",
                "Check BLIS simulation errors. May be routing logic causing crashes or timeouts.",
                failed_workloads=failed_workloads, workload_results=workload_results,
            )

        avg_latency = sum(all_latencies) / len(all_latencies)
        avg_tail_latency = sum(all_tail_latencies) / len(all_tail_latencies)

        # Baseline-normalized scoring: each workload contributes equally
        # regardless of absolute latency magnitude.
        # Per-workload improvement = 1 - (candidate_latency / baseline_latency)
        # Score = mean(improvements) × 100  (percentage points)
        # Baseline = 0, positive = better than baseline, negative = worse.
        # Example: 20% faster on both workloads → score = +20.0
        improvements = []
        for wl_name, _ in WORKLOADS:
            wl_data = workload_results.get(wl_name, {})
            wl_e2e = wl_data.get("e2e_ms")
            wl_p95 = wl_data.get("e2e_p95_ms")
            bl_e2e = baseline.get(f"{wl_name}_e2e_ms")
            bl_p95 = baseline.get(f"{wl_name}_p95_ms")
            if wl_e2e is not None and bl_e2e and bl_e2e > 0:
                if wl_p95 is not None and bl_p95 and bl_p95 > 0:
                    candidate_val = 0.5 * wl_e2e + 0.5 * wl_p95
                    baseline_val = 0.5 * bl_e2e + 0.5 * bl_p95
                else:
                    candidate_val = wl_e2e
                    baseline_val = bl_e2e
                improvements.append(1.0 - candidate_val / baseline_val)

        if improvements:
            score = sum(improvements) / len(improvements) * 100.0
        else:
            # Fallback to raw scoring if baseline unavailable
            score = -0.5 * avg_latency - 0.5 * avg_tail_latency
        total_runs = len(SIM_SEEDS) * len(models) * len(WORKLOADS)
        num_successful = len(all_latencies)
        success_rate = num_successful / total_runs

        return {
            "combined_score": score,
            "avg_e2e_ms": avg_latency,
            "avg_p95_ms": avg_tail_latency,
            **{f"{wk}_e2e_ms": workload_results.get(wk, {}).get("e2e_ms")
               for wk, _ in WORKLOADS},
            "success_rate": success_rate,
            "num_successful": num_successful,
            "num_failed": len(failed_workloads),
            "artifacts": {
                "workload_results": workload_results,
                "successful_workloads": num_successful,
                "failed_workloads": len(failed_workloads),
                "success_rate": f"{success_rate:.0%}",
                **({"warning": f"Some workloads failed: {', '.join(failed_workloads)}",
                    "suggestion": "Check if evolved routing logic causes crashes or extreme slowdowns"}
                   if failed_workloads else {}),
            },
        }

    finally:
        # Always restore original routing.go — no state leaks between evaluations
        _restore_routing_go(routing_go_path, original_routing)


if __name__ == "__main__":
    print(f"Testing evaluator with initial program (seeds={SIM_SEEDS})...")
    script_dir = Path(__file__).parent
    prog = script_dir / "initial_program.go"
    if not prog.exists():
        prog = script_dir / "initial_program.py"
    result = evaluate(str(prog))
    if "error" in result:
        print(f"  ERROR: {result['error']}")
        print(f"  Suggestion: {result.get('artifacts', {}).get('suggestion', 'N/A')}")
        sys.exit(1)
    score = result.get("combined_score")
    avg_e2e = result.get("avg_e2e_ms")
    print(f"  Score: {score:.2f}" if score is not None else "  Score: N/A")
    print(f"  Avg E2E: {avg_e2e:.2f}ms" if avg_e2e and avg_e2e != float("inf") else "  Avg E2E: N/A")
