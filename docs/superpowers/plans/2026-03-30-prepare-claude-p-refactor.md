# prepare.py claude -p Refactor Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the direct LLM API inner loops in `scripts/prepare.py` Stages 3 and 5 with `claude -p` subprocess invocations, persisting codebase context once and organizing round artifacts under `rounds/N/`.

**Architecture:** A one-time preamble step builds two context docs (`prepare_codebase_context.md`, `prepare_reviewer_context.md`) by running `claude -p` with file-reading access to the real llm-d-inference-scheduler codebase. Each generate and review iteration is then a bounded `claude -p` subprocess that reads the pre-built context instead of re-crawling the codebase. Round artifacts are written to `rounds/N/` directories for inspection.

**Tech Stack:** Python 3.10+, claude CLI (`claude -p`), existing `review_translation.py` + `build_review_request.py` (extended with `--extra-context`)

**Spec:** `docs/superpowers/specs/2026-03-30-prepare-claude-p-refactor-design.md`

**DO NOT COMMIT** — user controls all commits.

---

## Current State (as of plan creation)

Tasks 1 and 2 below are **already complete**:

- ✅ `build_review_request.py` — `--extra-context FILE` added
- ✅ `review_translation.py` — `--extra-context FILE` pass-through added
- ✅ `prepare.py` helpers block inserted (between `_load_evolve_block` and old `stage_generate`):
  - `_run_claude()`, `_preamble_prompt()`, `stage_build_context()`
  - `_write_evolve_block()`
  - `_generate_prompt()`, `stage_generate_iteration()`
  - `_review_prompt()`, `stage_review_iteration()`

Remaining work: replace `stage_generate()` and `stage_final_review()` bodies to call the new helpers, and update `print_intro()`.

---

## File Map

| File | Status | Change |
|---|---|---|
| `.claude/skills/sim2real-prepare/scripts/build_review_request.py` | ✅ Done | `--extra-context` added |
| `.claude/skills/sim2real-prepare/scripts/review_translation.py` | ✅ Done | `--extra-context` pass-through |
| `scripts/prepare.py` | 🔧 In progress | helpers inserted; `stage_generate` + `stage_final_review` + `print_intro` still old |

---

## Chunk 1: Replace `stage_generate()`

### Task 3: Replace the body of `stage_generate`

**Files:**
- Modify: `scripts/prepare.py` (function `stage_generate`, lines ~1162–1370)

- [ ] **Step 1: Replace `stage_generate` with the round-based implementation**

Find the entire `stage_generate` function (from `def stage_generate(` through its closing `return stage3_out`) and replace with:

```python
def stage_generate(run_dir: Path, algo_summary_path: Path,
                   signal_coverage_path: Path, reviews: int,
                   force: bool = False, skip_generate: bool = False) -> Path:
    stage3_out = run_dir / "prepare_stage3_output.json"

    if skip_generate:
        if not stage3_out.exists():
            stage3_out = _reconstruct_stage3_output(run_dir, stage3_out)
        info(f"[skip] Generate — --skip-generate passed, using {stage3_out.relative_to(REPO_ROOT)}")
        s3 = json.loads(stage3_out.read_text())
        scorer_name = Path(s3["scorer_file"]).stem
        scorer_type = s3.get("scorer_type", _scorer_type_from_name(scorer_name))
        _ensure_scorer_registered(scorer_name, scorer_type)
        return stage3_out

    if not force and stage3_out.exists():
        try:
            _s3 = json.loads(stage3_out.read_text())
            if (REPO_ROOT / _s3.get("scorer_file", "")).exists():
                if _should_skip(stage3_out, "Generate", force):
                    return stage3_out
        except (json.JSONDecodeError, KeyError):
            pass

    step(3, "Generate  (writer: claude -p  |  reviewers: all 3 models)")
    out_dir = run_dir / "prepare_tekton"
    out_dir.mkdir(parents=True, exist_ok=True)
    stage3_out.unlink(missing_ok=True)

    algo_summary = json.loads(algo_summary_path.read_text())
    signal_coverage = json.loads(signal_coverage_path.read_text())

    # Preamble: build context documents once (skippable on rerun)
    stage_build_context(run_dir, force=force)

    # Write evolve block to stable file for review script
    evolve_block_path = _write_evolve_block(algo_summary, run_dir)

    # Round loop
    round_num = 0
    remaining = reviews
    scorer_path: Path | None = None
    test_path: Path | None = None
    consensus = False

    while True:
        round_num += 1
        round_dir = run_dir / "rounds" / str(round_num)
        round_dir.mkdir(parents=True, exist_ok=True)

        revision = " (revision)" if round_num > 1 else ""
        print(f"\n  ── Round {round_num}{revision} " + "─" * max(0, 48 - len(revision)))

        scorer_path, test_path = stage_generate_iteration(
            round_num, round_dir, run_dir, algo_summary_path, signal_coverage_path,
        )

        # Snapshots written before build — records exactly what was compiled
        shutil.copy(scorer_path, round_dir / "scorer_snapshot.go")
        if test_path and test_path.exists():
            shutil.copy(test_path, round_dir / "scorer_test_snapshot.go")

        scorer_name = scorer_path.stem
        scorer_type = _scorer_type_from_name(scorer_name)
        _ensure_scorer_registered(scorer_name, scorer_type)

        build_passed, build_error = _run_build_test()
        (round_dir / "build_output.txt").write_text(build_error or "PASS")

        if not build_passed:
            (round_dir / "build_issues.json").write_text(json.dumps({
                "round": round_num,
                "issues": [build_error],
            }, indent=2))
            info("  Build failed — passing errors to next generate round...")
            continue  # skip review; build error feeds next round

        consensus = stage_review_iteration(
            round_dir, run_dir, scorer_path,
            algo_summary_path, signal_coverage_path, evolve_block_path,
        )

        issues = json.loads(
            (round_dir / "review_issues.json").read_text()
        ).get("issues", [])
        print(f"  Round logs: {round_dir.relative_to(REPO_ROOT)}/")

        if consensus:
            ok(f"  Consensus reached in round {round_num}")
            break

        remaining -= 1
        if remaining > 0:
            info(f"  Passing {len(issues)} issue(s) to next round...")
            continue

        # Max rounds reached — pause for user
        extra = _prompt_user_continue(consensus, label=f"Generate round {round_num}")
        if extra == 0:
            break
        remaining = extra

    # Generate Tekton artifacts (logic unchanged from original)
    algo_values_path = _generate_algorithm_values(algo_summary, signal_coverage, out_dir)
    values_path = _run_merge_values(algo_values_path, out_dir)

    scorer_name = scorer_path.stem
    scorer_type = _scorer_type_from_name(scorer_name)
    register_path = _ensure_scorer_registered(scorer_name, scorer_type)

    payload = json.dumps({
        "scorer_file": str(scorer_path.relative_to(REPO_ROOT)),
        "test_file": str(test_path.relative_to(REPO_ROOT)) if test_path and test_path.exists() else "",
        "register_file": str(register_path.relative_to(REPO_ROOT)),
        "scorer_type": scorer_type,
        "tekton_artifacts": {
            "values_yaml": str(values_path.relative_to(REPO_ROOT)),
        },
    }, indent=2)

    venv_python = str(REPO_ROOT / ".venv/bin/python")
    cli = str(REPO_ROOT / "tools/transfer_cli.py")

    ws_out = REPO_ROOT / "workspace/stage3_output.json"
    ws_out.write_text(payload)
    run([venv_python, cli, "validate-schema", str(ws_out)], cwd=REPO_ROOT)
    shutil.copy(ws_out, stage3_out)

    ok(f"Generate complete → {stage3_out.relative_to(REPO_ROOT)}")
    ok(f"Round logs: {(run_dir / 'rounds').relative_to(REPO_ROOT)}/")
    return stage3_out
```

- [ ] **Step 2: Verify the old `stage_generate` body is gone**

```bash
grep -n "call_model\|call_models_parallel\|writer_messages\|build_revision_prompt" scripts/prepare.py | grep -v "^.*def \|^.*#"
```

Expected: matches only in the old helper functions (`build_generate_prompt`, `build_revision_prompt`, etc.) that are now dead code above `stage_generate` — not inside `stage_generate` itself.

- [ ] **Step 3: Verify Python syntax**

```bash
python3 -c "import ast; ast.parse(open('scripts/prepare.py').read()); print('OK')"
```

Expected: `OK`

---

## Chunk 2: Replace `stage_final_review()` and update `print_intro()`

### Task 4: Replace `stage_final_review`

**Files:**
- Modify: `scripts/prepare.py` (function `stage_final_review`, lines ~1576–1638)

- [ ] **Step 1: Replace `stage_final_review` with the claude -p based version**

Find the entire `stage_final_review` function and replace with:

```python
def stage_final_review(run_dir: Path, stage3_path: Path,
                        algo_summary_path: Path, signal_coverage_path: Path,
                        reviews: int, force: bool = False) -> bool:
    """Run final 3-model review via claude -p. Returns True if consensus reached."""
    out = run_dir / "prepare_translation_reviews.json"
    if not force and out.exists():
        try:
            if json.loads(out.read_text()).get("passed"):
                if _should_skip(out, "Final Review", force):
                    return True
        except (json.JSONDecodeError, KeyError):
            pass
    step(5, "Final Review (3 models)")

    stage3 = json.loads(stage3_path.read_text())
    scorer_path = REPO_ROOT / stage3["scorer_file"]

    algo_summary = json.loads(algo_summary_path.read_text())
    evolve_block_path = _write_evolve_block(algo_summary, run_dir)

    final_dir = run_dir / "final-review"
    final_dir.mkdir(parents=True, exist_ok=True)

    consensus = stage_review_iteration(
        final_dir, run_dir, scorer_path,
        algo_summary_path, signal_coverage_path, evolve_block_path,
    )

    issues = json.loads(
        (final_dir / "review_issues.json").read_text()
    ).get("issues", [])
    passed = consensus

    out.write_text(json.dumps({
        "rounds": 1,
        "consensus": consensus,
        "accepted_by_user": False,
        "passed": passed,
        "issues": issues,
        "logs": str(final_dir.relative_to(REPO_ROOT)),
    }, indent=2))
    status = "passed ✓" if passed else "failed — issues found"
    ok(f"Final review {status} → {out.relative_to(REPO_ROOT)}")
    ok(f"Final review logs: {final_dir.relative_to(REPO_ROOT)}/")
    return passed
```

- [ ] **Step 2: Verify Python syntax**

```bash
python3 -c "import ast; ast.parse(open('scripts/prepare.py').read()); print('OK')"
```

Expected: `OK`

### Task 5: Update `print_intro`

**Files:**
- Modify: `scripts/prepare.py` (function `print_intro`, lines ~106–121)

- [ ] **Step 1: Update the LLM interaction descriptions**

Replace the two description lines inside `print_intro` that name API models:

```python
# OLD lines to find and replace:
    print(f"  2. Generate      writer loop   claude-opus-4-6 writes; {reviewer_note} review;")
    print("                                 issues fed back to writer until consistent")
    print("  3. Final Review  review loop   all 3 models do a final check after build/test;")
    print("                                 if issues found, returns to Generate")
```

Replace with:

```python
    print(f"  2. Generate      writer loop   claude -p writes (with codebase access);")
    print(f"                                 {reviewer_note} review via claude -p + review script;")
    print("                                 issues fed back to writer until consistent")
    print("  3. Final Review  review loop   claude -p invokes all 3 models for final check;")
    print("                                 if issues found, returns to Generate")
```

- [ ] **Step 2: Verify Python syntax**

```bash
python3 -c "import ast; ast.parse(open('scripts/prepare.py').read()); print('OK')"
```

Expected: `OK`

---

## Chunk 3: Smoke Test

### Task 6: Verify the refactor is wired correctly end-to-end

- [ ] **Step 1: Check that `stage_generate` calls the new helpers**

```bash
grep -n "stage_build_context\|stage_generate_iteration\|stage_review_iteration\|_write_evolve_block" scripts/prepare.py
```

Expected: Each name appears at least twice (definition + call).

- [ ] **Step 2: Check that old API-based calls are gone from the two stage functions**

```bash
python3 -c "
import ast, sys
src = open('scripts/prepare.py').read()
tree = ast.parse(src)
bad = {'call_model', 'call_models_parallel', 'run_consensus_loop'}
for node in ast.walk(tree):
    if isinstance(node, ast.FunctionDef) and node.name in ('stage_generate', 'stage_final_review'):
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                fn = ''
                if isinstance(child.func, ast.Name):
                    fn = child.func.id
                elif isinstance(child.func, ast.Attribute):
                    fn = child.func.attr
                if fn in bad:
                    print(f'FAIL: {node.name} still calls {fn}')
                    sys.exit(1)
print('OK — no legacy API calls in stage_generate or stage_final_review')
"
```

Expected: `OK — no legacy API calls in stage_generate or stage_final_review`

- [ ] **Step 3: Check that `stage_review_iteration` is called from `stage_final_review`**

```bash
grep -A 30 "^def stage_final_review" scripts/prepare.py | grep "stage_review_iteration"
```

Expected: one match.

- [ ] **Step 4: Check `build_review_request.py` accepts `--extra-context`**

```bash
python3 .claude/skills/sim2real-prepare/scripts/build_review_request.py --help 2>&1 | grep extra-context
```

Expected: line showing `--extra-context FILE`.

- [ ] **Step 5: Check `review_translation.py` accepts `--extra-context`**

```bash
python3 .claude/skills/sim2real-prepare/scripts/review_translation.py --help 2>&1 | grep extra-context
```

Expected: line showing `--extra-context FILE`.

- [ ] **Step 6: Dry-run import of prepare.py**

```bash
python3 -c "
import sys
sys.argv = ['prepare.py', '--help']
try:
    import scripts.prepare as p
except SystemExit:
    pass
print('import OK')
" 2>/dev/null || python3 -c "import ast; ast.parse(open('scripts/prepare.py').read()); print('syntax OK')"
```

Expected: `syntax OK` or `import OK`.
