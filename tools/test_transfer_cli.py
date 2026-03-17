# tools/test_transfer_cli.py
import json
import os
import subprocess
import sys
import pytest
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
CLI = REPO_ROOT / "tools" / "transfer_cli.py"
ROUTING_DIR = REPO_ROOT / "routing"
WORKSPACE = REPO_ROOT / "workspace"


def run_cli(*args) -> tuple[int, dict]:
    """Run CLI command, return (exit_code, parsed_json_output)."""
    env = {k: v for k, v in os.environ.items() if k != "CI"}
    result = subprocess.run(
        [sys.executable, str(CLI), *args],
        capture_output=True, text=True, cwd=str(REPO_ROOT), env=env,
    )
    try:
        output = json.loads(result.stdout)
    except json.JSONDecodeError:
        output = {"raw_stdout": result.stdout, "raw_stderr": result.stderr}
    return result.returncode, output


class TestExtract:
    def setup_method(self):
        """Ensure workspace dir exists and clean up prior artifacts."""
        WORKSPACE.mkdir(exist_ok=True)
        summary = WORKSPACE / "algorithm_summary.json"
        if summary.exists():
            summary.unlink()

    def test_extract_produces_valid_summary(self):
        """BC-1: extract produces workspace/algorithm_summary.json with required fields."""
        code, output = run_cli("extract", str(ROUTING_DIR))
        assert code == 0, f"Expected exit 0, got {code}: {output}"
        assert output["status"] == "ok"
        summary_path = WORKSPACE / "algorithm_summary.json"
        assert summary_path.exists()
        summary = json.loads(summary_path.read_text())
        assert "algorithm_name" in summary
        assert "evolve_block_source" in summary
        assert "evolve_block_content_hash" in summary
        assert len(summary["evolve_block_content_hash"]) == 64
        assert "signals" in summary
        assert isinstance(summary["signals"], list)
        assert len(summary["signals"]) > 0
        assert "metrics" in summary
        assert "scope_validation_passed" in summary

    def test_extract_identifies_signals(self):
        """BC-2: extract finds RoutingSnapshot fields from EVOLVE-BLOCK."""
        code, output = run_cli("extract", str(ROUTING_DIR))
        assert code == 0
        summary = json.loads((WORKSPACE / "algorithm_summary.json").read_text())
        signal_names = {s["name"] for s in summary["signals"]}
        assert "KVUtilization" in signal_names
        assert "CacheHitRate" in signal_names
        # EffectiveLoad() expansion
        assert "QueueDepth" in signal_names, "EffectiveLoad() expansion missing QueueDepth"
        assert "BatchSize" in signal_names, "EffectiveLoad() expansion missing BatchSize"
        assert "InFlightRequests" in signal_names, "EffectiveLoad() expansion missing InFlightRequests"

    def test_extract_signals_have_required_fields(self):
        """BC-2: each signal has name, type, access_path."""
        code, _ = run_cli("extract", str(ROUTING_DIR))
        assert code == 0
        summary = json.loads((WORKSPACE / "algorithm_summary.json").read_text())
        for signal in summary["signals"]:
            assert "name" in signal, f"Signal missing 'name': {signal}"
            assert "type" in signal, f"Signal missing 'type': {signal}"
            assert "access_path" in signal, f"Signal missing 'access_path': {signal}"

    def test_extract_includes_metrics(self):
        """BC-1: metrics from best_program_info.json are included."""
        code, _ = run_cli("extract", str(ROUTING_DIR))
        assert code == 0
        summary = json.loads((WORKSPACE / "algorithm_summary.json").read_text())
        assert "combined_score" in summary["metrics"]

    def test_extract_content_hash_matches_evolve_block(self):
        """F-18: evolve_block_content_hash is SHA-256 of actual EVOLVE-BLOCK content.
        Slicing convention: lines[start_idx:end_idx + 1] (inclusive of marker lines)."""
        import hashlib
        code, _ = run_cli("extract", str(ROUTING_DIR))
        assert code == 0
        summary = json.loads((WORKSPACE / "algorithm_summary.json").read_text())
        source = (ROUTING_DIR / "best_program.py").read_text()
        lines = source.split("\n")
        start_idx = end_idx = None
        for i, line in enumerate(lines):
            if "EVOLVE-BLOCK-START" in line:
                start_idx = i
            if "EVOLVE-BLOCK-END" in line:
                end_idx = i
                break
        assert start_idx is not None and end_idx is not None
        block = "\n".join(lines[start_idx:end_idx + 1])
        expected_hash = hashlib.sha256(block.encode()).hexdigest()
        assert summary["evolve_block_content_hash"] == expected_hash

    def test_extract_scope_validation_passes_for_routing(self):
        """BC-5: routing-only algorithm passes scope validation."""
        code, _ = run_cli("extract", str(ROUTING_DIR))
        assert code == 0
        summary = json.loads((WORKSPACE / "algorithm_summary.json").read_text())
        assert summary["scope_validation_passed"] is True

    def test_extract_scope_validation_fails_for_out_of_scope(self):
        """BC-5 negative: out-of-scope patterns cause scope_validation_passed=false."""
        import tempfile, shutil
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            shutil.copy2(str(ROUTING_DIR / "best_program.py"), str(tmpdir / "best_program.py"))
            shutil.copy2(str(ROUTING_DIR / "best_program_info.json"), str(tmpdir / "best_program_info.json"))
            src = (tmpdir / "best_program.py").read_text()
            src = src.replace(
                "// EVOLVE-BLOCK-START",
                "// EVOLVE-BLOCK-START\n\tPrefillInstance disaggregation check",
            )
            (tmpdir / "best_program.py").write_text(src)
            code, output = run_cli("extract", str(tmpdir))
            assert code == 1, f"Scope validation failure should exit 1, got {code}: {output}"
            summary = json.loads((WORKSPACE / "algorithm_summary.json").read_text())
            assert summary["scope_validation_passed"] is False

    def test_extract_missing_directory_exits_2(self):
        """BC-8: missing input directory exits with code 2."""
        code, output = run_cli("extract", "/nonexistent/path")
        assert code == 2
        assert output["status"] == "error"
        assert any("not found" in e.lower() or "routing directory" in e.lower()
                    for e in output.get("errors", [])), (
            f"Expected 'directory not found' error, got: {output.get('errors', [])}"
        )

    def test_extract_no_signals_exits_1(self):
        """F-15: EVOLVE-BLOCK found but no recognizable signals -> exit 1."""
        import tempfile, shutil
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            shutil.copy2(str(ROUTING_DIR / "best_program_info.json"), str(tmpdir / "best_program_info.json"))
            (tmpdir / "best_program.py").write_text(
                '# EVOLVE-BLOCK-START\n'
                'def route():\n'
                '    return 42  # no signal access\n'
                '# EVOLVE-BLOCK-END\n'
            )
            code, output = run_cli("extract", str(tmpdir))
            assert code == 1, f"No signals should be exit 1 (validation), got {code}: {output}"
            assert output["status"] == "error"

    def test_extract_empty_evolve_block_exits_1(self):
        """F-15 edge case: EVOLVE-BLOCK markers present but empty content."""
        import tempfile, shutil
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            shutil.copy2(str(ROUTING_DIR / "best_program_info.json"), str(tmpdir / "best_program_info.json"))
            (tmpdir / "best_program.py").write_text(
                '# EVOLVE-BLOCK-START\n'
                '# EVOLVE-BLOCK-END\n'
            )
            code, output = run_cli("extract", str(tmpdir))
            assert code == 1, f"Empty EVOLVE-BLOCK should be exit 1, got {code}: {output}"

    def test_extract_multiple_evolve_blocks_warns(self):
        """F-27: Multiple EVOLVE-BLOCK pairs should emit a stderr warning."""
        import tempfile, shutil
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            shutil.copy2(str(ROUTING_DIR / "best_program_info.json"), str(tmpdir / "best_program_info.json"))
            (tmpdir / "best_program.py").write_text(
                '# EVOLVE-BLOCK-START\n'
                'snap.QueueDepth\n'
                '# EVOLVE-BLOCK-END\n'
                '# EVOLVE-BLOCK-START\n'
                'snap.BatchSize\n'
                '# EVOLVE-BLOCK-END\n'
            )
            env = {k: v for k, v in os.environ.items() if k != "CI"}
            result = subprocess.run(
                [sys.executable, str(CLI), "extract", str(tmpdir)],
                capture_output=True, text=True, cwd=str(REPO_ROOT),
                env=env,
            )
            assert result.returncode == 0, (
                f"Multiple EVOLVE-BLOCK should still succeed, got exit {result.returncode}: {result.stderr}"
            )
            stdout = json.loads(result.stdout)
            assert stdout["status"] == "ok"
            summary = json.loads((WORKSPACE / "algorithm_summary.json").read_text())
            signal_names = {s["name"] for s in summary["signals"]}
            assert "QueueDepth" in signal_names
            assert "WARNING" in result.stderr
            assert "2" in result.stderr

    def test_extract_few_signals_strict_exits_1(self):
        """F-9: 1-2 signals in --strict mode should exit 1."""
        import tempfile, shutil
        summary_path = WORKSPACE / "algorithm_summary.json"
        if summary_path.exists():
            summary_path.unlink()
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            shutil.copy2(str(ROUTING_DIR / "best_program_info.json"), str(tmpdir / "best_program_info.json"))
            (tmpdir / "best_program.py").write_text(
                '# EVOLVE-BLOCK-START\n'
                'func route(snap RoutingSnapshot) {\n'
                '    x := snap.QueueDepth\n'
                '}\n'
                '# EVOLVE-BLOCK-END\n'
            )
            code, output = run_cli("extract", "--strict", str(tmpdir))
            assert code == 1, (
                f"--strict with {output.get('signal_count', '?')} signals "
                f"(< MINIMUM_EXPECTED_SIGNALS=3) should exit 1, got {code}: {output}"
            )
            assert output["status"] == "error"
            error_text = " ".join(output.get("errors", []))
            assert "signal" in error_text.lower() and ("expected" in error_text.lower() or "minimum" in error_text.lower())
            assert not summary_path.exists(), "Strict-mode minimum-signal failure must not write artifact"

    def test_extract_few_signals_boundary_2_fails(self):
        """R3-F-15: Exactly 2 signals (< MINIMUM_EXPECTED_SIGNALS=3) should exit 1 in --strict."""
        import tempfile, shutil
        summary_path = WORKSPACE / "algorithm_summary.json"
        if summary_path.exists():
            summary_path.unlink()
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            shutil.copy2(str(ROUTING_DIR / "best_program_info.json"), str(tmpdir / "best_program_info.json"))
            (tmpdir / "best_program.py").write_text(
                '# EVOLVE-BLOCK-START\n'
                'func route(snap RoutingSnapshot) {\n'
                '    x := snap.QueueDepth\n'
                '    y := snap.BatchSize\n'
                '}\n'
                '# EVOLVE-BLOCK-END\n'
            )
            code, output = run_cli("extract", "--strict", str(tmpdir))
            assert code == 1, (
                f"--strict with 2 signals (< MINIMUM_EXPECTED_SIGNALS=3) "
                f"should exit 1, got {code}: {output}"
            )
            assert not summary_path.exists(), "Strict-mode minimum-signal failure must not write artifact"

    @pytest.mark.skipif(
        not (Path(__file__).parent.parent / "docs" / "transfer" / "blis_to_llmd_mapping.md").exists(),
        reason="Mapping artifact not present (pre-Task 5)"
    )
    def test_extract_few_signals_boundary_3_passes_threshold(self):
        """R3-F-15: Exactly 3 signals (= MINIMUM_EXPECTED_SIGNALS=3) should pass threshold in --strict."""
        import tempfile, shutil
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            shutil.copy2(str(ROUTING_DIR / "best_program_info.json"), str(tmpdir / "best_program_info.json"))
            (tmpdir / "best_program.py").write_text(
                '# EVOLVE-BLOCK-START\n'
                'func route(snap RoutingSnapshot) {\n'
                '    x := snap.QueueDepth\n'
                '    y := snap.BatchSize\n'
                '    z := snap.InFlightRequests\n'
                '}\n'
                '# EVOLVE-BLOCK-END\n'
            )
            code, output = run_cli("extract", "--strict", str(tmpdir))
            assert code == 0, (
                f"3 signals should pass the MINIMUM_EXPECTED_SIGNALS threshold, "
                f"but got exit code {code}: {output.get('errors', [])}"
            )

    def test_extract_missing_info_json_exits_2(self):
        """F-9: best_program_info.json not existing should exit 2 (infra error)."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            (tmpdir / "best_program.py").write_text(
                '# EVOLVE-BLOCK-START\n'
                'func route(snap RoutingSnapshot) {\n'
                '    x := snap.QueueDepth\n'
                '}\n'
                '# EVOLVE-BLOCK-END\n'
            )
            code, output = run_cli("extract", str(tmpdir))
            assert code == 2, (
                f"Missing best_program_info.json should exit 2, got {code}: {output}"
            )
            assert output["status"] == "error"
            assert any("best_program_info.json" in e for e in output.get("errors", []))

    def test_extract_malformed_info_json_exits_2(self):
        """Malformed best_program_info.json (non-JSON) should exit 2."""
        import tempfile, shutil
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            shutil.copy2(str(ROUTING_DIR / "best_program.py"), str(tmpdir / "best_program.py"))
            (tmpdir / "best_program_info.json").write_text("not valid json {{")
            env = {k: v for k, v in os.environ.items() if k != "CI"}
            result = subprocess.run(
                [sys.executable, str(CLI), "extract", str(tmpdir)],
                capture_output=True, text=True, cwd=str(REPO_ROOT), env=env,
            )
            assert result.returncode == 2
            stdout = json.loads(result.stdout)
            assert stdout["status"] == "error"
            assert any("malformed" in e.lower() or "json" in e.lower()
                       for e in stdout.get("errors", []))

    def test_extract_evolve_block_end_without_start_exits_2(self):
        """Asymmetric markers: EVOLVE-BLOCK-END without START should exit 2."""
        import tempfile, shutil
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            (tmpdir / "best_program.py").write_text(
                'func route(snap RoutingSnapshot) {\n'
                '    x := snap.QueueDepth\n'
                '}\n'
                '// EVOLVE-BLOCK-END\n'
            )
            (tmpdir / "best_program_info.json").write_text('{"metrics": {"combined_score": -1.0}}')
            code, output = run_cli("extract", str(tmpdir))
            assert code == 2, f"END without START should exit 2, got {code}: {output}"
            assert output["status"] == "error"
            assert output.get("error_detail") == "end_without_start", (
                f"error_detail should be 'end_without_start', got: {output.get('error_detail')}"
            )

    def test_extract_evolve_block_start_without_end_exits_2(self):
        """Asymmetric markers: EVOLVE-BLOCK-START without END should exit 2."""
        import tempfile, shutil
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            (tmpdir / "best_program.py").write_text(
                '// EVOLVE-BLOCK-START\n'
                'func route(snap RoutingSnapshot) {\n'
                '    x := snap.QueueDepth\n'
                '}\n'
            )
            (tmpdir / "best_program_info.json").write_text('{"metrics": {"combined_score": -1.0}}')
            code, output = run_cli("extract", str(tmpdir))
            assert code == 2, f"START without END should exit 2, got {code}: {output}"
            assert output["status"] == "error"
            assert output.get("error_detail") == "start_without_end", (
                f"error_detail should be 'start_without_end', got: {output.get('error_detail')}"
            )

    def test_extract_inverted_markers_exits_2(self):
        """R8-F-2: EVOLVE-BLOCK-END before EVOLVE-BLOCK-START should exit 2."""
        import tempfile, shutil
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            (tmpdir / "best_program.py").write_text(
                'func route(snap RoutingSnapshot) {\n'
                '    x := snap.QueueDepth\n'
                '// EVOLVE-BLOCK-END\n'
                '    y := snap.BatchSize\n'
                '// EVOLVE-BLOCK-START\n'
                '    z := snap.InFlightRequests\n'
                '}\n'
            )
            (tmpdir / "best_program_info.json").write_text('{"metrics": {"combined_score": -1.0}}')
            code, output = run_cli("extract", str(tmpdir))
            assert code == 2, f"Inverted markers should exit 2, got {code}: {output}"
            assert output["status"] == "error"
            assert output.get("error_detail") == "inverted_markers", (
                f"error_detail should be 'inverted_markers', got: {output.get('error_detail')}"
            )

    def test_extract_missing_metrics_key_warns(self):
        """F-15: best_program_info.json exists but has no 'metrics' key."""
        import tempfile, shutil
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            shutil.copy2(str(ROUTING_DIR / "best_program.py"), str(tmpdir / "best_program.py"))
            (tmpdir / "best_program_info.json").write_text('{"generation": 100}')
            env = {k: v for k, v in os.environ.items() if k != "CI"}
            result = subprocess.run(
                [sys.executable, str(CLI), "extract", str(tmpdir)],
                capture_output=True, text=True, cwd=str(REPO_ROOT),
                env=env,
            )
            assert result.returncode == 0, f"Missing metrics key should not abort: {result.stderr}"
            assert "metrics" in result.stderr.lower() or "warning" in result.stderr.lower()
            summary_path = WORKSPACE / "algorithm_summary.json"
            assert summary_path.exists()
            summary = json.loads(summary_path.read_text())
            assert "signals" in summary

    def test_extract_missing_metrics_key_strict_fails(self):
        """F-26: In --strict mode, missing combined_score should fail with exit 1."""
        import tempfile, shutil
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            shutil.copy2(str(ROUTING_DIR / "best_program.py"), str(tmpdir / "best_program.py"))
            (tmpdir / "best_program_info.json").write_text('{"generation": 100}')
            result = subprocess.run(
                [sys.executable, str(CLI), "extract", "--strict", str(tmpdir)],
                capture_output=True, text=True, cwd=str(REPO_ROOT),
            )
            assert result.returncode == 1, (
                f"--strict with missing combined_score should exit 1, got {result.returncode}"
            )
            stdout = json.loads(result.stdout)
            assert stdout["status"] == "error"

    def test_extract_output_is_json(self):
        """BC-7: CLI outputs valid JSON to stdout."""
        env = {k: v for k, v in os.environ.items() if k != "CI"}
        result = subprocess.run(
            [sys.executable, str(CLI), "extract", str(ROUTING_DIR)],
            capture_output=True, text=True, cwd=str(REPO_ROOT), env=env,
        )
        parsed = json.loads(result.stdout)
        assert "status" in parsed

    def test_extract_stdout_differs_from_file_artifact(self):
        """F-7: stdout JSON is an operational report, NOT the file artifact."""
        code, stdout_output = run_cli("extract", str(ROUTING_DIR))
        assert code == 0
        file_artifact = json.loads((WORKSPACE / "algorithm_summary.json").read_text())
        assert stdout_output.get("output_type") == "operational_report"
        assert "output_type" not in file_artifact
        assert "status" in stdout_output
        assert "signals" in file_artifact
        assert "signals" not in stdout_output

    @pytest.mark.skipif(
        not (Path(__file__).parent.parent / "docs" / "transfer" / "blis_to_llmd_mapping.md").exists(),
        reason="Mapping artifact not present (pre-Task 5)"
    )
    def test_extract_non_determinism_boundary_documented(self):
        """F-2/F-19: Verify extract produces different fidelity outcomes with/without mapping."""
        import shutil
        mapping = REPO_ROOT / "docs" / "transfer" / "blis_to_llmd_mapping.md"
        backup = mapping.with_suffix(".md.bak")
        code_with, _ = run_cli("extract", str(ROUTING_DIR))
        assert code_with == 0
        if mapping.exists():
            shutil.move(str(mapping), str(backup))
        try:
            code_without, _ = run_cli("extract", str(ROUTING_DIR))
            assert code_without == 0
            code_strict, output_strict = run_cli("extract", "--strict", str(ROUTING_DIR))
            assert code_strict != 0
        finally:
            if backup.exists():
                shutil.move(str(backup), str(mapping))

    def test_extract_without_mapping_graceful_degradation(self):
        """BC-6: extract succeeds when mapping artifact absent. Verifies fidelity_checked=false."""
        import shutil
        mapping = REPO_ROOT / "docs" / "transfer" / "blis_to_llmd_mapping.md"
        backup = mapping.with_suffix(".md.bak")
        if mapping.exists():
            shutil.move(str(mapping), str(backup))
        try:
            code, output = run_cli("extract", str(ROUTING_DIR))
            assert code == 0, f"Extract should succeed without mapping: {output}"
            assert output["status"] == "ok"
            summary = json.loads((WORKSPACE / "algorithm_summary.json").read_text())
            assert summary.get("fidelity_checked") is False
        finally:
            if backup.exists():
                shutil.move(str(backup), str(mapping))

    def test_extract_strict_fails_without_mapping(self):
        """F-1/F-16: --strict mode exits 1 when mapping artifact absent."""
        import shutil
        mapping = REPO_ROOT / "docs" / "transfer" / "blis_to_llmd_mapping.md"
        backup = mapping.with_suffix(".md.bak")
        if mapping.exists():
            shutil.move(str(mapping), str(backup))
        try:
            code, output = run_cli("extract", "--strict", str(ROUTING_DIR))
            assert code == 1, f"--strict should fail without mapping: {output}"
            assert output["status"] == "error"
            assert any("strict" in e.lower() or "mapping" in e.lower() for e in output["errors"])
        finally:
            if backup.exists():
                shutil.move(str(backup), str(mapping))

    @pytest.mark.skipif(
        not (Path(__file__).parent.parent / "docs" / "transfer" / "blis_to_llmd_mapping.md").exists(),
        reason="Mapping artifact not present (pre-Task 5)"
    )
    def test_extract_strict_succeeds_with_mapping(self):
        """F-1: --strict mode succeeds when mapping artifact exists."""
        code, output = run_cli("extract", "--strict", str(ROUTING_DIR))
        assert code == 0, f"--strict should succeed with mapping: {output}"

    @pytest.mark.skipif(
        not (Path(__file__).parent.parent / "docs" / "transfer" / "blis_to_llmd_mapping.md").exists(),
        reason="Mapping artifact not present (pre-Task 5)"
    )
    def test_extract_mapping_version_parsed(self):
        """F-18: mapping_artifact_version is parsed from mapping artifact."""
        code, _ = run_cli("extract", str(ROUTING_DIR))
        assert code == 0
        summary = json.loads((WORKSPACE / "algorithm_summary.json").read_text())
        version = summary.get("mapping_artifact_version", "")
        assert version != "unknown", "mapping_artifact_version should be parsed, got 'unknown'"
        mapping_content = (REPO_ROOT / "docs" / "transfer" / "blis_to_llmd_mapping.md").read_text()
        assert f"**Version:** {version}" in mapping_content


class TestGoldenSignalList:
    """Golden-file test verifying extracted signals match manually-verified ground truth.

    Manually verified from EVOLVE-BLOCK inspection:
      snap.EffectiveLoad() -> QueueDepth, BatchSize, InFlightRequests
      snap.KVUtilization (direct access)
      snap.CacheHitRate (direct access)
      req.SessionID (boolean check)
    """

    EXPECTED_SIGNALS = {
        "QueueDepth", "BatchSize", "InFlightRequests",
        "KVUtilization", "CacheHitRate", "SessionID",
    }

    EXPECTED_COMPOSITES = {
        "EffectiveLoad": {"QueueDepth", "BatchSize", "InFlightRequests"},
    }

    def setup_method(self):
        WORKSPACE.mkdir(exist_ok=True)
        summary = WORKSPACE / "algorithm_summary.json"
        if summary.exists():
            summary.unlink()

    def test_extracted_signals_match_golden_list(self):
        """Extract must produce exactly the manually-verified signal set."""
        code, output = run_cli("extract", str(ROUTING_DIR))
        assert code == 0, f"Extract failed: {output}"
        summary = json.loads((WORKSPACE / "algorithm_summary.json").read_text())
        extracted = {s["name"] for s in summary["signals"]}
        missing = self.EXPECTED_SIGNALS - extracted
        extra = extracted - self.EXPECTED_SIGNALS
        assert not missing, (
            f"Signals in golden list but NOT extracted: {missing}. "
            f"If the EVOLVE-BLOCK changed, update EXPECTED_SIGNALS after manual verification."
        )
        assert not extra, (
            f"Signals extracted but NOT in golden list: {extra}. "
            f"If these are real signals, add them to EXPECTED_SIGNALS after manual verification."
        )

    def test_composite_signals_match_golden_list(self):
        """F-25: Verify composite_signals array content."""
        code, output = run_cli("extract", str(ROUTING_DIR))
        assert code == 0, f"Extract failed: {output}"
        summary = json.loads((WORKSPACE / "algorithm_summary.json").read_text())
        composites = {c["name"]: set(c["constituents"]) for c in summary["composite_signals"]}
        assert composites == self.EXPECTED_COMPOSITES


class TestSourceSyncVerification:
    """Automated verification that hardcoded dicts match inference-sim source."""

    def test_ci_must_not_skip_sync_tests(self):
        """F-8: In CI, submodule MUST be checked out."""
        import os
        routing_go = REPO_ROOT / "inference-sim" / "sim" / "routing.go"
        if os.environ.get("CI"):
            assert routing_go.exists(), (
                "CI environment detected but inference-sim submodule not checked out."
            )
            assert routing_go.stat().st_size > 0

    def test_method_expansion_matches_source(self):
        """F-1: Verify EffectiveLoad() expansion matches inference-sim implementation."""
        routing_go = REPO_ROOT / "inference-sim" / "sim" / "routing.go"
        if not routing_go.exists():
            pytest.skip("inference-sim submodule not checked out")
        source = routing_go.read_text()
        import re
        match = re.search(
            r'func\s+\([^)]+\)\s+EffectiveLoad\(\)\s+\w+\s*\{([^}]+)\}', source
        )
        assert match is not None, "EffectiveLoad() method not found in routing.go"
        body = match.group(1)
        for field in ["QueueDepth", "BatchSize", "InFlightRequests"]:
            assert field in body, (
                f"METHOD_EXPANSIONS says EffectiveLoad includes {field}, "
                f"but {field} not found in EffectiveLoad() body"
            )

    def test_routing_snapshot_fields_match_source(self):
        """F-3: Verify ROUTING_SNAPSHOT_FIELDS matches RoutingSnapshot struct."""
        routing_go = REPO_ROOT / "inference-sim" / "sim" / "routing.go"
        if not routing_go.exists():
            pytest.skip("inference-sim submodule not checked out")
        source = routing_go.read_text()
        import re
        match = re.search(
            r'type\s+RoutingSnapshot\s+struct\s*\{(.*?)\}', source, re.DOTALL
        )
        assert match is not None, "RoutingSnapshot struct not found in routing.go"
        struct_body = match.group(1)
        # Parse Go struct fields: "FieldName Type" at start of line, ignoring comments
        struct_fields = set()
        for line in struct_body.split('\n'):
            line = line.strip()
            if not line or line.startswith('//'):
                continue
            # Remove inline comments
            if '//' in line:
                line = line[:line.index('//')]
            field_match = re.match(r'^(\w+)\s+\S+', line.strip())
            if field_match:
                struct_fields.add(field_match.group(1))
        from tools.transfer_cli import ROUTING_SNAPSHOT_FIELDS
        hardcoded = set(ROUTING_SNAPSHOT_FIELDS.keys())
        missing = struct_fields - hardcoded
        extra = hardcoded - struct_fields
        assert not missing, f"Fields in source but not in ROUTING_SNAPSHOT_FIELDS: {missing}"
        assert not extra, f"Fields in ROUTING_SNAPSHOT_FIELDS but not in source: {extra}"


class TestValidateMapping:
    def setup_method(self):
        WORKSPACE.mkdir(exist_ok=True)
        run_cli("extract", str(ROUTING_DIR))

    @pytest.mark.skipif(
        not (REPO_ROOT / "docs" / "transfer" / "blis_to_llmd_mapping.md").exists(),
        reason="Mapping artifact not yet created (expected in Task 5)"
    )
    def test_validate_mapping_passes_with_complete_mapping(self):
        """BC-3: all signals mapped, commit hash present."""
        code, output = run_cli("validate-mapping")
        assert code == 0, f"Expected pass, got: {output}"
        assert output["mapping_complete"] is True
        assert output["missing_signals"] == []

    def test_validate_mapping_reports_missing_artifact(self):
        """BC-9: missing mapping artifact exits with code 2."""
        import shutil
        mapping = REPO_ROOT / "docs" / "transfer" / "blis_to_llmd_mapping.md"
        backup = mapping.with_suffix(".md.bak")
        if mapping.exists():
            shutil.move(str(mapping), str(backup))
        try:
            code, output = run_cli("validate-mapping")
            assert code == 2, f"Missing mapping artifact should be exit 2, got {code}"
            assert output["status"] == "error"
        finally:
            if backup.exists():
                shutil.move(str(backup), str(mapping))

    def test_validate_mapping_without_summary_exits_2(self):
        """BC-9: missing algorithm summary exits with code 2."""
        summary = WORKSPACE / "algorithm_summary.json"
        backup = summary.with_suffix(".json.bak")
        if summary.exists():
            summary.rename(backup)
        try:
            code, output = run_cli("validate-mapping")
            assert code == 2
        finally:
            if backup.exists():
                backup.rename(summary)

    @pytest.mark.skipif(
        not (Path(__file__).parent.parent / "docs" / "transfer" / "blis_to_llmd_mapping.md").exists(),
        reason="Mapping artifact not present (pre-Task 5)"
    )
    def test_validate_mapping_rejects_placeholder_hash(self):
        """F-2: validate-mapping MUST reject the placeholder commit hash."""
        import shutil
        mapping = REPO_ROOT / "docs" / "transfer" / "blis_to_llmd_mapping.md"
        backup = mapping.with_suffix(".md.bak")
        if mapping.exists():
            shutil.copy2(str(mapping), str(backup))
        try:
            content = mapping.read_text()
            import re
            content = re.sub(
                r'(\*\*Pinned commit hash:\*\*\s*)[0-9a-f]{7,40}',
                r'\1PLACEHOLDER_REQUIRES_STEP_2',
                content,
            )
            mapping.write_text(content)
            code, output = run_cli("validate-mapping")
            assert code == 1, (
                f"validate-mapping should reject placeholder hash, got exit {code}: {output}."
            )
            assert output.get("stale_commit") is True
        finally:
            if backup.exists():
                shutil.move(str(backup), str(mapping))

    @pytest.mark.skipif(
        not (Path(__file__).parent.parent / "docs" / "transfer" / "blis_to_llmd_mapping.md").exists(),
        reason="Mapping artifact not present (pre-Task 5)"
    )
    def test_validate_mapping_detects_extra_signals(self):
        """F-3: validate-mapping detects signals in mapping that aren't in extract output."""
        import shutil
        mapping = REPO_ROOT / "docs" / "transfer" / "blis_to_llmd_mapping.md"
        backup = mapping.with_suffix(".md.bak")
        if mapping.exists():
            shutil.copy2(str(mapping), str(backup))
        try:
            content = mapping.read_text()
            content = content.replace(
                "| QueueDepth |",
                "| FakeSignal | int | `snap.FakeSignal` | N/A | N/A | low | 0 | Spurious test row |\n| QueueDepth |",
            )
            mapping.write_text(content)
            code, output = run_cli("validate-mapping")
            assert code == 1, f"Expected failure for extra signal, got: {output}"
            assert "FakeSignal" in str(output.get("extra_signals", []))
        finally:
            if backup.exists():
                shutil.move(str(backup), str(mapping))

    @pytest.mark.skipif(
        not (Path(__file__).parent.parent / "docs" / "transfer" / "blis_to_llmd_mapping.md").exists(),
        reason="Mapping artifact not present (pre-Task 5)"
    )
    def test_validate_mapping_detects_duplicate_signals(self):
        """F-19: validate-mapping detects duplicate signal rows."""
        import shutil
        mapping = REPO_ROOT / "docs" / "transfer" / "blis_to_llmd_mapping.md"
        backup = mapping.with_suffix(".md.bak")
        if mapping.exists():
            shutil.copy2(str(mapping), str(backup))
        try:
            content = mapping.read_text()
            content = content.replace(
                "| QueueDepth |",
                "| QueueDepth | int | `snap.QueueDepth` | duplicate | N/A | medium | 0 | Duplicate test row |\n| QueueDepth |",
            )
            mapping.write_text(content)
            code, output = run_cli("validate-mapping")
            assert code == 1, f"Expected failure for duplicate signal, got: {output}"
            assert any("duplicate" in e.lower() for e in output.get("errors", []))
            assert "QueueDepth" in output.get("duplicate_signals", []), (
                f"duplicate_signals structured field should contain 'QueueDepth': {output}"
            )
        finally:
            if backup.exists():
                shutil.move(str(backup), str(mapping))


