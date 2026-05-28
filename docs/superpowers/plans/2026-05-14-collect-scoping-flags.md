# Collect Scoping Flags Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `--only` and `--workload` scoping flags to `deploy.py collect` so users can pull results for specific workloads/pairs instead of entire phases.

**Architecture:** The existing `_resolve_scope()` utility already implements `--only`/`--workload`/`--package` filtering against `progress.json` for `run`/`status`/`reset`. For `collect`, we reuse this same utility to determine which workload names are in scope, then pass those workload names to the existing `workload` parameter of `_extract_phases_from_pvc()` (which already supports single-workload extraction). Since `_extract_phases_from_pvc` only takes a single workload string, we call it once per workload when scoped, or once with `workload=None` (full phase copy) when unscoped. Collect's existing `--package` (nargs="+", phase-level filter) remains unchanged and composes orthogonally with the new pair-level flags.

**Tech Stack:** Python, argparse, pytest

---

### Task 1: Add `--only` and `--workload` CLI flags to collect subparser

**Files:**
- Modify: `pipeline/deploy.py:1535-1539` (collect_p argparse block)
- Modify: `pipeline/completions.bash:84-86`
- Modify: `pipeline/completions.zsh:92-96`

- [ ] **Step 1: Add the two new arguments to collect_p**

In `pipeline/deploy.py`, after line 1535 (`collect_p = sub.add_parser(...)`), add `--only` and `--workload` before the existing `--package`:

```python
collect_p.add_argument("--only",     metavar="PAIR",
                       help="Scope to one specific pair key (wl- prefix optional)")
collect_p.add_argument("--workload", metavar="NAME",
                       help="Scope to pairs matching this workload")
```

- [ ] **Step 2: Update bash completions**

In `pipeline/completions.bash`, change the `collect)` case (line 84-85) from:
```
COMPREPLY=($(compgen -W "--package --skip-logs" -- "$cur"))
```
to:
```
COMPREPLY=($(compgen -W "--only --workload --package --skip-logs" -- "$cur"))
```

- [ ] **Step 3: Update zsh completions**

In `pipeline/completions.zsh`, change the `collect)` case (lines 92-96) from:
```zsh
collect)
    _arguments \
        '*--package[Collect only these packages]:package:_deploy_py_packages' \
        '--skip-logs[Skip vLLM and EPP log files]'
    ;;
```
to:
```zsh
collect)
    _arguments \
        '--only[Scope to one pair key]:pair key:_deploy_py_pair_keys' \
        '--workload[Scope to workload]:workload:_deploy_py_workloads' \
        '*--package[Collect only these packages]:package:_deploy_py_packages' \
        '--skip-logs[Skip vLLM and EPP log files]'
    ;;
```

- [ ] **Step 4: Commit**

```bash
git add pipeline/deploy.py pipeline/completions.bash pipeline/completions.zsh
git commit -m "collect: add --only and --workload CLI flags (#122)"
```

---

### Task 2: Wire scoping flags into `_cmd_collect` logic

**Files:**
- Modify: `pipeline/deploy.py:595-685` (`_cmd_collect` function)

The key design decisions:

1. **`--only`/`--workload` filter at the workload level** (which subdirectories within a phase to pull), while `--package` filters at the **phase level** (which phases to pull). They compose as AND: `--workload X --package baseline` means "pull workload X's subdirectory from the baseline phase only."

2. **Reuse `_resolve_scope()`** to get the set of in-scope pair keys from progress.json, then derive the unique workload names from those pairs. Pass each workload name individually to `_extract_phases_from_pvc(workload=...)`.

3. **Warn on non-done pairs** that match the scope filters. Don't block — just warn.

4. **When no `--only`/`--workload` given**, behavior is unchanged: `workload=None` → extract entire phase directories.

- [ ] **Step 1: Write the failing tests**

Create new tests in `pipeline/tests/test_deploy_collect.py`. Add these test functions at the end of the file:

```python
def test_collect_with_workload_scope(tmp_path):
    """--workload scopes extraction to matching workloads only."""
    from pipeline import deploy

    run_dir = tmp_path / "workspace" / "runs" / "test-run"
    (run_dir / "cluster").mkdir(parents=True)
    _write_progress(run_dir, {
        "wl-smoke-baseline": {"workload": "smoke", "package": "baseline", "status": "done"},
        "wl-smoke-treatment": {"workload": "smoke", "package": "treatment", "status": "done"},
        "wl-load-baseline": {"workload": "load", "package": "baseline", "status": "done"},
        "wl-load-treatment": {"workload": "load", "package": "treatment", "status": "done"},
    })

    class Args:
        only = None
        workload = "smoke"
        package = None
        skip_logs = False

    extract_calls = []

    def mock_extract(phases, run_name, namespace, run_dir_arg, *, skip_logs=False, workload=None):
        extract_calls.append({"phases": phases, "workload": workload})
        return {p: None for p in phases}

    with patch.object(deploy, "_extract_phases_from_pvc", mock_extract):
        deploy._cmd_collect(Args(), run_dir, {"namespace": "ns-0"})

    assert len(extract_calls) == 1
    assert extract_calls[0]["workload"] == "smoke"
    assert sorted(extract_calls[0]["phases"]) == ["baseline", "treatment"]


def test_collect_with_only_scope(tmp_path):
    """--only scopes extraction to one pair's workload and package."""
    from pipeline import deploy

    run_dir = tmp_path / "workspace" / "runs" / "test-run"
    (run_dir / "cluster").mkdir(parents=True)
    _write_progress(run_dir, {
        "wl-smoke-baseline": {"workload": "smoke", "package": "baseline", "status": "done"},
        "wl-smoke-treatment": {"workload": "smoke", "package": "treatment", "status": "done"},
        "wl-load-baseline": {"workload": "load", "package": "baseline", "status": "done"},
    })

    class Args:
        only = "wl-smoke-baseline"
        workload = None
        package = None
        skip_logs = False

    extract_calls = []

    def mock_extract(phases, run_name, namespace, run_dir_arg, *, skip_logs=False, workload=None):
        extract_calls.append({"phases": phases, "workload": workload})
        return {p: None for p in phases}

    with patch.object(deploy, "_extract_phases_from_pvc", mock_extract):
        deploy._cmd_collect(Args(), run_dir, {"namespace": "ns-0"})

    assert len(extract_calls) == 1
    assert extract_calls[0]["workload"] == "smoke"
    assert extract_calls[0]["phases"] == ["baseline"]


def test_collect_only_without_prefix(tmp_path):
    """--only resolves wl- prefix automatically."""
    from pipeline import deploy

    run_dir = tmp_path / "workspace" / "runs" / "test-run"
    (run_dir / "cluster").mkdir(parents=True)
    _write_progress(run_dir, {
        "wl-smoke-baseline": {"workload": "smoke", "package": "baseline", "status": "done"},
    })

    class Args:
        only = "smoke-baseline"
        workload = None
        package = None
        skip_logs = False

    extract_calls = []

    def mock_extract(phases, run_name, namespace, run_dir_arg, *, skip_logs=False, workload=None):
        extract_calls.append({"phases": phases, "workload": workload})
        return {p: None for p in phases}

    with patch.object(deploy, "_extract_phases_from_pvc", mock_extract):
        deploy._cmd_collect(Args(), run_dir, {"namespace": "ns-0"})

    assert len(extract_calls) == 1
    assert extract_calls[0]["workload"] == "smoke"


def test_collect_workload_with_package_filter(tmp_path):
    """--workload + --package compose: workload scopes within specified phases."""
    from pipeline import deploy

    run_dir = tmp_path / "workspace" / "runs" / "test-run"
    (run_dir / "cluster").mkdir(parents=True)
    _write_progress(run_dir, {
        "wl-smoke-baseline": {"workload": "smoke", "package": "baseline", "status": "done"},
        "wl-smoke-treatment": {"workload": "smoke", "package": "treatment", "status": "done"},
        "wl-load-baseline": {"workload": "load", "package": "baseline", "status": "done"},
    })

    class Args:
        only = None
        workload = "smoke"
        package = ["baseline"]
        skip_logs = False

    extract_calls = []

    def mock_extract(phases, run_name, namespace, run_dir_arg, *, skip_logs=False, workload=None):
        extract_calls.append({"phases": phases, "workload": workload})
        return {p: None for p in phases}

    with patch.object(deploy, "_extract_phases_from_pvc", mock_extract):
        deploy._cmd_collect(Args(), run_dir, {"namespace": "ns-0"})

    assert len(extract_calls) == 1
    assert extract_calls[0]["workload"] == "smoke"
    assert extract_calls[0]["phases"] == ["baseline"]


def test_collect_workload_no_match_exits(tmp_path):
    """--workload with no matching pairs exits with error."""
    from pipeline import deploy

    run_dir = tmp_path / "workspace" / "runs" / "test-run"
    (run_dir / "cluster").mkdir(parents=True)
    _write_progress(run_dir, {
        "wl-smoke-baseline": {"workload": "smoke", "package": "baseline", "status": "done"},
    })

    class Args:
        only = None
        workload = "nonexistent"
        package = None
        skip_logs = False

    with pytest.raises(SystemExit):
        deploy._cmd_collect(Args(), run_dir, {"namespace": "ns-0"})


def test_collect_warns_nondone_scoped_pairs(tmp_path):
    """When scoped pairs include non-done entries, warn but continue with done ones."""
    from pipeline import deploy

    run_dir = tmp_path / "workspace" / "runs" / "test-run"
    (run_dir / "cluster").mkdir(parents=True)
    _write_progress(run_dir, {
        "wl-smoke-baseline": {"workload": "smoke", "package": "baseline", "status": "done"},
        "wl-smoke-treatment": {"workload": "smoke", "package": "treatment", "status": "running"},
    })

    class Args:
        only = None
        workload = "smoke"
        package = None
        skip_logs = False

    extract_calls = []

    def mock_extract(phases, run_name, namespace, run_dir_arg, *, skip_logs=False, workload=None):
        extract_calls.append({"phases": phases, "workload": workload})
        return {p: None for p in phases}

    with patch.object(deploy, "_extract_phases_from_pvc", mock_extract), \
         patch.object(deploy, "warn") as mock_warn:
        deploy._cmd_collect(Args(), run_dir, {"namespace": "ns-0"})

    assert len(extract_calls) == 1
    assert extract_calls[0]["phases"] == ["baseline"]
    assert any("running" in str(c) for c in mock_warn.call_args_list)


def test_collect_unscoped_unchanged(tmp_path):
    """Without --only/--workload, collect behaves exactly as before (no workload param)."""
    from pipeline import deploy

    run_dir = tmp_path / "workspace" / "runs" / "test-run"
    (run_dir / "cluster").mkdir(parents=True)
    _write_progress(run_dir, {
        "wl-smoke-baseline": {"workload": "smoke", "package": "baseline", "status": "done"},
        "wl-smoke-treatment": {"workload": "smoke", "package": "treatment", "status": "done"},
    })

    class Args:
        only = None
        workload = None
        package = None
        skip_logs = False

    extract_calls = []

    def mock_extract(phases, run_name, namespace, run_dir_arg, *, skip_logs=False, workload=None):
        extract_calls.append({"phases": phases, "workload": workload})
        return {p: None for p in phases}

    with patch.object(deploy, "_extract_phases_from_pvc", mock_extract):
        deploy._cmd_collect(Args(), run_dir, {"namespace": "ns-0"})

    assert len(extract_calls) == 1
    assert extract_calls[0]["workload"] is None
    assert sorted(extract_calls[0]["phases"]) == ["baseline", "treatment"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest pipeline/tests/test_deploy_collect.py -v -k "workload_scope or only_scope or only_without or workload_with_package or workload_no_match or warns_nondone or unscoped_unchanged"`

