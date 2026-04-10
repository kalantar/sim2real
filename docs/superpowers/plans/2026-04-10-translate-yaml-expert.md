# sim2real-translate: YAML Generation + Expert Agent Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Redesign sim2real-translate to derive baseline + treatment EPP configs from sim configs via a new Expert agent, and add user pause points after each derivation phase.

**Architecture:** Add v3 manifest support (baseline.sim/real split) in `pipeline/lib/manifest.py`; update `prepare.py` to pass new baseline fields in `skill_input.json`; create a new Expert agent prompt that stays alive and answers queries from Writer/Reviewer; update Writer prompt with Phase 2 (baseline derivation) and Phase 3 (treatment derivation) before code generation; update SKILL.md to spawn Expert and handle user pause points.

**Tech Stack:** Python 3.10 (pipeline), PyYAML, pytest; Markdown prompt files

**Spec:** `docs/superpowers/specs/2026-04-10-translate-yaml-expert-design.md`

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `pipeline/lib/manifest.py` | Modify | Accept v3, normalize v2 baseline.config → baseline.sim.config |
| `pipeline/tests/test_manifest.py` | Modify | v3 loading, v2 normalization, optional real fields |
| `pipeline/prepare.py` | Modify | Add baseline_sim/real fields to skill_input.json |
| `pipeline/tests/test_prepare.py` | Modify | Verify new skill_input fields for v2 and v3 manifests |
| `sim2real_golden_correct/routers/baseline_epp_template.yaml` | Create | Example real EPP YAML template for the golden run |
| `config/transfer.yaml` | Modify | Upgrade to v3: rename baseline.config, add baseline.real.* |
| `prompts/prepare/agent-expert.md` | Create | Expert agent: initialization, repo exploration, query handling |
| `prompts/prepare/agent-writer.md` | Modify | Phase 2 (baseline derivation), Phase 3 (treatment derivation), Expert query, revised Phase 4 user pause |
| `prompts/prepare/agent-reviewer.md` | Modify | Criterion 6 (treatment config constraint), Expert query |
| `.claude/skills/sim2real-translate/SKILL.md` | Modify | Spawn Expert, load new skill_input fields, Phase 2/3 steps, user pause protocol |

---

## Chunk 1: manifest.py v3 support + tests

### Task 1: Add v3 baseline fields to `pipeline/lib/manifest.py`

**Files:**
- Modify: `pipeline/lib/manifest.py`

- [ ] **Step 1: Write failing tests for v3 manifest loading**

Add to `pipeline/tests/test_manifest.py`:

```python
# ── v3 manifest fixtures ───────────────────────────────────────────────────

MINIMAL_V3 = {
    "kind": "sim2real-transfer",
    "version": 3,
    "scenario": "routing",
    "algorithm": {
        "source": "sim2real_golden/routers/router_adaptive_v2.go",
        "config": "sim2real_golden/routers/policy_adaptive_v2.yaml",
    },
    "baseline": {
        "sim": {"config": "sim2real_golden/routers/policy_baseline_211.yaml"},
    },
    "workloads": ["sim2real_golden/workloads/wl1.yaml"],
    "llm_config": "sim2real_golden/llm_config.yaml",
}


def test_load_valid_v3_minimal(tmp_path):
    """v3 with just baseline.sim.config loads cleanly."""
    path = _write_manifest(tmp_path, MINIMAL_V3)
    m = load_manifest(path)
    assert m["baseline"]["sim"]["config"] == "sim2real_golden/routers/policy_baseline_211.yaml"
    assert m["baseline"]["real"]["config"] is None
    assert m["baseline"]["real"]["notes"] == ""


def test_v3_with_real_config_and_notes(tmp_path):
    """v3 with baseline.real.config + notes loads and preserves both."""
    data = {
        **MINIMAL_V3,
        "baseline": {
            "sim": {"config": "sim2real_golden/routers/policy_baseline_211.yaml"},
            "real": {
                "config": "sim2real_golden/routers/baseline_epp_template.yaml",
                "notes": "Use EndpointPickerConfig.Scorers[]",
            },
        },
    }
    path = _write_manifest(tmp_path, data)
    m = load_manifest(path)
    assert m["baseline"]["real"]["config"] == "sim2real_golden/routers/baseline_epp_template.yaml"
    assert "EndpointPickerConfig" in m["baseline"]["real"]["notes"]


def test_v3_missing_sim_config_raises(tmp_path):
    """v3 without baseline.sim.config raises ManifestError."""
    data = {**MINIMAL_V3, "baseline": {"real": {"config": "x.yaml"}}}
    path = _write_manifest(tmp_path, data)
    with pytest.raises(ManifestError, match="baseline.sim.config"):
        load_manifest(path)


def test_v3_missing_sim_section_raises(tmp_path):
    """v3 baseline without sim key raises ManifestError."""
    data = {**MINIMAL_V3, "baseline": {}}
    path = _write_manifest(tmp_path, data)
    with pytest.raises(ManifestError, match="baseline.sim.config"):
        load_manifest(path)


def test_v3_real_section_entirely_optional(tmp_path):
    """v3 without baseline.real at all is valid; defaults applied."""
    data = {**MINIMAL_V3}  # no baseline.real
    path = _write_manifest(tmp_path, data)
    m = load_manifest(path)
    assert m["baseline"]["real"] == {"config": None, "notes": ""}


def test_v3_real_partial_defaults_applied(tmp_path):
    """v3 with baseline.real present but missing notes gets default."""
    data = {
        **MINIMAL_V3,
        "baseline": {
            "sim": {"config": "sim2real_golden/routers/policy_baseline_211.yaml"},
            "real": {"config": "x.yaml"},  # no notes
        },
    }
    path = _write_manifest(tmp_path, data)
    m = load_manifest(path)
    assert m["baseline"]["real"]["notes"] == ""


def test_v2_normalizes_to_v3_shape(tmp_path):
    """v2 manifest: baseline.config is mapped to baseline.sim.config in output."""
    path = _write_manifest(tmp_path, MINIMAL_V2)
    m = load_manifest(path)
    assert "sim" in m["baseline"]
    assert m["baseline"]["sim"]["config"] == "sim2real_golden/routers/policy_baseline_211.yaml"
    assert m["baseline"]["real"]["config"] is None
    assert m["baseline"]["real"]["notes"] == ""


def test_v2_baseline_config_missing_raises(tmp_path):
    """v2 without baseline.config raises ManifestError."""
    data = {**MINIMAL_V2, "baseline": {}}
    path = _write_manifest(tmp_path, data)
    with pytest.raises(ManifestError, match="baseline.config"):
        load_manifest(path)


def test_v3_accepted_alongside_v2(tmp_path):
    """Version 3 is accepted; version 2 is still accepted."""
    for ver, data in [(2, MINIMAL_V2), (3, MINIMAL_V3)]:
        path = _write_manifest(tmp_path / f"v{ver}.yaml", data)
        m = load_manifest(path)
        assert m["version"] == ver
```

