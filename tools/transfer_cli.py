#!/usr/bin/env python3
"""Transfer pipeline CLI — mechanical support for sim-to-production transfer.

Commands:
    extract <routing_dir>    Parse EVOLVE-BLOCK, produce algorithm_summary.json
    validate-mapping         Check mapping artifact completeness
    validate-schema <path>   Validate workspace artifact against JSON Schema
    test-status              Classify go build/test output (stdin) into error classes
    benchmark                Compute T_eff and mechanism check from noise/baseline/treatment results
    convert-trace            Convert blis observe TraceV2 output to metrics JSON
    benchmark-state          Read/write workspace/benchmark_state.json phase tracking
    compile-pipeline         Compile a tektonc pipeline template for a given phase
    render-pipelinerun       Substitute variables in a PipelineRun stub
    preflight                Run pre-flight cluster checks before submitting a pipeline phase
    generate-evidence        Generate workspace/transfer_evidence.md from workspace artifacts
    merge-values             Merge env_defaults.yaml + algorithm_values.yaml → values.yaml
    validate-translation     Mechanical translation fidelity pre-checks (Stage 3.5)
    compare                  Print latency comparison table from baseline/treatment results

Exit codes: 0 = success, 1 = validation failure, 2 = infrastructure error
All commands output JSON to stdout (compare outputs plain text).
"""
import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKSPACE = REPO_ROOT / "workspace"
SCHEMAS_DIR = Path(__file__).resolve().parent / "schemas"
MAPPING_PATH = REPO_ROOT / "docs" / "transfer" / "blis_to_llmd_mapping.md"
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB

# RoutingSnapshot fields and their Go types (from inference-sim/sim/routing.go)
# SYNC NOTE: This dict is derived from the struct at the pinned submodule commit.
# If inference-sim evolves, re-derive via:
#   grep -A 20 'type RoutingSnapshot struct' inference-sim/sim/routing.go
# F-1 RECONCILIATION: This dict contains ALL 7 struct fields (mirrors the Go struct).
# Of these, only 5 are accessed in the EVOLVE-BLOCK as routing signals (QueueDepth,
# BatchSize, KVUtilization, CacheHitRate, InFlightRequests). ID is used as a map key
# (endpoint identifier) but is NOT a routing signal. FreeKVBlocks is not accessed.
# They appear here because test_routing_snapshot_fields_match_source verifies this
# dict matches the Go struct exactly.
ROUTING_SNAPSHOT_FIELDS = {
    "ID": "string",              # Struct field — used as map key, NOT a routing signal
    "Model": "string",           # Struct field — identifies served model, NOT a routing signal
    "QueueDepth": "int",
    "BatchSize": "int",
    "KVUtilization": "float64",
    "FreeKVBlocks": "int64",     # Struct field — NOT accessed in EVOLVE-BLOCK
    "CacheHitRate": "float64",
    "InFlightRequests": "int",
}

# Fields used as identifiers/keys, not routing signals. These are in
# ROUTING_SNAPSHOT_FIELDS (for struct completeness) but excluded from signal extraction.
_IDENTIFIER_FIELDS = {"ID", "Model"}

# Method calls that expand to multiple fields.
# Verified against: inference-sim EffectiveLoad() = QueueDepth + BatchSize + InFlightRequests
# AUTOMATED CHECK: See test_method_expansion_matches_source in test_transfer_cli.py
METHOD_EXPANSIONS = {
    "EffectiveLoad": ["QueueDepth", "BatchSize", "InFlightRequests"],
}

# Request-level fields accessed outside RoutingSnapshot (e.g., req.SessionID)
REQUEST_LEVEL_FIELDS = {
    "SessionID": "string",
}

# Fields that are routing-scope (non-routing would be scheduling, batching, etc.)
ROUTING_SCOPE_FIELDS = set(ROUTING_SNAPSHOT_FIELDS.keys()) | set(REQUEST_LEVEL_FIELDS.keys())

# Out-of-scope patterns (P/D disaggregation, scheduling internals)
OUT_OF_SCOPE_PATTERNS = [
    r"PrefillInstance|DecodeInstance",  # P/D disaggregation
    r"BatchFormation|SchedulingPolicy",  # Scheduling internals
]


def _output(status: str, exit_code: int, **kwargs) -> int:
    """Print JSON result and return exit code."""
    result = {"status": status, **kwargs}
    if "errors" not in result:
        result["errors"] = []
    print(json.dumps(result, indent=2))
    return exit_code


def _extract_evolve_block(source: str) -> tuple[str | None, str | None, str | None]:
    """Extract EVOLVE-BLOCK region and line range from Go source embedded in Python.

    Returns (block_text, line_range, error_detail). On success, error_detail is None.
    On failure, block_text and line_range are None, and error_detail describes the
    specific failure mode for structured JSON output.

    Detects multiple EVOLVE-BLOCK pairs and emits a warning if more than
    one is found. Only the first pair is extracted.
    """
    lines = source.split("\n")
    start_idx = None
    end_idx = None
    block_count = 0
    end_count = 0
    for i, line in enumerate(lines):
        if "EVOLVE-BLOCK-START" in line:
            if start_idx is None:
                start_idx = i
            block_count += 1
        if "EVOLVE-BLOCK-END" in line:
            if end_idx is None:
                end_idx = i
            end_count += 1
    if start_idx is None and end_idx is None:
        return None, None, "no_markers"
    if start_idx is None:
        print("WARNING: EVOLVE-BLOCK-END found without EVOLVE-BLOCK-START", file=sys.stderr)
        return None, None, "end_without_start"
    if end_idx is None:
        print(f"WARNING: EVOLVE-BLOCK-START found at line "
              f"{start_idx + 1} but no EVOLVE-BLOCK-END", file=sys.stderr)
        return None, None, "start_without_end"
    if end_idx < start_idx:
        print(f"WARNING: EVOLVE-BLOCK-END (line {end_idx + 1}) appears before "
              f"EVOLVE-BLOCK-START (line {start_idx + 1}) — inverted markers.",
              file=sys.stderr)
        return None, None, "inverted_markers"
    if block_count != end_count:
        print(f"WARNING: Mismatched markers: {block_count} EVOLVE-BLOCK-START vs "
              f"{end_count} EVOLVE-BLOCK-END. Extracting first matched pair "
              f"(lines {start_idx + 1}-{end_idx + 1}).",
              file=sys.stderr)
    elif block_count > 1:
        print(f"WARNING: Found {block_count} EVOLVE-BLOCK-START markers but only "
              f"extracting the first block (lines {start_idx + 1}-{end_idx + 1}). "
              f"Additional blocks are silently ignored. If multiple blocks are "
              f"intentional, extend _extract_evolve_block to handle them.",
              file=sys.stderr)
    block = "\n".join(lines[start_idx:end_idx + 1])
    line_range = f"{start_idx + 1}-{end_idx + 1}"
    return block, line_range, None


def _extract_signals(block: str) -> list[dict]:
    r"""Identify RoutingSnapshot fields accessed in the EVOLVE-BLOCK.

    NOTE: Regex-based extraction — may miss signals accessed through aliased
    variables or chained method calls not matching the patterns below.

    FALSE POSITIVE MITIGATION: The `[a-z]\.` pattern is intentionally broad
    but only matches whose field name appears in ROUTING_SNAPSHOT_FIELDS are kept.
    A negative lookbehind ensures only standalone single-char variables match.

    FALSE NEGATIVE RISK: Signals accessed through patterns not covered by the
    regex will be missed silently. The TestGoldenSignalList golden-file test
    is the primary safety net.
    """
    found: dict[str, str] = {}  # name -> access_path

    # Match direct field access on known receiver names
    # NOTE: (?<![a-zA-Z0-9_]) ensures [a-z] only matches standalone single-char
    # variables (like 's', 'l'), NOT the last char of multi-char identifiers (like 'ws').
    for match in re.finditer(
        r'(?:snap(?:shots?\[\w+\])?|target|(?<![a-zA-Z0-9_])[a-z])\.(\w+)', block
    ):
        field = match.group(1)
        if field in ROUTING_SNAPSHOT_FIELDS and field not in _IDENTIFIER_FIELDS:
            found[field] = f"snap.{field}"

    # Match method calls that expand to multiple fields
    _IGNORE_METHODS = {"String", "Error", "Format", "GoString", "Reset", "ProtoMessage"}
    _matched_methods_with_parens: set[str] = set()
    for match in re.finditer(r'\.\b(\w+)\(\)', block):
        method = match.group(1)
        if method in METHOD_EXPANSIONS:
            _matched_methods_with_parens.add(method)
            for field in METHOD_EXPANSIONS[method]:
                if field not in found:
                    found[field] = f"snap.{method}() -> {field}"
        elif method not in _IGNORE_METHODS:
            print(f"WARNING: Unrecognized method call '{method}()' in EVOLVE-BLOCK — "
                  f"not in METHOD_EXPANSIONS. If this is a new RoutingSnapshot method, "
                  f"add it to METHOD_EXPANSIONS with its constituent fields.",
                  file=sys.stderr)

    # Match request-level field access: req.FieldName, request.FieldName
    for match in re.finditer(r'(?:req(?:uest)?)\.\b(\w+)', block):
        field = match.group(1)
        if field in REQUEST_LEVEL_FIELDS:
            found[field] = f"req.{field}"

    # Detect method-value access (known method without parentheses)
    for match in re.finditer(
        r'(?:snap(?:shots?\[\w+\])?|target|(?<![a-zA-Z0-9_])[a-z])\.(\w+)(?!\s*\()', block
    ):
        field = match.group(1)
        if field in METHOD_EXPANSIONS and field not in _matched_methods_with_parens:
            print(f"WARNING: '{field}' accessed as a method value (without parentheses) in "
                  f"EVOLVE-BLOCK — this is a known composite method. If this is a method call, "
                  f"ensure it uses '{field}()' with parentheses. Constituent signals will NOT "
                  f"be extracted from method-value access.",
                  file=sys.stderr)

    # Detect unrecognized field accesses and include them with type 'unknown'
    all_known = set(ROUTING_SNAPSHOT_FIELDS.keys()) | set(REQUEST_LEVEL_FIELDS.keys()) | set(METHOD_EXPANSIONS.keys())
    _IGNORE_FIELDS = {"String", "Error", "Len", "Less", "Swap", "Format"}
    for match in re.finditer(r'(?:snap(?:shots?\[\w+\])?|target|req(?:uest)?|(?<![a-zA-Z0-9_])[a-z])\.\b(\w+)', block):
        field = match.group(1)
        if field not in all_known and field not in _IGNORE_FIELDS and field not in found:
            print(f"WARNING: Unrecognized field access '{field}' in EVOLVE-BLOCK — "
                  f"not in ROUTING_SNAPSHOT_FIELDS or REQUEST_LEVEL_FIELDS. "
                  f"Included with type 'unknown'. Downstream stages MUST resolve this.",
                  file=sys.stderr)
            receiver = match.group(0).split(".")[0]
            found[field] = f"{receiver}.{field}"

    # Normalization notes for signals with known unit mismatches
    NORMALIZATION_NOTES = {
        "KVUtilization": "divide_prod_by_100: production value (0-100 percentage) must be divided by 100 to match sim's 0.0-1.0 ratio (i.e., normalized = prod_kv / 100.0)",
        "CacheHitRate": "verify_and_normalize: EVOLVE-BLOCK threshold 0.35 assumes 0.0-1.0 range. PR3 MUST verify production PrecisePrefixCache metric scale — if 0-100 percentage, divide by 100 (same as KVUtilization); if already 0.0-1.0 ratio, use directly. UNVERIFIED until PR3 confirms against llm-d-inference-scheduler source",
        "SessionID": "boolean_presence_check: compare against empty string (req.SessionID != empty)",
    }

    signals = []
    all_fields = {**ROUTING_SNAPSHOT_FIELDS, **REQUEST_LEVEL_FIELDS}
    for name, access_path in sorted(found.items()):
        sig = {
            "name": name,
            "type": all_fields.get(name, "unknown"),
            "access_path": access_path,
        }
        if name in NORMALIZATION_NOTES:
            sig["normalization_note"] = NORMALIZATION_NOTES[name]
        signals.append(sig)
    return signals


def _check_scope(block: str, signals: list[dict]) -> tuple[bool, list[str]]:
    """Check that the algorithm is routing-scope only.

    Note: re.search(pattern, block) matches anywhere including Go comments.
    This is acceptable for v1 because the EVOLVE-BLOCK is Go code where
    comments are unlikely to reference out-of-scope struct names.
    """
    errors = []

    for pattern in OUT_OF_SCOPE_PATTERNS:
        match = re.search(pattern, block)
        if match:
            errors.append(f"Out-of-scope pattern found: '{match.group()}' (matched by /{pattern}/)")

    for sig in signals:
        if sig["type"] == "unknown":
            continue  # Already flagged for downstream resolution via type='unknown'
        if sig["name"] not in ROUTING_SCOPE_FIELDS:
            errors.append(f"Non-routing signal: {sig['name']}")

    return len(errors) == 0, errors


