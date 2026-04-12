# Pipeline Redesign Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rewrite the sim2real transfer pipeline from a monolithic 9-stage architecture into a clean 4-script + Claude Code skill architecture with deterministic Python for mechanical work and an interactive skill for LLM translation.

**Architecture:** `setup.py` (interactive bootstrap) → `prepare.py` (6-phase state machine with translation checkpoint) → translation skill (out of scope) → `deploy.py` (fire-and-forget) → `analyze.py` (local comparison). New foundation libraries: manifest v2 loader, state machine, context builder. Config restructured into `common` + `scenarios`.

**Tech Stack:** Python 3.10+, PyYAML, requests, pytest. Go toolchain for build/test gates. kubectl/tkn for cluster operations.

**Spec:** `docs/superpowers/brainstorms/2026-04-09-pipeline-redesign.md`

---

## File Structure

### New Files
| File | Responsibility |
|------|---------------|
| `scripts/lib/state_machine.py` | Phase state tracking with `.state.json` persistence |
| `scripts/lib/context_builder.py` | Context document assembly with SHA-256 caching |
| `scripts/test_state_machine.py` | State machine unit tests |
| `scripts/test_context_builder.py` | Context builder unit tests |
| `prompts/prepare/translate.md` | Writer prompt (generic, scenario-agnostic) |
| `prompts/prepare/review.md` | Multi-model reviewer prompt |
| `prompts/deploy/build-push.md` | EPP build prompt (moved from `prompts/build-push.md`) |
| `prompts/deploy/validate.md` | Cluster validation prompt (moved from `prompts/validate.md`) |

### Modified Files
| File | Change |
|------|--------|
| `scripts/lib/manifest.py` | Rewrite for v2 schema (scenario, algorithm.config, etc.) |
| `config/env_defaults.yaml` | Restructure: flat → `common` + `scenarios` sections |
| `config/transfer.yaml` | Migrate v1 → v2 schema |
| `config/transfer_golden.yaml` | Migrate v1 → v2 schema |
| `tools/transfer_cli.py` | Add `--scenario` to `merge-values`; strip pipeline-only keys |
| `scripts/prepare.py` | Complete rewrite: 2553-line monolith → 6-phase state machine |
| `scripts/deploy.py` | Simplify: remove polling/waiting, add fire-and-forget + `collect` |
| `scripts/validate.py` | Remove `pre-deploy` subcommand |
| `scripts/analyze.py` | Add package discovery from `cluster/` dirs, `--package` flag |
| `scripts/setup.py` | Add interactive-by-default mode, `--test-push` credential verification |
| `scripts/lib/validate_checks.py` | Remove `run_pre_deploy_checks` and related functions |
| `scripts/test_manifest.py` | Rewrite tests for v2 schema |

### Deleted Files
| File | Reason |
|------|--------|
| `scripts/lib/gates.py` | Replaced by skill's built-in reviewer loop |
| `scripts/test_prepare_snapshot.py` | Replaced by prepare.py rewrite tests |
| `prompts/extract.md` | Extract folded into translate |
| `prompts/extract-full.md` | Extract folded into translate |
| `prompts/translate.md` | Replaced by `prompts/prepare/translate.md` |
| `prompts/test.md` | Build/test handled by writer conversation |
| `prompts/transfer.md` | Old orchestrator replaced by skill |
| `prompts/validate-translation.md` | Replaced by `prompts/prepare/review.md` |
| `prompts/equivalence-gate.md` | Equivalence gate removed |
| `prompts/pr.md` | PR feature removed |
| `prompts/generate.md` | Replaced by `prompts/prepare/translate.md` |

---

## Chunk 1: Foundation Libraries

Foundation libraries that everything else depends on. Pure library code with full test coverage, no script changes yet.

### Task 1: Manifest v2 Loader

Rewrite `scripts/lib/manifest.py` for the v2 transfer.yaml schema.

**Files:**
- Modify: `scripts/lib/manifest.py`
- Modify: `scripts/test_manifest.py`

- [ ] **Step 1: Write failing tests for v2 schema validation**

In `scripts/test_manifest.py`, replace v1 tests with v2 tests:

```python
"""Tests for v2 manifest loader."""
import pytest
import yaml
from pathlib import Path
from lib.manifest import load_manifest, ManifestError

MINIMAL_V2 = {
    "kind": "sim2real-transfer",
    "version": 2,
    "scenario": "routing",
    "algorithm": {
        "source": "sim2real_golden/routers/router_adaptive_v2.go",
        "config": "sim2real_golden/routers/policy_adaptive_v2.yaml",
    },
    "baseline": {"config": "sim2real_golden/routers/policy_baseline_211.yaml"},
    "workloads": ["sim2real_golden/workloads/wl1.yaml"],
    "llm_config": "sim2real_golden/llm_config.yaml",
}

def _write_manifest(tmp_path, data):
    p = tmp_path / "transfer.yaml"
    p.write_text(yaml.dump(data))
    return p

def test_load_valid_v2(tmp_path):
    path = _write_manifest(tmp_path, MINIMAL_V2)
    m = load_manifest(path)
    assert m["scenario"] == "routing"
    assert m["algorithm"]["source"].endswith(".go")

def test_v1_raises_migration_error(tmp_path):
    v1 = {"kind": "sim2real-transfer", "version": 1, "algorithm": {"experiment_dir": "x"}}
    path = _write_manifest(tmp_path, v1)
    with pytest.raises(ManifestError, match="v1.*v2"):
        load_manifest(path)

def test_missing_version_raises(tmp_path):
    data = {k: v for k, v in MINIMAL_V2.items() if k != "version"}
    path = _write_manifest(tmp_path, data)
    with pytest.raises(ManifestError, match="version"):
        load_manifest(path)

def test_missing_required_field(tmp_path):
    for field in ["scenario", "algorithm", "baseline", "workloads", "llm_config"]:
        data = {k: v for k, v in MINIMAL_V2.items() if k != field}
        path = _write_manifest(tmp_path, data)
        with pytest.raises(ManifestError, match=field):
            load_manifest(path)

def test_missing_algorithm_source(tmp_path):
    data = {**MINIMAL_V2, "algorithm": {"config": "x.yaml"}}
    path = _write_manifest(tmp_path, data)
    with pytest.raises(ManifestError, match="algorithm.source"):
        load_manifest(path)

def test_optional_context_fields(tmp_path):
    data = {**MINIMAL_V2, "context": {
        "files": ["docs/mapping.md"],
        "notes": "Use regime detection pattern",
    }}
    path = _write_manifest(tmp_path, data)
    m = load_manifest(path)
    assert m["context"]["notes"] == "Use regime detection pattern"
    assert len(m["context"]["files"]) == 1

def test_missing_algorithm_config(tmp_path):
    data = {**MINIMAL_V2, "algorithm": {"source": "x.go"}}
    path = _write_manifest(tmp_path, data)
    with pytest.raises(ManifestError, match="algorithm.config"):
        load_manifest(path)

def test_missing_baseline_config(tmp_path):
    data = {**MINIMAL_V2, "baseline": {}}
    path = _write_manifest(tmp_path, data)
    with pytest.raises(ManifestError, match="baseline.config"):
        load_manifest(path)

def test_workloads_must_be_list(tmp_path):
    data = {**MINIMAL_V2, "workloads": "not_a_list.yaml"}
    path = _write_manifest(tmp_path, data)
    with pytest.raises(ManifestError, match="workloads.*list"):
        load_manifest(path)

def test_workloads_must_be_nonempty(tmp_path):
    data = {**MINIMAL_V2, "workloads": []}
    path = _write_manifest(tmp_path, data)
    with pytest.raises(ManifestError, match="workloads.*at least"):
        load_manifest(path)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/jchen/go/src/inference-sim/sim2real && python -m pytest scripts/test_manifest.py -v`
Expected: FAIL (v2 validation not implemented yet)

- [ ] **Step 3: Rewrite manifest.py for v2**

Replace contents of `scripts/lib/manifest.py`:

```python
"""Manifest loader for sim2real pipeline (v2 schema)."""
import yaml
from pathlib import Path

class ManifestError(Exception):
    """Manifest validation error."""

_REQUIRED_TOP = ["kind", "version", "scenario", "algorithm", "baseline", "workloads", "llm_config"]
_REQUIRED_ALGORITHM = ["source", "config"]
_REQUIRED_BASELINE = ["config"]

def load_manifest(path: Path | str) -> dict:
    """Load and validate a v2 sim2real transfer manifest."""
    path = Path(path)
    if not path.exists():
        raise ManifestError(f"Manifest not found: {path}")

    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        raise ManifestError(f"YAML parse error in {path}: {e}") from e

    if data.get("kind") != "sim2real-transfer":
        raise ManifestError(f"Expected kind: sim2real-transfer, got: {data.get('kind')}")

    version = data.get("version")
    if version is None:
        raise ManifestError("Missing required field: version")
    if version == 1:
        raise ManifestError(
            "This is a v1 manifest. v2 is required.\n"
            "Migration: rename algorithm.policy → algorithm.config, "
            "add scenario field, move target/config to env_defaults.yaml.\n"
            "See docs/transfer/migration-v1-to-v2.md for details."
        )
    if version != 2:
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
    for f in _REQUIRED_BASELINE:
        if f not in bl:
            raise ManifestError(f"Missing required field: baseline.{f}")

    if not isinstance(data["workloads"], list):
        raise ManifestError("workloads must be a list")
    if len(data["workloads"]) == 0:
        raise ManifestError("workloads must contain at least one path")

    return data
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/jchen/go/src/inference-sim/sim2real && python -m pytest scripts/test_manifest.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/lib/manifest.py scripts/test_manifest.py
git commit -m "feat(manifest): rewrite loader for v2 schema

V2 requires: scenario, algorithm.{source,config}, baseline.config,
workloads, llm_config. V1 manifests raise with migration instructions."
```

---

### Task 2: State Machine

New module for tracking prepare.py phase state in `.state.json`.

**Files:**
- Create: `scripts/lib/state_machine.py`
- Create: `scripts/test_state_machine.py`

- [ ] **Step 1: Write failing tests**