- [ ] **Step 2: Run failing tests**

```bash
cd /Users/jchen/go/src/inference-sim/sim2real
python -m pytest pipeline/tests/test_manifest.py::test_load_valid_v3_minimal -x -v 2>&1 | head -20
```

Expected: `FAILED` — `Unsupported manifest version: 3`

- [ ] **Step 3: Update `pipeline/lib/manifest.py` to accept v3 and normalize v2**

Replace the existing `_REQUIRED_BASELINE` and baseline validation block with:

```python
_REQUIRED_TOP = ["kind", "version", "scenario", "algorithm", "baseline", "workloads", "llm_config"]
_REQUIRED_ALGORITHM = ["source", "config"]
```

(Remove `_REQUIRED_BASELINE = ["config"]` — v2 and v3 validate baseline inline now.)

Replace the version check and baseline validation block (lines 33–59 in the original):

```python
    version = data.get("version")
    if version is None:
        raise ManifestError("Missing required field: version")
    if version == 1:
        raise ManifestError(
            "This is a v1 manifest. v2 is required.\n"
            "Migration: rename algorithm.policy \u2192 algorithm.config, "
            "add scenario field, move target/config to env_defaults.yaml.\n"
            "See docs/transfer/migration-v1-to-v2.md for details."
        )
    if version not in (2, 3):
        raise ManifestError(f"Unsupported manifest version: {version}")

    for field in _REQUIRED_TOP:
        if field not in data:
            raise ManifestError(f"Missing required field: {field}")

    algo = data["algorithm"]
    if not isinstance(algo, dict):
        raise ManifestError("algorithm must be a mapping")
    for f in _REQUIRED_ALGORITHM:
        if f not in algo:
            raise ManifestError(f"Missing required field: algorithm.{f}")

    bl = data["baseline"]
    if not isinstance(bl, dict):
        raise ManifestError("baseline must be a mapping")

    if version == 2:
        # v2: flat baseline.config — normalize to v3 shape for uniform downstream access
        if "config" not in bl:
            raise ManifestError("Missing required field: baseline.config")
        data["baseline"] = {
            "sim": {"config": bl["config"]},
            "real": {"config": None, "notes": ""},
        }
    else:
        # v3: baseline.sim.config required; baseline.real.* optional with defaults
        sim_section = bl.get("sim")
        if not isinstance(sim_section, dict) or "config" not in sim_section:
            raise ManifestError("Missing required field: baseline.sim.config")
        real_section = bl.get("real")
        if real_section is None:
            data["baseline"]["real"] = {"config": None, "notes": ""}
        elif not isinstance(real_section, dict):
            raise ManifestError("baseline.real must be a mapping")
        else:
            data["baseline"]["real"].setdefault("config", None)
            data["baseline"]["real"].setdefault("notes", "")
```

- [ ] **Step 4: Run all manifest tests**

```bash
python -m pytest pipeline/tests/test_manifest.py -v 2>&1 | tail -30
```

Expected: All pass. `test_missing_baseline_config` should still pass (v2 path). New v3 tests pass.

- [ ] **Step 5: Commit**

```bash
git add pipeline/lib/manifest.py pipeline/tests/test_manifest.py
git commit -m "feat(manifest): accept v3, normalize v2 baseline to sim/real shape"
```

---

## Chunk 2: prepare.py baseline fields in skill_input.json

### Task 2: Add baseline_sim/real fields to `skill_input.json` in `pipeline/prepare.py`

**Files:**
- Modify: `pipeline/prepare.py`
- Modify: `pipeline/tests/test_prepare.py`

**Important:** After the manifest.py change, `load_manifest` normalizes all manifests (v2 and v3)
to the same `baseline.sim / baseline.real` shape. The `_phase_translate` function accesses
`manifest["baseline"]["sim"]["config"]`. Existing tests in `TestPhaseTranslate` use
`manifest = dict(MINIMAL_MANIFEST)` which is a raw dict that bypasses normalization — these
will break unless `MINIMAL_MANIFEST` is updated to the normalized v3 shape.

Fix: update `MINIMAL_MANIFEST` in `test_prepare.py` to v3 normalized shape. The `repo` fixture
writes this to disk, and `load_manifest` on the written file will produce the same normalized
dict — so all tests are consistent.