def _check_fidelity(signals: list[dict], *, strict: bool = False) -> tuple[bool, list[str]]:
    """Check signal fidelity if mapping artifact exists. Returns (ok, errors).

    NOTE: This function has a deliberate side effect — it mutates signal dicts
    in-place by adding sig['fidelity_provisional'] = True for signals with
    provisional ratings.
    """
    if not MAPPING_PATH.exists():
        if strict:
            return False, [
                "Mapping artifact not found and --strict mode is enabled. "
                "Cannot perform fidelity check without mapping artifact. "
                "Ensure docs/transfer/blis_to_llmd_mapping.md exists."
            ]
        print("WARNING: Mapping artifact not found — fidelity check skipped. "
              "Run validate-mapping after creating the mapping artifact. "
              "Use --strict to enforce mapping artifact presence (recommended for CI).",
              file=sys.stderr)
        return True, []

    MAX_MAPPING_SIZE = 10 * 1024 * 1024  # 10 MB
    try:
        if MAPPING_PATH.stat().st_size > MAX_MAPPING_SIZE:
            return False, [f"INFRA: Mapping artifact exceeds {MAX_MAPPING_SIZE} bytes — refusing to read."]
    except OSError as e:
        return False, [f"INFRA: Failed to stat mapping artifact: {e}"]

    if not strict:
        print("NOTICE: Running without --strict. Fidelity checks are active (mapping artifact "
              "found), but CI uses --strict for deterministic enforcement. Use --strict locally "
              "to match CI behavior.",
              file=sys.stderr)

    try:
        content = MAPPING_PATH.read_text()
    except OSError as e:
        return False, [f"INFRA: Failed to read mapping artifact: {e}"]

    errors = []
    # Column skip counts for fidelity extraction
    MAIN_TABLE_FIDELITY_COL_OFFSET = 4
    ADDITIONAL_TABLE_FIDELITY_COL_OFFSET = 2

    for sig in signals:
        if sig["type"] == "unknown":
            continue  # Already flagged for downstream resolution via type='unknown'
        pattern = rf'\|\s*{re.escape(sig["name"])}(?:\s*\([^)]*\))?\s*\|(?:[^|]*\|){{{MAIN_TABLE_FIDELITY_COL_OFFSET}}}\s*(low|medium|high)\s*(?:\*\([^)]*\)\*)?\s*\|'
        match = re.search(pattern, content, re.IGNORECASE)
        if not match:
            pattern_alt = rf'\|\s*{re.escape(sig["name"])}(?:\s*\([^)]*\))?\s*\|(?:[^|]*\|){{{ADDITIONAL_TABLE_FIDELITY_COL_OFFSET}}}\s*(low|medium|high)\s*(?:\*\([^)]*\)\*)?\s*\|'
            match = re.search(pattern_alt, content, re.IGNORECASE)
        if match:
            rating = match.group(1).lower()
            match_text = match.group(0)
            is_provisional = "*(provisional)*" in match_text
            is_zeroed = "*(zeroed" in match_text
            if is_provisional:
                print(f"WARNING: Signal '{sig['name']}' has provisional {rating} fidelity rating. "
                      f"No empirical data supports this rating — PR5 must validate.",
                      file=sys.stderr)
                sig["fidelity_provisional"] = True
            if is_zeroed:
                print(f"NOTICE: Signal '{sig['name']}' has {rating} fidelity but is zeroed in "
                      f"production scorer — pipeline continues.",
                      file=sys.stderr)
                sig["fidelity_zeroed"] = True
            elif rating == "low":
                errors.append(
                    f"Signal '{sig['name']}' has low fidelity rating — "
                    f"pipeline halted (low-fidelity signals not supported in v1)"
                )
        else:
            errors.append(
                f"Signal '{sig['name']}' not found in mapping artifact — "
                f"unknown fidelity treated as unsafe (add signal to mapping table)"
            )
    return len(errors) == 0, errors


def _relative_source_path(routing_dir: Path) -> str:
    """Return routing_dir as a repo-relative path prefix (with trailing slash).

    Falls back to the absolute path if routing_dir is outside the repo root.
    """
    try:
        rel = routing_dir.relative_to(REPO_ROOT)
        return f"{rel}/"
    except ValueError:
        return f"{routing_dir}/"


def cmd_extract(args: argparse.Namespace) -> int:
    """Extract algorithm metadata from routing artifacts."""
    routing_dir = Path(args.routing_dir).resolve()

    # Infrastructure validation FIRST
    if not routing_dir.is_dir():
        return _output("error", 2, errors=[f"Routing directory not found: {routing_dir}"])

    # CI auto-detection — FAIL if --strict not used in CI environment
    ci_val = os.environ.get("CI", "").lower()
    if ci_val in ("true", "1", "yes") and not getattr(args, 'strict', False):
        return _output("error", 2, errors=[
            "CI environment detected (CI env var is set) but --strict flag not set. "
            "CI pipelines MUST use --strict to ensure deterministic fidelity checks. "
            "Fix: either pass --strict (recommended), set CI=false, or unset the CI "
            "environment variable if you are running locally. "
            "Usage: python tools/transfer_cli.py extract --strict blis_router/best/"
        ])

    program_go = routing_dir / "best_program.go"
    if not program_go.exists():
        return _output("error", 2, errors=[f"best_program.go not found in {routing_dir}"])

    info_json = routing_dir / "best_program_info.json"
    if not info_json.exists():
        return _output("error", 2, errors=[f"best_program_info.json not found in {routing_dir}"])

    # Size guard
    MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB
    try:
        if program_go.stat().st_size > MAX_FILE_SIZE:
            return _output("error", 2, errors=[
                f"best_program.go exceeds {MAX_FILE_SIZE} bytes — refusing to read."])
        if info_json.stat().st_size > MAX_FILE_SIZE:
            return _output("error", 2, errors=[
                f"best_program_info.json exceeds {MAX_FILE_SIZE} bytes — refusing to read."])
    except OSError as e:
        return _output("error", 2, errors=[f"Failed to stat input files: {e}"])

    # Read and parse
    try:
        source = program_go.read_text()
    except OSError as e:
        return _output("error", 2, errors=[f"Failed to read {program_go}: {e}"])
    block, line_range, block_error = _extract_evolve_block(source)
    if block is None:
        _ERROR_MESSAGES = {
            "no_markers": "Neither EVOLVE-BLOCK-START nor EVOLVE-BLOCK-END markers found in best_program.go.",
            "end_without_start": "EVOLVE-BLOCK-END found without EVOLVE-BLOCK-START in best_program.go.",
            "start_without_end": "EVOLVE-BLOCK-START found but no EVOLVE-BLOCK-END in best_program.go.",
            "inverted_markers": "EVOLVE-BLOCK-END appears before EVOLVE-BLOCK-START in best_program.go (inverted markers).",
        }
        error_msg = _ERROR_MESSAGES.get(block_error,
            "EVOLVE-BLOCK markers not found or malformed in best_program.go.")
        return _output("error", 2, errors=[error_msg], error_detail=block_error)

    # Extract signals
    signals = _extract_signals(block)
    if not signals:
        return _output("error", 1, errors=["No routing signals found in EVOLVE-BLOCK"])

    # Sanity check: warn if signal count is suspiciously low
    MINIMUM_EXPECTED_SIGNALS = 2
    if len(signals) < MINIMUM_EXPECTED_SIGNALS:
        msg = (f"Only {len(signals)} signals found (expected >= {MINIMUM_EXPECTED_SIGNALS}). "
               f"Regex may have missed field access patterns. Manually verify against EVOLVE-BLOCK.")
        if getattr(args, 'strict', False):
            return _output("error", 1, errors=[msg])
        print(f"WARNING: {msg}", file=sys.stderr)

    # Scope validation
    scope_ok, scope_errors = _check_scope(block, signals)

    # Fidelity check
    fidelity_ok, fidelity_errors = _check_fidelity(signals, strict=getattr(args, 'strict', False))
    if not fidelity_ok:
        # Infrastructure errors (e.g., oversized file) use exit code 2;
        # fidelity validation failures use exit code 1.
        exit_code = 2 if any(e.startswith("INFRA:") for e in fidelity_errors) else 1
        return _output("error", exit_code, errors=fidelity_errors)

    # Read metrics
    try:
        info = json.loads(info_json.read_text())
    except OSError as e:
        return _output("error", 2, errors=[f"Failed to read {info_json}: {e}"])
    except json.JSONDecodeError as e:
        return _output("error", 2, errors=[f"Malformed JSON in {info_json}: {e}"])
    if not isinstance(info, dict):
        return _output("error", 2, errors=[
            f"Expected JSON object in {info_json}, got {type(info).__name__}"])
    metrics = info.get("metrics", {})
    if not isinstance(metrics, dict) or not metrics or "combined_score" not in metrics:
        msg = ("WARNING: No 'metrics' key or missing 'combined_score' in "
               "best_program_info.json — algorithm_summary.json will have "
               "empty/incomplete metrics and WILL FAIL schema validation. "
               "Use --strict to enforce schema validity.")
        print(msg, file=sys.stderr)
        if getattr(args, 'strict', False):
            return _output("error", 1,
                           errors=["metrics.combined_score missing in "
                                   "best_program_info.json (required by schema). "
                                   "Cannot produce schema-valid artifact in --strict mode."])

    # Build summary — the FILE ARTIFACT written to workspace/
    block_hash = hashlib.sha256(block.encode()).hexdigest()

    # Read mapping artifact version
    mapping_version = "unknown"
    if MAPPING_PATH.exists():
        try:
            _mapping_content = MAPPING_PATH.read_text()
        except OSError as e:
            print(f"WARNING: Failed to read mapping artifact for version parsing: {e}. "
                  f"mapping_artifact_version will be 'unknown'.", file=sys.stderr)
            _mapping_content = ""
        ver_match = re.search(r'\*\*Version:\*\*\s*(\S+)', _mapping_content)
        if ver_match:
            raw_version = ver_match.group(1)
            if re.fullmatch(r'\d+\.\d+', raw_version):
                mapping_version = raw_version
            else:
                print(f"WARNING: Mapping artifact version '{raw_version}' does not match "
                      f"expected MAJOR.MINOR format. mapping_artifact_version will be 'unknown'.",
                      file=sys.stderr)
        else:
            print("WARNING: Could not parse mapping artifact version — "
                  "mapping_artifact_version will be 'unknown'. "
                  "Ensure mapping has a '**Version:** X.Y' line.",
                  file=sys.stderr)

    # Composite signals
    composites = []
    for method, fields in METHOD_EXPANSIONS.items():
        found_constituents = [f for f in fields if any(s["name"] == f for s in signals)]
        if found_constituents and re.search(r'\.' + re.escape(method) + r'\(\)', block):
            composites.append({
                "name": method,
                "constituents": found_constituents,
                "formula": "sum",
            })

    has_unknown_signals = any(s["type"] == "unknown" for s in signals)
    fidelity_checked = MAPPING_PATH.exists() and not has_unknown_signals

    summary = {
        "algorithm_name": "blis_weighted_scoring",
        "evolve_block_source": f"{_relative_source_path(routing_dir)}best_program.go:{line_range}",
        "evolve_block_content_hash": block_hash,
        "signals": signals,
        "composite_signals": composites,
        "metrics": metrics,
        "scope_validation_passed": scope_ok,
        "mapping_artifact_version": mapping_version,
        "fidelity_checked": fidelity_checked,
    }

    # Write to workspace
    WORKSPACE.mkdir(exist_ok=True)
    summary_path = WORKSPACE / "algorithm_summary.json"
    try:
        summary_path.write_text(json.dumps(summary, indent=2) + "\n")
    except OSError as e:
        return _output("error", 2, errors=[
            f"Failed to write {summary_path}: {e}."])

    # Note: fidelity_errors is always [] here (non-empty fidelity_errors
    # caused an early return at line 360), so only scope_errors remain.
    if not scope_ok:
        return _output("error", 1,
                        output_type="operational_report",
                        artifact_path=str(summary_path),
                        algorithm_name=summary["algorithm_name"],
                        signal_count=len(signals),
                        errors=scope_errors)

    return _output("ok", 0,
                    output_type="operational_report",
                    artifact_path=str(summary_path),
                    algorithm_name=summary["algorithm_name"],
                    signal_count=len(signals),
                    errors=scope_errors)


