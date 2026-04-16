#!/bin/bash
# Compare GAIE-legacy vs Adaptive-Admission results for Qwen3-14B
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RESULTS="$SCRIPT_DIR/results"

# Extract completed count (n from E2E line) for a tier
get_completed() {
  local file=$1 tier=$2
  awk -v t="$tier" '
    /^  [a-z]/ { cur=$1; sub(/:$/,"",cur) }
    cur==t && /E2E:/ && !/SLO/ {
      s=$0; sub(/.*\(n=/, "", s); sub(/\).*/, "", s); print s
    }
  ' "$file"
}

# Extract shed count for a tier
get_shed() {
  local file=$1 tier=$2
  val=$(grep "Shed ($tier):" "$file" 2>/dev/null | awk '{print $NF}')
  echo "${val:-0}"
}

# Extract mean E2E (ms) for a tier
get_mean_e2e() {
  local file=$1 tier=$2
  awk -v t="$tier" '
    /^  [a-z]/ { cur=$1; sub(/:$/,"",cur) }
    cur==t && /E2E:/ && /mean=/ && !/SLO/ {
      s=$0; sub(/.*mean=/, "", s); sub(/[^0-9.].*/, "", s)
      printf "%.0f\n", s/1000
    }
  ' "$file"
}

# Extract P99 E2E (ms) for a tier
get_p99() {
  local file=$1 tier=$2
  awk -v t="$tier" '
    /^  [a-z]/ { cur=$1; sub(/:$/,"",cur) }
    cur==t && /E2E:/ && /p99=/ && !/SLO/ {
      s=$0; sub(/.*p99=/, "", s); sub(/[^0-9.].*/, "", s)
      printf "%.0f\n", s/1000
    }
  ' "$file"
}

# Extract SLO<10s for a tier
get_slo() {
  local file=$1 tier=$2
  awk -v t="$tier" '
    /^  [a-z]/ { cur=$1; sub(/:$/,"",cur) }
    cur==t && /SLO_attainment\(E2E<10000ms\)/ { print $2 }
  ' "$file"
}

printf "\n"
printf "╔══════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════╗\n"
printf "║                                    Qwen3-14B: GAIE-Legacy vs Adaptive-Admission (trained-physics, 4x H100 TP=1)                                              ║\n"
printf "╚══════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════════╝\n"

for w in w1 w2 w3; do
  baseline="$RESULTS/14b_baseline_${w}.txt"
  treatment="$RESULTS/14b_iter11_${w}.txt"

  if [ ! -f "$baseline" ] || [ ! -f "$treatment" ]; then
    echo "  Missing results for $w — run run.sh first"
    continue
  fi

  case $w in
    w1) desc="W1: Sustained overload (rate=110, ~1.5x cap, uniform tokens)" ;;
    w2) desc="W2: Heavy sheddable (rate=110, ~2.25x token volume, sheddable/batch 2x heavier)" ;;
    w3) desc="W3: High sheddable fraction (rate=210, ~2.9x cap, 70% sheddable+batch)" ;;
  esac

  printf "\n┌────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐\n"
  printf "│ %-170s │\n" "$desc"
  printf "├──────────────┬───────┬─────────────┬─────────────┬──────────────────────────────┬──────────────────────────────┬──────────────────────────────────────────────┤\n"
  printf "│ %-12s │ %-5s │ %-11s │ %-11s │ %-28s │ %-28s │ %-44s │\n" \
    "SLO Tier" "Total" "GAIE Rej%" "Adapt Rej%" "GAIE-Legacy Latency" "Adaptive Latency" "Difference"
  printf "│              │       │ shed/total  │ shed/total  │   Mean E2E       P99 E2E    │   Mean E2E       P99 E2E    │  Mean E2E %%    P99 E2E %%    SLO<10s      │\n"
  printf "├──────────────┼───────┼─────────────┼─────────────┼──────────────────────────────┼──────────────────────────────┼──────────────────────────────────────────────┤\n"

  for tier in critical standard sheddable batch; do
    b_comp=$(get_completed "$baseline" "$tier")
    t_comp=$(get_completed "$treatment" "$tier")
    b_shed=$(get_shed "$baseline" "$tier")
    t_shed=$(get_shed "$treatment" "$tier")

    # Skip tier if not present in both
    [ -z "$b_comp" ] && continue

    b_total=$((b_shed + b_comp))
    t_total=$((t_shed + ${t_comp:-0}))

    # Rejection percentages
    if [ "$b_total" -gt 0 ]; then
      b_rej_pct=$(awk "BEGIN { printf \"%.1f\", $b_shed / $b_total * 100 }")
    else
      b_rej_pct="0.0"
    fi
    if [ "$t_total" -gt 0 ]; then
      t_rej_pct=$(awk "BEGIN { printf \"%.1f\", $t_shed / $t_total * 100 }")
    else
      t_rej_pct="0.0"
    fi

    b_rej_str=$(printf "%s/%-5s" "$b_shed" "$b_total")
    t_rej_str=$(printf "%s/%-5s" "$t_shed" "$t_total")

    b_mean=$(get_mean_e2e "$baseline" "$tier")
    t_mean=$(get_mean_e2e "$treatment" "$tier")
    b_p99=$(get_p99 "$baseline" "$tier")
    t_p99=$(get_p99 "$treatment" "$tier")
    b_slo=$(get_slo "$baseline" "$tier")
    t_slo=$(get_slo "$treatment" "$tier")

    if [ -n "$t_mean" ] && [ -n "$b_mean" ] && [ "$b_mean" -gt 0 ] 2>/dev/null; then
      mean_pct=$(awk "BEGIN { printf \"%+.0f%%\", ($t_mean - $b_mean) / $b_mean * 100 }")
      p99_pct=$(awk "BEGIN { printf \"%+.0f%%\", ($t_p99 - $b_p99) / $b_p99 * 100 }")
      slo_delta=$(awk "BEGIN { printf \"%+.0fpp\", ($t_slo - $b_slo) * 100 }")
    else
      mean_pct="--"
      p99_pct="--"
      slo_delta="--"
    fi

    printf "│ %-12s │ %5s │ %7s %3s%% │ %7s %3s%% │   %5sms        %5sms    │   %5sms        %5sms    │  %-14s %-14s %-13s│\n" \
      "$tier" "$b_total" "$b_rej_str" "$b_rej_pct" "$t_rej_str" "$t_rej_pct" \
      "${b_mean:-  --}" "${b_p99:-  --}" "${t_mean:-  --}" "${t_p99:-  --}" \
      "$mean_pct" "$p99_pct" "$slo_delta"
  done

  printf "└──────────────┴───────┴─────────────┴─────────────┴──────────────────────────────┴──────────────────────────────┴──────────────────────────────────────────────┘\n"
done

printf "\n"