- [ ] **Step 1: Update `MINIMAL_MANIFEST` in `pipeline/tests/test_prepare.py` to v3 shape**

Replace:
```python
MINIMAL_MANIFEST = {
    ...
    "baseline": {"config": "sim2real_golden/routers/policy_baseline_211.yaml"},
    ...
}
```

With:
```python
MINIMAL_MANIFEST = {
    "kind": "sim2real-transfer",
    "version": 3,
    "scenario": "routing",
    "algorithm": {
        "source": "sim2real_golden/routers/router_adaptive_v2.go",
        "config": "sim2real_golden/routers/policy_adaptive_v2.yaml",
    },
    "baseline": {
        "sim": {"config": "sim2real_golden/routers/policy_baseline_211.yaml"},
        "real": {"config": None, "notes": ""},
    },
    "workloads": ["sim2real_golden/workloads/wl1.yaml"],
    "llm_config": "sim2real_golden/llm_config.yaml",
}
```

- [ ] **Step 2: Run existing tests to confirm they still pass with updated fixture**

```bash
python -m pytest pipeline/tests/test_prepare.py -v 2>&1 | tail -20
```

Expected: All pass (tests use `dict(MINIMAL_MANIFEST)` which now has v3 shape with `baseline.sim.config`).

- [ ] **Step 3: Write failing tests for new skill_input fields**

Add to `pipeline/tests/test_prepare.py` inside `class TestPhaseTranslate`:

```python
    def test_skill_input_includes_baseline_sim_config(self, repo):
        """skill_input.json contains baseline_sim_config from v3 manifest."""
        mod = _import_prepare_with_root(repo)
        manifest = dict(MINIMAL_MANIFEST)  # already v3 normalized shape

        run_dir = repo / "workspace" / "runs" / "test-run"
        run_dir.mkdir(parents=True, exist_ok=True)
        resolved = mod._load_resolved_config(manifest)
        state = StateMachine("test-run", "routing", run_dir)
        state.mark_done("init")
        context_path = run_dir / "context.md"
        context_path.write_text("# Context")

        class Args:
            force = False
            manifest = None

        with pytest.raises(SystemExit):
            mod._phase_translate(Args(), state, manifest, run_dir, resolved, context_path)

        si = json.loads((run_dir / "skill_input.json").read_text())
        assert si["baseline_sim_config"] == "sim2real_golden/routers/policy_baseline_211.yaml"
        assert si["baseline_real_config"] is None
        assert si["baseline_real_notes"] == ""

    # (note: test_skill_input_includes_baseline_real_config_when_present uses load_manifest
    # to get a v3 manifest with real config present)

    def test_skill_input_includes_baseline_real_config_when_present(self, repo):
        """skill_input.json includes baseline_real_config when v3 provides it."""
        mod = _import_prepare_with_root(repo)

        # Write a v3 manifest with baseline.real fields
        v3_data = {
            "kind": "sim2real-transfer",
            "version": 3,
            "scenario": "routing",
            "algorithm": {
                "source": "sim2real_golden/routers/router_adaptive_v2.go",
                "config": "sim2real_golden/routers/policy_adaptive_v2.yaml",
            },
            "baseline": {
                "sim": {"config": "sim2real_golden/routers/policy_baseline_211.yaml"},
                "real": {
                    "config": "sim2real_golden/routers/baseline_epp_template.yaml",
                    "notes": "Use EndpointPickerConfig",
                },
            },
            "workloads": ["sim2real_golden/workloads/wl1.yaml"],
            "llm_config": "sim2real_golden/llm_config.yaml",
        }
        _write_yaml(repo / "config" / "transfer.yaml", v3_data)
        # Create the real config file so manifest validation passes
        _write_text(repo / "sim2real_golden" / "routers" / "baseline_epp_template.yaml",
                    "kind: EndpointPickerConfig\n")

        from pipeline.lib.manifest import load_manifest
        manifest = load_manifest(repo / "config" / "transfer.yaml")

        run_dir = repo / "workspace" / "runs" / "test-run"
        run_dir.mkdir(parents=True, exist_ok=True)
        resolved = mod._load_resolved_config(manifest)
        state = StateMachine("test-run", "routing", run_dir)
        state.mark_done("init")
        context_path = run_dir / "context.md"
        context_path.write_text("# Context")

        class Args:
            force = False
            manifest = None

        with pytest.raises(SystemExit):
            mod._phase_translate(Args(), state, manifest, run_dir, resolved, context_path)

        si = json.loads((run_dir / "skill_input.json").read_text())
        assert si["baseline_sim_config"] == "sim2real_golden/routers/policy_baseline_211.yaml"
        assert si["baseline_real_config"] == "sim2real_golden/routers/baseline_epp_template.yaml"
        assert si["baseline_real_notes"] == "Use EndpointPickerConfig"
```

- [ ] **Step 4: Run failing tests**

```bash
python -m pytest pipeline/tests/test_prepare.py::TestPhaseTranslate::test_skill_input_includes_baseline_sim_config -xvs 2>&1 | tail -20
```

Expected: `FAILED` — `KeyError: 'baseline_sim_config'` when asserting `si["baseline_sim_config"]`
(the field doesn't exist in skill_input.json yet — prepare.py hasn't been updated)

- [ ] **Step 5: Update `_phase_translate` in `pipeline/prepare.py`**

In `_phase_translate`, locate the `skill_input = {` block and add three new fields after `"hints"`:

```python
    skill_input = {
        "run_name": state.run_name,
        "run_dir": str(run_dir.relative_to(REPO_ROOT)),
        "scenario": manifest["scenario"],
        "context_path": str(context_path.relative_to(REPO_ROOT)
                           if context_path.is_relative_to(REPO_ROOT)
                           else context_path),
        "manifest_path": str(getattr(args, "manifest", None) or "config/transfer.yaml"),
        "algorithm_source": manifest["algorithm"]["source"],
        "algorithm_config": manifest["algorithm"]["config"],
        "baseline_sim_config": manifest["baseline"]["sim"]["config"],
        "baseline_real_config": manifest["baseline"]["real"].get("config"),
        "baseline_real_notes": manifest["baseline"]["real"].get("notes", ""),
        "target": {"repo": target.get("repo", "")},
        "build_commands": commands,
        "config_kind": config_cfg.get("kind", ""),
        "hints": manifest.get("hints", {"text": "", "files": []}),
    }
```

- [ ] **Step 6: Run all prepare tests**

```bash
python -m pytest pipeline/tests/test_prepare.py -v 2>&1 | tail -30
```

Expected: All pass. Existing tests pass because `MINIMAL_MANIFEST` now has v3 shape, so
`manifest["baseline"]["sim"]["config"]` works. New tests pass because prepare.py now writes
the new fields.

- [ ] **Step 7: Run the full pipeline test suite**

```bash
python -m pytest pipeline/tests/ -v 2>&1 | tail -30
```

Expected: All pass.

- [ ] **Step 8: Commit**

```bash
git add pipeline/prepare.py pipeline/tests/test_prepare.py
git commit -m "feat(prepare): add baseline_sim/real fields to skill_input.json"
```

---

## Chunk 3: Config data files

### Task 3: Create baseline EPP template and upgrade transfer.yaml to v3

**Files:**
- Create: `sim2real_golden_correct/routers/baseline_epp_template.yaml`
- Modify: `config/transfer.yaml`

- [ ] **Step 1: Create `sim2real_golden_correct/routers/baseline_epp_template.yaml`**

This is the structural template for the real baseline EPP config. The Writer agent fills in
the actual scorer names and weights from `baseline.sim.config` (policy_baseline_322.yaml:
`precise-prefix-cache:3, queue-depth:2, kv-utilization:2`). Use real GAIE/llm-d scorer
type names (not sim names). Weights shown are placeholders — the agent derives them from sim config.

```yaml
# Real EPP baseline config template for adaptive-routing scenario.
# Structure: EndpointPickerConfig with weighted scoring profile.
# Weights are filled in by the translate agent from baseline.sim.config.
#
# Sim scorer → real EPP type mapping:
#   precise-prefix-cache → precise-prefix-cache-scorer (llm-d plugin)
#   queue-depth          → queue-depth-scorer           (llm-d plugin)
#   kv-utilization       → kv-cache-utilization-scorer  (GAIE built-in, do not reimplement)
apiVersion: inference.networking.x-k8s.io/v1alpha1
kind: EndpointPickerConfig
plugins:
- type: precise-prefix-cache-scorer
- type: queue-depth-scorer
- type: kv-cache-utilization-scorer
- type: decode-filter
- type: max-score-picker
- type: single-profile-handler
schedulingProfiles:
- name: default
  plugins:
  - pluginRef: decode-filter
  - pluginRef: max-score-picker
  - pluginRef: precise-prefix-cache-scorer
    weight: WEIGHT_FROM_SIM  # precise-prefix-cache weight in baseline.sim.config
  - pluginRef: queue-depth-scorer
    weight: WEIGHT_FROM_SIM  # queue-depth weight in baseline.sim.config
  - pluginRef: kv-cache-utilization-scorer
    weight: WEIGHT_FROM_SIM  # kv-utilization weight in baseline.sim.config
```

- [ ] **Step 2: Update `config/transfer.yaml` to v3**

```yaml
kind: sim2real-transfer
version: 3

scenario: adaptive-routing

algorithm:
  source: sim2real_golden_correct/routers/router_adaptive.go
  config: sim2real_golden_correct/routers/policy_adaptive.yaml

baseline:
  sim:
    config: sim2real_golden_correct/routers/policy_baseline_322.yaml
  real:
    config: sim2real_golden_correct/routers/baseline_epp_template.yaml
    notes: |
      The baseline uses a weighted scoring profile with three scorers.
      Sim scorer names map to real EPP types as follows:
        precise-prefix-cache → precise-prefix-cache-scorer (llm-d plugin)
        queue-depth          → queue-depth-scorer (llm-d plugin)
        kv-utilization       → kv-cache-utilization-scorer (GAIE built-in — do NOT reimplement)
      Weights come from baseline.sim.config (policy_baseline_322.yaml): 3:2:2 ratio.
      Use EndpointPickerConfig with a single schedulingProfile named "default".

workloads:
  - sim2real_golden_correct/workloads/workload_fm8_short_output_highrate.yaml
  - sim2real_golden_correct/workloads/workload_fm2a_groups_gt_instances.yaml

llm_config: admission_by60/llm_config.yaml

hints:
  files:
  - sim2real_golden_correct/README.md
  - sim2real_golden_correct/transfer_hint.md
  notes: Do not implement kv-cache-utilization scorer. There are already scorers available in the parent project which is https://github.com/kubernetes-sigs/gateway-api-inference-extension. The list of scorers available are documented in https://gateway-api-inference-extension.sigs.k8s.io/guides/epp-configuration/config-text/?h=scorer.

context:
  files:
  - blis-context.md
  - gaie_context.md
  - llm-d-inference-scheduler-context.md
```