def cmd_validate_mapping(args: argparse.Namespace) -> int:
    """Validate mapping artifact completeness against algorithm summary."""
    if not MAPPING_PATH.exists():
        return _output("error", 2, errors=[f"Mapping artifact not found: {MAPPING_PATH}"],
                        output_type="mapping_validation",
                        mapping_complete=False, missing_signals=[], extra_signals=[],
                        duplicate_signals=[], double_counting_risks=[], stale_commit=False)

    # Read and validate mapping format before loading the algorithm summary.
    # Malformed table check (exit 1) must precede summary checks (exit 2) so
    # tests that write a malformed mapping see exit 1 regardless of summary state.
    MAX_MAPPING_SIZE = 10 * 1024 * 1024
    try:
        _mapping_size = MAPPING_PATH.stat().st_size
    except OSError as e:
        return _output("error", 2,
                        errors=[f"Failed to stat mapping artifact: {e}"],
                        output_type="mapping_validation",
                        mapping_complete=False, missing_signals=[], extra_signals=[],
                        duplicate_signals=[], double_counting_risks=[], stale_commit=False)
    if _mapping_size > MAX_MAPPING_SIZE:
        return _output("error", 2,
                        errors=[f"Mapping artifact exceeds {MAX_MAPPING_SIZE} bytes — refusing to read."],
                        output_type="mapping_validation",
                        mapping_complete=False, missing_signals=[], extra_signals=[],
                        duplicate_signals=[], double_counting_risks=[], stale_commit=False)

    try:
        content = MAPPING_PATH.read_text()
    except OSError as e:
        return _output("error", 2,
                        errors=[f"Failed to read mapping artifact: {e}"],
                        output_type="mapping_validation",
                        mapping_complete=False, missing_signals=[], extra_signals=[],
                        duplicate_signals=[], double_counting_risks=[], stale_commit=False)

    # Detect malformed mapping artifact
    if '|' not in content or not re.search(r'^\|.*\|.*\|', content, re.MULTILINE):
        # Check commit hash even in malformed-table case so stale_commit
        # reflects actual hash presence, not a side effect of table parsing.
        has_commit_in_malformed = bool(re.search(
            r'(?:commit[_ ]hash|pinned[_ ]commit[_ ]hash)[:\s*]*([0-9a-f]{7,40})',
            content, re.IGNORECASE,
        ))
        return _output("error", 1,
                        errors=["Malformed mapping artifact: no Markdown table found. "
                                "Expected pipe-delimited table rows."],
                        output_type="mapping_validation",
                        mapping_complete=False, missing_signals=[], extra_signals=[],
                        duplicate_signals=[], double_counting_risks=[],
                        stale_commit=not has_commit_in_malformed)

    # Load algorithm summary after mapping format is validated.
    summary_path = args.summary if hasattr(args, "summary") and args.summary else (
        WORKSPACE / "algorithm_summary.json"
    )
    summary_path = Path(summary_path).resolve()

    if not summary_path.is_relative_to(REPO_ROOT):
        return _output("error", 2, errors=[
            f"Summary path '{summary_path}' is outside repository root '{REPO_ROOT}'."],
                        output_type="mapping_validation",
                        mapping_complete=False, missing_signals=[], extra_signals=[],
                        duplicate_signals=[], double_counting_risks=[], stale_commit=False)

    if not summary_path.exists():
        return _output("error", 2,
                        errors=[f"Algorithm summary not found: {summary_path}. Run 'extract' first."],
                        output_type="mapping_validation",
                        mapping_complete=False, missing_signals=[], extra_signals=[],
                        duplicate_signals=[], double_counting_risks=[], stale_commit=False)

    MAX_SUMMARY_SIZE = 10 * 1024 * 1024
    try:
        if summary_path.stat().st_size > MAX_SUMMARY_SIZE:
            return _output("error", 2,
                            errors=[f"Algorithm summary exceeds {MAX_SUMMARY_SIZE} bytes — refusing to read."],
                            output_type="mapping_validation",
                            mapping_complete=False, missing_signals=[], extra_signals=[],
                        duplicate_signals=[], double_counting_risks=[], stale_commit=False)
    except OSError as e:
        return _output("error", 2,
                        errors=[f"Failed to stat algorithm summary: {e}"],
                        output_type="mapping_validation",
                        mapping_complete=False, missing_signals=[], extra_signals=[],
                        duplicate_signals=[], double_counting_risks=[], stale_commit=False)

    try:
        summary = json.loads(summary_path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        return _output("error", 2,
                        errors=[f"Failed to read/parse algorithm summary: {e}"],
                        output_type="mapping_validation",
                        mapping_complete=False, missing_signals=[], extra_signals=[],
                        duplicate_signals=[], double_counting_risks=[], stale_commit=False)

    try:
        extracted_names = {sig['name'] for sig in summary.get('signals', [])}
    except (TypeError, KeyError) as e:
        return _output("error", 2,
                        errors=[f"Malformed algorithm summary — 'signals' is not a valid list of dicts: {e}"],
                        output_type="mapping_validation",
                        mapping_complete=False, missing_signals=[], extra_signals=[],
                        duplicate_signals=[], double_counting_risks=[], stale_commit=False)

    # Check each extracted signal has a mapping entry (allow optional parenthetical annotation)
    missing = [name for name in sorted(extracted_names)
               if not re.search(rf'\|\s*{re.escape(name)}(?:\s*\([^)]*\))?\s*\|', content)]

    # Check for extra signals in mapping not present in extract
    mapping_signals = set()
    mapping_signal_counts: dict[str, int] = {}
    for row_match in re.finditer(r'^\|\s*(\w+)(?:\s*\([^)]*\))?\s*\|', content, re.MULTILINE):
        candidate = row_match.group(1)
        if candidate in ("Sim", "Signal", "Composite"):
            continue
        mapping_signal_counts[candidate] = mapping_signal_counts.get(candidate, 0) + 1
        mapping_signals.add(candidate)
    # Include composite signal names so they're not flagged as extra
    composite_names = {c['name'] for c in summary.get('composite_signals', [])
                       if isinstance(c, dict) and 'name' in c}
    known_names = extracted_names | composite_names
    extra = sorted(mapping_signals - known_names)
    duplicates = sorted(k for k, v in mapping_signal_counts.items() if v > 1)

    # Check commit hash presence
    has_commit = bool(re.search(
        r'(?:commit[_ ]hash|pinned[_ ]commit[_ ]hash)[:\s*]*([0-9a-f]{7,40})',
        content, re.IGNORECASE,
    ))

    mapping_complete = (len(missing) == 0 and len(extra) == 0
                        and len(duplicates) == 0 and has_commit)
    errors = []
    if missing:
        errors.append(f"Missing signal mappings: {', '.join(missing)}")
    if extra:
        errors.append(
            f"Extra signals in mapping not found in extract: {', '.join(extra)}. "
            f"Resolution: First check if these signals are accessed in the EVOLVE-BLOCK. "
            f"If not, remove the stale rows from the mapping artifact. "
            f"If they are accessed, the extract regex has a gap — fix _extract_signals."
        )
    if duplicates:
        dup_details = [f"{name} ({mapping_signal_counts[name]} rows)" for name in duplicates]
        errors.append(
            f"Duplicate signal rows found: {', '.join(dup_details)}. "
            f"Each signal MUST appear exactly once in the mapping table."
        )
    if not has_commit:
        errors.append("No commit hash found in mapping artifact")

    # Detect production metric overlap (double-counting risk)
    # Main Signal Mapping Table: Signal | Go Type | Sim Access Path | Production Equivalent | ...
    #   -> skip 2 columns (Go Type, Sim Access Path) to reach Production Equivalent
    # Additional Signals Table: Signal | Context | Production Mapping | Fidelity | Notes
    #   -> skip 1 column (Context) to reach Production Mapping
    MAIN_TABLE_PROD_COL_OFFSET = 2
    ADDITIONAL_TABLE_PROD_COL_OFFSET = 1
    prod_metric_signals: dict[str, list[str]] = {}
    for sig_name in extracted_names:
        prod_match = re.search(
            rf'\|\s*{re.escape(sig_name)}(?:\s*\([^)]*\))?\s*\|(?:[^|]*\|){{{MAIN_TABLE_PROD_COL_OFFSET}}}([^|]+)\|',
            content, re.IGNORECASE,
        )
        if not prod_match:
            prod_match = re.search(
                rf'\|\s*{re.escape(sig_name)}(?:\s*\([^)]*\))?\s*\|(?:[^|]*\|){{{ADDITIONAL_TABLE_PROD_COL_OFFSET}}}([^|]+)\|',
                content, re.IGNORECASE,
            )
        if prod_match:
            prod_metric = prod_match.group(1).strip()
            prod_metric_signals.setdefault(prod_metric, []).append(sig_name)
    double_counting_risks = []
    for prod_metric, sigs in prod_metric_signals.items():
        if len(sigs) > 1:
            double_counting_risks.append({
                "production_metric": prod_metric,
                "signals": sigs,
            })
            print(
                f"WARNING: Double-counting risk — signals {', '.join(sigs)} "
                f"both map to production metric '{prod_metric}'.",
                file=sys.stderr,
            )

    status = "ok" if mapping_complete else "error"
    exit_code = 0 if mapping_complete else 1
    return _output(status, exit_code,
                    output_type="mapping_validation",
                    mapping_complete=mapping_complete,
                    missing_signals=missing,
                    extra_signals=extra,
                    duplicate_signals=duplicates,
                    double_counting_risks=double_counting_risks,
                    stale_commit=not has_commit,
                    errors=errors)


def cmd_validate_schema(args: argparse.Namespace) -> int:
    """Validate a workspace artifact against its JSON Schema."""
    try:
        from schema_validator import validate_artifact, load_schema
    except ImportError:
        try:
            from tools.schema_validator import validate_artifact, load_schema
        except ImportError as e:
            return _output("error", 2, errors=[
                f"Failed to import schema_validator module: {e}. "
                f"Ensure tools/schema_validator.py exists and is valid Python."],
                output_type="schema_validation", violations=[])

    artifact_path = Path(args.artifact_path).resolve()
    if not artifact_path.is_relative_to(REPO_ROOT):
        return _output("error", 2, errors=[
            f"Artifact path '{artifact_path}' is outside repository root '{REPO_ROOT}'."],
                        output_type="schema_validation", violations=[])
    if not artifact_path.exists():
        return _output("error", 2, errors=[f"Artifact not found: {artifact_path}"],
                        output_type="schema_validation", violations=[])

    stem = artifact_path.stem
    schema_path = (SCHEMAS_DIR / f"{stem}.schema.json").resolve()
    if not schema_path.is_relative_to(SCHEMAS_DIR):
        return _output("error", 2, errors=[
            f"Schema path '{schema_path}' resolves outside schemas directory '{SCHEMAS_DIR}'."],
                        output_type="schema_validation", violations=[])
    if not schema_path.exists():
        return _output("error", 2, errors=[f"Schema not found: {schema_path}"],
                        output_type="schema_validation", violations=[])

    try:
        schema = load_schema(schema_path)
    except (json.JSONDecodeError, OSError) as e:
        return _output("error", 2, errors=[f"Failed to load schema: {e}"],
                        output_type="schema_validation", violations=[])

    try:
        import yaml
        raw = artifact_path.read_text()
        if artifact_path.suffix in (".yaml", ".yml"):
            data = yaml.safe_load(raw)
            if not isinstance(data, dict):
                return _output("error", 2, errors=[f"YAML file did not parse to a mapping: {artifact_path}"],
                                output_type="schema_validation", violations=[])
        else:
            data = json.loads(raw)
    except yaml.YAMLError as e:
        return _output("error", 2, errors=[f"Failed to load YAML artifact: {e}"],
                        output_type="schema_validation", violations=[])
    except (json.JSONDecodeError, OSError) as e:
        return _output("error", 2, errors=[f"Failed to load artifact: {e}"],
                        output_type="schema_validation",
                        schema_path=str(schema_path), artifact_path=str(artifact_path),
                        violations=[])

    errors = validate_artifact(data, schema)
    violations = [{"field": e.split(":")[0].strip(), "message": e} for e in errors]

    if errors:
        return _output("error", 1,
                        output_type="schema_validation",
                        schema_path=str(schema_path),
                        artifact_path=str(artifact_path),
                        violations=violations,
                        errors=errors)

    return _output("ok", 0,
                    output_type="schema_validation",
                    schema_path=str(schema_path),
                    artifact_path=str(artifact_path),
                    violations=[])


def cmd_validate_translation(args: argparse.Namespace) -> int:
    """Mechanical pre-checks for translation fidelity (Stage 3.5).

    Three checks against the generated scorer Go source:
      1. Signal presence: every sim signal's production metric name appears in the scorer.
      2. Constant audit: evolved numeric constants from the EVOLVE-BLOCK appear in the scorer.
      3. Normalization check: signals with divide_prod_by_100 have a /100 in the scorer.

    Exit codes: 0 = all pass, 1 = one or more failures, 2 = infrastructure error.
    """
    # ── load algorithm_summary.json ──────────────────────────────────────────
    algorithm_path = Path(args.algorithm)
    if not algorithm_path.exists():
        return _output("error", 2, errors=[f"algorithm_summary not found: {algorithm_path}"],
                       output_type="validate_translation",
                       signal_checks=[], constant_checks=[], normalization_checks=[])
    try:
        algorithm = json.loads(algorithm_path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        return _output("error", 2, errors=[f"Failed to read algorithm_summary: {e}"],
                       output_type="validate_translation",
                       signal_checks=[], constant_checks=[], normalization_checks=[])

    # ── load signal_coverage.json ────────────────────────────────────────────
    coverage_path = Path(args.signal_coverage)
    if not coverage_path.exists():
        return _output("error", 2, errors=[f"signal_coverage not found: {coverage_path}"],
                       output_type="validate_translation",
                       signal_checks=[], constant_checks=[], normalization_checks=[])
    try:
        coverage = json.loads(coverage_path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        return _output("error", 2, errors=[f"Failed to read signal_coverage: {e}"],
                       output_type="validate_translation",
                       signal_checks=[], constant_checks=[], normalization_checks=[])

    # ── load scorer source ────────────────────────────────────────────────────
    scorer_path = Path(args.scorer_file)
    if not scorer_path.exists():
        return _output("error", 2, errors=[f"scorer file not found: {scorer_path}"],
                       output_type="validate_translation",
                       signal_checks=[], constant_checks=[], normalization_checks=[])
    try:
        scorer_source = scorer_path.read_text()
    except OSError as e:
        return _output("error", 2, errors=[f"Failed to read scorer file: {e}"],
                       output_type="validate_translation",
                       signal_checks=[], constant_checks=[], normalization_checks=[])

    # ── load EVOLVE-BLOCK source for constant extraction ─────────────────────
    evolve_block_source_field = algorithm.get("evolve_block_source", "")
    # Format is "relative/path.go:START-END"
    evolve_source_text: str | None = None
    evolve_source_error: str | None = None
    if ":" in evolve_block_source_field:
        evolve_file_path_str = evolve_block_source_field.rsplit(":", 1)[0]
        evolve_file_path = REPO_ROOT / evolve_file_path_str
        if evolve_file_path.exists():
            try:
                evolve_source_text = evolve_file_path.read_text()
            except OSError as e:
                evolve_source_error = f"Failed to read EVOLVE-BLOCK source: {e}"
        else:
            evolve_source_error = f"EVOLVE-BLOCK source file not found: {evolve_file_path}"
    else:
        evolve_source_error = f"evolve_block_source field has unexpected format: {evolve_block_source_field!r}"

    # Build sim->prod mapping from signal_coverage
    coverage_map: dict[str, dict] = {}
    for sig in coverage.get("signals", []):
        coverage_map[sig.get("sim_name", "")] = sig

    # ── Check 1: Signal presence ─────────────────────────────────────────────
    signal_checks = []
    all_errors = []
    for sig in algorithm.get("signals", []):
        sim_name = sig.get("name", "")
        cov = coverage_map.get(sim_name, {})
        prod_name = cov.get("prod_name", "")
        if not prod_name:
            signal_checks.append({
                "sim_name": sim_name,
                "prod_name": "",
                "present_in_scorer": False,
                "normalization_correct": None,
                "notes": f"No prod_name found in signal_coverage for sim signal '{sim_name}'"
            })
            all_errors.append(f"Signal '{sim_name}': no prod_name in signal_coverage")
            continue

        present = prod_name in scorer_source
        norm_action = cov.get("normalization")
        normalization_correct: bool | None = None
        norm_notes = ""

        if norm_action == "divide_prod_by_100":
            # Check that a /100 or *0.01 division appears in the scorer near the prod metric name.
            # Only non-comment code lines are considered to prevent false positives from
            # comments like "// KVCacheUsagePercent: integer [0, 100]".
            # Window: 2 lines before through 5 lines after each occurrence of prod_name.
            _WINDOW_BEFORE = 2
            _WINDOW_AFTER = 5
            lines = scorer_source.splitlines()
            prod_lines = [i for i, ln in enumerate(lines) if prod_name in ln]
            norm_found = False
            for li in prod_lines:
                window_lines = lines[max(0, li - _WINDOW_BEFORE):min(len(lines), li + _WINDOW_AFTER + 1)]
                # Strip comment-only lines (Go single-line comments starting with //)
                code_lines = [ln for ln in window_lines if not ln.lstrip().startswith("//")]
                window = "\n".join(code_lines)
                if re.search(r'/\s*100(?:\s*\.0)?\b|[*×]\s*0\.01\b', window):
                    norm_found = True
                    break
            normalization_correct = norm_found
            if not norm_found:
                norm_notes = f"Expected /100 or *0.01 near '{prod_name}' in scorer (normalization: divide_prod_by_100)"
                all_errors.append(
                    f"Signal '{sim_name}' → '{prod_name}': normalization divide_prod_by_100 not found in scorer"
                )

        check = {
            "sim_name": sim_name,
            "prod_name": prod_name,
            "present_in_scorer": present,
            "normalization_correct": normalization_correct,
        }
        if norm_notes:
            check["notes"] = norm_notes
        signal_checks.append(check)

        if not present:
            all_errors.append(
                f"Signal '{sim_name}' → '{prod_name}': production metric not found in scorer source"
            )

    # ── Check 2: Constant audit ───────────────────────────────────────────────
    constant_checks = []
    if evolve_source_text is not None:
        # Extract the EVOLVE-BLOCK region text
        block_text, _, extract_err = _extract_evolve_block(evolve_source_text)
        if block_text is None:
            constant_checks.append({
                "constant": 0,
                "evolve_block_context": f"Could not extract EVOLVE-BLOCK: {extract_err}",
                "present_in_scorer": False,
                "notes": "Constant audit skipped: EVOLVE-BLOCK extraction failed"
            })
        else:
            # Extract numeric literals that are not 0 or 1 (evolved constants)
            # Match float literals (0.6, 0.9, 0.5, 0.01) and small ints that are not 0/1
            raw_literals = re.findall(r'\b(\d+\.\d+)\b', block_text)
            seen: dict[float, str] = {}  # value -> first context
            for lit in raw_literals:
                val = float(lit)
                if val in (0.0, 1.0):
                    continue
                if val not in seen:
                    # Get context: the line containing this literal
                    for line in block_text.splitlines():
                        if re.search(r'\b' + re.escape(lit) + r'\b', line):
                            seen[val] = line.strip()
                            break
                    else:
                        seen[val] = f"literal {lit}"

            for val, ctx in seen.items():
                # Match the value in various equivalent forms in scorer
                # e.g., 0.6 matches "0.6", "0.60", ".6"
                # Build a regex that matches the float in various notations
                val_str = f"{val:.10g}"  # canonical string
                # Try a few equivalent forms
                int_part, _, frac_part = val_str.partition(".")
                # Pattern: matches the exact value or trailing-zero forms
                pattern = r'\b' + re.escape(val_str) + r'\b'
                present = bool(re.search(pattern, scorer_source))
                if not present:
                    # Try integer equivalent if frac part is all zeros
                    if frac_part and all(c == "0" for c in frac_part):
                        present = bool(re.search(r'\b' + re.escape(int_part) + r'\b', scorer_source))

                constant_checks.append({
                    "constant": val,
                    "evolve_block_context": ctx,
                    "present_in_scorer": present,
                })
                if not present:
                    all_errors.append(
                        f"Constant {val_str} (from EVOLVE-BLOCK: {ctx!r}) not found in scorer"
                    )
    elif evolve_source_error:
        constant_checks.append({
            "constant": 0,
            "evolve_block_context": evolve_source_error,
            "present_in_scorer": False,
            "notes": "Constant audit skipped: EVOLVE-BLOCK source not available"
        })
        all_errors.append(f"Constant audit skipped: EVOLVE-BLOCK source unavailable — {evolve_source_error}")

    # ── Normalization checks (separate list for output clarity) ───────────────
    # These are already embedded in signal_checks above; build a summary view
    normalization_checks = [
        {
            "sim_name": sc["sim_name"],
            "prod_name": sc["prod_name"],
            "normalization_correct": sc["normalization_correct"],
        }
        for sc in signal_checks
        if sc["normalization_correct"] is not None
    ]

    if all_errors:
        return _output("fail", 1,
                       output_type="validate_translation",
                       signal_checks=signal_checks,
                       constant_checks=constant_checks,
                       normalization_checks=normalization_checks,
                       errors=all_errors)

    return _output("ok", 0,
                   output_type="validate_translation",
                   signal_checks=signal_checks,
                   constant_checks=constant_checks,
                   normalization_checks=normalization_checks)


def cmd_test_status(args: argparse.Namespace) -> int:
    """Classify errors from go build/test output (reads from stdin).

    Error classes:
      - compilation: Go compiler errors (file:line:col: message)
      - test_failure: Go test failures (--- FAIL: TestName)
      - infrastructure: Module resolution, timeouts, missing packages
      - none: No errors detected

    Precedence: infrastructure > compilation > test_failure
    (infrastructure errors mask other errors since they indicate
    the build environment is broken, not the generated code).

    Exit codes: 0 = no errors found, 1 = errors classified, 2 = CLI infrastructure error
    """
    MAX_INPUT_SIZE = 10 * 1024 * 1024  # 10 MB
    try:
        input_text = sys.stdin.read(MAX_INPUT_SIZE + 1)
        if len(input_text) > MAX_INPUT_SIZE:
            return _output("error", 2,
                           output_type="test_status",
                           error_class="none",
                           error_count=0,
                           errors=[{"class": "cli_error", "message": "stdin exceeds 10 MB limit", "file": ""}])
    except UnicodeDecodeError as exc:
        return _output("error", 2,
                       output_type="test_status",
                       error_class="none",
                       error_count=0,
                       errors=[{"class": "cli_error", "message": f"stdin contains invalid UTF-8: {exc}", "file": ""}])
    except OSError as exc:
        return _output("error", 2,
                       output_type="test_status",
                       error_class="none",
                       error_count=0,
                       errors=[{"class": "cli_error", "message": f"Failed to read stdin: {exc}", "file": ""}])

    errors_found: list[dict] = []
    classes_found: set[str] = set()

    # Infrastructure patterns (checked first — highest precedence).
    # Note: patterns are ordered from most specific to most general.
    # Known gap: toolchain-internal .go file paths (e.g. internal/buildcfg/) can
    # be misclassified as compilation errors; the Stage 4 prompt handles this by
    # checking whether error file paths appear in stage3_output.json.
    infra_patterns = [
        (r'go:\s+.*(?:reading|downloading|Get\s+").*(?:410 Gone|404 Not Found|connection refused|i/o timeout)', 'module_fetch_failure'),
        (r'cannot find module providing package', 'missing_module'),
        (r'context deadline exceeded', 'timeout'),
        (r'dial tcp.*(?:connection refused|i/o timeout)', 'network_timeout'),
        (r'go: (?:finding|downloading|extracting)\s+\S+.*(?:error|failed)', 'module_error'),
        (r'no required module provides package', 'missing_module'),
        (r'go: no Go toolchain found', 'missing_toolchain'),
        (r'signal:\s+killed', 'process_killed'),
        (r'\[build failed\]', 'build_failed'),
    ]
    for pattern, sub_class in infra_patterns:
        for match in re.finditer(pattern, input_text, re.MULTILINE):
            classes_found.add("infrastructure")
            errors_found.append({
                "class": "infrastructure",
                "sub_class": sub_class,
                "message": match.group(0).strip(),
                "file": "",
            })

    # Compilation errors: file.go:line:col: message OR file.go:line: message
    for match in re.finditer(
        r'^(?:#\s+\S+\n)?(\S+\.go):(\d+):(?:(\d+):)?\s+(.+)$',
        input_text, re.MULTILINE
    ):
        classes_found.add("compilation")
        errors_found.append({
            "class": "compilation",
            "message": match.group(4).strip(),
            "file": match.group(1),
            "line": int(match.group(2)),
            "column": int(match.group(3)) if match.group(3) else None,
        })

    # Test failures: --- FAIL: TestName
    for match in re.finditer(
        r'^--- FAIL:\s+(\S+)\s+\([\d.]+s\)',
        input_text, re.MULTILINE
    ):
        classes_found.add("test_failure")
        errors_found.append({
            "class": "test_failure",
            "message": f"Test failed: {match.group(1)}",
            "file": "",
            "test_name": match.group(1),
        })

    # Also detect panics in tests
    for match in re.finditer(
        r'^panic:\s+(.+)$',
        input_text, re.MULTILINE
    ):
        classes_found.add("test_failure")
        errors_found.append({
            "class": "test_failure",
            "message": f"Panic: {match.group(1).strip()}",
            "file": "",
        })

    if not errors_found:
        return _output("ok", 0,
                       output_type="test_status",
                       error_class="none",
                       error_count=0)

    # Precedence: infrastructure > compilation > test_failure
    # Exception: [build failed] summary-only (BC-3) is infrastructure when there are no
    # individual compilation errors. When both co-occur (e.g. test-file compilation
    # failure from 'go test' Step 3), prefer compilation so the retry loop is entered
    # rather than halting — the [build failed] line is just a summary in that case.
    infra_is_build_failed_only = (
        "infrastructure" in classes_found
        and all(
            e.get("sub_class") == "build_failed"
            for e in errors_found
            if e["class"] == "infrastructure"
        )
    )
    if "infrastructure" in classes_found and not (infra_is_build_failed_only and ("compilation" in classes_found or "test_failure" in classes_found)):
        primary_class = "infrastructure"
    elif "compilation" in classes_found:
        primary_class = "compilation"
    else:
        primary_class = "test_failure"

    # Filter to only primary-class errors
    primary_errors = [e for e in errors_found if e["class"] == primary_class]

    return _output("error", 1,
                   output_type="test_status",
                   error_class=primary_class,
                   error_count=len(primary_errors),
                   errors=primary_errors)


def _kubectl_current_context() -> str:
    """Return current kubectl context, or empty string on error."""
    import subprocess
    try:
        r = subprocess.run(
            ["kubectl", "config", "current-context"],
            capture_output=True, text=True, timeout=10
        )
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception as e:
        print(f"WARNING: kubectl context check failed: {e}", file=sys.stderr)
        return ""


def _default_benchmark_state(algorithm_name: str, namespace: str, context: str) -> dict:
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    phase_template = {
        "status": "pending", "pipelinerun_name": None,
        "submitted_at": None, "completed_at": None,
        "results_local_path": None, "failure_reason": None,
    }
    return {
        "schema_version": 1,
        "algorithm_name": algorithm_name,
        "created_at": now,
        "cluster_context": context,
        "namespace": namespace,
        "phases": {
            "noise":     {**phase_template, "results_pvc_path": "noise/"},
            "baseline":  {**phase_template, "results_pvc_path": "baseline/"},
            "treatment": {**phase_template, "results_pvc_path": "treatment/"},
        }
    }


_PHASE_ORDER = ["noise", "baseline", "treatment"]


def cmd_benchmark_state(args: "argparse.Namespace") -> int:
    import json
    from datetime import datetime, timezone
    from pathlib import Path

    ws = Path(args.workspace)
    state_path = ws / "benchmark_state.json"

    # ---- read or create ----
    if not state_path.exists():
        alg_path = ws / "algorithm_summary.json"
        if not alg_path.exists():
            print(f"ERROR: {alg_path} not found — run Stage 1 extract first.",
                  file=sys.stderr)
            return 2
        try:
            raw = alg_path.read_text()
        except OSError as e:
            print(f"ERROR: cannot read {alg_path}: {e}", file=sys.stderr)
            return 2
        try:
            alg = json.loads(raw)
        except json.JSONDecodeError as e:
            print(f"ERROR: {alg_path} contains invalid JSON: {e}", file=sys.stderr)
            return 2
        if "algorithm_name" not in alg:
            print(f"ERROR: {alg_path} missing required 'algorithm_name' field.",
                  file=sys.stderr)
            return 2
        alg_name = alg["algorithm_name"]
        if not getattr(args, "namespace", None):
            print("ERROR: --namespace required on first invocation.", file=sys.stderr)
            return 2
        ctx = _kubectl_current_context()
        if not ctx:
            print(
                "WARNING: kubectl context could not be determined — "
                "cluster context guard will be disabled for this state file. "
                "Ensure kubectl is configured before continuing.",
                file=sys.stderr,
            )
        state = _default_benchmark_state(alg_name, args.namespace, ctx)
        try:
            state_path.write_text(json.dumps(state, indent=2))
        except OSError as e:
            print(f"ERROR: cannot write {state_path}: {e}", file=sys.stderr)
            return 2
    else:
        try:
            raw = state_path.read_text()
        except OSError as e:
            print(f"ERROR: cannot read {state_path}: {e}", file=sys.stderr)
            return 2
        try:
            state = json.loads(raw)
        except json.JSONDecodeError as e:
            print(f"ERROR: {state_path} contains invalid JSON: {e}", file=sys.stderr)
            return 2
        if not isinstance(state, dict) or "phases" not in state:
            print(
                f"ERROR: {state_path} is missing 'phases' key or is not a dict — "
                "file may be corrupted. Delete it to start fresh.",
                file=sys.stderr,
            )
            return 2
        for expected_phase in _PHASE_ORDER:
            if expected_phase not in state["phases"]:
                print(
                    f"ERROR: {state_path} 'phases' dict is missing phase '{expected_phase}' — "
                    "file may be corrupted. Delete it to start fresh.",
                    file=sys.stderr,
                )
                return 2

    # ---- context guard (read-only calls) ----
    if not getattr(args, "set_phase", None):
        current_ctx = _kubectl_current_context()
        recorded_ctx = state.get("cluster_context", "")
        if recorded_ctx and current_ctx and current_ctx != recorded_ctx:
            print(
                f"ERROR: State was recorded against cluster '{recorded_ctx}' "
                f"but current context is '{current_ctx}'. "
                "Delete workspace/benchmark_state.json to start fresh against "
                "the new cluster, or switch back to the original context.",
                file=sys.stderr,
            )
            return 1
        print(json.dumps(state, indent=2))
        return 0

    # ---- set-phase update ----
    phase = args.set_phase
    if phase not in _PHASE_ORDER:
        print(f"ERROR: unknown phase '{phase}'. Must be one of {_PHASE_ORDER}.",
              file=sys.stderr)
        return 2

    new_status = args.status
    if new_status is None:
        print("ERROR: --status is required when --set-phase is used.", file=sys.stderr)
        return 2
    current_status = state["phases"][phase]["status"]

    # status regression guard
    if not getattr(args, "force", False):
        if current_status == "done" and new_status in ("pending", "running"):
            print(
                f"ERROR: cannot regress phase '{phase}' from 'done' to '{new_status}'. "
                "Use --force to override.", file=sys.stderr
            )
            return 2

    # ordering guard
    if new_status == "running" and not getattr(args, "force", False):
        idx = _PHASE_ORDER.index(phase)
        if idx > 0:
            prev = _PHASE_ORDER[idx - 1]
            if state["phases"][prev]["status"] != "done":
                print(
                    f"ERROR: cannot set '{phase}' to running — "
                    f"previous phase '{prev}' is not done (status: "
                    f"'{state['phases'][prev]['status']}'). "
                    "Use --force to bypass.", file=sys.stderr
                )
                return 1

    state["phases"][phase]["status"] = new_status
    if getattr(args, "pipelinerun", None):
        state["phases"][phase]["pipelinerun_name"] = args.pipelinerun
        state["phases"][phase]["submitted_at"] = (
            datetime.now(timezone.utc).isoformat(timespec="seconds")
        )
    if getattr(args, "results", None):
        state["phases"][phase]["results_local_path"] = args.results
        state["phases"][phase]["completed_at"] = (
            datetime.now(timezone.utc).isoformat(timespec="seconds")
        )
    if getattr(args, "failure_reason", None):
        state["phases"][phase]["failure_reason"] = args.failure_reason

    try:
        state_path.write_text(json.dumps(state, indent=2))
    except OSError as e:
        print(f"ERROR: cannot write {state_path}: {e}", file=sys.stderr)
        return 2
    return 0


def _percentile(values: list, p: int) -> float:
    """Compute p-th percentile of a sorted list (nearest-rank, ceiling method)."""
    import math
    if not values:
        return 0.0
    values = sorted(values)
    idx = min(len(values) - 1, max(0, math.ceil(len(values) * p / 100) - 1))
    return values[idx]


def _parse_tracev2_dir(directory: "Path") -> dict:
    """Parse a single TraceV2 directory → metrics dict. Raises on error."""
    import csv as csv_mod
    header_path = directory / "trace_header.yaml"
    data_path = directory / "trace_data.csv"
    if not header_path.exists():
        raise FileNotFoundError(
            f"missing trace_header.yaml in {directory} — blis observe may have crashed mid-write."
        )
    if not data_path.exists():
        raise FileNotFoundError(
            f"missing trace_data.csv in {directory} — blis observe may have crashed mid-write."
        )
    ttft_vals, tpot_vals = [], []
    try:
        with open(data_path, newline="") as f:
            reader = csv_mod.DictReader(f)
            for row in reader:
                if row.get("status") != "ok":
                    continue
                send = int(row["send_time_us"])
                first = int(row["first_chunk_time_us"])
                last = int(row["last_chunk_time_us"])
                chunks = max(int(row["num_chunks"]) - 1, 1)
                ttft_vals.append((first - send) / 1000.0)
                tpot_vals.append((last - first) / chunks / 1000.0)
    except (ValueError, KeyError) as e:
        raise ValueError(
            f"malformed CSV in {data_path}: {e} — check that all numeric columns "
            "(send_time_us, first_chunk_time_us, last_chunk_time_us, num_chunks) "
            "contain valid integers for rows with status='ok'"
        ) from e
    return {
        "ttft_p50": _percentile(ttft_vals, 50),
        "ttft_p99": _percentile(ttft_vals, 99),
        "tpot_p50": _percentile(tpot_vals, 50),
        "tpot_p99": _percentile(tpot_vals, 99),
        "_valid_rows": len(ttft_vals),
    }


def cmd_convert_trace(args: "argparse.Namespace") -> int:
    import json
    from pathlib import Path

    input_dir = Path(args.input_dir)
    output = Path(args.output)

    if not input_dir.is_dir():
        print(f"ERROR: input directory '{input_dir}' does not exist.", file=sys.stderr)
        return 2

    workloads = []
    for wl_dir in sorted(input_dir.iterdir()):
        if not wl_dir.is_dir():
            continue
        # Normalize to match _classify_workloads: strip "workload_" prefix, underscores→hyphens
        wl_name = wl_dir.name.removeprefix("workload_").replace("_", "-")
        # auto-detect noise (has run-* subdirs) vs baseline/treatment
        run_dirs = sorted(wl_dir.glob("run-*"))
        if run_dirs:
            # noise per-run structure
            runs = []
            for run_dir in run_dirs:
                try:
                    metrics = _parse_tracev2_dir(run_dir)
                except (FileNotFoundError, ValueError) as e:
                    print(f"ERROR: {e}", file=sys.stderr)
                    return 1
                if metrics["_valid_rows"] == 0:
                    print(
                        f"ERROR: workload '{wl_name}' run '{run_dir.name}' has 0 rows "
                        f"with status 'ok' in {run_dir}/trace_data.csv — "
                        "all requests failed or timed out.",
                        file=sys.stderr,
                    )
                    return 1
                del metrics["_valid_rows"]
                runs.append({"metrics": metrics})
            workloads.append({"name": wl_name, "runs": runs})
        else:
            # baseline/treatment single-value structure
            try:
                metrics = _parse_tracev2_dir(wl_dir)
            except (FileNotFoundError, ValueError) as e:
                print(f"ERROR: {e}", file=sys.stderr)
                return 1
            if metrics["_valid_rows"] == 0:
                print(
                    f"ERROR: workload '{wl_name}' has 0 rows with status 'ok' "
                    f"in {wl_dir}/trace_data.csv — all requests failed or timed out.",
                    file=sys.stderr,
                )
                return 1
            del metrics["_valid_rows"]
            workloads.append({"name": wl_name, "metrics": metrics})

    if not workloads:
        print(f"ERROR: no workload directories found in '{input_dir}'.", file=sys.stderr)
        return 2

    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        output.write_text(json.dumps({"workloads": workloads}, indent=2))
    except OSError as e:
        print(f"ERROR: cannot write output file '{output}': {e}", file=sys.stderr)
        return 2
    return 0


def cmd_render_pipelinerun(args: "argparse.Namespace") -> int:
    template = Path(args.template)
    out = Path(args.out)

    if not template.exists():
        print(f"ERROR: template file '{template}' not found.", file=sys.stderr)
        return 2

    # Parse KEY=VAL pairs
    var_map = {}
    for item in (args.vars or []):
        if "=" not in item:
            print(f"ERROR: --vars entry '{item}' is not KEY=VAL format.", file=sys.stderr)
            return 2
        k, v = item.split("=", 1)
        var_map[k.strip()] = v.strip()

    content = template.read_text()

    # Substitute ${VAR} and $VAR patterns
    def replacer(m):
        name = m.group(1) or m.group(2)
        return var_map[name] if name in var_map else m.group(0)

    rendered = re.sub(r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)',
                      replacer, content)

    # Check for unresolved placeholders
    remaining = re.findall(r'\$\{?[A-Za-z_][A-Za-z0-9_]*\}?', rendered)
    if remaining:
        print(
            f"ERROR: unresolved placeholders in rendered output: {remaining}. "
            "Provide all required --vars.", file=sys.stderr
        )
        return 1

    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        out.write_text(rendered)
    except OSError as e:
        print(f"ERROR: cannot write output file '{out}': {e}", file=sys.stderr)
        return 2
    return 0


def cmd_compile_pipeline(args: "argparse.Namespace") -> int:
    import subprocess
    import tempfile
    import yaml

    template_dir = Path(args.template_dir)
    values_file = Path(args.values)
    phase = args.phase
    out_dir = Path(args.out)

    if not template_dir.is_dir():
        print(f"ERROR: template directory '{template_dir}' not found.", file=sys.stderr)
        return 2
    if not values_file.exists():
        print(f"ERROR: values file '{values_file}' not found.", file=sys.stderr)
        return 2

    # Prefer unified pipeline.yaml.j2; fall back to per-phase {phase}-pipeline.yaml.j2
    unified_template = template_dir / "pipeline.yaml.j2"
    phase_template = template_dir / f"{phase}-pipeline.yaml.j2"
    if unified_template.exists():
        template_file = unified_template
    elif phase_template.exists():
        template_file = phase_template
    else:
        print(f"ERROR: pipeline template '{unified_template}' not found.", file=sys.stderr)
        return 2

    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{phase}-pipeline.yaml"

    tektonc = Path(__file__).resolve().parent.parent / "tektonc-data-collection" / "tektonc" / "tektonc.py"
    if not tektonc.exists():
        print(f"ERROR: tektonc not found at '{tektonc.resolve()}'.", file=sys.stderr)
        return 2

    # Load values and inject phase-specific synthetic keys
    try:
        values = yaml.safe_load(values_file.read_text()) or {}
    except Exception as e:
        print(f"ERROR: failed to load values file: {e}", file=sys.stderr)
        return 2

    values["phase"] = phase
    gaie_key = "treatment" if phase == "treatment" else "baseline"
    values["gaie_config"] = (
        values.get("stack", {}).get("gaie", {}).get(gaie_key, {}).get("helmValues", {})
    )

    # Write augmented values to a temp file for tektonc
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False)
    try:
        yaml.dump(values, tmp, default_flow_style=False, allow_unicode=True)
        tmp.flush()
        tmp.close()

        try:
            r = subprocess.run(
                [sys.executable, str(tektonc),
                 "-t", str(template_file),
                 "-f", tmp.name,
                 "-o", str(out_file)],
                capture_output=True, text=True, shell=False, timeout=120
            )
        except subprocess.TimeoutExpired:
            print("ERROR: tektonc timed out after 120s.", file=sys.stderr)
            return 2
        except OSError as e:
            print(f"ERROR: failed to launch tektonc: {e}", file=sys.stderr)
            return 2
        if r.returncode != 0:
            print(f"ERROR: tektonc compilation failed:\n{r.stderr}", file=sys.stderr)
            return 1
    finally:
        Path(tmp.name).unlink(missing_ok=True)

    return 0


def _preflight_check_values(values_path: "Path", namespace: str, phase: str) -> list:
    """Check values.yaml for issues that don't need kubectl. Returns list of error strings."""
    import yaml
    errors = []
    try:
        raw = values_path.read_text()
    except OSError as e:
        return [f"Cannot read values.yaml: {e}"]
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        return [f"Cannot parse values.yaml: {e}"]

    if not isinstance(data, dict):
        return [f"values.yaml is empty or not a mapping (got {type(data).__name__})"]

    image = (data.get("observe") or {}).get("image", "")
    if "<TAG>" in image:
        errors.append(
            f"FAIL: observe.image contains unresolved <TAG> placeholder '{image}' — "
            "re-run Stage 3 generate to resolve."
        )

    if phase == "treatment":
        # Check treatment scorer config in current values.yaml format:
        # stack.gaie.treatment.helmValues.inferenceExtension.pluginsCustomConfig
        gaie_treatment = ((data.get("stack") or {}).get("gaie") or {}).get("treatment", {})
        plugins_custom_config = (
            (gaie_treatment.get("helmValues") or {})
            .get("inferenceExtension", {})
            .get("pluginsCustomConfig", {})
        )
        if not plugins_custom_config:
            errors.append(
                "FAIL: scorer.treatment.configContent is empty — "
                "treatment scorer config must be generated by Stage 3."
            )

    return errors


def cmd_preflight(args: "argparse.Namespace") -> int:
    import subprocess
    from pathlib import Path
    import yaml

    phase = args.phase
    values_path = Path(args.values)
    namespace = args.namespace

    checks = []  # list of (label, passed, detail)

    def run(label: str, cmd: list) -> bool:
        err_reason = ""
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30,
                                shell=False)
            ok = r.returncode == 0
        except Exception as e:
            ok = False
            err_reason = f"check failed: {e}"
        checks.append((label, ok, err_reason))
        return ok

    # --- values-only checks (no kubectl) ---
    val_errors = _preflight_check_values(values_path, namespace, phase)
    for e in val_errors:
        checks.append((e, False, ""))

    # --- kubectl checks ---
    run("kubectl reachable", ["kubectl", "cluster-info"])
    run("Tekton CRD installed",
        ["kubectl", "get", "crd", "pipelines.tekton.dev"])
    run(f"Namespace '{namespace}' exists",
        ["kubectl", "get", "ns", namespace])
    run("hf-secret present",
        ["kubectl", "get", "secret", "hf-secret", "-n", namespace])
    run("model-pvc present",
        ["kubectl", "get", "pvc", "model-pvc", "-n", namespace])
    run("data-pvc present",
        ["kubectl", "get", "pvc", "data-pvc", "-n", namespace])
    run("tkn CLI present", ["tkn", "version"])

    # GPU nodes check
    try:
        data = yaml.safe_load(values_path.read_text())
        acc = (data.get("stack", {}).get("model", {})
               .get("helmValues", {}).get("decode", {})
               .get("acceleratorTypes", {}))
        label_key = acc.get("labelKey", "")
        label_vals = acc.get("labelValues", [])
        replicas = (data.get("stack", {}).get("model", {})
                    .get("helmValues", {}).get("decode", {})
                    .get("replicas", 1))
        if label_key and label_vals:
            selector = f"{label_key}={label_vals[0]}"
            r = subprocess.run(
                ["kubectl", "get", "nodes", "-l", selector, "-o", "name"],
                capture_output=True, text=True, timeout=30, shell=False
            )
            count = len([l for l in r.stdout.strip().splitlines() if l]) if r.returncode == 0 else 0
            ok = r.returncode == 0 and count >= replicas
            checks.append((
                f"GPU nodes (≥{replicas} with {selector})", ok,
                f"found {count}"
            ))
    except Exception as e:
        checks.append(("GPU nodes check", False, f"error: {e}"))

    if phase == "treatment":
        scheduler_dir = REPO_ROOT / "llm-d-inference-scheduler"
        if scheduler_dir.is_dir():
            try:
                import tempfile
                _gocache = __import__("os").environ.get("GOCACHE") or __import__("os").path.join(tempfile.gettempdir(), "go-cache")
                env = {**__import__("os").environ, "GOWORK": "off", "GOCACHE": _gocache}
                r = subprocess.run(
                    ["go", "build", "./pkg/plugins/scorer/..."],
                    capture_output=True, text=True, cwd=str(scheduler_dir),
                    shell=False, timeout=120, env=env
                )
                ok = r.returncode == 0
                detail = r.stderr.strip() if not ok else ""
            except subprocess.TimeoutExpired:
                ok = False
                detail = "go build timed out after 120s"
            except OSError as e:
                ok = False
                detail = f"failed to launch go: {e}"
        else:
            ok = False
            detail = f"scheduler submodule not found at {scheduler_dir}"
        checks.append(("Stage 4 scorer builds", ok, detail))

    # Print checklist to stderr; output JSON to stdout per module contract
    any_fail = False
    check_results = []
    for label, passed, detail in checks:
        mark = "✓" if passed else "✗"
        suffix = f" ({detail})" if detail else ""
        print(f"  [{mark}] {label}{suffix}", file=sys.stderr)
        if not passed:
            any_fail = True
        check_results.append({"label": label, "passed": passed, "detail": detail})

    rc = 1 if any_fail else 0
    _output("ok" if rc == 0 else "fail", rc,
            **{"phase": phase, "checks": check_results, "passed": not any_fail})
    return rc