Expected: FAIL (Args classes have `only`/`workload` attributes but `_cmd_collect` doesn't read them)

- [ ] **Step 3: Implement the scoping logic in `_cmd_collect`**

Replace the body of `_cmd_collect` (lines 595-685) with logic that:

1. Loads progress.json (same as now).
2. Checks if `--only` or `--workload` are given. If so:
   a. Use `_resolve_scope(progress, args)` to get in-scope pair keys. Note: `_resolve_scope` uses `args.package` as a single-value pair-level filter, but collect's `--package` is `nargs="+"` (a list). We must NOT pass collect's `--package` through `_resolve_scope` — instead, create a scoping-only args namespace with `package=None` and only `only`/`workload` set, so `_resolve_scope` operates purely on `--only`/`--workload`.
   b. From the in-scope pairs, derive: unique workload names and unique package (phase) names.
   c. Warn about any scoped pairs that aren't done/collecting.
   d. Filter phases further by `args.package` if given (the existing phase-level filter).
   e. For each unique workload name, call `_extract_phases_from_pvc(phases, ..., workload=wl_name)`.
3. If neither `--only` nor `--workload` given, proceed exactly as before (no workload scoping).

Here is the replacement `_cmd_collect`:

```python
def _cmd_collect(args, run_dir: Path, setup_config: dict):
    """Pull results from cluster for completed phases."""
    namespace = os.environ.get("NAMESPACE", setup_config.get("namespace", ""))
    if not namespace:
        err("No namespace configured.")
        sys.exit(1)

    run_name = run_dir.name

    # Derive known phases from progress.json, fall back to _discover_phases()
    progress_path = run_dir / "progress.json"
    if progress_path.exists():
        try:
            progress = json.loads(progress_path.read_text())
        except json.JSONDecodeError:
            warn(f"Corrupt progress.json at {progress_path} — falling back to default phases")
            progress = None
        else:
            if not isinstance(progress, dict):
                warn("progress.json is not a JSON object — falling back to default phases")
                progress = None
    else:
        progress = None

    # ── Pair-level scoping (--only / --workload) ──────────────────────────
    scope_only = getattr(args, "only", None)
    scope_workload = getattr(args, "workload", None)
    scoped = scope_only is not None or scope_workload is not None

    if scoped and progress:
        # Build a lightweight args namespace for _resolve_scope with only
        # pair-level filters (--only, --workload).  Collect's --package is
        # a phase-level filter (nargs="+") and must NOT be mixed in.
        class _ScopeArgs:
            only = scope_only
            workload = scope_workload
            package = None
            status = None

        in_scope = _resolve_scope(progress, _ScopeArgs())

        # Warn about non-done scoped pairs
        for key in sorted(in_scope):
            entry = progress.get(key, {})
            st = entry.get("status", "")
            if st not in ("done", "collecting"):
                warn(f"Scoped pair {key} has status '{st}' — skipping")

        # Derive phases and workloads from done/collecting scoped pairs
        scoped_phases = sorted({
            progress[k].get("package", "")
            for k in in_scope
            if isinstance(progress[k], dict) and progress[k].get("status") in ("done", "collecting")
        } - {""})
        scoped_workloads = sorted({
            progress[k].get("workload", "")
            for k in in_scope
            if isinstance(progress[k], dict) and progress[k].get("status") in ("done", "collecting")
        } - {""})

        if not scoped_phases:
            warn("No done/collecting phases for scoped pairs.")
            print()
            return

        # Apply --package phase-level filter on top
        if args.package:
            valid = set(scoped_phases) | {"experiment"}
            unknown = set(args.package) - valid
            if unknown:
                err(f"Unknown packages: {sorted(unknown)}. Valid: {sorted(valid)}")
                sys.exit(1)
            phases_to_collect: list[str] = []
            for p in args.package:
                if p == "experiment":
                    phases_to_collect.extend(scoped_phases)
                else:
                    phases_to_collect.append(p)
            seen: set[str] = set()
            phases_to_collect = [p for p in phases_to_collect
                                 if not (p in seen or seen.add(p))]  # type: ignore[func-returns-value]
        else:
            phases_to_collect = list(scoped_phases)

        step(1, "Collecting Results")
        collected: list[str] = []
        failed: list[str] = []

        for wl_name in scoped_workloads:
            try:
                skip_logs = getattr(args, "skip_logs", False)
                errors = _extract_phases_from_pvc(
                    phases_to_collect, run_name, namespace, run_dir,
                    skip_logs=skip_logs, workload=wl_name)
            except RuntimeError as e:
                warn(f"Extractor pod failed for workload {wl_name}: {e}")
                failed.extend(phases_to_collect)
            else:
                for phase, exc in errors.items():
                    if exc is None:
                        ok(f"Collected: {phase}/{wl_name}")
                        if phase not in collected:
                            collected.append(phase)
                    else:
                        warn(f"Extraction failed for {phase}/{wl_name}: {exc}")
                        if phase not in failed:
                            failed.append(phase)
    else:
        # ── Unscoped path (original behavior) ────────────────────────────
        if progress:
            known_phases = sorted({
                entry.get("package", "")
                for entry in progress.values()
                if isinstance(entry, dict) and entry.get("status") in ("done", "collecting")
            } - {""})
        else:
            known_phases = []

        if not known_phases:
            cluster_dir = run_dir / "cluster"
            known_phases = _discover_phases(cluster_dir)
            if progress is None and not progress_path.exists():
                warn(f"No progress.json found — discovered phases from cluster/: {known_phases}")
            elif progress is not None:
                warn(f"No done/collecting phases in progress — discovered from cluster/: {known_phases}")

        if args.package:
            valid = set(known_phases) | {"experiment"}
            unknown = set(args.package) - valid
            if unknown:
                err(f"Unknown packages: {sorted(unknown)}. Valid: {sorted(valid)}")
                sys.exit(1)
            phases_to_collect = []
            for p in args.package:
                if p == "experiment":
                    phases_to_collect.extend(known_phases)
                else:
                    phases_to_collect.append(p)
            seen = set()
            phases_to_collect = [p for p in phases_to_collect
                                 if not (p in seen or seen.add(p))]  # type: ignore[func-returns-value]
        else:
            phases_to_collect = list(known_phases)

        step(1, "Collecting Results")
        collected = []
        failed = []

        if phases_to_collect:
            try:
                skip_logs = getattr(args, "skip_logs", False)
                errors = _extract_phases_from_pvc(
                    phases_to_collect, run_name, namespace, run_dir,
                    skip_logs=skip_logs)
            except RuntimeError as e:
                warn(f"Extractor pod failed: {e}")
                failed.extend(phases_to_collect)
            else:
                for phase, exc in errors.items():
                    if exc is None:
                        ok(f"Collected: {phase}")
                        collected.append(phase)
                    else:
                        warn(f"Extraction failed for {phase}: {exc}")
                        failed.append(phase)

    # Print summary
    print(f"\n  Collected: {len(collected)}/{len(phases_to_collect)} phases")
    if failed:
        print(f"  Failed:    {', '.join(failed)}")
    if collected:
        dirs = "  ".join(f"results/{p}/" for p in collected)
        print(f"  Results:   {run_dir}/{dirs}")
        print("\n  Next:      /sim2real-analyze")
    print()
```

- [ ] **Step 4: Run all collect tests**

Run: `python -m pytest pipeline/tests/test_deploy_collect.py -v`

Expected: ALL PASS (both new scoping tests and all existing tests)

- [ ] **Step 5: Commit**

```bash
git add pipeline/deploy.py pipeline/tests/test_deploy_collect.py
git commit -m "collect: wire --only/--workload scoping into _cmd_collect (#122)"
```

---

### Task 3: Full test suite + lint

- [ ] **Step 1: Run lint**

Run: `ruff check pipeline/ --select F`

Expected: No errors

- [ ] **Step 2: Run full test suite**

Run: `python -m pytest pipeline/ -v`

Expected: ALL PASS

- [ ] **Step 3: Fix any failures, commit if needed**
