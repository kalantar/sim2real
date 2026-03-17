# Submodule Update Guide

**Date:** 2026-03-13
**Purpose:** What to update in pinned artifacts when a submodule is bumped.

---

## `llm-d-inference-scheduler` — most critical, most pinned

### What pins it

- `docs/transfer/blis_to_llmd_mapping.md` header: `**Pinned commit hash:**`
- `docs/transfer/scorer_template.go.md` header: `**Pinned commit:**`

### Process

1. Diff the relevant paths against the old commit:
   ```bash
   cd llm-d-inference-scheduler
   git diff <old-commit>..<new-commit> -- pkg/plugins/scorer/ vendor/sigs.k8s.io/gateway-api-inference-extension/
   ```
2. For each changed field name, type, or interface method: update the affected rows in `docs/transfer/blis_to_llmd_mapping.md`
3. If the `scheduling.Scorer` interface changed: update Section 2 of `docs/transfer/scorer_template.go.md`
4. If `fwkdl.Metrics` struct fields changed: update the UNVERIFIED field comments in Section 3 of the scorer template
5. Update the pinned commit hash in **both** `blis_to_llmd_mapping.md` and `scorer_template.go.md`
6. Confirm structural completeness:
   ```bash
   .venv/bin/python tools/transfer_cli.py validate-mapping
   ```
7. Confirm the submodule still builds:
   ```bash
   cd llm-d-inference-scheduler && go build ./... && cd ..
   ```

### In-flight transfers

The pipeline's staleness guard (Stage 2) detects when the submodule HEAD differs from the mapping artifact's pinned commit. If a transfer is already in progress when you bump the submodule, Stage 2 will warn and require you to either acknowledge the drift or re-verify the mappings before continuing.

---

## `inference-sim` — affects CLI extract logic

### What pins it

- `ROUTING_SNAPSHOT_FIELDS` dict in `tools/transfer_cli.py` (hardcoded field names and types derived from `inference-sim/sim/routing.go`)
- `METHOD_EXPANSIONS` dict in `tools/transfer_cli.py` (`EffectiveLoad` constituent fields)

### Process

1. Re-derive the routing struct:
   ```bash
   grep -A 20 'type RoutingSnapshot struct' inference-sim/sim/routing.go
   ```
2. Update `ROUTING_SNAPSHOT_FIELDS` in `tools/transfer_cli.py` to match
3. Re-derive the `EffectiveLoad` expansion:
   ```bash
   grep -A 5 'func.*EffectiveLoad' inference-sim/sim/routing.go
   ```
4. Update `METHOD_EXPANSIONS` in `tools/transfer_cli.py` if the expansion changed
5. Run the full test suite — the golden-file test `TestGoldenSignalList` will catch signal extraction regressions:
   ```bash
   .venv/bin/python -m pytest tools/ -v
   ```

---

## `llm-d-benchmark` — no pinned artifacts

### What pins it

Nothing. No commit hash in any artifact.

### Process

No artifact updates required. Verify Stage 5/6 benchmark tooling still works after the bump, particularly if CLI flag names for `noise-characterize` or `benchmark` commands changed.

---

## Prompt to use with Claude Code

When bumping any submodule, paste this prompt (fill in the placeholders):

```
I've updated the <submodule-name> submodule from <old-commit> to <new-commit>.

1. Run: git diff <old-commit>..<new-commit> -- <relevant-paths>  (in the submodule)
2. Identify any changes to: [scorer interface / RoutingSnapshot struct / metric field names]
3. Update all pinned artifacts in docs/transfer/ to reflect the new commit and any API changes
4. Run .venv/bin/python tools/transfer_cli.py validate-mapping and confirm exit 0
5. Run .venv/bin/python -m pytest tools/ -v and confirm all tests pass
6. List any signal fidelity ratings that may need re-evaluation due to the changes
```

Substitute `<relevant-paths>` per submodule:

| Submodule | Relevant paths |
|-----------|---------------|
| `llm-d-inference-scheduler` | `pkg/plugins/ vendor/sigs.k8s.io/gateway-api-inference-extension/` |
| `inference-sim` | `sim/routing.go` |
| `llm-d-benchmark` | _(no pinned artifacts — verify tooling manually)_ |