- [ ] **Step 3: Verify transfer.yaml loads cleanly with updated manifest loader**

```bash
python -c "
from pipeline.lib.manifest import load_manifest
from pathlib import Path
m = load_manifest(Path('config/transfer.yaml'))
print('version:', m['version'])
print('baseline.sim.config:', m['baseline']['sim']['config'])
print('baseline.real.config:', m['baseline']['real']['config'])
print('OK')
"
```

Expected output:
```
version: 3
baseline.sim.config: sim2real_golden_correct/routers/policy_baseline_322.yaml
baseline.real.config: sim2real_golden_correct/routers/baseline_epp_template.yaml
OK
```

- [ ] **Step 4: Commit**

```bash
git add sim2real_golden_correct/routers/baseline_epp_template.yaml config/transfer.yaml
git commit -m "feat(config): add baseline EPP template and upgrade transfer.yaml to v3"
```

---

## Chunk 4: Prompt files — Expert, Writer Phase 2/3, Reviewer Criterion 6

### Task 4: Create `prompts/prepare/agent-expert.md`

**Files:**
- Create: `prompts/prepare/agent-expert.md`

- [ ] **Step 1: Write `prompts/prepare/agent-expert.md`**

```markdown
---
stage: prepare
version: "1.0"
description: "Expert agent — initialized with full context, answers queries from Writer and Reviewer"
---

# Translation Expert Agent

You are the Expert in the sim2real translation team. Your job is to answer technical
questions about inference-sim, llm-d-inference-scheduler, and upstream GAIE (gateway-api-inference-extension).

You stay alive for the entire skill run. Both the Writer and Reviewer will query you via
SendMessage. Answer each query in order — do not attempt to answer queries in parallel.

## Working Directory

All paths relative to: {REPO_ROOT}
Target repo: {TARGET_REPO}

## Initialization — Do This Now

Read the following to build your foundation. These tell you what this run is translating and
what the mapping context is.

1. Read `config/transfer.yaml` — understand scenario, algorithm, baseline (sim + real), hints
2. Read the full content of `{ALGO_SOURCE}` — the simulation algorithm being translated
3. Read the full content of `{ALGO_CONFIG}` — algorithm weights and thresholds (ground truth)
4. Read `{BASELINE_SIM_CONFIG}` — the simulation baseline policy
5. If `{BASELINE_REAL_CONFIG}` is not null: read `{BASELINE_REAL_CONFIG}` — real EPP template
6. Read all files in context.files (listed in transfer.yaml)

Then do targeted exploration of all three repos:

### inference-sim exploration

Starting from the scorer/signal names you see in `{ALGO_CONFIG}` and `{BASELINE_SIM_CONFIG}`,
find their definitions in `{REPO_ROOT}/inference-sim/`. Use Grep to find type definitions
and signal constant declarations. Read the relevant source files to understand what each
scorer measures and how signals are computed.

### llm-d-inference-scheduler exploration

Use Glob to survey `{TARGET_REPO}/pkg/plugins/` (file names only first). Then read:
- All interface definitions (files ending in `interface.go` or containing `type Scorer interface`)
- Existing scorer implementations (`.go` files in scorer/ or similar subdirectory) — read 2-3 as examples
- Plugin registration file (likely `register.go` or `plugins.go` in `{TARGET_REPO}/pkg/plugins/`)
- Config types used by scorers (look for structs with yaml tags)

### GAIE upstream exploration

GAIE is the upstream framework. Read broadly — you need the full architecture, not just scoring.

Use Glob on `{TARGET_REPO}/vendor/sigs.k8s.io/gateway-api-inference-extension/` (or wherever
GAIE is vendored) to find:
- `pkg/epp/framework/interface/` — ALL interface files; read them all
- `pkg/epp/scheduling/` — scheduler config, weighted scorer types
- Runner/config loading: find `runner.go` or `cmd/epp/` and read how `WithSchedulerConfig`
  and the YAML loader work
- `EndpointPickerConfig` struct definition and all its fields
- `pkg/epp/framework/plugins/` — built-in filters, scorers, pickers
- Admission control interfaces if the scenario is `admission_control`

After reading, build a mental model of:
- How a request flows: filter → scorer → picker → profile
- How `WithSchedulerConfig` vs the YAML loader differ
- What built-in scorers/filters/pickers exist and their type strings

## Answering Queries

When the Writer or Reviewer sends you a question via SendMessage, answer it:
- Search the live repo files for authoritative answers (Grep/Read as needed)
- Give file path + line number for every claim
- Be precise: if you find a function signature, quote it exactly
- If a query references a symbol you haven't read yet, go read it now

Never guess. If you cannot find something, say so and describe where you looked.

## Tools

Glob, Grep, Read only. You do not modify any files.
```

- [ ] **Step 2: Commit**

```bash
git add prompts/prepare/agent-expert.md
git commit -m "feat(prompts): add Expert agent prompt for sim2real translation team"
```

### Task 5: Update `prompts/prepare/agent-writer.md` — Phase 2 + Phase 3

**Files:**
- Modify: `prompts/prepare/agent-writer.md`

- [ ] **Step 1: Add Phase 2 (Baseline Config Derivation) before existing Step 1**

The current agent-writer.md Step 1 ("Translate") becomes Phase 4. Insert Phase 2 and Phase 3
before it. Also add Expert query instructions and update the Phase 4 review loop to send
`review-passed:` instead of immediately sending `done:` on APPROVE.

Add `{BASELINE_SIM_CONFIG}`, `{BASELINE_REAL_CONFIG}`, `{BASELINE_REAL_NOTES}`, and
`{EXPERT_AGENT_NAME}` to the placeholder list.

