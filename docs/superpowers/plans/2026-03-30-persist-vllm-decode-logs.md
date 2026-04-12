# Persist vLLM Decode Logs Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Save vLLM decode pod logs into the per-phase raw folders, renamed from `deploy_{phase}_raw` to `deploy_{phase}_log`.

**Architecture:** In `_extract_phase_results`, after copying data from the cluster PVC, enumerate pods with "decode" in their name and save each pod's kubectl logs as `{pod}_decode_{n}.log` inside the renamed log dir. `analyze.py` needs no changes (it never references the raw dir). Update the sim2real-results SKILL.md reference.

**Tech Stack:** Python 3.10+, kubectl, existing `run()` helper in deploy.py

---

## File Map

| File | Action | What changes |
|------|--------|-------------|
| `scripts/deploy.py` | Modify | Rename raw_dir; add decode log collection in `_extract_phase_results` |
| `.claude/skills/sim2real-results/SKILL.md` | Modify | Update two lines that reference `${phase}_raw/` |
| `scripts/analyze.py` | No change | Only reads `deploy_{phase}_results.json`; never references the raw dir |
| `tests/test_deploy.py` | No change | No existing test covers `_extract_phase_results` (kubectl-bound) |

---

## Chunk 1: Rename dir + collect decode logs in deploy.py

### Task 1: Rename `deploy_{phase}_raw` → `deploy_{phase}_log` in `_extract_phase_results`

**Files:**
- Modify: `scripts/deploy.py:525-562`

- [ ] **Step 1: Rename `raw_dir` variable and folder name**

  In `_extract_phase_results`, line 525:
  ```python
  # Before
  raw_dir = run_dir / f"deploy_{phase}_raw"
  # After
  raw_dir = run_dir / f"deploy_{phase}_log"
  ```

- [ ] **Step 2: Verify the rest of `_extract_phase_results` uses `raw_dir` (not the literal string)**

  Grep for `deploy_{phase}_raw` — expect zero hits after the rename:
  ```bash
  grep -n "deploy_.*_raw" scripts/deploy.py
  ```
  Expected: no output.

- [ ] **Step 3: Update the ok() message at the end of `_run_noise_phase` (line 602)**

  ```python
  # Before
  ok(f"Noise characterization complete: {run_dir / 'deploy_noise_results.json'}")
  # After — no change needed (references results.json, not raw dir)
  ```
  This line is fine as-is. No edit required.

- [ ] **Step 4: Run the existing test suite to confirm no regressions**

  ```bash
  python -m pytest tests/test_deploy.py -v
  ```
  Expected: all tests pass.

---

### Task 2: Collect vLLM decode pod logs in `_extract_phase_results`

**Files:**
- Modify: `scripts/deploy.py:525-562` (inside `_extract_phase_results`, after the kubectl cp block)

- [ ] **Step 1: Add log collection block after the kubectl cp + pod cleanup**

  Insert after the `run(["kubectl", "delete", "pod", pod_name, ...])` call that cleans up the extractor pod (line ~532), and before the `convert-trace` invocation:

  ```python
  # Collect vLLM decode pod logs
  log_result = run(
      ["kubectl", "get", "pods", "-n", namespace,
       "--no-headers", "-o", "custom-columns=NAME:.metadata.name"],
      check=False, capture=True,
  )
  if log_result.returncode == 0:
      decode_pods = [p for p in log_result.stdout.splitlines() if "decode" in p.lower()]
      for n, pod in enumerate(decode_pods):
          pod_log = run(
              ["kubectl", "logs", pod, "-n", namespace],
              check=False, capture=True,
          )
          if pod_log.returncode == 0 and pod_log.stdout:
              (raw_dir / f"{pod}_decode_{n}.log").write_text(pod_log.stdout)
  ```

  Place this block **after** the extractor pod is deleted and **before** the `convert-trace` call (so the log dir exists and the extractor pod is already cleaned up).

- [ ] **Step 2: Run tests again to confirm no regressions**

  ```bash
  python -m pytest tests/test_deploy.py -v
  ```
  Expected: all tests pass (no tests cover `_extract_phase_results` directly).

- [ ] **Step 3: Commit**

  ```bash
  git add scripts/deploy.py
  git commit -m "feat(deploy): rename raw→log dirs, persist vLLM decode pod logs"
  ```

---

## Chunk 2: Update sim2real-results SKILL.md

### Task 3: Fix `${phase}_raw/` references in sim2real-results SKILL.md

**Files:**
- Modify: `.claude/skills/sim2real-results/SKILL.md:105,110`

- [ ] **Step 1: Update the two `_raw` references to `_log`**

  Line ~105:
  ```bash
  # Before
  kubectl cp ${NAMESPACE}/sim2real-extract-${phase}:/data/${phase}/ ${RUN_DIR}/${phase}_raw/ --retries=3
  # After
  kubectl cp ${NAMESPACE}/sim2real-extract-${phase}:/data/${phase}/ ${RUN_DIR}/${phase}_log/ --retries=3
  ```

  Line ~110:
  ```bash
  # Before
      --input-dir ${RUN_DIR}/${phase}_raw/ \
  # After
      --input-dir ${RUN_DIR}/${phase}_log/ \
  ```

- [ ] **Step 2: Verify no other `_raw` references remain in SKILL.md files**

  ```bash
  grep -rn "_raw" .claude/skills/
  ```
  Expected: no output.

- [ ] **Step 3: Commit**

  ```bash
  git add .claude/skills/sim2real-results/SKILL.md
  git commit -m "docs(skill): update raw→log dir references in sim2real-results"
  ```

---

## Summary of filename changes

| Old name | New name |
|----------|----------|
| `deploy_noise_raw/` | `deploy_noise_log/` |
| `deploy_baseline_raw/` | `deploy_baseline_log/` |
| `deploy_treatment_raw/` | `deploy_treatment_log/` |

**Unaffected filenames** (analyze.py reads these — no changes needed):
- `deploy_baseline_results.json`
- `deploy_treatment_results.json`
- `deploy_validation_results.json`
- `deploy_comparison_table.txt`