```python
"""Tests for phase state machine."""
import json
import pytest
from pathlib import Path
from lib.state_machine import StateMachine

def test_new_state_machine(tmp_path):
    sm = StateMachine("test-run", "routing", tmp_path)
    assert not sm.is_done("init")
    assert sm.run_name == "test-run"
    assert sm.scenario == "routing"

def test_mark_done_and_persist(tmp_path):
    sm = StateMachine("test-run", "routing", tmp_path)
    sm.mark_done("init")
    assert sm.is_done("init")
    # Verify persisted to disk
    state_file = tmp_path / ".state.json"
    assert state_file.exists()
    data = json.loads(state_file.read_text())
    assert data["phases"]["init"]["status"] == "done"

def test_mark_done_with_metadata(tmp_path):
    sm = StateMachine("test-run", "routing", tmp_path)
    sm.mark_done("context", hash="a1b2c3", cached=True)
    data = json.loads((tmp_path / ".state.json").read_text())
    assert data["phases"]["context"]["hash"] == "a1b2c3"
    assert data["phases"]["context"]["cached"] is True

def test_load_existing_state(tmp_path):
    # Write state, create new instance, verify loaded
    sm1 = StateMachine("test-run", "routing", tmp_path)
    sm1.mark_done("init")
    sm1.mark_done("context", hash="abc")
    sm2 = StateMachine.load(tmp_path)
    assert sm2.is_done("init")
    assert sm2.is_done("context")
    assert sm2.run_name == "test-run"

def test_load_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        StateMachine.load(tmp_path)

def test_reset_phase(tmp_path):
    sm = StateMachine("test-run", "routing", tmp_path)
    sm.mark_done("init")
    sm.reset("init")
    assert not sm.is_done("init")

def test_get_phase_metadata(tmp_path):
    sm = StateMachine("test-run", "routing", tmp_path)
    sm.mark_done("translate", plugin_type="adaptive-v2-scorer", review_rounds=3)
    meta = sm.get_phase("translate")
    assert meta["plugin_type"] == "adaptive-v2-scorer"
    assert meta["review_rounds"] == 3

def test_increment_checkpoint_hits(tmp_path):
    sm = StateMachine("test-run", "routing", tmp_path)
    sm.increment("translate", "checkpoint_hits")
    sm.increment("translate", "checkpoint_hits")
    assert sm.get_phase("translate")["checkpoint_hits"] == 2

def test_atomic_save(tmp_path):
    """Save uses tmp+rename so partial writes don't corrupt state."""
    sm = StateMachine("test-run", "routing", tmp_path)
    sm.mark_done("init")
    state_file = tmp_path / ".state.json"
    data = json.loads(state_file.read_text())
    assert "run_name" in data  # valid JSON after save
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/jchen/go/src/inference-sim/sim2real && python -m pytest scripts/test_state_machine.py -v`
Expected: FAIL (module doesn't exist)

- [ ] **Step 3: Implement state_machine.py**

```python
"""Phase state machine with JSON persistence."""
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

class StateMachine:
    """Tracks prepare.py phase progress in .state.json."""

    def __init__(self, run_name: str, scenario: str, run_dir: Path):
        self.run_name = run_name
        self.scenario = scenario
        self._run_dir = Path(run_dir)
        self._phases: dict = {}
        self._save()

    @classmethod
    def load(cls, run_dir: Path) -> "StateMachine":
        path = Path(run_dir) / ".state.json"
        if not path.exists():
            raise FileNotFoundError(f"No state file: {path}")
        data = json.loads(path.read_text())
        inst = object.__new__(cls)
        inst.run_name = data["run_name"]
        inst.scenario = data["scenario"]
        inst._run_dir = Path(run_dir)
        inst._phases = data.get("phases", {})
        return inst

    def is_done(self, phase: str) -> bool:
        return self._phases.get(phase, {}).get("status") == "done"

    def mark_done(self, phase: str, **metadata):
        self._phases[phase] = {
            "status": "done",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **metadata,
        }
        self._save()

    def reset(self, phase: str):
        self._phases.pop(phase, None)
        self._save()

    def get_phase(self, phase: str) -> dict:
        return dict(self._phases.get(phase, {}))

    def increment(self, phase: str, key: str):
        if phase not in self._phases:
            self._phases[phase] = {}
        self._phases[phase][key] = self._phases[phase].get(key, 0) + 1
        self._save()

    def _save(self):
        self._run_dir.mkdir(parents=True, exist_ok=True)
        data = {
            "run_name": self.run_name,
            "scenario": self.scenario,
            "phases": self._phases,
        }
        path = self._run_dir / ".state.json"
        # Atomic write: tmp file + rename
        fd, tmp = tempfile.mkstemp(dir=self._run_dir, suffix=".tmp")
        try:
            with open(fd, "w") as f:
                json.dump(data, f, indent=2)
            Path(tmp).replace(path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/jchen/go/src/inference-sim/sim2real && python -m pytest scripts/test_state_machine.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/lib/state_machine.py scripts/test_state_machine.py
git commit -m "feat(lib): add state machine for prepare phase tracking

Tracks phase status in .state.json with atomic saves. Supports
mark_done with metadata, reset, increment counters, and load from disk."
```

---

### Task 3: Context Builder

New module for assembling and caching context documents.

**Files:**
- Create: `scripts/lib/context_builder.py`
- Create: `scripts/test_context_builder.py`

- [ ] **Step 1: Write failing tests**

```python
"""Tests for context builder."""
import hashlib
import pytest
from pathlib import Path
from lib.context_builder import build_context, compute_context_hash

def _write_file(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)

def test_compute_hash_deterministic(tmp_path):
    f1 = tmp_path / "a.md"
    f1.write_text("content A")
    h1 = compute_context_hash([f1], {"sim": "abc", "sched": "def"})
    h2 = compute_context_hash([f1], {"sim": "abc", "sched": "def"})
    assert h1 == h2

def test_hash_changes_on_file_content(tmp_path):
    f1 = tmp_path / "a.md"
    f1.write_text("v1")
    h1 = compute_context_hash([f1], {"sim": "abc", "sched": "def"})
    f1.write_text("v2")
    h2 = compute_context_hash([f1], {"sim": "abc", "sched": "def"})
    assert h1 != h2

def test_hash_changes_on_submodule_sha(tmp_path):
    f1 = tmp_path / "a.md"
    f1.write_text("same")
    h1 = compute_context_hash([f1], {"sim": "abc", "sched": "def"})
    h2 = compute_context_hash([f1], {"sim": "abc", "sched": "xyz"})
    assert h1 != h2

def test_build_context_creates_file(tmp_path):
    f1 = tmp_path / "docs" / "mapping.md"
    _write_file(f1, "# Mapping\nSignal A → Signal B")
    cache_dir = tmp_path / "cache"
    path, cached = build_context(
        context_files=[f1],
        submodule_shas={"sim": "abc123", "sched": "def456"},
        scenario="routing",
        cache_dir=cache_dir,
    )
    assert path.exists()
    assert not cached
    content = path.read_text()
    assert "# Translation Context" in content
    assert "Signal A → Signal B" in content

def test_build_context_cache_hit(tmp_path):
    f1 = tmp_path / "docs" / "mapping.md"
    _write_file(f1, "# Mapping")
    cache_dir = tmp_path / "cache"
    shas = {"sim": "abc", "sched": "def"}
    _, cached1 = build_context([f1], shas, "routing", cache_dir)
    _, cached2 = build_context([f1], shas, "routing", cache_dir)
    assert not cached1
    assert cached2

def test_build_context_cache_miss_after_change(tmp_path):
    f1 = tmp_path / "mapping.md"
    f1.write_text("v1")
    cache_dir = tmp_path / "cache"
    shas = {"sim": "abc", "sched": "def"}
    _, c1 = build_context([f1], shas, "routing", cache_dir)
    f1.write_text("v2")
    _, c2 = build_context([f1], shas, "routing", cache_dir)
    assert not c1
    assert not c2

def test_missing_context_file_raises(tmp_path):
    cache_dir = tmp_path / "cache"
    with pytest.raises(FileNotFoundError):
        build_context(
            [tmp_path / "nonexistent.md"],
            {"sim": "abc", "sched": "def"},
            "routing",
            cache_dir,
        )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/jchen/go/src/inference-sim/sim2real && python -m pytest scripts/test_context_builder.py -v`
Expected: FAIL

- [ ] **Step 3: Implement context_builder.py**

```python
"""Context document assembly with SHA-256 caching."""
import hashlib
from pathlib import Path

def compute_context_hash(files: list[Path], submodule_shas: dict[str, str]) -> str:
    """SHA-256 of file contents + submodule SHAs. Notes excluded."""
    h = hashlib.sha256()
    for f in sorted(files):
        h.update(f.read_bytes())
    for key in sorted(submodule_shas):
        h.update(f"{key}={submodule_shas[key]}".encode())
    return h.hexdigest()[:12]

def build_context(
    context_files: list[Path],
    submodule_shas: dict[str, str],
    scenario: str,
    cache_dir: Path,
) -> tuple[Path, bool]:
    """Assemble context.md from files. Returns (path, was_cached)."""
    # Validate all files exist first
    for f in context_files:
        if not f.exists():
            raise FileNotFoundError(f"Context file not found: {f}")

    content_hash = compute_context_hash(context_files, submodule_shas)
    cache_path = cache_dir / scenario / f"{content_hash}.md"

    if cache_path.exists():
        return cache_path, True

    # Assemble context document
    sha_summary = " | ".join(f"{k}@{v[:7]}" for k, v in sorted(submodule_shas.items()))
    lines = [f"# Translation Context", f"Scenario: {scenario} | {sha_summary}", ""]
    for f in context_files:
        lines.append(f"## {f}")
        lines.append(f.read_text())
        lines.append("")

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text("\n".join(lines))
    return cache_path, False
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/jchen/go/src/inference-sim/sim2real && python -m pytest scripts/test_context_builder.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/lib/context_builder.py scripts/test_context_builder.py
git commit -m "feat(lib): add context builder with SHA-256 caching

Assembles context.md from context files, caches by content hash +
submodule SHAs. Notes excluded from hash (delivered via skill_input.json)."
```

---

## Chunk 2: Config Migration

Restructure configs and update merge-values. This is the schema break that enables scenarios.

### Task 4: Restructure env_defaults.yaml

Move from flat structure to `common` + `scenarios` sections.

**Files:**
- Modify: `config/env_defaults.yaml`

- [ ] **Step 1: Restructure env_defaults.yaml**

Rewrite `config/env_defaults.yaml` with the new structure. The current flat keys move into `common` (shared infra) and `scenarios.routing` (routing-specific). Key changes:
- `stack.*` → `common.stack.*` (shared infra like gateway, model, gaie images)
- `observe.*` → `common.observe.*`
- `pipeline.*` → removed (`fast_iteration` eliminated)
- Add `common.build.commands` with go build/vet
- Add `common.build.test_scope` default `"./..."`
- Add `scenarios.routing.target` with repo, plugin_dir, register_file, package
- Add `scenarios.routing.build.test_scope` override
- Add `scenarios.routing.config` with kind and helm_path
- Move `stack.gaie.baseline` → `scenarios.routing.gaie.baseline`
- Move `stack.gaie.shared` → `scenarios.routing.gaie.shared`
- Add stub `scenarios.admission_control` section

Reference the brainstorm spec Section 4 (`env_defaults.yaml` structure, lines 320-418) for exact field layout.

- [ ] **Step 2: Verify YAML parses correctly**

Run: `cd /Users/jchen/go/src/inference-sim/sim2real && python -c "import yaml; yaml.safe_load(open('config/env_defaults.yaml')); print('OK')"`
Expected: OK

- [ ] **Step 3: Commit**

```bash
git add config/env_defaults.yaml
git commit -m "refactor(config): restructure env_defaults to common + scenarios

Move flat structure into common (shared infra) and scenarios.routing
(routing-specific target, build, config, gaie). Add common.build.commands
for go build/vet. Remove pipeline.fast_iteration."
```

---

### Task 5: Migrate transfer.yaml to v2

Update both transfer.yaml and transfer_golden.yaml to v2 schema.

**Files:**
- Modify: `config/transfer.yaml`
- Modify: `config/transfer_golden.yaml`

- [ ] **Step 1: Migrate transfer.yaml**

Apply v1→v2 migration:
- `version: 1` → `version: 2`
- Add `scenario: routing`
- `algorithm.policy` → `algorithm.config`
- `algorithm.baseline` → `baseline.config` (top-level `baseline` section)
- Remove `algorithm.experiment_dir` — make all paths repo-root-relative
- Remove `target` section (moved to env_defaults scenarios)
- Remove `context.template`, `context.examples` (user describes in notes)
- `context.mapping` → entry in `context.files` list
- Add `context.notes` with routing-specific translation hints
- Remove `config` section (moved to env_defaults scenarios)
- Remove `validation` section
- Remove `artifacts` section

- [ ] **Step 2: Apply same migration to transfer_golden.yaml**

- [ ] **Step 3: Verify both load with new manifest.py**

Run: `cd /Users/jchen/go/src/inference-sim/sim2real && python -c "from scripts.lib.manifest import load_manifest; m = load_manifest('config/transfer.yaml'); print(f'v{m[\"version\"]} scenario={m[\"scenario\"]}')"`
Expected: `v2 scenario=routing`

- [ ] **Step 4: Commit**

```bash
git add config/transfer.yaml config/transfer_golden.yaml
git commit -m "feat(config): migrate transfer.yaml to v2 schema

Add scenario field, rename algorithm.policy → algorithm.config,
move target/config/validation/artifacts to env_defaults scenarios."
```

---

### Task 6: Update merge-values with --scenario support

Extend `tools/transfer_cli.py` `merge-values` to resolve `common + scenarios[scenario]` before merging with algorithm values.

**Files:**
- Modify: `tools/transfer_cli.py`

- [ ] **Step 1: Write a test for the new merge-values --scenario behavior**

Create a temporary test that verifies: given env_defaults with `common` + `scenarios.routing`, and an algorithm_values file, `merge-values --scenario routing` produces correct merged output with pipeline-only keys stripped.

Run the test with the existing `merge-values` command to confirm it fails (no `--scenario` flag yet).

- [ ] **Step 2: Add --scenario argument to merge-values subparser**

In `tools/transfer_cli.py`, find the `merge-values` subparser registration and add:
```python
p.add_argument("--scenario", metavar="NAME",
               help="Scenario name to resolve from env_defaults common + scenarios sections")
```

- [ ] **Step 3: Implement scenario resolution in cmd_merge_values**

In `cmd_merge_values()`, after loading `env_data`:

```python
# If --scenario provided, resolve common + scenario overlay
if args.scenario:
    common = env_data.get("common", {})
    scenarios = env_data.get("scenarios", {})
    if args.scenario not in scenarios:
        print(f"ERROR: scenario '{args.scenario}' not found in env_defaults. "
              f"Available: {list(scenarios.keys())}", file=sys.stderr)
        return 2
    env_data = _deep_merge(common, scenarios[args.scenario])
    # Strip pipeline-only keys (used by prepare.py, not cluster YAML)
    for key in ["target", "build", "config"]:
        env_data.pop(key, None)
```

- [ ] **Step 4: Run the test to verify it passes**

- [ ] **Step 5: Commit**

```bash
git add tools/transfer_cli.py
git commit -m "feat(cli): add --scenario to merge-values

Resolves common + scenarios[name] before merging with algorithm values.
Strips pipeline-only keys (target, build, config) from output."
```

---

## Chunk 3: prepare.py Rewrite

The core rewrite. Replace the 2553-line monolith with a 6-phase state machine.

### Task 7: prepare.py Skeleton + Phase 1 (Init)

Create the new prepare.py structure with CLI parser, shared helpers, and Phase 1.

**Files:**
- Modify: `scripts/prepare.py` (full rewrite)

- [ ] **Step 1: Back up the old prepare.py for reference**

```bash
cp scripts/prepare.py scripts/prepare_legacy.py
```

- [ ] **Step 2: Write the new prepare.py skeleton**

New structure with imports, constants, CLI parser (subcommands: `run`, `context`, `assemble`, `validate-assembly`, `status`; flags: `--force`, `--rebuild-context`, `--manifest`, `--run`), color helpers, and `main()` dispatcher.

Key imports: `lib.manifest.load_manifest`, `lib.state_machine.StateMachine`, `lib.context_builder.build_context`.

Phase 1 (Init) function:
```python
def _phase_init(args, manifest: dict, run_dir: Path) -> StateMachine:
    """Phase 1: Load manifest, resolve scenario config, create state."""
    import yaml

    # Load env_defaults and resolve scenario
    env_path = REPO_ROOT / "config" / "env_defaults.yaml"
    env_data = yaml.safe_load(env_path.read_text())
    common = env_data.get("common", {})
    scenarios = env_data.get("scenarios", {})
    scenario = manifest["scenario"]
    if scenario not in scenarios:
        err(f"Scenario '{scenario}' not found in env_defaults.yaml. "
            f"Available: {list(scenarios.keys())}")
        sys.exit(1)

    # Deep-merge common + scenario
    from tools_import import deep_merge  # or inline
    resolved = _deep_merge(common, scenarios[scenario])

    # Validate prerequisites
    for path_key in ["algorithm.source", "algorithm.config", "baseline.config"]:
        parts = path_key.split(".")
        val = manifest
        for p in parts:
            val = val[p]
        if not (REPO_ROOT / val).exists():
            err(f"File not found: {val}")
            sys.exit(1)

    # Create or load state machine
    if (run_dir / ".state.json").exists() and not args.force:
        state = StateMachine.load(run_dir)
        if state.is_done("init"):
            info("[skip] Init phase already complete")
            return state

    state = StateMachine(args.run or _default_run_name(), scenario, run_dir)
    state._resolved_config = resolved  # attach for later phases
    state.mark_done("init")
    ok(f"Init complete: run={state.run_name} scenario={scenario}")
    return state
```

- [ ] **Step 3: Implement main() dispatcher**

```python
def main():
    parser = build_parser()
    args = parser.parse_args()
    manifest = load_manifest(args.manifest or REPO_ROOT / "config" / "transfer.yaml")

    setup_config = _load_setup_config()
    run_name = args.run or setup_config.get("current_run", _default_run_name())
    run_dir = REPO_ROOT / "workspace" / "runs" / run_name

    cmd = getattr(args, "command", "run")
    if cmd == "status":
        return _cmd_status(run_dir)
    if cmd == "run":
        return _cmd_run(args, manifest, run_dir)
    if cmd == "context":
        return _cmd_context(args, manifest, run_dir)
    if cmd == "assemble":
        return _cmd_assemble(args, manifest, run_dir)
    if cmd == "validate-assembly":
        return _cmd_validate_assembly(args, manifest, run_dir)
```

- [ ] **Step 4: Verify skeleton runs**

Run: `cd /Users/jchen/go/src/inference-sim/sim2real && python scripts/prepare.py status`
Expected: Prints state or "no state file" message

- [ ] **Step 5: Commit**

```bash
git add scripts/prepare.py scripts/prepare_legacy.py
git commit -m "feat(prepare): rewrite as 6-phase state machine — Phase 1 Init

Replace monolithic prepare.py with state machine architecture.
Phase 1 loads v2 manifest, resolves scenario config from env_defaults,
validates prerequisites, creates .state.json."
```

---

### Task 8: Phase 2 (Context) + Phase 3 (Translation Checkpoint)

Implement context building with caching and the skill handoff checkpoint.

**Files:**
- Modify: `scripts/prepare.py`

- [ ] **Step 1: Implement Phase 2 (Context)**

```python
def _phase_context(args, state: StateMachine, manifest: dict, run_dir: Path,
                   resolved_config: dict) -> Path:
    """Phase 2: Build context document with caching."""
    if state.is_done("context") and not args.rebuild_context and not args.force:
        cached_path = Path(state.get_phase("context").get("path", ""))
        if cached_path.exists():
            info(f"[skip] Context cached: {cached_path}")
            return cached_path

    # Resolve context files from manifest
    context_files = []
    for f in manifest.get("context", {}).get("files", []):
        full = REPO_ROOT / f
        if not full.exists():
            err(f"Context file not found: {f}")
            sys.exit(1)
        context_files.append(full)

    # Get submodule SHAs
    shas = _get_submodule_shas(resolved_config)

    path, cached = build_context(
        context_files=context_files,
        submodule_shas=shas,
        scenario=manifest["scenario"],
        cache_dir=REPO_ROOT / "workspace" / "context",
    )

    state.mark_done("context", hash=path.stem, cached=cached, path=str(path))
    ok(f"Context {'cached' if cached else 'built'}: {path}")
    return path
```

- [ ] **Step 2: Implement Phase 3 (Translation Checkpoint)**

This is the critical handoff point. Write `skill_input.json`, check for `translation_output.json`.

```python
def _phase_translate(args, state: StateMachine, manifest: dict, run_dir: Path,
                     resolved_config: dict, context_path: Path):
    """Phase 3: Translation checkpoint — write skill_input.json, check for output."""
    if state.is_done("translate") and not args.force:
        info("[skip] Translation already complete")
        return

    target = resolved_config["target"]
    build_cfg = resolved_config.get("build", {})
    config_cfg = resolved_config.get("config", {})

    # Build commands: common commands + test with scenario test_scope
    commands = list(build_cfg.get("commands", []))
    test_scope = build_cfg.get("test_scope", "./...")
    commands.append(["go", "test", "-timeout", "10m", "-v", test_scope])

    # Write skill_input.json
    skill_input = {
        "run_name": state.run_name,
        "run_dir": str(run_dir.relative_to(REPO_ROOT)),
        "scenario": manifest["scenario"],
        "context_path": str(context_path.relative_to(REPO_ROOT)),
        "context_notes": manifest.get("context", {}).get("notes", ""),
        "manifest_path": str(Path(args.manifest or "config/transfer.yaml")),
        "algorithm_source": manifest["algorithm"]["source"],
        "algorithm_config": manifest["algorithm"]["config"],
        "target": target,
        "build_commands": commands,
        "config_kind": config_cfg.get("kind", ""),
    }
    skill_input_path = run_dir / "skill_input.json"
    skill_input_path.write_text(json.dumps(skill_input, indent=2))

    # Check for translation output
    output_path = run_dir / "translation_output.json"
    if output_path.exists():
        output = json.loads(output_path.read_text())
        # Validate required fields
        for f in ["plugin_type", "files_created", "files_modified"]:
            if f not in output:
                err(f"translation_output.json missing required field: {f}")
                sys.exit(1)
        # Early kind validation
        tc_path = run_dir / "treatment_config.yaml"
        if tc_path.exists() and config_cfg.get("kind"):
            import yaml
            tc = yaml.safe_load(tc_path.read_text())
            if tc.get("kind") != config_cfg["kind"]:
                err(f"treatment_config.yaml kind '{tc.get('kind')}' doesn't match "
                    f"expected '{config_cfg['kind']}'")
                sys.exit(1)

        state.mark_done("translate",
                        plugin_type=output["plugin_type"],
                        files_created=output["files_created"])
        ok(f"Translation found: {output['plugin_type']}")
        return

    # No translation output yet — checkpoint
    state.increment("translate", "checkpoint_hits")
    hits = state.get_phase("translate").get("checkpoint_hits", 1)

    print(f"\n{'='*60}")
    print("  TRANSLATION CHECKPOINT")
    print(f"{'='*60}")
    print(f"\n  skill_input.json written to: {skill_input_path.relative_to(REPO_ROOT)}")
    print(f"\n  Next step: run the /sim2real-translate skill in Claude Code,")
    print(f"  then re-run: python scripts/prepare.py")
    if hits >= 3:
        warn(f"Checkpoint hit {hits} times. Have you run the translation skill?")
    print(f"\n{'='*60}\n")
    sys.exit(0)
```

- [ ] **Step 3: Wire phases into _cmd_run**

```python
def _cmd_run(args, manifest, run_dir):
    state = _phase_init(args, manifest, run_dir)
    resolved = _load_resolved_config(manifest)
    context_path = _phase_context(args, state, manifest, run_dir, resolved)
    _phase_translate(args, state, manifest, run_dir, resolved, context_path)
    _phase_assembly(args, state, manifest, run_dir, resolved)
    _phase_summary(state, manifest, run_dir, resolved)
    _phase_gate(state, run_dir)
```

- [ ] **Step 4: Test checkpoint behavior manually**

Run: `cd /Users/jchen/go/src/inference-sim/sim2real && python scripts/prepare.py`
Expected: Runs phases 1-2, pauses at Phase 3 checkpoint with instructions, exits 0.

- [ ] **Step 5: Commit**

```bash
git add scripts/prepare.py
git commit -m "feat(prepare): implement Phase 2 (Context) and Phase 3 (Translation Checkpoint)

Context phase builds and caches context.md by content hash.
Translation checkpoint writes skill_input.json and exits cleanly
if translation_output.json not found. Tracks checkpoint_hits."
```

---

### Task 9: Phase 4 (Assembly)

Mechanical assembly: treatment config validation, algorithm values generation, merge-values, cluster YAML compilation, validate-assembly checks.

**Files:**
- Modify: `scripts/prepare.py`

- [ ] **Step 1: Implement Phase 4a-4d (config validation + algorithm values + merge)**

```python
def _phase_assembly(args, state: StateMachine, manifest: dict, run_dir: Path,
                    resolved: dict):
    """Phase 4: Assemble cluster artifacts from translation output."""
    if state.is_done("assembly") and not args.force:
        info("[skip] Assembly already complete")
        return

    import yaml

    # 4a: Validate treatment config
    tc_path = run_dir / "treatment_config.yaml"
    if tc_path.exists():
        tc = yaml.safe_load(tc_path.read_text())
        expected_kind = resolved.get("config", {}).get("kind")
        if expected_kind and tc.get("kind") != expected_kind:
            err(f"treatment_config kind mismatch: got '{tc.get('kind')}', expected '{expected_kind}'")
            sys.exit(1)
        ok("Treatment config validated")

    # 4b: Baseline config from env_defaults (scenarios.<scenario>.gaie.baseline)
    # Already in resolved config — no action needed, will be in merged values

    # 4c: Generate algorithm_values.yaml
    alg_values_path = run_dir / "algorithm_values.yaml"
    _generate_algorithm_values(manifest, resolved, run_dir, alg_values_path)
    ok(f"Algorithm values: {alg_values_path.relative_to(REPO_ROOT)}")

    # 4d: Merge values
    values_path = run_dir / "values.yaml"
    _run_merge_values(manifest["scenario"], alg_values_path, values_path)
    ok(f"Values merged: {values_path.relative_to(REPO_ROOT)}")

    # 4e: Compile cluster YAMLs per package
    _compile_cluster_packages(manifest, run_dir, resolved, values_path)

    # 4f: Verify generated/ directory (created by translation skill)
    generated_dir = run_dir / "generated"
    if not generated_dir.exists():
        err("generated/ directory not found. The translation skill should create this.")
        err("Re-run the /sim2real-translate skill to produce generated file copies.")
        sys.exit(1)
    output = json.loads((run_dir / "translation_output.json").read_text())
    for f in output.get("files_created", []) + output.get("files_modified", []):
        if not (generated_dir / Path(f).name).exists():
            err(f"generated/ missing: {Path(f).name}")
            sys.exit(1)
    ok("Generated file copies verified")

    # 4g: validate-assembly
    _validate_assembly(run_dir, resolved)

    state.mark_done("assembly", packages=["baseline", "treatment"])
    ok("Assembly complete")
```

Implement each sub-function. `_generate_algorithm_values` can reuse logic from the existing `prepare.py:_generate_algorithm_values` (lines 1752-1898). `_run_merge_values` calls `transfer_cli.py merge-values --scenario`. `_compile_cluster_packages` creates `cluster/baseline/` and `cluster/treatment/` with `epp.yaml` + `pipelinerun.yaml`. `_validate_assembly` checks plugin_type consistency across register.go, epp.yaml, and values.yaml.

- [ ] **Step 2: Implement _generate_algorithm_values**

Port from existing prepare.py lines 1752-1898, adapting for v2 manifest fields:
- `manifest["llm_config"]` instead of `manifest["algorithm"]["llm_config"]`
- `manifest["workloads"]` is a flat list, not nested under experiment_dir
- Apply `request_multiplier` from resolved config

- [ ] **Step 3: Implement _compile_cluster_packages**

Create `cluster/baseline/` and `cluster/treatment/` directories. For each:
- Extract EPP config from merged values → `epp.yaml`
- Run `compile-pipeline` → `pipelinerun.yaml`

- [ ] **Step 4: Implement _validate_assembly**

```python
def _validate_assembly(run_dir: Path, resolved: dict):
    """Phase 4f: Deterministic consistency checks."""
    output = json.loads((run_dir / "translation_output.json").read_text())
    plugin_type = output["plugin_type"]
    config_cfg = resolved.get("config", {})
    target = resolved["target"]

    errors = []

    # Check 1: plugin_type in register.go
    register_path = REPO_ROOT / target["repo"] / target["register_file"]
    if register_path.exists():
        register_content = register_path.read_text()
        if plugin_type not in register_content:
            errors.append(f"plugin_type '{plugin_type}' not found in {target['register_file']}")

    # Check 2: plugin_type in treatment epp.yaml
    epp_path = run_dir / "cluster" / "treatment" / "epp.yaml"
    if epp_path.exists():
        epp_content = epp_path.read_text()
        if plugin_type not in epp_content:
            errors.append(f"plugin_type '{plugin_type}' not found in treatment epp.yaml")

    # Check 3: treatment_config kind matches scenario config
    tc_path = run_dir / "treatment_config.yaml"
    if tc_path.exists() and config_cfg.get("kind"):
        import yaml
        tc = yaml.safe_load(tc_path.read_text())
        if tc.get("kind") != config_cfg["kind"]:
            errors.append(f"treatment_config kind '{tc.get('kind')}' != expected '{config_cfg['kind']}'")

    # Check 4: all files_created exist in target repo
    for f in output.get("files_created", []):
        if not (REPO_ROOT / target["repo"] / f).exists():
            errors.append(f"files_created entry missing on disk: {f}")

    if errors:
        err("validate-assembly FAILED:")
        for e in errors:
            err(f"  - {e}")
        sys.exit(1)
    ok("validate-assembly: all checks passed")
```

- [ ] **Step 5: Implement standalone validate-assembly subcommand**

```python
def _cmd_validate_assembly(args, manifest, run_dir):
    resolved = _load_resolved_config(manifest)
    # Pre-check required files
    required = ["translation_output.json", "treatment_config.yaml"]
    for name in required:
        if not (run_dir / name).exists():
            err(f"Required file missing: {name}. Run translation skill first.")
            sys.exit(1)
    _validate_assembly(run_dir, resolved)
```

- [ ] **Step 6: Commit**

```bash
git add scripts/prepare.py
git commit -m "feat(prepare): implement Phase 4 (Assembly)

Validates treatment config, generates algorithm values, runs merge-values
with scenario support, compiles cluster packages (baseline + treatment),
runs validate-assembly consistency checks."
```

---

### Task 10: Phase 5 (Summary) + Phase 6 (Gate)

Generate run_summary.md and implement the human review gate.

**Files:**
- Modify: `scripts/prepare.py`

- [ ] **Step 1: Implement Phase 5 (Summary)**

```python
def _phase_summary(state: StateMachine, manifest: dict, run_dir: Path, resolved: dict):
    """Phase 5: Generate run_summary.md."""
    if state.is_done("summary") and not getattr(state, '_force', False):
        info("[skip] Summary already complete")
        return

    import yaml

    output = json.loads((run_dir / "translation_output.json").read_text())
    translate_meta = state.get_phase("translate")

    lines = [
        f"**Run Summary: `{state.run_name}`**",
        f"Generated: {datetime.now(timezone.utc).isoformat()} | Scenario: {manifest['scenario']}",
        "",
        "**Algorithm**",
        f"- Source: `{manifest['algorithm']['source']}`",
        f"- Description: {output.get('description', 'N/A')}",
        "",
        "**Translation**",
        f"- Plugin type: `{output['plugin_type']}`",
        f"- Files created: {', '.join(f'`{f}`' for f in output.get('files_created', []))}",
        f"- Files modified: {', '.join(f'`{f}`' for f in output.get('files_modified', []))}",
    ]

    if translate_meta.get("review_rounds"):
        lines.append(f"- Review: {translate_meta.get('consensus', 'N/A')} "
                      f"after {translate_meta['review_rounds']} rounds")

    # Baseline vs Treatment comparison
    lines.extend(["", "**Baseline vs. Treatment**", ""])
    # ... add comparison table from epp.yaml files

    # Checklist
    lines.extend([
        "", "**Checklist**",
        "- [x] Translation complete",
        "- [x] Assembly complete",
        "- [x] validate-assembly passed",
        "",
    ])

    summary_path = run_dir / "run_summary.md"
    summary_path.write_text("\n".join(lines))
    state.mark_done("summary")
    ok(f"Summary: {summary_path.relative_to(REPO_ROOT)}")
```

- [ ] **Step 2: Implement Phase 6 (Gate)**

```python
def _phase_gate(state: StateMachine, run_dir: Path):
    """Phase 6: Human review gate."""
    if state.is_done("gate"):
        verdict = state.get_phase("gate").get("verdict", "")
        info(f"[skip] Gate already complete: {verdict}")
        return

    summary_path = run_dir / "run_summary.md"
    print("\n" + summary_path.read_text())

    while True:
        choice = input("\n  [d]eploy / [e]dit / [q]uit: ").strip().lower()
        if choice in ("d", "deploy"):
            # Append verdict to summary
            with open(summary_path, "a") as f:
                f.write("\n**Verdict: READY TO DEPLOY**\n")
            state.mark_done("gate", verdict="READY TO DEPLOY")
            ok("Gate: READY TO DEPLOY")
            return
        elif choice in ("e", "edit"):
            info(f"Edit files in {run_dir}, then press Enter to re-display.")
            input("  Press Enter when done editing...")
            print("\n" + summary_path.read_text())
        elif choice in ("q", "quit"):
            state.mark_done("gate", verdict="abandoned")
            warn("Gate: abandoned")
            sys.exit(0)
        else:
            print("  Enter 'd' to deploy, 'e' to edit, or 'q' to quit.")
```

- [ ] **Step 3: Implement _cmd_run orchestrator**

Wire all 6 phases together:

```python
def _cmd_run(args, manifest, run_dir):
    state = _phase_init(args, manifest, run_dir)
    resolved = _load_resolved_config(manifest)
    context_path = _phase_context(args, state, manifest, run_dir, resolved)
    _phase_translate(args, state, manifest, run_dir, resolved, context_path)
    _phase_assembly(args, state, manifest, run_dir, resolved)
    _phase_summary(state, manifest, run_dir, resolved)
    _phase_gate(state, run_dir)
    ok(f"Pipeline complete. Deploy with: python scripts/deploy.py")
```

- [ ] **Step 4: Implement `context` and `assemble` subcommands**

```python
def _cmd_context(args, manifest, run_dir):
    """Rebuild context cache only."""
    state = _phase_init(args, manifest, run_dir)
    resolved = _load_resolved_config(manifest)
    args.rebuild_context = True  # force rebuild
    _phase_context(args, state, manifest, run_dir, resolved)

def _cmd_assemble(args, manifest, run_dir):
    """Re-run assembly from existing translation output."""
    state = StateMachine.load(run_dir)
    if not state.is_done("translate"):
        err("Cannot assemble: translation not complete. Run /sim2real-translate first.")
        sys.exit(1)
    resolved = _load_resolved_config(manifest)
    state.reset("assembly")
    state.reset("summary")
    state.reset("gate")
    _phase_assembly(args, state, manifest, run_dir, resolved)
    _phase_summary(state, manifest, run_dir, resolved)
    _phase_gate(state, run_dir)

def _cmd_status(run_dir):
    """Print current state."""
    try:
        state = StateMachine.load(run_dir)
    except FileNotFoundError:
        print("No active run.")
        return
    print(f"Run: {state.run_name} | Scenario: {state.scenario}")
    for phase, meta in state._phases.items():
        status = meta.get("status", "unknown")
        print(f"  {phase}: {status}")
```

- [ ] **Step 5: Commit**

```bash
git add scripts/prepare.py
git commit -m "feat(prepare): implement Phase 5 (Summary) and Phase 6 (Gate)

Summary generates run_summary.md with algorithm info, translation record,
and checklist. Gate prompts [d]eploy / [e]dit / [q]uit. Also implements
context, assemble, and status subcommands."
```

---

### Task 11: prepare.py Tests

Write unit tests for the new prepare.py.

**Files:**
- Create: `scripts/test_prepare_v2.py`

- [ ] **Step 1: Write tests for Phase 1 (Init)**

Test manifest loading, scenario resolution, prerequisite validation.

- [ ] **Step 2: Write tests for Phase 3 (Translation Checkpoint)**

Test skill_input.json writing, translation_output.json detection, checkpoint hit counting, early kind validation.

- [ ] **Step 3: Write tests for validate-assembly**

Test plugin_type consistency checks, kind matching, files_created existence.

- [ ] **Step 4: Run all tests**

Run: `cd /Users/jchen/go/src/inference-sim/sim2real && python -m pytest scripts/test_prepare_v2.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/test_prepare_v2.py
git commit -m "test(prepare): add unit tests for v2 prepare state machine

Tests Phase 1 init, Phase 3 translation checkpoint, and
validate-assembly consistency checks."
```

---

## Chunk 4: Deploy, Validate, Analyze, Setup

Smaller script changes that depend on the new workspace layout.

### Task 12: deploy.py Simplification

Simplify deploy.py to fire-and-forget with package support.

**Files:**
- Modify: `scripts/deploy.py`

- [ ] **Step 1: Simplify the CLI parser**

Replace existing argument parser with:
- Default command: deploy (build EPP + submit packages)
- `collect` subcommand (pull results from cluster)
- Flags: `--package NAME` (one or more), `--skip-build-epp`, `--dry-run`, `--run NAME`

Remove: `--fast-iteration`, `--parallel-workloads`, `--skip-noise`, `--skip-benchmarks`, PR-related flags.

- [ ] **Step 2: Rewrite main deploy flow**

Replace `stage_benchmarks()` (which polls and waits) with fire-and-forget:

```python
def _cmd_deploy(args, run_dir, setup_config):
    """Build EPP + submit packages, then exit."""
    # Pre-flight: check state
    state = StateMachine.load(run_dir)
    if not state.is_done("gate") or state.get_phase("gate").get("verdict") != "READY TO DEPLOY":
        err("Cannot deploy: prepare not complete or gate not approved.")
        sys.exit(1)

    namespace = setup_config["namespace"]
    registry = setup_config["registry"]

    # Discover packages
    cluster_dir = run_dir / "cluster"
    packages = _discover_packages(cluster_dir)
    if args.package:
        packages = [p for p in packages if p in args.package]
        missing = set(args.package) - set(packages)
        if missing:
            err(f"Packages not found: {missing}")
            sys.exit(1)

    if args.dry_run:
        _print_dry_run(packages, cluster_dir)
        return

    # Build EPP image
    if not args.skip_build_epp:
        _build_epp_image(run_dir, namespace, registry)

    # Submit packages
    submitted = {}
    for pkg in sorted(packages):
        pr_path = cluster_dir / pkg / "pipelinerun.yaml"
        run(["kubectl", "apply", "-f", str(pr_path), "-n", namespace])
        # Extract PipelineRun name from applied YAML
        import yaml
        pr_data = yaml.safe_load(pr_path.read_text())
        pr_name = pr_data["metadata"]["name"]
        submitted[pkg] = pr_name
        ok(f"Submitted: {pkg} → {pr_name}")

    # Print status and exit
    _print_status(submitted, namespace)
```

- [ ] **Step 3: Implement _discover_packages**

```python
def _discover_packages(cluster_dir: Path) -> list[str]:
    """A package is any subdirectory of cluster/ containing pipelinerun.yaml."""
    if not cluster_dir.exists():
        return []
    return sorted(
        d.name for d in cluster_dir.iterdir()
        if d.is_dir() and (d / "pipelinerun.yaml").exists()
    )
```

- [ ] **Step 4: Implement collect subcommand**

```python
def _cmd_collect(args, run_dir, setup_config):
    """Pull results from cluster PVC for completed packages."""
    namespace = setup_config["namespace"]
    cluster_dir = run_dir / "cluster"
    packages = _discover_packages(cluster_dir)
    if args.package:
        packages = [p for p in packages if p in args.package]

    results_dir = run_dir / "results"
    for pkg in packages:
        # Check PipelineRun status
        pr_path = cluster_dir / pkg / "pipelinerun.yaml"
        pr_data = yaml.safe_load(pr_path.read_text())
        pr_name = pr_data["metadata"]["name"]
        status = _check_pipelinerun_status(pr_name, namespace)

        if status == "Succeeded":
            _pull_results(pr_name, namespace, results_dir / pkg)
            ok(f"Collected: {pkg}")
        elif status == "Running":
            info(f"Pending: {pkg} (still running)")
        else:
            warn(f"Failed: {pkg} (status: {status})")
```

- [ ] **Step 5: Remove unused deploy code**

Remove: `stage_pr()`, `stage_benchmarks()` polling logic, `_run_pipeline_phase()` wait loops, noise phase logic, `_construct_validation_results()`, mechanism check code. Keep: `_build_epp_image()` (reuse existing `stage_build_epp`), result extraction helpers.

- [ ] **Step 6: Commit**

```bash
git add scripts/deploy.py
git commit -m "refactor(deploy): simplify to fire-and-forget with packages

Remove polling/waiting, noise phase, PR creation. Deploy now builds EPP,
applies PipelineRuns per package, prints status, and exits. New collect
subcommand pulls results for completed packages."
```

---

### Task 13: validate.py and validate_checks.py

Remove pre-deploy checks.

**Files:**
- Modify: `scripts/validate.py`
- Modify: `scripts/lib/validate_checks.py`
- Modify: `scripts/test_validate_checks.py`

- [ ] **Step 1: Remove pre-deploy from validate.py**

Delete the `_cmd_pre_deploy()` function and remove the `pre-deploy` subcommand from the parser. Keep `post-deploy` and `post-collection`.

- [ ] **Step 2: Remove pre-deploy checks from validate_checks.py**

Remove `run_pre_deploy_checks()` and any helper functions only used by pre-deploy checks (e.g., `check_signals`, `check_routing_policy` if only used there). Keep `run_post_deploy_checks()` and `run_post_collection_checks()`.

- [ ] **Step 3: Update test_validate_checks.py**

Remove test cases for pre-deploy checks. Keep post-deploy and post-collection tests.

- [ ] **Step 4: Run tests**

Run: `cd /Users/jchen/go/src/inference-sim/sim2real && python -m pytest scripts/test_validate_checks.py -v`
Expected: All remaining tests PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/validate.py scripts/lib/validate_checks.py scripts/test_validate_checks.py
git commit -m "refactor(validate): remove pre-deploy checks

Pre-deploy validation replaced by prepare.py validate-assembly.
Keep post-deploy (cluster health) and post-collection (results integrity)."
```

---

### Task 14: analyze.py Updates

Add package discovery and `--package` flag.

**Files:**
- Modify: `scripts/analyze.py`

- [ ] **Step 1: Add package discovery**

Update `load_artifacts()` to discover packages from `run_dir/cluster/` and load results from `run_dir/results/<package>/results.json`.

- [ ] **Step 2: Add --package flag**

Add `--package` argument to the parser. If specified, only analyze that package pair.

- [ ] **Step 3: Check results exist before analyzing**

Before loading, verify results files exist. If missing, print which packages need `deploy.py collect` and exit with a helpful message.

- [ ] **Step 4: Commit**

```bash
git add scripts/analyze.py
git commit -m "feat(analyze): add package discovery and --package flag

Discovers packages from cluster/ directories. Checks results exist
before analyzing. --package flag filters to specific experiment arm."
```

---

### Task 15: setup.py Interactive Mode

Make setup.py interactive by default (prompt for each field if no args).

**Files:**
- Modify: `scripts/setup.py`

- [ ] **Step 1: Add interactive-by-default logic**

After `build_parser()`, detect if user provided any args. If none, enter interactive mode using existing `prompt()` and `prompt_secret()` helpers. Show current values from `setup_config.json` as defaults.

```python
def _interactive_setup(args, existing_config):
    """Prompt for each field with existing values as defaults."""
    defaults = existing_config or {}

    args.namespace = args.namespace or prompt(
        "namespace", "Kubernetes namespace",
        default=defaults.get("namespace", "sim2real-" + os.environ.get("USER", "dev")))

    args.registry = args.registry or prompt(
        "registry", "Container registry (e.g. quay.io/username)",
        default=defaults.get("registry", ""))

    args.repo_name = args.repo_name if args.repo_name != "llm-d-inference-scheduler" else prompt(
        "repo_name", "Registry repo name",
        default=defaults.get("repo_name", "llm-d-inference-scheduler"))

    args.run = args.run or prompt(
        "run", "Run name",
        default=defaults.get("current_run", f"sim2real-{datetime.now().strftime('%Y-%m-%d')}"))

    # Secrets — show masked current value, reuse on Enter
    if not args.hf_token:
        current = defaults.get("_hf_token_set", False)
        if current:
            info("HuggingFace token: [set] (press Enter to reuse, or type new value)")
        args.hf_token = prompt_secret("HuggingFace token", env_var="HF_TOKEN") or None

    # Registry credentials
    if not args.registry_user:
        args.registry_user = prompt(
            "registry_user", "Registry username",
            default=defaults.get("registry_user", ""))
    if not args.registry_token:
        args.registry_token = prompt_secret("Registry token", env_var="QUAY_ROBOT_TOKEN") or None
```

- [ ] **Step 2: Add --test-push registry credential verification**

```python
def _test_registry_push(registry, repo_name, tag, runtime):
    """Push a test image to verify registry credentials."""
    test_image = f"{registry}/{repo_name}:{tag}"
    info(f"Testing registry push: {test_image}")
    # Create minimal test image
    run([runtime, "pull", "busybox:latest"], check=False)
    run([runtime, "tag", "busybox:latest", test_image])
    result = run([runtime, "push", test_image], check=False, capture=True)
    if result.returncode == 0:
        ok("Registry credentials verified (push + pull)")
        # Clean up test image
        run([runtime, "rmi", test_image], check=False)
        return True
    else:
        err(f"Registry push failed: {result.stderr}")
        return False
```

In interactive mode, prompt:
```
Push test image to {registry}/{repo_name}:_test-push? [y/s]kip:
```

- [ ] **Step 3: Commit**

```bash
git add scripts/setup.py
git commit -m "feat(setup): add interactive-by-default mode and --test-push

When run without args, prompts for each field with existing values as
defaults. --test-push verifies registry credentials with a busybox push."
```

---

## Chunk 5: Cleanup and Integration

Remove unused files, reorganize prompts, verify everything works together.

### Task 16: Remove gates.py

**Files:**
- Delete: `scripts/lib/gates.py`

- [ ] **Step 1: Verify gates.py is no longer imported**

Run: `cd /Users/jchen/go/src/inference-sim/sim2real && grep -r "from.*lib.*gates\|import.*gates" scripts/ --include="*.py"`
Expected: Only `prepare_legacy.py` (if kept) should reference it. The new prepare.py must not.

- [ ] **Step 2: Delete gates.py**

```bash
git rm scripts/lib/gates.py
```

- [ ] **Step 3: Commit**

```bash
git commit -m "refactor(lib): remove gates.py

AI review and human gate helpers replaced by translation skill's
built-in reviewer loop and prepare.py's inline gate prompt."
```

---

### Task 17: Prompt Reorganization

Move kept prompts to new directory structure, delete unused ones.

**Files:**
- Create: `prompts/prepare/translate.md` (from `prompts/generate.md`, rewritten for v2)
- Create: `prompts/prepare/review.md` (new reviewer prompt)
- Move: `prompts/build-push.md` → `prompts/deploy/build-push.md`
- Move: `prompts/validate.md` → `prompts/deploy/validate.md`
- Delete: 9 unused prompt files

- [ ] **Step 1: Create prompts/prepare/ directory and write translate.md**

Rewrite the writer prompt to be scenario-agnostic. Remove scorer-specific references. The prompt should:
- Accept `skill_input.json` fields as input
- Reference `context.md` for mapping and codebase knowledge
- Describe the write → build/test → review loop
- Be generic for any `config_kind`

- [ ] **Step 2: Write prompts/prepare/review.md**

Multi-model reviewer prompt. Instructs reviewers to check:
- Translation fidelity against context.md
- Config correctness (kind matches, plugin types consistent)
- Code quality (follows production patterns)
Returns: APPROVE or NEEDS_CHANGES with specific issues.

- [ ] **Step 3: Move deploy prompts**

```bash
mkdir -p prompts/deploy
git mv prompts/build-push.md prompts/deploy/build-push.md
git mv prompts/validate.md prompts/deploy/validate.md
```

- [ ] **Step 4: Delete unused prompts**

```bash
git rm prompts/extract.md prompts/extract-full.md prompts/translate.md \
      prompts/test.md prompts/transfer.md prompts/validate-translation.md \
      prompts/equivalence-gate.md prompts/pr.md prompts/generate.md
```

- [ ] **Step 5: Commit**

```bash
git add prompts/
git commit -m "refactor(prompts): reorganize into prepare/ and deploy/

Keep translate.md (rewritten for v2), review.md (new), build-push.md,
validate.md. Remove 9 unused prompts (extract, test, transfer, etc.)."
```

---

### Task 18: Delete Legacy Files

Clean up old files and test files that have been replaced.

**Files:**
- Delete: `scripts/prepare_legacy.py` (backup from Task 7)
- Delete: `scripts/test_prepare_snapshot.py` (replaced by test_prepare_v2.py)

- [ ] **Step 1: Remove legacy files**

```bash
git rm scripts/prepare_legacy.py scripts/test_prepare_snapshot.py
```

- [ ] **Step 2: Commit**

```bash
git commit -m "chore: remove legacy prepare.py and old tests"
```

---

### Task 19: Integration Testing

Verify the full pipeline works end-to-end with the new architecture.

- [ ] **Step 1: Run all unit tests**

Run: `cd /Users/jchen/go/src/inference-sim/sim2real && python -m pytest scripts/ -v`
Expected: All tests PASS

- [ ] **Step 2: Test prepare.py phases 1-3 (up to checkpoint)**

```bash
cd /Users/jchen/go/src/inference-sim/sim2real
python scripts/prepare.py
```
Expected: Phases 1-2 complete, Phase 3 writes skill_input.json and exits with instructions.

Verify:
- `workspace/runs/<name>/.state.json` exists with init and context phases done
- `workspace/runs/<name>/skill_input.json` exists with correct schema
- `workspace/context/routing/<hash>.md` exists

- [ ] **Step 3: Test prepare.py re-run (phase skip)**

```bash
python scripts/prepare.py
```
Expected: Phases 1-2 skipped (already done), Phase 3 checkpoint again with incremented hits.

- [ ] **Step 4: Test prepare.py subcommands**

```bash
python scripts/prepare.py status
python scripts/prepare.py context --rebuild-context
python scripts/prepare.py validate-assembly  # should fail: no translation output
```

- [ ] **Step 5: Test deploy.py --dry-run**

```bash
# (requires translation output — test with mock if needed)
python scripts/deploy.py --dry-run
```

- [ ] **Step 6: Commit any fixes discovered during integration testing**

---

### Task 20: Migration Documentation

Write v1→v2 migration guide.

**Files:**
- Create: `docs/transfer/migration-v1-to-v2.md`

- [ ] **Step 1: Write migration guide**

Document:
- Config changes: transfer.yaml field renames, env_defaults restructuring
- Command changes: new prepare.py subcommands, deploy.py simplification
- Workflow changes: translation checkpoint, skill handoff
- v1→v2 field mapping table (from brainstorm Section 3.7)

- [ ] **Step 2: Commit**

```bash
git add docs/transfer/migration-v1-to-v2.md
git commit -m "docs: add v1 to v2 migration guide"
```

---

## Verification

After all chunks are complete, run this end-to-end check:

1. **Unit tests pass:** `python -m pytest scripts/ -v` — all green
2. **Manifest v2 loads:** `python -c "from scripts.lib.manifest import load_manifest; print(load_manifest('config/transfer.yaml'))"`
3. **Config resolution works:** Verify `merge-values --scenario routing` produces correct output
4. **Prepare phases 1-3:** `python scripts/prepare.py` — runs init, context, checkpoint
5. **State machine persists:** Re-run `python scripts/prepare.py` — skips completed phases
6. **Status subcommand:** `python scripts/prepare.py status` — shows phase states
7. **Deploy dry-run:** `python scripts/deploy.py --dry-run` — shows what would be applied
8. **No dead imports:** `grep -r "from.*lib.*gates\|import.*gates" scripts/ --include="*.py"` — no results
9. **Prompts reorganized:** `ls prompts/prepare/ prompts/deploy/` — translate.md, review.md, build-push.md, validate.md
10. **Old prompts removed:** `ls prompts/*.md` — only build-push.md and validate.md should be gone from root