def _compute_cv(values: list) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    if mean == 0:
        return 0.0
    variance = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    return (variance ** 0.5) / mean


def _classify_workloads(workloads_dir: "Path", signal_coverage_path: "Path",
                         mapping_path: "Path | None" = None) -> dict:
    """Return {workload_name: {"classification": "matched"|"unmatched", "matched_signals": [...]}}"""
    import json, yaml
    if mapping_path is None:
        mapping_path = Path(__file__).parent / "workload_signal_mapping.json"
    if not mapping_path.exists():
        raise FileNotFoundError(
            f"workload signal mapping not found at '{mapping_path}' — "
            "ensure Task 1 (Step 1.8) was completed to create this file"
        )
    try:
        parsed = json.loads(mapping_path.read_text())
    except json.JSONDecodeError as e:
        raise ValueError(f"workload signal mapping '{mapping_path}' is malformed JSON: {e}") from e
    try:
        mapping = parsed["mappings"]
    except KeyError:
        raise ValueError(
            f"workload signal mapping '{mapping_path}' is missing required top-level "
            f"'mappings' key"
        )
    try:
        sc = json.loads(signal_coverage_path.read_text())
    except json.JSONDecodeError as e:
        raise ValueError(f"signal coverage file '{signal_coverage_path}' is malformed JSON: {e}") from e
    mapped_signals = {s["sim_name"] for s in sc.get("signals", []) if s.get("mapped")}

    result = {}
    for wf in sorted(workloads_dir.iterdir()):
        if wf.suffix not in (".yaml", ".yml"):
            continue
        # Normalize underscores to hyphens: workload_glia_40qps.yaml → glia-40qps
        wl_name = wf.stem.removeprefix("workload_").replace("_", "-")
        try:
            raw = wf.read_text()
        except OSError as e:
            raise ValueError(f"cannot read workload file {wf}: {e}") from e
        try:
            wl_data = yaml.safe_load(raw) or {}
        except yaml.YAMLError as e:
            raise ValueError(f"cannot parse workload file {wf}: {e}") from e
        # Collect all keys at top level AND within clients[] items
        all_keys = set(wl_data.keys())
        for client in wl_data.get("clients", []):
            if isinstance(client, dict):
                all_keys.update(client.keys())
        matched = []
        for entry in mapping:
            if entry["workload_field"] in all_keys:
                for sig in entry["signals"]:
                    if sig in mapped_signals:
                        matched.append(sig)
        result[wl_name] = {
            "classification": "matched" if matched else "unmatched",
            "matched_signals": list(set(matched)),
        }
    return result


