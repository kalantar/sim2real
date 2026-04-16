#!/bin/bash
# Run GAIE-legacy vs Adaptive-Admission comparison for Qwen3-14B
# Requires: ./blis binary built at repo root
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
RESULTS="$SCRIPT_DIR/results"
WORKLOADS="$SCRIPT_DIR/workloads"
BLIS="$REPO_ROOT/blis"

mkdir -p "$RESULTS"

# Calibrated to match real vLLM deployment parameters:
#   block_size=64, gpu_memory_utilization=0.95, max_num_batched_tokens=2048, max_num_seqs=256
#   total-kv-blocks=4719 (302,016 tokens / 64 block_size, from real vLLM log)
COMMON="\
  --model Qwen/Qwen3-14B \
  --hardware H100 \
  --tp 1 \
  --latency-model trained-physics \
  --num-instances 4 \
  --routing-policy round-robin \
  --snapshot-refresh-interval 50000 \
  --block-size-in-tokens 64 \
  --gpu-memory-utilization 0.95 \
  --total-kv-blocks 4719 \
  --max-num-scheduled-tokens 2048 \
  --max-num-running-reqs 256 \
  --max-model-len 40960"

cd "$REPO_ROOT"

run() {
  local admission=$1 workload=$2 output=$3
  echo "Running: $output"
  $BLIS run $COMMON --admission-policy "$admission" --workload-spec "$workload" \
    > "$RESULTS/$output" 2>&1
  echo "  Done: $output"
}

# W1: Sustained overload (rate=110, ~1.5x capacity, uniform token cost)
run gaie-legacy        "$WORKLOADS/w1_14b.yaml"                14b_baseline_w1.txt
run adaptive-admission "$WORKLOADS/w1_14b.yaml"                14b_iter11_w1.txt

# # W2: Heavy sheddable (rate=110, ~2.25x by token volume, sheddable/batch 2x heavier)
# run gaie-legacy        "$WORKLOADS/w2_14b_heavy_sheddable.yaml" 14b_baseline_w2.txt
# run adaptive-admission "$WORKLOADS/w2_14b_heavy_sheddable.yaml" 14b_iter11_w2.txt

# W3: High sheddable fraction (rate=210, ~2.9x overload, 70% sheddable+batch)
run gaie-legacy        "$WORKLOADS/w3_14b_high_sheddable.yaml" 14b_baseline_w3.txt
run adaptive-admission "$WORKLOADS/w3_14b_high_sheddable.yaml" 14b_iter11_w3.txt

echo ""
echo "All 6 runs complete. Run: bash compare.sh"