class TestValidateSchema:
    def setup_method(self):
        WORKSPACE.mkdir(exist_ok=True)
        run_cli("extract", str(ROUTING_DIR))

    def test_validate_schema_passes_on_valid_summary(self):
        """BC-4: validate-schema passes on extract output."""
        code, output = run_cli("validate-schema", str(WORKSPACE / "algorithm_summary.json"))
        assert code == 0, f"Expected pass: {output}"
        assert output["status"] == "ok"
        assert output["violations"] == []

    def test_validate_schema_fails_on_missing_file(self):
        """BC-10: missing artifact exits with code 2."""
        code, output = run_cli("validate-schema", str(WORKSPACE / "nonexistent.json"))
        assert code == 2

    def test_validate_schema_fails_on_invalid_artifact(self):
        """BC-4: invalid artifact reports violations."""
        real = WORKSPACE / "algorithm_summary.json"
        backup = real.read_text()
        real.write_text(json.dumps({"algorithm_name": 123}))
        try:
            code, output = run_cli("validate-schema", str(real))
            assert code == 1
            assert len(output["violations"]) > 0
        finally:
            real.write_text(backup)


class TestCompositeSignalConsistency:
    """Cross-validate METHOD_EXPANSIONS against the mapping artifact."""

    def test_method_expansions_match_mapping_composite_table(self):
        from tools.transfer_cli import METHOD_EXPANSIONS
        mapping = REPO_ROOT / "docs" / "transfer" / "blis_to_llmd_mapping.md"
        if not mapping.exists():
            pytest.skip("Mapping artifact not yet created")
        content = mapping.read_text()
        for method, fields in METHOD_EXPANSIONS.items():
            assert method in content, (
                f"METHOD_EXPANSIONS has '{method}' but it's not in the mapping artifact"
            )
            expansion_str = " + ".join(fields)
            assert expansion_str in content or all(f in content for f in fields)