def cmd_benchmark_new(args: "argparse.Namespace") -> int:
    import json
    from pathlib import Path

    noise_path = Path(args.noise)
    baseline_path = Path(args.baseline)
    treatment_path = Path(args.treatment)
    sc_path = Path(args.signal_coverage)
    wd_path = Path(args.workloads_dir)

    mapping_path = Path(__file__).parent / "workload_signal_mapping.json"
    for p in [noise_path, baseline_path, treatment_path, sc_path, wd_path, mapping_path]:
        if not p.exists():
            print(f"ERROR: required input '{p}' not found.", file=sys.stderr)
            return 2

    try:
        noise = json.loads(noise_path.read_text())
        baseline = json.loads(baseline_path.read_text())
        treatment = json.loads(treatment_path.read_text())
    except Exception as e:
        print(f"ERROR: cannot parse input JSON: {e}", file=sys.stderr)
        return 2

    for label, data in [("noise", noise), ("baseline", baseline), ("treatment", treatment)]:
        if not isinstance(data, dict) or "workloads" not in data:
            print(
                f"ERROR: {label} results file is missing 'workloads' key or is not a dict — "
                "run convert-trace to regenerate.",
                file=sys.stderr,
            )
            return 2

    # Compute T_eff from noise — per-workload CV (not pooled across workloads)
    metrics_keys = ["ttft_p50", "ttft_p99", "tpot_p50", "tpot_p99"]
    noise_cv = 0.0
    try:
        for wl in noise["workloads"]:
            wl_name = wl.get("name", "?")
            per_metric = {k: [] for k in metrics_keys}
            for run in wl["runs"]:
                for k in metrics_keys:
                    per_metric[k].append(run["metrics"][k])
            for k in metrics_keys:
                if len(per_metric[k]) < 2:
                    print(
                        f"ERROR: noise workload '{wl_name}' has only "
                        f"{len(per_metric[k])} run(s) for metric '{k}' — "
                        "at least 2 runs are required to compute a noise estimate.",
                        file=sys.stderr,
                    )
                    return 2
            wl_cv = max(_compute_cv(per_metric[k]) for k in metrics_keys)
            noise_cv = max(noise_cv, wl_cv)
    except (KeyError, TypeError) as e:
        print(
            f"ERROR: malformed noise results — missing expected field: {e}. "
            "Each workload must have 'runs' with 'metrics' containing "
            "ttft_p50, ttft_p99, tpot_p50, tpot_p99.",
            file=sys.stderr,
        )
        return 2
    t_eff = max(0.05, 2.0 * noise_cv)

    # Build lookup maps
    try:
        bl_map = {w["name"]: w["metrics"]["ttft_p99"] for w in baseline["workloads"]}
        tr_map = {w["name"]: w["metrics"]["ttft_p99"] for w in treatment["workloads"]}
    except (KeyError, TypeError) as e:
        print(
            f"ERROR: malformed baseline or treatment results — missing expected field: {e}. "
            "Each workload entry must have 'name' and 'metrics.ttft_p99'.",
            file=sys.stderr,
        )
        return 2

    # Classify workloads
    try:
        classification = _classify_workloads(wd_path, sc_path, mapping_path)
    except (OSError, ValueError) as e:
        print(f"ERROR: workload classification failed: {e}", file=sys.stderr)
        return 2

    workload_classification = []
    matched_improvements = []
    unmatched_above_teff = []
    skipped_workloads = []

    for wl_name, cls_info in sorted(classification.items()):
        bl_p99 = bl_map.get(wl_name)
        tr_p99 = tr_map.get(wl_name)
        if bl_p99 is None or tr_p99 is None:
            print(
                f"WARNING: workload '{wl_name}' from workloads_dir not found in "
                f"{'baseline' if bl_p99 is None else 'treatment'} results "
                f"(available: {sorted(bl_map.keys())}). Skipping.",
                file=sys.stderr,
            )
            skipped_workloads.append(wl_name)
            continue
        improvement = (bl_p99 - tr_p99) / bl_p99 if bl_p99 else 0.0
        entry = {
            "workload": wl_name,
            "classification": cls_info["classification"],
            "improvement": round(improvement, 4),
            "matched_signals": cls_info["matched_signals"],
        }
        workload_classification.append(entry)
        if cls_info["classification"] == "matched":
            matched_improvements.append(improvement)
        elif improvement >= t_eff:
            unmatched_above_teff.append(
                f"workload {wl_name}: improvement={improvement:.2%} >= T_eff={t_eff:.2%}"
            )

    if skipped_workloads and not workload_classification:
        msg = (
            f"ERROR: all {len(skipped_workloads)} workload(s) were skipped due to name "
            f"mismatch between workloads_dir and result files. "
            f"Skipped: {skipped_workloads}. "
            f"Baseline result names: {sorted(bl_map.keys())}."
        )
        print(msg, file=sys.stderr)
        error_output = {
            "mechanism_check_verdict": "ERROR",
            "passed": False,
            "t_eff": round(t_eff, 4),
            "noise_cv": round(noise_cv, 4),
            "workload_classification": [],
            "specificity_notes": [
                f"all {len(skipped_workloads)} workload(s) skipped due to name mismatch "
                f"between workloads_dir and result files; "
                f"skipped={skipped_workloads}; "
                f"baseline names={sorted(bl_map.keys())}"
            ],
        }
        if getattr(args, "out", None):
            out_path = Path(args.out)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(error_output, indent=2))
        else:
            print(json.dumps(error_output, indent=2))
        return 2

    # Mechanism check
    if not matched_improvements:
        verdict = "ERROR"
        passed = False
    elif max(matched_improvements) >= t_eff:
        verdict = "PASS"
        passed = True
    elif max(matched_improvements) > 0:
        verdict = "INCONCLUSIVE"
        passed = False
    else:
        verdict = "FAIL"
        passed = False

    output = {
        "t_eff": round(t_eff, 4),
        "noise_cv": round(noise_cv, 4),
        "mechanism_check_verdict": verdict,
        "passed": passed,
        "workload_classification": workload_classification,
        "specificity_notes": unmatched_above_teff,
    }

    if getattr(args, "out", None):
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(output, indent=2))
    else:
        print(json.dumps(output, indent=2))

    # INCONCLUSIVE exits 0 — pipeline should proceed with operator review (see validate.md)
    return 2 if verdict == "ERROR" else (1 if verdict == "FAIL" else 0)


