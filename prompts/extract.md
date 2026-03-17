---
stage: 1
version: "1.0"
pipeline_commit: "set-at-runtime"
description: "Stage 1 — Extract algorithm metadata from routing artifacts"
---

# Stage 1: Extract

Extract algorithm metadata from routing artifacts using `transfer_cli.py extract`.
This stage produces `workspace/algorithm_summary.json` containing signal metadata,
EVOLVE-BLOCK location, and content hash.

## Prerequisites

Verify all required input files exist before proceeding. **HALT if any check fails.**

```bash
test -f blis_router/best/best_program.go || { echo "HALT: missing blis_router/best/best_program.go"; exit 1; }
test -f blis_router/best/best_program_info.json || { echo "HALT: missing blis_router/best/best_program_info.json"; exit 1; }
```

## Stale Artifact Guard

Delete any existing output artifact to prevent downstream stages from consuming
a stale result if this extraction fails before writing.

```bash
rm -f workspace/algorithm_summary.json
```

## Step 1: Run Extract

```bash
mkdir -p workspace
# Use --strict in CI environments (enforces minimum signal count).
# transfer_cli.py auto-detects CI via the CI env var and will fail without --strict.
.venv/bin/python tools/transfer_cli.py extract ${CI:+--strict} blis_router/best/
```

**Exit code handling:**
- Exit 0 → proceed to Step 2.
- Exit 1 → **HALT.** Three distinct failure modes all return exit code 1:
  - *Fidelity failure:* artifact NOT written to disk.
  - *Strict-mode minimum-signal failure:* artifact NOT written to disk.
  - *Scope failure:* artifact IS written (with `scope_validation_passed: false`).
- Use exit code, not file existence, as the success signal.

## Step 2: Schema Validation

```bash
.venv/bin/python tools/transfer_cli.py validate-schema workspace/algorithm_summary.json
```

**HALT if validation fails** (exit != 0).

## Step 3: Scope Validation

Schema validation alone is insufficient — the schema permits `scope_validation_passed: false`.

```bash
.venv/bin/python -c "import json,sys; d=json.load(open('workspace/algorithm_summary.json')); sys.exit(0 if d.get('scope_validation_passed') is True else 1)"
```

**HALT if exit != 0** — out-of-scope patterns detected.

## Step 4: Signal Review

Display the extracted signals for operator review:

```bash
.venv/bin/python -c "
import json
d = json.load(open('workspace/algorithm_summary.json'))
print('Algorithm:', d.get('algorithm_name'))
print('Signals:')
for s in d.get('signals', []):
    norm = s.get('normalization_note', '')
    prov = ' [PROVISIONAL]' if s.get('fidelity_provisional') else ''
    print(f'  - {s[\"name\"]} ({s[\"type\"]}){prov}')
    if norm:
        print(f'    Normalization: {norm}')
print('Composite signals:')
for c in d.get('composite_signals', []):
    print(f'  - {c[\"name\"]}: {c[\"formula\"]}({c[\"constituents\"]})')
print('Content hash:', d.get('evolve_block_content_hash'))
"
```

**Review checklist:**
- Verify KVUtilization has `normalization_note` with `divide_prod_by_100` action.
- Verify CacheHitRate has `fidelity_provisional: true` if present.
- Verify that the extracted signals match those actually referenced in the EVOLVE-BLOCK being processed. For the current blis_router EVOLVE-BLOCK, the expected signals are `InFlightRequests` and `KVUtilization`. Do **not** flag absence of `QueueDepth`, `BatchSize`, `CacheHitRate`, or `SessionID` as a bug — these signals are not present in the current EVOLVE-BLOCK. If the algorithm changes, re-verify against the new EVOLVE-BLOCK source.

## Halt Conditions

| Condition | Action |
|-----------|--------|
| Extract exit code != 0 | HALT — extraction failed |
| Schema validation fails | HALT — malformed output |
| `scope_validation_passed == false` | HALT — out-of-scope patterns |
| Missing expected signals | Review — may indicate extraction bug |

## Expected Outputs

- `workspace/algorithm_summary.json` — contains:
  - `algorithm_name`: string
  - `evolve_block_source`: path with line range (e.g., `blis_router/best/best_program.go:177-258`)
  - `evolve_block_content_hash`: SHA-256 hex string
  - `signals[]`: array of signal objects with name, type, access_path, normalization_note
  - `composite_signals[]`: array of composite signal definitions
  - `metrics`: performance metrics from evolutionary optimization
  - `scope_validation_passed`: boolean
  - `mapping_artifact_version`: string
  - `fidelity_checked`: boolean