class TestRoundTrip:
    """F-17: Explicit round-trip test: extract -> validate-schema on the output."""

    def setup_method(self):
        WORKSPACE.mkdir(exist_ok=True)
        summary = WORKSPACE / "algorithm_summary.json"
        if summary.exists():
            summary.unlink()

    def test_extract_then_validate_schema_round_trip(self):
        """Extract produces an artifact that passes schema validation."""
        extract_code, extract_output = run_cli("extract", str(ROUTING_DIR))
        assert extract_code == 0, f"Extract failed: {extract_output}"
        validate_code, validate_output = run_cli(
            "validate-schema", str(WORKSPACE / "algorithm_summary.json")
        )
        assert validate_code == 0, (
            f"validate-schema failed on extract output: {validate_output}"
        )
        assert validate_output["violations"] == []


class TestHashDriftDetection:
    """BC-11: Verify content hash mechanism detects EVOLVE-BLOCK modifications."""

    def setup_method(self):
        WORKSPACE.mkdir(exist_ok=True)
        summary = WORKSPACE / "algorithm_summary.json"
        if summary.exists():
            summary.unlink()

    def test_hash_detects_source_modification(self):
        """BC-11: Modified EVOLVE-BLOCK produces different content hash."""
        import tempfile, shutil
        code1, _ = run_cli("extract", str(ROUTING_DIR))
        assert code1 == 0
        summary1 = json.loads((WORKSPACE / "algorithm_summary.json").read_text())
        hash1 = summary1["evolve_block_content_hash"]

        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            shutil.copy2(str(ROUTING_DIR / "best_program.py"), str(tmpdir / "best_program.py"))
            shutil.copy2(str(ROUTING_DIR / "best_program_info.json"), str(tmpdir / "best_program_info.json"))
            src = (tmpdir / "best_program.py").read_text()
            src = src.replace(
                "// EVOLVE-BLOCK-START",
                "// EVOLVE-BLOCK-START\n\t// BC-11 drift detection test modification",
            )
            (tmpdir / "best_program.py").write_text(src)
            code2, _ = run_cli("extract", str(tmpdir))
            assert code2 == 0
            summary2 = json.loads((WORKSPACE / "algorithm_summary.json").read_text())
            hash2 = summary2["evolve_block_content_hash"]

        assert hash1 != hash2