def _deep_merge(base: dict, overlay: dict) -> dict:
    """Deep-merge overlay onto base. Dict keys merged recursively; non-dict values replaced.

    Lists are replaced entirely (not appended). Returns a new dict (deep copy).
    """
    import copy
    result = copy.deepcopy(base)
    for key, oval in overlay.items():
        if key in result and isinstance(result[key], dict) and isinstance(oval, dict):
            result[key] = _deep_merge(result[key], oval)
        else:
            result[key] = copy.deepcopy(oval)
    return result


def _flatten_gaie_shared(merged: dict) -> dict:
    """Flatten gaie.shared.helmValues into each phase's helmValues, inject EPP images,
    and remove the gaie.shared and gaie.epp_image keys from output.

    For each phase in ['baseline', 'treatment']:
      1. Deep-merge gaie.shared.helmValues (base) with gaie.<phase>.helmValues (overlay)
      2. Inject inferenceExtension.image from gaie.epp_image:
           - baseline/noise: use epp_image.upstream
           - treatment: use epp_image.build tag if set; fall back to upstream otherwise
             (treatment tag is set by build-push-epp; upstream fallback allows pipeline
             testing without a custom build)
    Then delete gaie.shared and gaie.epp_image from the result.

    Returns the modified merged dict (modified in place and also returned).
    """
    gaie = merged.get("stack", {}).get("gaie", {})
    shared = gaie.get("shared", {})
    shared_helm = shared.get("helmValues", {})
    epp_image = gaie.get("epp_image", {})
    upstream_img = epp_image.get("upstream", {})
    build_img = epp_image.get("build", {})

    for phase in ["baseline", "treatment"]:
        if phase not in gaie:
            continue
        phase_helm = gaie[phase].get("helmValues", {})
        gaie[phase]["helmValues"] = _deep_merge(shared_helm, phase_helm)

        # Inject EPP image reference into inferenceExtension.image if epp_image is configured
        if upstream_img:
            if phase == "treatment" and build_img.get("hub") and build_img.get("tag"):
                img = {"hub": build_img["hub"], "name": build_img["name"],
                       "tag": build_img["tag"]}
            else:
                img = {"hub": upstream_img["hub"], "name": upstream_img["name"],
                       "tag": upstream_img["tag"]}
            ie = gaie[phase]["helmValues"].setdefault("inferenceExtension", {})
            # Only set if not already explicitly provided in algorithm values
            ie.setdefault("image", img)

    gaie.pop("shared", None)
    gaie.pop("epp_image", None)

    return merged


def _apply_vllm_image_override(merged: dict) -> dict:
    """Apply stack.model.vllm_image override to decode.containers[0].image.

    If stack.model.vllm_image is set, replaces decode.containers[0].image with that
    value and removes the vllm_image key from the output. If not set, no-op.
    """
    model = merged.get("stack", {}).get("model", {})
    override = model.get("vllm_image")
    if not override:
        return merged

    containers = (
        model.get("helmValues", {})
        .get("decode", {})
        .get("containers")
    )
    if containers and isinstance(containers, list) and len(containers) > 0:
        containers[0]["image"] = override

    model.pop("vllm_image", None)
    return merged


def _apply_request_multiplier(merged: dict) -> dict:
    """Scale num_requests in each observe.workloads[].spec by observe.request_multiplier.

    If observe.request_multiplier is present and > 1, parses each workload's spec
    string as YAML, multiplies num_requests (if present), and re-serializes.
    The request_multiplier key is stripped from the output regardless.

    Specs that fail YAML parsing are left unchanged (warning emitted to stderr).
    Specs without a num_requests field are left unchanged.
    """
    import yaml

    observe = merged.get("observe", {})
    multiplier = observe.pop("request_multiplier", None)

    if multiplier is None:
        return merged

    if not isinstance(multiplier, (int, float)):
        print(
            f"ERROR: observe.request_multiplier must be a number, "
            f"got {type(multiplier).__name__!r} (value: {multiplier!r}).",
            file=sys.stderr,
        )
        return merged

    if multiplier <= 1:
        return merged

    workloads = observe.get("workloads", [])
    for wl in workloads:
        spec_str = wl.get("spec")
        if not spec_str or not isinstance(spec_str, str):
            continue
        try:
            spec_data = yaml.safe_load(spec_str)
        except yaml.YAMLError:
            print(
                f"WARNING: could not parse spec YAML for workload "
                f"'{wl.get('name', '?')}', skipping request_multiplier scaling.",
                file=sys.stderr,
            )
            continue
        if not isinstance(spec_data, dict):
            print(
                f"WARNING: workload '{wl.get('name', '?')}' spec parsed to "
                f"{type(spec_data).__name__!r} (expected dict), "
                f"skipping request_multiplier scaling.",
                file=sys.stderr,
            )
            continue
        if "num_requests" in spec_data:
            raw = spec_data["num_requests"]
            if not isinstance(raw, (int, float)):
                print(
                    f"WARNING: workload '{wl.get('name', '?')}' num_requests is not a number "
                    f"(got {type(raw).__name__!r}: {raw!r}), "
                    f"skipping request_multiplier scaling.",
                    file=sys.stderr,
                )
                continue
            spec_data["num_requests"] = int(round(raw * multiplier))
            try:
                wl["spec"] = yaml.dump(spec_data, default_flow_style=False, sort_keys=False)
            except yaml.YAMLError as e:
                print(
                    f"WARNING: could not re-serialize spec YAML for workload "
                    f"'{wl.get('name', '?')}' after scaling, skipping: {e}",
                    file=sys.stderr,
                )

    return merged