After the existing "## Inputs — Read These Now" table, insert:

```markdown
Expert agent name (for queries): {EXPERT_AGENT_NAME}

## Consulting the Expert

At any point during Phases 2, 3, or 4, you can ask the Expert a question:
```
SendMessage({EXPERT_AGENT_NAME}, "Your question here")
```
Wait for the reply before proceeding. The Expert has deep knowledge of all three repos
and will give you file:line references.

## Phase 2: Baseline Config Derivation

Use TaskCreate: `"Phase 2: Baseline Config Derivation"` → TaskUpdate in_progress

Read:
1. `{BASELINE_SIM_CONFIG}` — the sim baseline policy (scorer names + weights)
2. `{BASELINE_REAL_CONFIG}` (if not null) — the real EPP YAML template
3. `{BASELINE_REAL_NOTES}` — translation hints for baseline mapping

Your goal: produce `{RUN_DIR}/baseline_config.yaml` — a real, functional EPP YAML with the
actual scorer names and weights from `{BASELINE_SIM_CONFIG}` substituted into the real template.

Rules:
- Every scorer in `{BASELINE_SIM_CONFIG}` must appear in `baseline_config.yaml` (mapped to
  its real EPP type via the signal mapping in `{CONTEXT_PATH}` and `{BASELINE_REAL_NOTES}`)
- Weights must match exactly — do not approximate or normalize unless the real config requires it
- Ask the Expert if you are unsure about any scorer type string or config field name
- If `{BASELINE_REAL_CONFIG}` is null, derive the structure from the context document and Expert

Write `{RUN_DIR}/baseline_config.yaml`. Then send to main session:
```
SendMessage({MAIN_SESSION_NAME}, "baseline-ready: {RUN_DIR}/baseline_config.yaml")
```

Wait for the reply. The main session will either forward user feedback ("feedback: ...") or
send "continue". If feedback: revise `baseline_config.yaml` and re-send `baseline-ready:`.
Repeat until you receive "continue".

TaskUpdate Phase 2 → completed

## Phase 3: Treatment Config Derivation

Use TaskCreate: `"Phase 3: Treatment Config Derivation"` → TaskUpdate in_progress

Read:
1. `{RUN_DIR}/baseline_config.yaml` — the approved real baseline EPP YAML
2. `{ALGO_CONFIG}` — the algorithm policy config (what changes from baseline)
3. `{ALGO_SOURCE}` — the algorithm source (regime detection logic, thresholds)

Your goal: produce `{RUN_DIR}/treatment_config.yaml` — start from `baseline_config.yaml` and
apply the algorithm's changes. The treatment config must be **functional** (the Go code you
will write in Phase 4 must read its parameters from this YAML, not hardcode them).

Rules:
- Start from `baseline_config.yaml` as the structural base
- Identify the delta: which scorers change, which weights change, any new logic or thresholds
- Every threshold and weight from `{ALGO_CONFIG}` must have a corresponding YAML field in
  `treatment_config.yaml` — you will wire the Go code to read from these fields in Phase 4
- Ask the Expert about config struct field names and yaml tags if needed

Write `{RUN_DIR}/treatment_config.yaml`. Then send to main session:
```
SendMessage({MAIN_SESSION_NAME}, "treatment-ready: {RUN_DIR}/treatment_config.yaml")
```

Wait for the reply. Handle feedback / continue as in Phase 2.

TaskUpdate Phase 3 → completed
```

- [ ] **Step 2: Update Phase 4 review loop — send `review-passed:` instead of `done:` on APPROVE**

In the existing "### On APPROVE" section, replace the final SendMessage with:

```markdown
### On APPROVE

1. Write `{RUN_DIR}/translation_output.json` (see schema below)
2. Create `{RUN_DIR}/review/` directory if needed, write `round_<N>.json` (see schema below)
3. Update `.state.json` using the StateMachine code in the Output Artifacts section
4. Send to main session:
   ```
   SendMessage({MAIN_SESSION_NAME}, "review-passed: round=<N> plugin_type=<plugin_type>")
   ```
5. Wait for main session reply:
   - If "done": proceed to exit (Step 5 below)
   - If "feedback: <text>": treat as a new review round with the feedback as additional
     requirements. Apply the feedback, re-run build/test (Step 2), snapshot (Step 3), and
     send another review request. The round counter continues from N+1.

### Step 5: Exit

After receiving "done" from main session, send:
```
SendMessage({MAIN_SESSION_NAME}, "done: translation complete, plugin_type=<plugin_type>")
```
Then exit.
```

- [ ] **Step 3: Update placeholder substitution list in SKILL.md**

(This step is tracked in Task 7 below — adding the new placeholders to the substitution list.)

- [ ] **Step 4: Commit**

```bash
git add prompts/prepare/agent-writer.md
git commit -m "feat(prompts): add Phase 2/3 baseline+treatment derivation to writer agent"
```

### Task 6: Update `prompts/prepare/agent-reviewer.md` — Criterion 6 + Expert query

**Files:**
- Modify: `prompts/prepare/agent-reviewer.md`

- [ ] **Step 1: Add Expert query capability to Initialization section**

After the existing initialization list, add:

```markdown
Expert agent name (for queries): {EXPERT_AGENT_NAME}

You may query the Expert at any time before issuing your verdict:
```
SendMessage({EXPERT_AGENT_NAME}, "Your question here")
```
Use this when you are uncertain about a GAIE interface, scorer type string, or config struct field.
```

- [ ] **Step 2: Add Criterion 6 after existing Criterion 5**