class TestUnknownSignalDetection:
    """F-23: Verify unrecognized field accesses produce 'unknown' type signals."""

    def setup_method(self):
        WORKSPACE.mkdir(exist_ok=True)
        summary = WORKSPACE / "algorithm_summary.json"
        if summary.exists():
            summary.unlink()

    def test_unknown_field_access_produces_unknown_type(self):
        import tempfile, shutil
        mapping = REPO_ROOT / "docs" / "transfer" / "blis_to_llmd_mapping.md"
        backup = mapping.with_suffix(".md.bak")
        # Temporarily hide mapping so fidelity check doesn't reject the unknown signal
        if mapping.exists():
            shutil.move(str(mapping), str(backup))
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                tmpdir = Path(tmpdir)
                shutil.copy2(str(ROUTING_DIR / "best_program.py"), str(tmpdir / "best_program.py"))
                shutil.copy2(str(ROUTING_DIR / "best_program_info.json"), str(tmpdir / "best_program_info.json"))
                src = (tmpdir / "best_program.py").read_text()
                src = src.replace(
                    "// EVOLVE-BLOCK-START",
                    "// EVOLVE-BLOCK-START\n\tunknown_val = snap.NovelMetricXYZ",
                )
                (tmpdir / "best_program.py").write_text(src)
                env = {k: v for k, v in os.environ.items() if k != "CI"}
                result = subprocess.run(
                    [sys.executable, str(CLI), "extract", str(tmpdir)],
                    capture_output=True, text=True, cwd=str(REPO_ROOT),
                    env=env,
                )
                assert result.returncode == 0
                summary = json.loads((WORKSPACE / "algorithm_summary.json").read_text())
                unknown_signals = [s for s in summary["signals"] if s["type"] == "unknown"]
                assert len(unknown_signals) > 0
                assert any(s["name"] == "NovelMetricXYZ" for s in unknown_signals)
                assert "NovelMetricXYZ" in result.stderr
        finally:
            if backup.exists():
                shutil.move(str(backup), str(mapping))