def cmd_merge_values(args: "argparse.Namespace") -> int:
    """Merge env_defaults.yaml and algorithm_values.yaml into values.yaml.

    Exit 0 = success, 1 = validation failure, 2 = infrastructure error.
    """
    import yaml

    env_path = Path(args.env)
    alg_path = Path(args.algorithm)
    out_path = Path(args.out)

    # Load both YAML files (exit 2 if missing or parse failure)
    for p, label in [(env_path, "--env"), (alg_path, "--algorithm")]:
        if not p.exists():
            print(f"ERROR: {label} file '{p}' not found.", file=sys.stderr)
            return 2

    try:
        env_data = yaml.safe_load(env_path.read_text()) or {}
    except yaml.YAMLError as e:
        print(f"ERROR: failed to parse --env file '{env_path}': {e}", file=sys.stderr)
        return 2

    try:
        alg_data = yaml.safe_load(alg_path.read_text()) or {}
    except yaml.YAMLError as e:
        print(f"ERROR: failed to parse --algorithm file '{alg_path}': {e}", file=sys.stderr)
        return 2

    # Deep-merge: algorithm_values overlays env_defaults
    merged = _deep_merge(env_data, alg_data)

    # Strip pipeline config from merged output — not consumed by Tekton templates.
    # Stripping after merge (not just env_data) ensures the key is absent regardless
    # of which input file contains it. Only env_data carries this key in practice;
    # algorithm_values pipeline keys (unusual) also pass through _deep_merge and are
    # removed here.
    merged.pop("pipeline", None)

    # Flatten gaie.shared into each phase, then remove gaie.shared
    merged = _flatten_gaie_shared(merged)

    # Apply vllm_image override if set in env_defaults
    merged = _apply_vllm_image_override(merged)

    # Apply request_multiplier scaling to workload specs
    merged = _apply_request_multiplier(merged)

    # Validate required keys in merged output
    def _get_nested(d: dict, *keys):
        for k in keys:
            if not isinstance(d, dict):
                return None
            d = d.get(k)
        return d

    required_checks = [
        (["stack", "gateway", "helmValues", "gateway", "provider"],
         "stack.gateway.helmValues.gateway.provider"),
        (["stack", "model", "modelName"],
         "stack.model.modelName"),
        (["observe", "workloads"],
         "observe.workloads"),
        (["stack", "gaie", "treatment", "helmValues", "inferenceExtension", "pluginsCustomConfig"],
         "stack.gaie.treatment.helmValues.inferenceExtension.pluginsCustomConfig"),
    ]

    missing = []
    for key_path, label in required_checks:
        val = _get_nested(merged, *key_path)
        if val is None:
            missing.append(label)
        elif label == "observe.workloads" and (not isinstance(val, list) or len(val) == 0):
            missing.append(f"{label} (must be a non-empty list)")

    if missing:
        print("ERROR: merged output is missing required keys:", file=sys.stderr)
        for m in missing:
            print(f"  - {m}", file=sys.stderr)
        return 1

    # Write output
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        out_path.write_text(yaml.dump(merged, default_flow_style=False, sort_keys=False))
    except OSError as e:
        print(f"ERROR: cannot write output file '{out_path}': {e}", file=sys.stderr)
        return 2

    _output("ok", 0, out=str(out_path))
    return 0


def cmd_build_push_epp(args: "argparse.Namespace") -> int:
    """Build and push the llm-d-inference-scheduler EPP image for the treatment phase.

    Reads epp_image.build config from env_defaults.yaml, builds the image via
    `make image-build-epp`, pushes it (unless --dry-run), then injects the image
    reference into algorithm_values.yaml and re-runs merge-values + compile-pipeline.

    Exit 0 = success, 1 = build/push failure, 2 = infrastructure error.
    """
    import shutil
    import subprocess
    import yaml

    scheduler_dir = Path(args.scheduler_dir)
    env_path = Path(args.env)
    algo_path = Path(args.values)
    merged_path = Path(args.merged_values)
    dry_run: bool = getattr(args, "dry_run", False)

    # --- Infrastructure checks ---
    if not scheduler_dir.exists():
        print(f"ERROR: --scheduler-dir '{scheduler_dir}' not found.", file=sys.stderr)
        return 2
    if not env_path.exists():
        print(f"ERROR: --env '{env_path}' not found.", file=sys.stderr)
        return 2
    if not algo_path.exists():
        print(f"ERROR: --values '{algo_path}' not found.", file=sys.stderr)
        return 2

    runtime = shutil.which("podman") or shutil.which("docker")
    if not runtime:
        print("ERROR: neither 'podman' nor 'docker' found on PATH.", file=sys.stderr)
        return 2

    # --- Read build config ---
    try:
        env_data = yaml.safe_load(env_path.read_text()) or {}
    except yaml.YAMLError as e:
        print(f"ERROR: failed to parse '{env_path}': {e}", file=sys.stderr)
        return 2

    build_cfg = (env_data.get("stack", {}).get("gaie", {})
                 .get("epp_image", {}).get("build", {}))
    hub = build_cfg.get("hub", "")
    name = build_cfg.get("name", "llm-d-inference-scheduler")
    platform = build_cfg.get("platform", "linux/amd64")
    # Makefile uses TARGETARCH (e.g. "amd64"), not PLATFORMS ("linux/amd64")
    targetarch = platform.split("/")[-1]  # "linux/amd64" → "amd64"

    if not hub or "REPLACE_ME" in hub:
        print("ERROR: epp_image.build.hub is not configured in env_defaults.yaml. "
              "Set it to your container registry (e.g. ghcr.io/yourorg).", file=sys.stderr)
        return 2

    # --- Generate tag from scheduler HEAD commit ---
    r = subprocess.run(
        ["git", "rev-parse", "--short=8", "HEAD"],
        capture_output=True, text=True, cwd=str(scheduler_dir)
    )
    if r.returncode != 0:
        print(f"ERROR: could not get git SHA from '{scheduler_dir}': {r.stderr.strip()}",
              file=sys.stderr)
        return 2
    tag = f"sim2real-{r.stdout.strip()}"
    image_ref = f"{hub}/{name}:{tag}"
    print(f"Building EPP image: {image_ref} (platform=linux/{targetarch})")

    # --- Build ---
    make_env = {
        "IMAGE_REGISTRY": hub,
        "EPP_TAG": tag,
        "CONTAINER_RUNTIME": Path(runtime).name,
        "TARGETARCH": targetarch,
    }
    make_vars = [f"{k}={v}" for k, v in make_env.items()]

    r = subprocess.run(
        ["make", "image-build-epp"] + make_vars,
        cwd=str(scheduler_dir), text=True
    )
    if r.returncode != 0:
        print(f"ERROR: image build failed (exit {r.returncode}).", file=sys.stderr)
        return 1

    if dry_run:
        print(f"--dry-run: skipping push and config update. Image built: {image_ref}")
        return 0

    # --- Push ---
    print(f"Pushing {image_ref} ...")
    r = subprocess.run(
        ["make", "image-push-epp"] + make_vars,
        cwd=str(scheduler_dir), text=True
    )
    if r.returncode != 0:
        print(f"ERROR: image push failed (exit {r.returncode}).", file=sys.stderr)
        return 1

    # --- Inject image ref into algorithm_values.yaml ---
    try:
        algo_data = yaml.safe_load(algo_path.read_text()) or {}
    except yaml.YAMLError as e:
        print(f"ERROR: failed to parse '{algo_path}': {e}", file=sys.stderr)
        return 2

    (algo_data
     .setdefault("stack", {})
     .setdefault("gaie", {})
     .setdefault("treatment", {})
     .setdefault("helmValues", {})
     .setdefault("inferenceExtension", {})
     ["image"]) = {"hub": hub, "name": name, "tag": tag}

    algo_path.write_text(yaml.dump(algo_data, default_flow_style=False, sort_keys=False))
    print(f"Updated {algo_path}: treatment EPP image = {image_ref}")

    # --- Re-run merge-values ---
    import argparse as _ap
    mv_args = _ap.Namespace(env=str(env_path), algorithm=str(algo_path), out=str(merged_path))
    rc = cmd_merge_values(mv_args)
    if rc != 0:
        print("ERROR: merge-values failed after image injection.", file=sys.stderr)
        return rc

    # --- Re-compile all three phases ---
    template_dir = scheduler_dir.parent / "tektonc-data-collection" / "tektoncsample" / "sim2real"
    compiled_dir = merged_path.parent / "compiled"
    if template_dir.exists():
        for phase in ["noise", "baseline", "treatment"]:
            cp_args = _ap.Namespace(
                template_dir=str(template_dir),
                values=str(merged_path),
                phase=phase,
                out=str(compiled_dir),
            )
            rc = cmd_compile_pipeline(cp_args)
            if rc != 0:
                print(f"ERROR: compile-pipeline failed for phase '{phase}'.", file=sys.stderr)
                return rc
        print(f"Compiled pipelines updated in {compiled_dir}/")
    else:
        print(f"NOTE: template dir '{template_dir}' not found; skipping pipeline recompile.",
              file=sys.stderr)

    print(f"Stage 4.5 complete. EPP image: {image_ref}")
    return 0


def cmd_generate_evidence(args: "argparse.Namespace") -> int:
    import json
    from datetime import date
    from pathlib import Path

    ws = Path(args.workspace)
    out_path = Path(args.out)
    cal_log = Path(getattr(args, "calibration_log", "docs/transfer/calibration_log.md"))

    alg_path = ws / "algorithm_summary.json"
    val_path = ws / "validation_results.json"

    for p, label in [(alg_path, "algorithm_summary.json"),
                     (val_path, "validation_results.json")]:
        if not p.exists():
            print(f"ERROR: generate-evidence requires '{p}' — "
                  f"{label} not found.", file=sys.stderr)
            return 1

    try:
        alg = json.loads(alg_path.read_text())
        val = json.loads(val_path.read_text())
    except json.JSONDecodeError as e:
        print(f"ERROR: cannot parse JSON from workspace artifact: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"ERROR: cannot read workspace artifact: {e}", file=sys.stderr)
        return 1

    bench = val.get("benchmark", {})
    if not bench:
        print("ERROR: 'benchmark' key missing from validation_results.json — "
              "run Step 5c (benchmark) first.", file=sys.stderr)
        return 1

    # Calibration count
    calib_n = 1
    if cal_log.exists():
        calib_n = cal_log.read_text().count("### Transfer:") + 1

    # Extract fields
    alg_name = alg.get("algorithm_name", "unknown")
    overall = val.get("overall_verdict", "UNKNOWN")
    tau = val.get("suite_a", {}).get("kendall_tau", "N/A")
    err = val.get("suite_a", {}).get("max_abs_error", "N/A")
    suite_a_pass = val.get("suite_a", {}).get("passed", False)
    suite_c_pass = val.get("suite_c", {}).get("passed", False)
    pile_on = val.get("suite_c", {}).get("max_pile_on_ratio", "N/A")
    t_eff_pct = round(bench.get("t_eff", 0) * 100, 1)
    mech = bench.get("mechanism_check_verdict", "UNKNOWN")

    wc = bench.get("workload_classification", [])
    matched_entry = next((w for w in wc if w.get("classification") == "matched"), None)
    matched_wl = (matched_entry or {}).get("workload", alg_name)
    unmatched_entries = [w for w in wc if w.get("classification") == "unmatched"]
    matched_pct = round((matched_entry or {}).get("improvement", 0) * 100, 1)
    unmatched_mean_pct = (
        round(sum(w.get("improvement", 0) for w in unmatched_entries) /
              len(unmatched_entries) * 100, 1)
        if unmatched_entries else 0.0
    )

    narrative = {
        "PASS": f"Simulation-predicted benefit transferred to production.",
        "FAIL": f"Transfer failed — production improvement did not exceed noise floor.",
        "INCONCLUSIVE": f"Transfer result is inconclusive — see operator notes.",
    }.get(overall, f"Transfer verdict: {overall}.")

    evidence = f"""## Evidence: {alg_name} sim-to-real transfer

**Date:** {date.today().isoformat()}
**Verdict:** {overall}

### Claim
The evolved routing algorithm improves performance on {matched_wl}
in production with improvement above noise floor (T_eff={t_eff_pct}%).

### Evidence Chain

**1. Algorithm source**
- Algorithm: {alg_name}
- Source: {alg.get('evolve_block_source', 'N/A')}

**2. Translation fidelity verified**
- Suite A Kendall-tau: {tau} (threshold: 0.8) — {"PASS" if suite_a_pass else "FAIL"}
- Suite A max absolute error: {err}
- Suite C concurrent safety: {"PASS" if suite_c_pass else "FAIL"}, pile-on ratio: {pile_on}
- Interpretation: The production plugin reproduces the simulation
  algorithm's ranking behavior within measured tolerance.

**3. Production result**
- Observed improvement: {matched_pct}% on {matched_wl}
- Noise floor (T_eff): {t_eff_pct}%

**4. Mechanism specificity**
- Matched workload improvement: {matched_pct}%
- Mean unmatched workload improvement: {unmatched_mean_pct}%
- Mechanism check: {mech}

**5. Calibration**
- Running calibration: transfer {calib_n} of 3 (uncalibrated period)

### Summary
{narrative}
"""

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(evidence)
    return 0


def cmd_append_calibration_log(args: "argparse.Namespace") -> int:
    """Append a per-transfer calibration entry to docs/transfer/calibration_log.md.

    Exit 0 = success; 1 = corruption detected; 2 = infrastructure error.
    """
    import json
    from datetime import date
    from pathlib import Path
    import subprocess

    ws = Path(args.workspace)
    cal_path = Path(args.calibration_log)

    alg_path = ws / "algorithm_summary.json"
    val_path = ws / "validation_results.json"

    for p, label in [(alg_path, "algorithm_summary.json"),
                     (val_path, "validation_results.json")]:
        if not p.exists():
            print(f"ERROR: append-calibration-log requires '{p}' — {label} not found.",
                  file=sys.stderr)
            return 2

    try:
        alg = json.loads(alg_path.read_text())
        val = json.loads(val_path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        print(f"ERROR: cannot read workspace artifacts: {e}", file=sys.stderr)
        return 2

    pipeline_commit = alg.get("pipeline_commit", "")
    if not pipeline_commit:
        try:
            pipeline_commit = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], text=True).strip()
        except Exception:
            pipeline_commit = "unknown"

    alg_name = alg.get("algorithm_name", "unknown")
    overall = val.get("overall_verdict", "UNKNOWN")
    suite_a = val.get("suite_a", {})
    suite_b = val.get("suite_b", {})
    suite_c = val.get("suite_c", {})
    bench = val.get("benchmark", {})

    matched_improvement = 0.0
    for wc in bench.get("workload_classification", []):
        if wc.get("classification") == "matched":
            matched_improvement = max(matched_improvement, wc.get("improvement", 0.0))

    entry = (
        f"\n### Transfer: {alg_name}\n"
        f"```yaml\n"
        f"transfer_date: {date.today().isoformat()}\n"
        f"algorithm_name: {alg_name}\n"
        f"pipeline_commit: {pipeline_commit}\n"
        f"single_run_provisional: true\n"
        f"suite_a_results:\n"
        f"  kendall_tau: {suite_a.get('kendall_tau', 0.0)}\n"
        f"  max_abs_error: {suite_a.get('max_abs_error', 0.0)}\n"
        f"suite_b_results:\n"
        f"  rank_stability_tau: {suite_b.get('rank_stability_tau', 0.0)}\n"
        f"  threshold_crossing_pct: {suite_b.get('threshold_crossing_pct', 0.0)}\n"
        f"  informational_only: true\n"
        f"suite_c_results:\n"
        f"  deterministic: {str(suite_c.get('deterministic', False)).lower()}\n"
        f"  max_pile_on_ratio: {suite_c.get('max_pile_on_ratio', 0.0)}\n"
        f"benchmark_results:\n"
        f"  mechanism_check_verdict: {bench.get('mechanism_check_verdict', 'UNKNOWN')}\n"
        f"  t_eff: {bench.get('t_eff', 0.0)}\n"
        f"  matched_improvement: {matched_improvement}\n"
        f"noise_cv: {val.get('noise_cv', 0.0)}\n"
        f"overall_verdict: {overall}\n"
        f"threshold_adjustments: []\n"
        f"```\n"
    )

    if not cal_path.exists():
        cal_path.parent.mkdir(parents=True, exist_ok=True)
        cal_path.write_text(
            "# Transfer Pipeline Calibration Log\n\nAppend-only: do not modify existing entries.\n\n"
            "## Entries\n\n<!-- Stage 6 appends entries below this line -->\n"
        )

    try:
        before_text = cal_path.read_text()
    except OSError as e:
        print(f"ERROR: cannot read calibration log: {e}", file=sys.stderr)
        return 2
    count_before = before_text.count("### Transfer:")

    try:
        cal_path.write_text(before_text + entry)
    except OSError as e:
        print(f"ERROR: cannot write to calibration log: {e}", file=sys.stderr)
        return 2

    try:
        after_text = cal_path.read_text()
    except OSError as e:
        print(f"ERROR: cannot re-read calibration log after append: {e}", file=sys.stderr)
        return 2
    count_after = after_text.count("### Transfer:")

    if count_after != count_before + 1:
        print(
            f"ERROR: calibration log corruption — count changed from {count_before} "
            f"to {count_after} (expected {count_before + 1}). "
            f"Inspect {cal_path} and repair from git history.",
            file=sys.stderr,
        )
        return 1

    if not after_text.endswith(entry):
        print(
            "ERROR: calibration log corruption — last entry does not match appended content. "
            f"Inspect {cal_path} and repair from git history.",
            file=sys.stderr,
        )
        return 1

    print(f"OK: calibration entry appended for '{alg_name}' "
          f"(overall_verdict={overall}, entry {count_after} of log).")
    return 0