```markdown
### Criterion 6: Treatment Config Constraint

`treatment_config.yaml` must be a **functional YAML** that the deployed Go code reads at
runtime. It must never be documentation-only.

Verify mechanically:

1. For every numeric threshold and weight in `{ALGO_CONFIG}`, confirm a corresponding field
   exists in `{RUN_DIR}/treatment_config.yaml`.
2. Confirm the plugin Go file(s) contain a config struct with yaml field tags that match
   the fields in `treatment_config.yaml` (look for `yaml:"fieldname"` tags), or a call to
   a config-loading function.
3. If any scoring threshold or weight appears as a **numeric literal** in the Go code without
   a corresponding `yaml:` tagged field, raise `[treatment-config]` NEEDS_CHANGES.
4. Exception: compile-time constants for framework-level concerns (buffer sizes, timeouts
   unrelated to scoring logic) are allowed without YAML representation.

Flag as `[treatment-config]` NEEDS_CHANGES if the constraint is violated.
```

- [ ] **Step 3: Add `treatment-config` to the categories list at the bottom**

Change:
```
Categories: `fidelity` | `code-quality` | `registration` | `config` | `assembly`
```
To:
```
Categories: `fidelity` | `code-quality` | `registration` | `config` | `assembly` | `treatment-config`
```

- [ ] **Step 4: Commit**

```bash
git add prompts/prepare/agent-reviewer.md
git commit -m "feat(prompts): add Criterion 6 (treatment config constraint) and Expert query to reviewer"
```

---

## Chunk 5: SKILL.md — Expert spawn, Phase 2/3, user pause protocol

### Task 7: Update `.claude/skills/sim2real-translate/SKILL.md`

**Files:**
- Modify: `.claude/skills/sim2real-translate/SKILL.md`

- [ ] **Step 1: Add new skill_input.json fields to the load/validate block**

In the "Validate and load `skill_input.json`" section, add `baseline_sim_config` to the
required fields list:

```python
required = ['run_name', 'run_dir', 'scenario', 'context_path', 'manifest_path',
            'algorithm_source', 'algorithm_config', 'baseline_sim_config',
            'target', 'build_commands', 'config_kind', 'hints']
```

- [ ] **Step 2: Add new shell variables after the existing load block**

After the existing `HINTS_FILES_CONTENT` assignment, add:

```bash
BASELINE_SIM_CONFIG=$(python3 -c "import json; print(json.load(open('$RUN_DIR/skill_input.json'))['baseline_sim_config'])")
BASELINE_REAL_CONFIG=$(python3 -c "import json; v=json.load(open('$RUN_DIR/skill_input.json')).get('baseline_real_config'); print(v if v else 'null')")
BASELINE_REAL_NOTES=$(python3 -c "import json; print(json.load(open('$RUN_DIR/skill_input.json')).get('baseline_real_notes', ''))")
```

- [ ] **Step 3: Update Resumability Check for Phase 2/3**

Replace the existing resumability check block with an extended version that prints all phase
states and sets shell variables used to skip phases:

```bash
python3 -c "
import json, os, sys
from pathlib import Path
state_path = Path('$RUN_DIR') / '.state.json'
if not state_path.exists():
    print('BASELINE_DONE=false')
    print('TREATMENT_DONE=false')
    sys.exit(0)
state = json.loads(state_path.read_text())
phases = state.get('phases', {})
ctx = phases.get('context', {})
bd = phases.get('baseline_derivation', {})
td = phases.get('treatment_derivation', {})
tr = phases.get('translate', {})
print(f'context: {ctx.get(\"status\", \"pending\")}')
print(f'baseline_derivation: {bd.get(\"status\", \"pending\")} user_approved={bd.get(\"user_approved\", False)}')
print(f'treatment_derivation: {td.get(\"status\", \"pending\")} user_approved={td.get(\"user_approved\", False)}')
print(f'translate: {tr.get(\"status\", \"pending\")}')
if tr.get('status') == 'done':
    print(f'  files={tr.get(\"files\", [])}')
    print(f'  review_rounds={tr.get(\"review_rounds\", 0)} consensus={tr.get(\"consensus\", \"?\")}')
# Emit shell variables for skip logic
bd_done = bd.get('status') == 'done' and bd.get('user_approved', False)
td_done = td.get('status') == 'done' and td.get('user_approved', False)
print(f'BASELINE_DONE={\"true\" if bd_done else \"false\"}')
print(f'TREATMENT_DONE={\"true\" if td_done else \"false\"}')
" | tee /tmp/resume_state.txt

# Parse skip flags from python output
BASELINE_DONE=$(grep '^BASELINE_DONE=' /tmp/resume_state.txt | cut -d= -f2)
TREATMENT_DONE=$(grep '^TREATMENT_DONE=' /tmp/resume_state.txt | cut -d= -f2)
```

Use these variables to skip phases in Steps 4/5 of the SKILL.md flow:

After spawning Expert (Step 1.5) and Reviewer, the skill checks:
```bash
if [ "$BASELINE_DONE" = "true" ] && [ "$TREATMENT_DONE" = "true" ]; then
    echo "[skip] Phases 2 and 3 already approved — jumping to code generation"
    # Jump directly to Step 2 Team Translation (writer starts at Phase 4)
elif [ "$BASELINE_DONE" = "true" ]; then
    echo "[skip] Phase 2 already approved — resuming from Phase 3"
    # Send writer message to start at Phase 3
fi
```

- [ ] **Step 4: Add Step 2: Spawn Expert (new step before existing Step 2)**

Insert between Step 1 (Context Check) and the existing Step 2 (Team Translation):