class TestExtractDeterminism:
    """F-22: Verify extract produces identical output for identical input."""

    def setup_method(self):
        WORKSPACE.mkdir(exist_ok=True)
        summary = WORKSPACE / "algorithm_summary.json"
        if summary.exists():
            summary.unlink()

    def test_extract_is_deterministic(self):
        code1, _ = run_cli("extract", str(ROUTING_DIR))
        assert code1 == 0
        output1 = (WORKSPACE / "algorithm_summary.json").read_text()

        code2, _ = run_cli("extract", str(ROUTING_DIR))
        assert code2 == 0
        output2 = (WORKSPACE / "algorithm_summary.json").read_text()

        assert output1 == output2


class TestFidelityHalt:
    """BC-6: Low-fidelity signal halts pipeline."""

    def setup_method(self):
        WORKSPACE.mkdir(exist_ok=True)
        summary = WORKSPACE / "algorithm_summary.json"
        if summary.exists():
            summary.unlink()

    @pytest.mark.skipif(
        not (Path(__file__).parent.parent / "docs" / "transfer" / "blis_to_llmd_mapping.md").exists(),
        reason="Mapping artifact not present (pre-Task 5)"
    )
    def test_low_fidelity_signal_halts_extract(self):
        """BC-6: extract exits 1 when mapping has a low-fidelity signal."""
        import shutil
        mapping = REPO_ROOT / "docs" / "transfer" / "blis_to_llmd_mapping.md"
        backup = mapping.with_suffix(".md.bak")
        if mapping.exists():
            shutil.copy2(str(mapping), str(backup))
        try:
            code_setup, _ = run_cli("extract", str(ROUTING_DIR))
            summary_path = WORKSPACE / "algorithm_summary.json"
            assert code_setup == 0 and summary_path.exists()
            content = mapping.read_text()
            import re
            new_content = re.sub(
                r'(\|\s*QueueDepth\s*\|[^|]*\|[^|]*\|[^|]*\|[^|]*\|)\s*high\s*(\|)',
                r'\1 low \2',
                content,
                count=1,
            )
            assert new_content != content
            mapping.write_text(new_content)
            summary_path.unlink()
            code, output = run_cli("extract", str(ROUTING_DIR))
            assert code == 1, f"Expected exit 1 for low-fidelity, got {code}: {output}"
            assert output["status"] == "error"
            assert any("low fidelity" in e.lower() for e in output["errors"])
            assert not summary_path.exists()
        finally:
            if backup.exists():
                shutil.move(str(backup), str(mapping))

    def test_medium_fidelity_signal_does_not_halt(self):
        """BC-6 negative: medium fidelity does not halt."""
        code, output = run_cli("extract", str(ROUTING_DIR))
        assert code == 0, f"Medium fidelity should not halt: {output}"

    @pytest.mark.skipif(
        not (Path(__file__).parent.parent / "docs" / "transfer" / "blis_to_llmd_mapping.md").exists(),
        reason="Mapping artifact not present (pre-Task 5)"
    )
    def test_provisional_detection_matches_mapping_format(self):
        """R2-F-13: Verify *(zeroed ...)* annotation detection works against actual mapping."""
        mapping = REPO_ROOT / "docs" / "transfer" / "blis_to_llmd_mapping.md"
        if not mapping.exists():
            pytest.skip("Mapping artifact not present")
        content = mapping.read_text()
        assert "*(zeroed" in content, (
            "Mapping should contain at least one *(zeroed ...)* annotation "
            "(CacheHitRate and BatchSize are zeroed in PR5)"
        )
        code, output = run_cli("extract", str(ROUTING_DIR))
        assert code == 0
        summary = json.loads((WORKSPACE / "algorithm_summary.json").read_text())
        cache_hit = [s for s in summary["signals"] if s["name"] == "CacheHitRate"]
        assert len(cache_hit) == 1
        assert cache_hit[0].get("fidelity_zeroed") is True

    @pytest.mark.skipif(
        not (Path(__file__).parent.parent / "docs" / "transfer" / "blis_to_llmd_mapping.md").exists(),
        reason="Mapping artifact not present (pre-Task 5)"
    )
    def test_fidelity_fallback_pattern_matches_additional_signals(self):
        """R5-F-11: Verify fallback pattern matches SessionID in Additional Signals table."""
        mapping = REPO_ROOT / "docs" / "transfer" / "blis_to_llmd_mapping.md"
        if not mapping.exists():
            pytest.skip("Mapping artifact not present")
        content = mapping.read_text()
        import re
        pattern_alt = r'\|\s*SessionID(?:\s*\([^)]*\))?\s*\|(?:[^|]*\|){2}\s*(low|medium|high)\s*(?:\*\(provisional\)\*)?\s*\|'
        match = re.search(pattern_alt, content, re.IGNORECASE)
        assert match is not None
        assert match.group(1).lower() == "high"