def cmd_compare(args: "argparse.Namespace") -> int:
    """Print a latency comparison table from baseline and treatment result files.

    Exit 0 = table produced (even with partial workload matches).
    Exit 1 = no workloads could be paired, or input files missing/malformed.
    """
    import json as _json

    def _load(path: str, label: str):
        p = Path(path)
        if not p.exists():
            print(f"ERROR: {label} file '{path}' not found.", file=sys.stderr)
            return None
        try:
            data = _json.loads(p.read_text())
        except (OSError, _json.JSONDecodeError) as e:
            print(f"ERROR: failed to read/parse {label} file '{path}': {e}", file=sys.stderr)
            return None
        if "workloads" not in data or not isinstance(data["workloads"], list):
            print(f"ERROR: {label} file '{path}' missing 'workloads' list.", file=sys.stderr)
            return None
        result = {}
        for w in data["workloads"]:
            if "name" not in w or "metrics" not in w:
                print(f"WARN: workload entry in {label} missing 'name' or 'metrics' — skipped",
                      file=sys.stderr)
                continue
            result[w["name"]] = w["metrics"]
        return result

    baseline = _load(args.baseline, "--baseline")
    treatment = _load(args.treatment, "--treatment")
    if baseline is None or treatment is None:
        return 1

    METRICS = [
        ("ttft_p50", "TTFT p50"),
        ("ttft_p99", "TTFT p99"),
        ("tpot_p50", "TPOT p50"),
        ("tpot_p99", "TPOT p99"),
    ]

    all_names = sorted(set(baseline) | set(treatment))
    paired = [(n, baseline[n], treatment[n]) for n in all_names
              if n in baseline and n in treatment]
    for n in all_names:
        if n in baseline and n not in treatment:
            print(f"WARN: workload '{n}' missing in --treatment file — skipped", file=sys.stderr)
        elif n in treatment and n not in baseline:
            print(f"WARN: workload '{n}' missing in --baseline file — skipped", file=sys.stderr)

    if not paired:
        print("ERROR: no workloads could be paired between baseline and treatment files.",
              file=sys.stderr)
        return 1

    lines = []
    for wname, b_metrics, t_metrics in paired:
        lines.append(f"=== Workload: {wname} ===")
        header = f"{'Metric':<12}{'Baseline':>10}{'Treatment':>11}{'Delta(ms)':>11}{'Change':>20}"
        sep = "─" * len(header)
        lines.append(header)
        lines.append(sep)
        for key, label in METRICS:
            bval = b_metrics.get(key)
            tval = t_metrics.get(key)
            if bval is None or tval is None:
                missing_side = "--baseline" if bval is None else "--treatment"
                print(f"WARN: workload '{wname}' metric '{key}' missing in {missing_side} — N/A",
                      file=sys.stderr)
                lines.append(f"{label:<12}{'N/A':>10}{'N/A':>11}{'N/A':>11}{'N/A':>14}")
                continue
            try:
                delta = tval - bval
                if bval != 0:
                    pct_str_val = f"{delta / bval * 100:+.1f}%"
                    direction = "better" if delta < 0 else ("worse" if delta > 0 else "no change")
                else:
                    pct_str_val = "N/A"
                    direction = "N/A"
            except TypeError:
                print(
                    f"WARN: workload '{wname}' metric '{key}': non-numeric values "
                    f"(baseline={type(bval).__name__}:{bval!r}, "
                    f"treatment={type(tval).__name__}:{tval!r}) — skipped",
                    file=sys.stderr,
                )
                lines.append(f"{label:<12}{'N/A':>10}{'N/A':>11}{'N/A':>11}{'N/A':>14}")
                continue
            delta_str = f"{delta:+.1f}"
            pct_str = f"{pct_str_val} ({direction})"
            lines.append(f"{label:<12}{bval:>10.1f}{tval:>11.1f}{delta_str:>11}{pct_str:>20}")
        lines.append("")

    output = "\n".join(lines)
    print(output)

    if args.out:
        out_path = Path(args.out)
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(output + "\n")
        except OSError as e:
            print(f"ERROR: cannot write output file '{out_path}': {e}", file=sys.stderr)
            return 1

    return 0


def main():
    if sys.version_info < (3, 10):
        print("ERROR: transfer_cli.py requires Python >= 3.10 "
              f"(running {sys.version_info.major}.{sys.version_info.minor})",
              file=sys.stderr)
        sys.exit(2)
    parser = argparse.ArgumentParser(
        description="Transfer pipeline CLI — mechanical support for sim-to-production transfer",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # extract
    p_extract = subparsers.add_parser("extract",
        help="Parse EVOLVE-BLOCK, produce algorithm_summary.json")
    p_extract.add_argument("routing_dir", help="Path to routing/ directory")
    p_extract.add_argument("--strict", action="store_true",
        help="CI mode: require mapping artifact, enforce fidelity checks, "
             "fail if signal count < minimum threshold")
    p_extract.set_defaults(func=cmd_extract)

    # validate-mapping
    p_mapping = subparsers.add_parser("validate-mapping",
        help="Check mapping artifact completeness (NOTE: commit hash check only "
             "verifies presence of a hex string, not currency against submodule HEAD; "
             "CI workflow performs the real hash comparison)")
    p_mapping.add_argument("--summary", help="Path to algorithm_summary.json (default: workspace/)")
    p_mapping.set_defaults(func=cmd_validate_mapping)

    # validate-schema
    p_schema = subparsers.add_parser("validate-schema", help="Validate workspace artifact against schema")
    p_schema.add_argument("artifact_path", help="Path to workspace JSON artifact")
    p_schema.set_defaults(func=cmd_validate_schema)

    # validate-translation
    p_vt = subparsers.add_parser("validate-translation",
        help="Mechanical pre-checks for translation fidelity (Stage 3.5): "
             "signal presence, constant audit, normalization")
    p_vt.add_argument("--algorithm", required=True,
                      help="Path to workspace/algorithm_summary.json")
    p_vt.add_argument("--signal-coverage", required=True, dest="signal_coverage",
                      help="Path to workspace/signal_coverage.json")
    p_vt.add_argument("--scorer-file", required=True, dest="scorer_file",
                      help="Path to generated scorer .go file")
    p_vt.set_defaults(func=cmd_validate_translation)

    # test-status
    p_test_status = subparsers.add_parser("test-status",
        help="Classify errors from go build/test output (reads stdin)")
    p_test_status.set_defaults(func=cmd_test_status)

    # benchmark subcommand
    p_bench = subparsers.add_parser("benchmark",
        help="Compute T_eff and mechanism check from noise/baseline/treatment results")
    p_bench.add_argument("--noise", required=True)
    p_bench.add_argument("--baseline", required=True)
    p_bench.add_argument("--treatment", required=True)
    p_bench.add_argument("--signal-coverage", required=True, dest="signal_coverage")
    p_bench.add_argument("--workloads-dir", required=True, dest="workloads_dir")
    p_bench.add_argument("--out")
    p_bench.set_defaults(func=cmd_benchmark_new)

    p_ct = subparsers.add_parser("convert-trace",
        help="Convert blis observe TraceV2 output to metrics JSON")
    p_ct.add_argument("--input-dir", required=True,
                       dest="input_dir",
                       help="Phase directory containing per-workload TraceV2 subdirs")
    p_ct.add_argument("--output", required=True,
                       help="Output metrics JSON file path")
    p_ct.set_defaults(func=cmd_convert_trace)

    p_bstate = subparsers.add_parser("benchmark-state",
        help="Read/write workspace/benchmark_state.json phase tracking")
    p_bstate.add_argument("--workspace", required=True)
    p_bstate.add_argument("--namespace")
    p_bstate.add_argument("--set-phase", dest="set_phase",
                           choices=["noise", "baseline", "treatment"])
    p_bstate.add_argument("--status",
                           choices=["pending", "running", "done", "failed"])
    p_bstate.add_argument("--pipelinerun")
    p_bstate.add_argument("--results")
    p_bstate.add_argument("--failure-reason", dest="failure_reason")
    p_bstate.add_argument("--force", action="store_true")
    p_bstate.set_defaults(func=cmd_benchmark_state)

    p_cp = subparsers.add_parser("compile-pipeline",
        help="Compile a tektonc pipeline template for a given phase")
    p_cp.add_argument("--template-dir", required=True, dest="template_dir")
    p_cp.add_argument("--values", required=True)
    p_cp.add_argument("--phase", required=True, choices=["noise", "baseline", "treatment"])
    p_cp.add_argument("--out", required=True)
    p_cp.set_defaults(func=cmd_compile_pipeline)

    p_rpr = subparsers.add_parser("render-pipelinerun",
        help="Substitute variables in a PipelineRun stub")
    p_rpr.add_argument("--template", required=True)
    p_rpr.add_argument("--vars", nargs="+", metavar="KEY=VAL")
    p_rpr.add_argument("--out", required=True)
    p_rpr.set_defaults(func=cmd_render_pipelinerun)

    p_pf = subparsers.add_parser("preflight",
        help="Run pre-flight cluster checks before submitting a pipeline phase")
    p_pf.add_argument("--phase", required=True, choices=["noise", "baseline", "treatment"])
    p_pf.add_argument("--values", required=True)
    p_pf.add_argument("--namespace", required=True)
    p_pf.set_defaults(func=cmd_preflight)

    p_ge = subparsers.add_parser("generate-evidence",
        help="Generate workspace/transfer_evidence.md from workspace artifacts")
    p_ge.add_argument("--workspace", required=True)
    p_ge.add_argument("--out", required=True)
    p_ge.add_argument("--calibration-log", dest="calibration_log",
                       default="docs/transfer/calibration_log.md")
    p_ge.set_defaults(func=cmd_generate_evidence)

    p_acl = subparsers.add_parser(
        "append-calibration-log",
        help="Append a calibration entry to docs/transfer/calibration_log.md",
    )
    p_acl.add_argument("--workspace", default="workspace/",
                       help="Path to workspace directory")
    p_acl.add_argument("--calibration-log", default="docs/transfer/calibration_log.md",
                       help="Path to calibration log")
    p_acl.set_defaults(func=cmd_append_calibration_log)

    p_cmp = subparsers.add_parser("compare",
        help="Print latency comparison table from baseline/treatment results")
    p_cmp.add_argument("--baseline", required=True,
                       help="Path to workspace/baseline_results.json")
    p_cmp.add_argument("--treatment", required=True,
                       help="Path to workspace/treatment_results.json")
    p_cmp.add_argument("--out", help="Output file for comparison table (optional)")
    p_cmp.set_defaults(func=cmd_compare)

    p_mv = subparsers.add_parser("merge-values",
        help="Merge env_defaults.yaml and algorithm_values.yaml into values.yaml")
    p_mv.add_argument("--env", required=True,
                      help="Path to infrastructure defaults YAML (e.g. config/env_defaults.yaml)")
    p_mv.add_argument("--algorithm", required=True,
                      help="Path to algorithm-specific values YAML (e.g. workspace/tekton/algorithm_values.yaml)")
    p_mv.add_argument("--out", required=True,
                      help="Output path for merged values.yaml")
    p_mv.set_defaults(func=cmd_merge_values)

    p_bpe = subparsers.add_parser("build-push-epp",
        help="Build and push the EPP image for the treatment phase (Stage 4.5)")
    p_bpe.add_argument("--scheduler-dir", required=True, dest="scheduler_dir",
                       help="Path to llm-d-inference-scheduler submodule")
    p_bpe.add_argument("--env", required=True,
                       help="Path to env_defaults.yaml (provides epp_image.build config)")
    p_bpe.add_argument("--values", required=True,
                       help="Path to workspace/tekton/algorithm_values.yaml")
    p_bpe.add_argument("--merged-values", required=True, dest="merged_values",
                       help="Path to workspace/tekton/values.yaml (output of merge-values)")
    p_bpe.add_argument("--dry-run", action="store_true", dest="dry_run",
                       help="Build image locally but skip push and config update")
    p_bpe.set_defaults(func=cmd_build_push_epp)

    args = parser.parse_args()
    exit_code = args.func(args)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