```markdown
## Step 1.5: Spawn Expert Agent

Use TaskCreate: `"Step 1.5: Spawn Expert"` → TaskUpdate in_progress

Read `prompts/prepare/agent-expert.md`, substitute all `{PLACEHOLDER}` values, and spawn:

```
Agent(
  subagent_type: general-purpose,
  name: "expert",
  run_in_background: true,
  prompt: <substituted agent-expert.md content>
)
```

Add to placeholder substitution list:
- `{BASELINE_SIM_CONFIG}` → `$BASELINE_SIM_CONFIG`
- `{BASELINE_REAL_CONFIG}` → `$BASELINE_REAL_CONFIG`
- `{BASELINE_REAL_NOTES}` → `$BASELINE_REAL_NOTES`
- `{EXPERT_AGENT_NAME}` → `"expert"`

The Expert initializes in the background. The Writer and Reviewer will use it during
translation. Do not wait for Expert initialization to complete before proceeding.

TaskUpdate Step 1.5 → completed
```

- [ ] **Step 5: Update Step 2 (Team Translation) to spawn Reviewer + Writer with new placeholders**

In the existing Step 2 placeholder substitution list, add:
- `{BASELINE_SIM_CONFIG}` → `$BASELINE_SIM_CONFIG`
- `{BASELINE_REAL_CONFIG}` → `$BASELINE_REAL_CONFIG`
- `{BASELINE_REAL_NOTES}` → `$BASELINE_REAL_NOTES`
- `{EXPERT_AGENT_NAME}` → `"expert"`
- `{MAIN_SESSION_NAME}` → `"main-session"` (already present)

- [ ] **Step 6: Add Phase 2/3 message handling to Step 2 wait loop**

In Step 2, the main session waits for messages from the writer. Extend the message handler
to cover `baseline-ready:` and `treatment-ready:` in addition to the existing `done:`,
`escalate:`, and `build-failed:` cases:

```markdown
**On "baseline-ready: ...":**

```python
python3 -c "print(open('$RUN_DIR/baseline_config.yaml').read())"
```

Print to user:
```
━━━ Baseline Config (derived from sim → real EPP) ━━━
<file contents above>
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Provide feedback to revise, or type 'done' to proceed.
```

Read user input via `AskUserQuestion` or standard input. If feedback:
```
SendMessage("writer", "feedback: <user feedback text>")
```
If "done": update state and continue —
```bash
python3 -c "
import sys
sys.path.insert(0, '$REPO_ROOT')
from pipeline.lib.state_machine import StateMachine
state = StateMachine.load('$RUN_DIR')
state.update('baseline_derivation', status='done', user_approved=True)
print('State: baseline_derivation done')
"
SendMessage("writer", "continue")
```

**On "treatment-ready: ...":**

```python
python3 -c "print(open('$RUN_DIR/treatment_config.yaml').read())"
```

Print to user:
```
━━━ Treatment Config (derived from baseline + algorithm) ━━━
<file contents above>
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Provide feedback to revise, or type 'done' to proceed.
```

Handle feedback / continue as for `baseline-ready:`. On continue, update state:
```bash
python3 -c "
import sys
sys.path.insert(0, '$REPO_ROOT')
from pipeline.lib.state_machine import StateMachine
state = StateMachine.load('$RUN_DIR')
state.update('treatment_derivation', status='done', user_approved=True)
print('State: treatment_derivation done')
"
SendMessage("writer", "continue")
```

**On "review-passed: round=N ...":**

```bash
python3 -c "
import json
o = json.load(open('$RUN_DIR/translation_output.json'))
print('Plugin files:', o.get('files_created', []))
"
python3 -c "print(open('$RUN_DIR/treatment_config.yaml').read())"
```

Print to user:
```
━━━ Round N: Reviewer APPROVE ━━━
Treatment Config: <contents above>
Plugin files: <list above>

Provide feedback for another round, or type 'done' to finish.
```

If feedback: `SendMessage("writer", "feedback: <text>")`
If "done": `SendMessage("writer", "done")`

**On "done: ...":**

Proceed to Step 6 (shutdown + output).
```

- [ ] **Step 7: Add Expert shutdown to Step 6 output**

In Step 6, add Expert to the shutdown sequence:

```bash
SendMessage("writer", "shutdown")
SendMessage("reviewer", "shutdown")
SendMessage("expert", "shutdown")
TeamDelete()
```

- [ ] **Step 8: Commit**

```bash
git add .claude/skills/sim2real-translate/SKILL.md
git commit -m "feat(skill): add Expert agent, Phase 2/3 baseline+treatment derivation, user pause points"
```

### Task 8: Smoke-test the full pipeline with updated manifest

- [ ] **Step 1: Verify full test suite still passes**

```bash
python -m pytest pipeline/tests/ -v 2>&1 | tail -30
```

Expected: All pass.

- [ ] **Step 2: Verify prepare.py loads and runs Phase 1 with updated transfer.yaml**

```bash
python pipeline/prepare.py --dry-run 2>&1 | head -30
```

If `--dry-run` is not supported, verify manifest loads:
```bash
python -c "
from pipeline.lib.manifest import load_manifest
from pathlib import Path
m = load_manifest(Path('config/transfer.yaml'))
assert m['version'] == 3
assert 'baseline_epp_template.yaml' in m['baseline']['real']['config']
print('transfer.yaml v3 loads cleanly')
"
```

Expected: No errors, version 3 confirmed.

- [ ] **Step 3: Final commit if any fixups needed**

```bash
git add -p
git commit -m "fix: post-integration fixups from smoke test"
```