class TestCIStrictEnforcement:
    """F-1: Enforce --strict in CI."""

    def setup_method(self):
        WORKSPACE.mkdir(exist_ok=True)
        summary = WORKSPACE / "algorithm_summary.json"
        if summary.exists():
            summary.unlink()

    def test_ci_env_requires_strict_flag(self):
        """F-1: In CI, extract without --strict FAILS with exit 1."""
        import os
        result = subprocess.run(
            [sys.executable, str(CLI), "extract", str(ROUTING_DIR)],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
            env={**os.environ, "CI": "true"},
        )
        assert result.returncode == 1
        stdout = json.loads(result.stdout)
        assert stdout["status"] == "error"
        assert any("strict" in e.lower() for e in stdout.get("errors", []))

    def test_ci_false_does_not_enforce_strict(self):
        """F-9: CI='false' should NOT trigger --strict enforcement."""
        import os
        env = {**os.environ, "CI": "false"}
        result = subprocess.run(
            [sys.executable, str(CLI), "extract", str(ROUTING_DIR)],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
            env=env,
        )
        assert result.returncode == 0, (
            f"CI='false' should not enforce --strict. Got exit {result.returncode}."
        )

    def test_ci_env_with_strict_no_warning(self):
        """F-1: In CI with --strict, no warning about missing --strict."""
        import os
        result = subprocess.run(
            [sys.executable, str(CLI), "extract", "--strict", str(ROUTING_DIR)],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
            env={**os.environ, "CI": "true"},
        )
        assert result.returncode == 0
        assert "strict" not in result.stderr.lower(), (
            f"Unexpected 'strict' warning in stderr on successful --strict run: {result.stderr}"
        )


class TestValidateMappingEdgeCases:
    """Defense-in-depth tests for validate-mapping error paths."""

    def setup_method(self):
        WORKSPACE.mkdir(exist_ok=True)
        # Ensure a valid summary exists for validate-mapping to consume
        run_cli("extract", str(ROUTING_DIR))

    @pytest.mark.skipif(
        not (Path(__file__).parent.parent / "docs" / "transfer" / "blis_to_llmd_mapping.md").exists(),
        reason="Mapping artifact not present (pre-Task 5)"
    )
    def test_validate_mapping_malformed_no_table(self):
        """Malformed mapping artifact with no Markdown table should exit 1."""
        import shutil
        mapping = REPO_ROOT / "docs" / "transfer" / "blis_to_llmd_mapping.md"
        backup = mapping.with_suffix(".md.bak")
        shutil.copy2(str(mapping), str(backup))
        try:
            mapping.write_text("This is a plain text file with no pipe-delimited table.\n")
            code, output = run_cli("validate-mapping")
            assert code == 1, f"Expected exit 1 for malformed mapping, got {code}: {output}"
            assert any("malformed" in e.lower() or "no markdown table" in e.lower()
                       for e in output.get("errors", []))
        finally:
            shutil.move(str(backup), str(mapping))

    def test_validate_mapping_path_traversal_rejected(self):
        """Summary path outside repo root should exit 2."""
        code, output = run_cli("validate-mapping", "--summary", "/etc/passwd")
        assert code == 2, f"Expected exit 2 for path traversal, got {code}: {output}"
        assert output["status"] == "error"


class TestValidateSchemaEdgeCases:
    """Defense-in-depth tests for validate-schema error paths."""

    def test_validate_schema_path_traversal_rejected(self):
        """Artifact path outside repo root should exit 2."""
        code, output = run_cli("validate-schema", "/etc/passwd")
        assert code == 2, f"Expected exit 2 for path traversal, got {code}: {output}"
        assert output["status"] == "error"


