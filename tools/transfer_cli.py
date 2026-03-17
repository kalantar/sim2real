#!/usr/bin/env python3
"""Transfer pipeline CLI — mechanical support for sim-to-production transfer.

Commands:
    extract <routing_dir>    Parse EVOLVE-BLOCK, produce algorithm_summary.json
    validate-mapping         Check mapping artifact completeness
    validate-schema <path>   Validate workspace artifact against JSON Schema
    test-status              Classify go build/test output (stdin) into error classes
    noise-characterize       Compute per-metric CV and T_eff from baseline latency runs
    benchmark                Compute mechanism check from benchmark results

Exit codes: 0 = success, 1 = validation failure, 2 = infrastructure error
All commands output JSON to stdout.
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
    "QueueDepth": "int",
    "BatchSize": "int",
    "KVUtilization": "float64",
    "FreeKVBlocks": "int64",     # Struct field — NOT accessed in EVOLVE-BLOCK
    "CacheHitRate": "float64",
    "InFlightRequests": "int",
}

# Fields used as identifiers/keys, not routing signals. These are in
# ROUTING_SNAPSHOT_FIELDS (for struct completeness) but excluded from signal extraction.
_IDENTIFIER_FIELDS = {"ID"}

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
        data = json.loads(artifact_path.read_text())
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


# ─── noise-characterize ───────────────────────────────────────────────────────

def _cmd_noise_characterize(args: argparse.Namespace) -> int:
    """Compute per-metric CV and T_eff from baseline latency runs.

    Input JSON format:
        {"runs": [{"p50": float, "p95": float, "p99": float}, ...]}

    Exit codes:
        0 = success (halt=false)
        1 = validation failure (halt=true, CV > 15%)
        2 = infrastructure error (file missing or invalid JSON)
    """
    import math

    runs_path = Path(args.runs).resolve()
    allowed_root = Path(os.environ["_SIM2REAL_ALLOWED_ROOT"]).resolve() if "_SIM2REAL_ALLOWED_ROOT" in os.environ else REPO_ROOT
    if not runs_path.is_relative_to(allowed_root):
        return _output("error", 2,
                       errors=[f"Runs path '{runs_path}' is outside allowed root '{allowed_root}'."],
                       per_metric_cv={}, t_eff=0.0, halt=False)
    if not runs_path.exists():
        return _output("error", 2, errors=[f"runs file not found: {args.runs}"],
                       per_metric_cv={}, t_eff=0.0, halt=False)

    try:
        if runs_path.stat().st_size > MAX_FILE_SIZE:
            return _output("error", 2,
                           errors=[f"runs file exceeds {MAX_FILE_SIZE} bytes: {args.runs}"],
                           per_metric_cv={}, t_eff=0.0, halt=False)
    except OSError as e:
        return _output("error", 2,
                       errors=[f"cannot stat runs file '{args.runs}': {e}"],
                       per_metric_cv={}, t_eff=0.0, halt=False)

    try:
        data = json.loads(runs_path.read_text())
    except OSError as e:
        return _output("error", 2, errors=[f"cannot read runs file '{args.runs}': {e}"],
                       per_metric_cv={}, t_eff=0.0, halt=False)
    except json.JSONDecodeError as e:
        return _output("error", 2, errors=[f"invalid JSON in {args.runs}: {e}"],
                       per_metric_cv={}, t_eff=0.0, halt=False)

    if not isinstance(data, dict) or "runs" not in data:
        return _output("error", 2,
                       errors=["missing 'runs' key in input JSON"],
                       per_metric_cv={}, t_eff=0.0, halt=False)

    runs = data["runs"]
    if not isinstance(runs, list) or len(runs) == 0:
        return _output("error", 2,
                       errors=["'runs' must be a non-empty list (BC-16: malformed input → exit 2)"],
                       per_metric_cv={}, t_eff=0.0, halt=False)

    metrics = ["p50", "p95", "p99"]
    per_metric_cv: dict[str, float] = {}

    for metric in metrics:
        raw_values = [r[metric] for r in runs if isinstance(r, dict) and metric in r
                      and isinstance(r[metric], (int, float))]
        values = [v for v in raw_values if v > 0]
        excluded = len(raw_values) - len(values)
        if excluded > 0:
            print(
                f"WARNING: metric '{metric}': {excluded} of {len(raw_values)} run(s) excluded "
                f"(zero or non-positive values) — CV computed from {len(values)} run(s)",
                file=sys.stderr,
            )
        if len(values) < 2:
            print(f"WARNING: metric '{metric}' has {len(values)} valid data point(s) (need ≥2), skipping from CV computation", file=sys.stderr)
            continue

        mean = sum(values) / len(values)
        # Sample variance (Bessel's correction: n-1)
        variance = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
        std = math.sqrt(variance)
        per_metric_cv[metric] = std / mean

    if not per_metric_cv:
        n_runs = len([r for r in runs if isinstance(r, dict)])
        return _output("error", 2,
                       errors=[f"insufficient runs for CV computation: need ≥2 data points per metric, got {n_runs} run(s) with valid latency values"],
                       per_metric_cv={}, t_eff=0.0, halt=False)

    max_cv = max(per_metric_cv.values())
    t_eff = max(0.05, 2.0 * max_cv)
    halt = max_cv > 0.15

    if halt:
        return _output("error", 1, per_metric_cv=per_metric_cv, t_eff=t_eff, halt=True,
                       errors=[f"noise too high: max CV={max_cv:.4f} > 0.15 threshold"])

    return _output("ok", 0, per_metric_cv=per_metric_cv, t_eff=t_eff, halt=False)


# ─── benchmark ────────────────────────────────────────────────────────────────

def _cmd_benchmark(args: argparse.Namespace) -> int:
    """Compute mechanism check from benchmark results.

    Input JSON format:
        {"workloads": [
            {"name": str, "classification": "matched"|"unmatched",
             "baseline_p99": float, "transfer_p99": float},
            ...
        ]}

    Exit codes:
        0 = success (PASS or INCONCLUSIVE — operator must check mechanism_check_verdict)
        1 = validation failure (FAIL verdict)
        2 = infrastructure error (file missing, invalid JSON, or missing --t-eff)
    """
    if args.t_eff is None:
        return _output("error", 2,
                       errors=["--t-eff required: run noise-characterize first"],
                       mechanism_check_verdict="ERROR", passed=False, workload_classification=[])

    t_eff = args.t_eff
    if t_eff <= 0:
        return _output("error", 2,
                       errors=[f"--t-eff must be > 0, got {t_eff}. "
                               "noise-characterize guarantees T_eff >= 0.05; "
                               "a non-positive value indicates manual override error."],
                       mechanism_check_verdict="ERROR", passed=False, workload_classification=[])

    results_path = Path(args.results).resolve()
    allowed_root = Path(os.environ["_SIM2REAL_ALLOWED_ROOT"]).resolve() if "_SIM2REAL_ALLOWED_ROOT" in os.environ else REPO_ROOT
    if not results_path.is_relative_to(allowed_root):
        return _output("error", 2,
                       errors=[f"Results path '{results_path}' is outside allowed root '{allowed_root}'."],
                       mechanism_check_verdict="ERROR", passed=False, workload_classification=[])
    if not results_path.exists():
        return _output("error", 2,
                       errors=[f"results file not found: {args.results}"],
                       mechanism_check_verdict="ERROR", passed=False, workload_classification=[])

    try:
        if results_path.stat().st_size > MAX_FILE_SIZE:
            return _output("error", 2,
                           errors=[f"results file exceeds {MAX_FILE_SIZE} bytes: {args.results}"],
                           mechanism_check_verdict="ERROR", passed=False, workload_classification=[])
    except OSError as e:
        return _output("error", 2,
                       errors=[f"cannot stat results file '{args.results}': {e}"],
                       mechanism_check_verdict="ERROR", passed=False, workload_classification=[])

    try:
        data = json.loads(results_path.read_text())
    except OSError as e:
        return _output("error", 2,
                       errors=[f"cannot read results file '{args.results}': {e}"],
                       mechanism_check_verdict="ERROR", passed=False, workload_classification=[])
    except json.JSONDecodeError as e:
        return _output("error", 2,
                       errors=[f"invalid JSON in {args.results}: {e}"],
                       mechanism_check_verdict="ERROR", passed=False, workload_classification=[])

    if not isinstance(data, dict) or "workloads" not in data:
        return _output("error", 2,
                       errors=["missing 'workloads' key in input JSON"],
                       mechanism_check_verdict="ERROR", passed=False, workload_classification=[])

    workloads = data["workloads"]
    if not isinstance(workloads, list):
        return _output("error", 2,
                       errors=["'workloads' must be a list"],
                       mechanism_check_verdict="ERROR", passed=False, workload_classification=[])

    results = []
    matched_improvements = []
    specificity_failures = []
    errors = []
    format_errors = []

    for idx, w in enumerate(workloads):
        if not isinstance(w, dict):
            format_errors.append(f"workloads[{idx}] is not an object (got {type(w).__name__!r}); skipped")
            continue
        name = w.get("name", "unknown")
        classification = w.get("classification", "unmatched")
        baseline_p99 = w.get("baseline_p99", None)
        transfer_p99 = w.get("transfer_p99", None)

        if baseline_p99 is None or transfer_p99 is None:
            missing = [k for k in ["baseline_p99", "transfer_p99"]
                       if k not in w or w[k] is None]
            results.append({"workload": name, "classification": classification,
                             "improvement": 0.0, "error": f"missing required field(s): {', '.join(missing)}"})
            continue

        if baseline_p99 <= 0:
            results.append({"workload": name, "classification": classification,
                             "improvement": 0.0, "error": "baseline_p99 must be > 0"})
            continue

        if transfer_p99 < 0:
            results.append({"workload": name, "classification": classification,
                             "improvement": 0.0, "error": "transfer_p99 must be >= 0"})
            continue

        improvement = (baseline_p99 - transfer_p99) / baseline_p99
        results.append({"workload": name, "classification": classification,
                         "improvement": round(improvement, 6)})

        if classification == "matched":
            matched_improvements.append(improvement)
        elif classification == "unmatched":
            if abs(baseline_p99 - transfer_p99) / baseline_p99 >= t_eff:
                specificity_failures.append({
                    "workload": name,
                    "change_ratio": round(abs(baseline_p99 - transfer_p99) / baseline_p99, 6)
                })
        else:
            errors.append(f"unrecognized classification value: {classification!r} for workload {name!r}")

    if not matched_improvements:
        return _output("error", 2,
                       errors=format_errors + ["no matched workloads found — cannot compute mechanism check (configuration error: check workload classification)"],
                       mechanism_check_verdict="ERROR", passed=False,
                       workload_classification=results,
                       t_eff=t_eff, specificity_failures=specificity_failures)

    # Mechanism check
    if any(imp >= t_eff for imp in matched_improvements):
        verdict = "PASS"
    elif any(imp > 0 for imp in matched_improvements):
        verdict = "INCONCLUSIVE"
    else:
        verdict = "FAIL"

    exit_code = 1 if verdict == "FAIL" else 0
    status = "error" if verdict == "FAIL" else ("inconclusive" if verdict == "INCONCLUSIVE" else "ok")
    if verdict == "FAIL":
        errors.append("mechanism check FAIL: no matched workload improvement >= T_eff")
    if specificity_failures:
        errors.append(f"specificity check failed for {len(specificity_failures)} unmatched workload(s)")

    passed = verdict == "PASS"
    return _output(status, exit_code, mechanism_check_verdict=verdict, passed=passed,
                   workload_classification=results, t_eff=t_eff, specificity_failures=specificity_failures,
                   errors=format_errors + errors)


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

    # test-status
    p_test_status = subparsers.add_parser("test-status",
        help="Classify errors from go build/test output (reads stdin)")
    p_test_status.set_defaults(func=cmd_test_status)

    # noise-characterize subcommand
    p_noise = subparsers.add_parser("noise-characterize",
        help="Compute per-metric CV and T_eff from baseline latency runs")
    p_noise.add_argument("--runs", required=True,
        help="Path to JSON file with baseline latency runs: {runs: [{p50, p95, p99}]}")
    p_noise.set_defaults(func=_cmd_noise_characterize)

    # benchmark subcommand
    p_bench = subparsers.add_parser("benchmark",
        help="Compute mechanism check from benchmark results")
    p_bench.add_argument("--results", required=True,
        help="Path to JSON file with workload results")
    p_bench.add_argument("--t-eff", type=float, default=None,
        help="Effective threshold from noise-characterize")
    p_bench.set_defaults(func=_cmd_benchmark)

    args = parser.parse_args()
    exit_code = args.func(args)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