class TestInfraFidelityPaths:
    """F-3: Test INFRA exit-code-2 paths in _check_fidelity and INFRA: prefix dispatch."""

    def setup_method(self):
        WORKSPACE.mkdir(exist_ok=True)
        summary = WORKSPACE / "algorithm_summary.json"
        if summary.exists():
            summary.unlink()

    @pytest.mark.skipif(
        not (Path(__file__).parent.parent / "docs" / "transfer" / "blis_to_llmd_mapping.md").exists(),
        reason="Mapping artifact not present (pre-Task 5)"
    )
    def test_oversized_mapping_file_exits_2(self):
        """INFRA: Oversized mapping file triggers exit code 2 via INFRA: prefix."""
        import shutil
        mapping = REPO_ROOT / "docs" / "transfer" / "blis_to_llmd_mapping.md"
        backup = mapping.with_suffix(".md.bak")
        shutil.copy2(str(mapping), str(backup))
        try:
            # Write a file > 10 MB to trigger the size guard
            mapping.write_text("x" * (10 * 1024 * 1024 + 1))
            code, output = run_cli("extract", str(ROUTING_DIR))
            assert code == 2, f"Oversized mapping should exit 2, got {code}: {output}"
            assert output["status"] == "error"
            assert any("INFRA" in e or "exceeds" in e.lower() for e in output.get("errors", []))
        finally:
            shutil.move(str(backup), str(mapping))

    @pytest.mark.skipif(
        not (Path(__file__).parent.parent / "docs" / "transfer" / "blis_to_llmd_mapping.md").exists(),
        reason="Mapping artifact not present (pre-Task 5)"
    )
    def test_unreadable_mapping_file_exits_2(self):
        """INFRA: Unreadable mapping file triggers exit code 2 via INFRA: prefix."""
        import shutil, stat
        mapping = REPO_ROOT / "docs" / "transfer" / "blis_to_llmd_mapping.md"
        backup = mapping.with_suffix(".md.bak")
        shutil.copy2(str(mapping), str(backup))
        original_mode = mapping.stat().st_mode
        try:
            # Remove read permissions
            mapping.chmod(0o000)
            code, output = run_cli("extract", str(ROUTING_DIR))
            assert code == 2, f"Unreadable mapping should exit 2, got {code}: {output}"
            assert output["status"] == "error"
            assert any("INFRA" in e or "failed to read" in e.lower() for e in output.get("errors", []))
        finally:
            mapping.chmod(original_mode)
            shutil.move(str(backup), str(mapping))

    def test_infra_prefix_produces_exit_code_2(self):
        """Verify the INFRA: prefix dispatch logic at line 414 produces exit 2."""
        from tools.transfer_cli import _check_fidelity, MAPPING_PATH
        import shutil
        if not MAPPING_PATH.exists():
            pytest.skip("Mapping artifact not present")
        backup = MAPPING_PATH.with_suffix(".md.bak")
        shutil.copy2(str(MAPPING_PATH), str(backup))
        try:
            # Write oversized file to trigger INFRA: error
            MAPPING_PATH.write_text("x" * (10 * 1024 * 1024 + 1))
            ok, errors = _check_fidelity([{"name": "QueueDepth", "type": "int"}])
            assert not ok
            assert any(e.startswith("INFRA:") for e in errors), (
                f"Expected INFRA: prefix in errors, got: {errors}"
            )
        finally:
            shutil.move(str(backup), str(MAPPING_PATH))


class TestMetricsTypeGuard:
    """F-1: Verify non-dict metrics value does not crash the CLI."""

    def setup_method(self):
        WORKSPACE.mkdir(exist_ok=True)
        summary = WORKSPACE / "algorithm_summary.json"
        if summary.exists():
            summary.unlink()

    def test_metrics_integer_value_does_not_crash(self):
        """F-1: metrics=42 in best_program_info.json should not raise TypeError."""
        import tempfile, shutil
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            shutil.copy2(str(ROUTING_DIR / "best_program.py"), str(tmpdir / "best_program.py"))
            (tmpdir / "best_program_info.json").write_text('{"metrics": 42}')
            env = {k: v for k, v in os.environ.items() if k != "CI"}
            result = subprocess.run(
                [sys.executable, str(CLI), "extract", str(tmpdir)],
                capture_output=True, text=True, cwd=str(REPO_ROOT), env=env,
            )
            # Should produce valid JSON (not crash with TypeError)
            stdout = json.loads(result.stdout)
            assert "status" in stdout, f"CLI should produce JSON output, got: {result.stdout}"
            # Should warn about missing combined_score
            assert "metrics" in result.stderr.lower() or "warning" in result.stderr.lower()

    def test_metrics_list_value_does_not_crash(self):
        """F-1: metrics=[1,2,3] in best_program_info.json should not raise TypeError."""
        import tempfile, shutil
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            shutil.copy2(str(ROUTING_DIR / "best_program.py"), str(tmpdir / "best_program.py"))
            (tmpdir / "best_program_info.json").write_text('{"metrics": [1, 2, 3]}')
            env = {k: v for k, v in os.environ.items() if k != "CI"}
            result = subprocess.run(
                [sys.executable, str(CLI), "extract", str(tmpdir)],
                capture_output=True, text=True, cwd=str(REPO_ROOT), env=env,
            )
            stdout = json.loads(result.stdout)
            assert "status" in stdout


def test_noise_characterize_halts_on_high_cv(tmp_path):
    """BC-11: CV > 15% causes halt=true and exit code 1."""
    runs = {"runs": [{"p99": v} for v in [0.40, 0.80, 0.20, 0.60, 0.30]]}  # CV ≈ 0.47
    runs_file = tmp_path / "baseline_runs.json"
    runs_file.write_text(json.dumps(runs))

    env = {**os.environ, "_SIM2REAL_ALLOWED_ROOT": str(tmp_path)}
    result = subprocess.run(
        [sys.executable, "tools/transfer_cli.py", "noise-characterize", "--runs", str(runs_file)],
        capture_output=True, text=True, env=env
    )
    assert result.returncode == 1, f"Expected exit 1, got {result.returncode}"
    output = json.loads(result.stdout)
    assert output["halt"] is True
    assert output["status"] == "error"


def test_noise_characterize_malformed_input(tmp_path):
    """BC-16: malformed JSON input causes exit code 2 (infrastructure error)."""
    runs_file = tmp_path / "bad_runs.json"
    runs_file.write_text("{invalid json")

    env = {**os.environ, "_SIM2REAL_ALLOWED_ROOT": str(tmp_path)}
    result = subprocess.run(
        [sys.executable, "tools/transfer_cli.py", "noise-characterize", "--runs", str(runs_file)],
        capture_output=True, text=True, env=env
    )
    assert result.returncode == 2, f"Expected exit 2, got {result.returncode}"
    output = json.loads(result.stdout)
    assert output["status"] == "error"


def test_noise_characterize_empty_runs(tmp_path):
    """BC-16: empty runs list is infrastructure error (no data to compute CV) — exit code 2."""
    runs_file = tmp_path / "empty_runs.json"
    runs_file.write_text('{"runs": []}')

    env = {**os.environ, "_SIM2REAL_ALLOWED_ROOT": str(tmp_path)}
    result = subprocess.run(
        [sys.executable, "tools/transfer_cli.py", "noise-characterize", "--runs", str(runs_file)],
        capture_output=True, text=True, env=env
    )
    assert result.returncode == 2, f"Expected exit 2, got {result.returncode}"
    output = json.loads(result.stdout)
    assert output["status"] == "error"


def test_noise_characterize_t_eff_computation(tmp_path):
    """BC-12: T_eff = max(0.05, 2*max_cv) using sample std (Bessel's correction)."""
    runs = {"runs": [{"p99": v} for v in [0.40, 0.42, 0.41, 0.39, 0.41]]}
    runs_file = tmp_path / "baseline_runs.json"
    runs_file.write_text(json.dumps(runs))

    env = {**os.environ, "_SIM2REAL_ALLOWED_ROOT": str(tmp_path)}
    result = subprocess.run(
        [sys.executable, "tools/transfer_cli.py", "noise-characterize", "--runs", str(runs_file)],
        capture_output=True, text=True, env=env
    )
    assert result.returncode == 0, f"Expected exit 0, got {result.returncode}: {result.stdout}"
    output = json.loads(result.stdout)
    assert output["halt"] is False
    assert output["status"] == "ok"
    assert "per_metric_cv" in output, "missing per_metric_cv in output"
    assert "p99" in output["per_metric_cv"], "missing p99 in per_metric_cv"
    expected_cv = 0.02809
    assert abs(output["per_metric_cv"]["p99"] - expected_cv) < 0.002, \
        f"Expected p99 CV ≈ {expected_cv}, got {output['per_metric_cv']['p99']}"
    expected_t_eff = max(0.05, 2 * expected_cv)
    assert abs(output["t_eff"] - expected_t_eff) < 0.002, \
        f"Expected T_eff ≈ {expected_t_eff}, got {output['t_eff']}"


def test_benchmark_mechanism_check_pass(tmp_path):
    """BC-13: improvement >= T_eff for matched workload → PASS."""
    results = {
        "workloads": [
            {"name": "chatbot", "classification": "matched",
             "baseline_p99": 0.45, "transfer_p99": 0.38},  # improvement ≈ 15.6%
            {"name": "batch",   "classification": "unmatched",
             "baseline_p99": 0.30, "transfer_p99": 0.31},  # change ≈ -3.3%
        ]
    }
    results_file = tmp_path / "results.json"
    results_file.write_text(json.dumps(results))

    env = {**os.environ, "_SIM2REAL_ALLOWED_ROOT": str(tmp_path)}
    result = subprocess.run(
        [sys.executable, "tools/transfer_cli.py", "benchmark",
         "--results", str(results_file), "--t-eff", "0.10"],
        capture_output=True, text=True, env=env
    )
    assert result.returncode == 0
    output = json.loads(result.stdout)
    assert output["mechanism_check_verdict"] == "PASS"
    assert output["status"] == "ok"


def test_benchmark_mechanism_check_inconclusive(tmp_path):
    """Improvement > 0 but < T_eff for all matched workloads → INCONCLUSIVE (exit 0)."""
    results = {
        "workloads": [
            {"name": "chatbot", "classification": "matched",
             "baseline_p99": 0.45, "transfer_p99": 0.44},  # improvement ≈ 2.2% < T_eff=0.10
        ]
    }
    results_file = tmp_path / "results.json"
    results_file.write_text(json.dumps(results))

    env = {**os.environ, "_SIM2REAL_ALLOWED_ROOT": str(tmp_path)}
    result = subprocess.run(
        [sys.executable, "tools/transfer_cli.py", "benchmark",
         "--results", str(results_file), "--t-eff", "0.10"],
        capture_output=True, text=True, env=env
    )
    assert result.returncode == 0, f"INCONCLUSIVE should exit 0, got {result.returncode}"
    output = json.loads(result.stdout)
    assert output["mechanism_check_verdict"] == "INCONCLUSIVE"
    assert output["status"] == "inconclusive", \
        f"INCONCLUSIVE should have status='inconclusive', got '{output['status']}'"


def test_benchmark_mechanism_check_fail(tmp_path):
    """All matched workload improvements <= 0 → FAIL (exit 1)."""
    results = {
        "workloads": [
            {"name": "chatbot", "classification": "matched",
             "baseline_p99": 0.45, "transfer_p99": 0.50},  # regression
        ]
    }
    results_file = tmp_path / "results.json"
    results_file.write_text(json.dumps(results))

    env = {**os.environ, "_SIM2REAL_ALLOWED_ROOT": str(tmp_path)}
    result = subprocess.run(
        [sys.executable, "tools/transfer_cli.py", "benchmark",
         "--results", str(results_file), "--t-eff", "0.10"],
        capture_output=True, text=True, env=env
    )
    assert result.returncode == 1, f"FAIL should exit 1, got {result.returncode}"
    output = json.loads(result.stdout)
    assert output["mechanism_check_verdict"] == "FAIL"


def test_benchmark_requires_t_eff(tmp_path):
    """BC-17: missing --t-eff → exit 2 (infrastructure error, not FAIL verdict)."""
    results_file = tmp_path / "results.json"
    results_file.write_text('{"workloads": []}')

    env = {**os.environ, "_SIM2REAL_ALLOWED_ROOT": str(tmp_path)}
    result = subprocess.run(
        [sys.executable, "tools/transfer_cli.py", "benchmark",
         "--results", str(results_file)],  # no --t-eff
        capture_output=True, text=True, env=env
    )
    assert result.returncode == 2
    output = json.loads(result.stdout)
    assert "t-eff" in output["errors"][0].lower()


def test_benchmark_malformed_input(tmp_path):
    """BC-18: malformed JSON input causes exit code 2 (infrastructure error)."""
    results_file = tmp_path / "bad_results.json"
    results_file.write_text("{invalid json")

    env = {**os.environ, "_SIM2REAL_ALLOWED_ROOT": str(tmp_path)}
    result = subprocess.run(
        [sys.executable, "tools/transfer_cli.py", "benchmark",
         "--results", str(results_file), "--t-eff", "0.10"],
        capture_output=True, text=True, env=env
    )
    assert result.returncode == 2, f"Expected exit 2, got {result.returncode}"
    output = json.loads(result.stdout)
    assert output["status"] == "error"


def test_benchmark_missing_workloads_key(tmp_path):
    """BC-18: valid JSON missing 'workloads' key causes exit code 2."""
    results_file = tmp_path / "no_workloads.json"
    results_file.write_text('{"other_key": 123}')

    env = {**os.environ, "_SIM2REAL_ALLOWED_ROOT": str(tmp_path)}
    result = subprocess.run(
        [sys.executable, "tools/transfer_cli.py", "benchmark",
         "--results", str(results_file), "--t-eff", "0.10"],
        capture_output=True, text=True, env=env
    )
    assert result.returncode == 2, f"Expected exit 2, got {result.returncode}"
    output = json.loads(result.stdout)
    assert output["status"] == "error"


def test_benchmark_no_matched_workloads(tmp_path):
    """S5: all workloads have classification='unmatched' (key present) → exit 2, 'no matched workloads'."""
    results = {
        "workloads": [
            {"name": "batch1", "classification": "unmatched",
             "baseline_p99": 0.40, "transfer_p99": 0.41},
            {"name": "batch2", "classification": "unmatched",
             "baseline_p99": 0.35, "transfer_p99": 0.34},
        ]
    }
    results_file = tmp_path / "results.json"
    results_file.write_text(json.dumps(results))

    env = {**os.environ, "_SIM2REAL_ALLOWED_ROOT": str(tmp_path)}
    result = subprocess.run(
        [sys.executable, "tools/transfer_cli.py", "benchmark",
         "--results", str(results_file), "--t-eff", "0.10"],
        capture_output=True, text=True, env=env
    )
    assert result.returncode == 2, f"Expected exit 2, got {result.returncode}"
    output = json.loads(result.stdout)
    assert any("no matched workloads" in e.lower() for e in output.get("errors", [])), \
        f"Expected 'no matched workloads' in errors, got: {output.get('errors')}"


class TestValidateSchemaValidationResults:
    """BC-1 schema roundtrip tests for validation_results.json."""

    def _make_valid_fixture(self):
        return {
            "suite_a": {
                "passed": True,
                "kendall_tau": 0.85,
                "max_abs_error": 0.02,
                "tuple_count": 100
            },
            "suite_b": {
                "passed": True,
                "rank_stability_tau": 0.90,
                "threshold_crossing_pct": 5.0,
                "informational_only": True
            },
            "suite_c": {
                "passed": True,
                "deterministic": True,
                "max_pile_on_ratio": 1.2
            },
            "benchmark": {
                "passed": True,
                "mechanism_check_verdict": "PASS",
                "t_eff": 0.10
            },
            "overall_verdict": "PASS",
            "noise_cv": 0.05
        }

    def test_valid_validation_results_passes(self):
        """BC-1: minimal valid validation_results.json passes schema validation (exit 0)."""
        WORKSPACE.mkdir(exist_ok=True)
        artifact_path = WORKSPACE / "validation_results.json"
        try:
            artifact_path.write_text(json.dumps(self._make_valid_fixture()))
            code, output = run_cli("validate-schema", str(artifact_path))
            assert code == 0, f"Expected exit 0, got {code}: {output}"
        finally:
            if artifact_path.exists():
                artifact_path.unlink()

    def test_missing_required_field_fails(self):
        """BC-1: validation_results.json missing 'overall_verdict' fails schema validation (exit 1)."""
        WORKSPACE.mkdir(exist_ok=True)
        artifact_path = WORKSPACE / "validation_results.json"
        try:
            fixture = self._make_valid_fixture()
            del fixture["overall_verdict"]
            artifact_path.write_text(json.dumps(fixture))
            code, output = run_cli("validate-schema", str(artifact_path))
            assert code == 1, f"Expected exit 1, got {code}: {output}"
        finally:
            if artifact_path.exists():
                artifact_path.unlink()
