# tools/test_transfer_cli.py
import json
import os
import subprocess
import sys
import pytest
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
CLI = REPO_ROOT / "tools" / "transfer_cli.py"
ROUTING_DIR = REPO_ROOT / "blis_router" / "best"
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
        assert "blis_router/best/best_program.go" in summary["evolve_block_source"], (
            f"evolve_block_source should reference blis_router/best/best_program.go, "
            f"got: {summary['evolve_block_source']!r}"
        )
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
        assert "InFlightRequests" in signal_names

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
        source = (ROUTING_DIR / "best_program.go").read_text()
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
            shutil.copy2(str(ROUTING_DIR / "best_program.go"), str(tmpdir / "best_program.go"))
            shutil.copy2(str(ROUTING_DIR / "best_program_info.json"), str(tmpdir / "best_program_info.json"))
            src = (tmpdir / "best_program.go").read_text()
            src = src.replace(
                "// EVOLVE-BLOCK-START",
                "// EVOLVE-BLOCK-START\n\tPrefillInstance disaggregation check",
            )
            (tmpdir / "best_program.go").write_text(src)
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
            (tmpdir / "best_program.go").write_text(
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
            (tmpdir / "best_program.go").write_text(
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
            (tmpdir / "best_program.go").write_text(
                '# EVOLVE-BLOCK-START\n'
                'snap.InFlightRequests\n'
                '# EVOLVE-BLOCK-END\n'
                '# EVOLVE-BLOCK-START\n'
                'snap.KVUtilization\n'
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
            assert "InFlightRequests" in signal_names
            assert "WARNING" in result.stderr
            assert "2" in result.stderr

    def test_extract_few_signals_strict_exits_1(self):
        """F-9: 1 signal in --strict mode should exit 1."""
        import tempfile, shutil
        summary_path = WORKSPACE / "algorithm_summary.json"
        if summary_path.exists():
            summary_path.unlink()
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            shutil.copy2(str(ROUTING_DIR / "best_program_info.json"), str(tmpdir / "best_program_info.json"))
            (tmpdir / "best_program.go").write_text(
                '# EVOLVE-BLOCK-START\n'
                'func route(snap RoutingSnapshot) {\n'
                '    x := snap.QueueDepth\n'
                '}\n'
                '# EVOLVE-BLOCK-END\n'
            )
            code, output = run_cli("extract", "--strict", str(tmpdir))
            assert code == 1, (
                f"--strict with {output.get('signal_count', '?')} signals "
                f"(< MINIMUM_EXPECTED_SIGNALS=2) should exit 1, got {code}: {output}"
            )
            assert output["status"] == "error"
            error_text = " ".join(output.get("errors", []))
            assert "signal" in error_text.lower() and ("expected" in error_text.lower() or "minimum" in error_text.lower())
            assert not summary_path.exists(), "Strict-mode minimum-signal failure must not write artifact"

    def test_extract_few_signals_boundary_1_fails(self):
        """R3-F-15: Exactly 1 signal (< MINIMUM_EXPECTED_SIGNALS=2) should exit 1 in --strict."""
        import tempfile, shutil
        summary_path = WORKSPACE / "algorithm_summary.json"
        if summary_path.exists():
            summary_path.unlink()
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            shutil.copy2(str(ROUTING_DIR / "best_program_info.json"), str(tmpdir / "best_program_info.json"))
            (tmpdir / "best_program.go").write_text(
                '# EVOLVE-BLOCK-START\n'
                'func route(snap RoutingSnapshot) {\n'
                '    x := snap.QueueDepth\n'
                '}\n'
                '# EVOLVE-BLOCK-END\n'
            )
            code, output = run_cli("extract", "--strict", str(tmpdir))
            assert code == 1, (
                f"--strict with 1 signal (< MINIMUM_EXPECTED_SIGNALS=2) "
                f"should exit 1, got {code}: {output}"
            )
            assert not summary_path.exists(), "Strict-mode minimum-signal failure must not write artifact"

    def test_extract_few_signals_boundary_2_passes_threshold(self):
        """R3-F-15: Exactly 2 signals (= MINIMUM_EXPECTED_SIGNALS=2) should pass threshold in --strict."""
        import tempfile, shutil
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            shutil.copy2(str(ROUTING_DIR / "best_program_info.json"), str(tmpdir / "best_program_info.json"))
            (tmpdir / "best_program.go").write_text(
                '# EVOLVE-BLOCK-START\n'
                'func route(snap RoutingSnapshot) {\n'
                '    x := snap.InFlightRequests\n'
                '    y := snap.KVUtilization\n'
                '}\n'
                '# EVOLVE-BLOCK-END\n'
            )
            code, output = run_cli("extract", "--strict", str(tmpdir))
            assert code == 0, (
                f"2 signals should pass the MINIMUM_EXPECTED_SIGNALS threshold, "
                f"but got exit code {code}: {output.get('errors', [])}"
            )

    def test_extract_missing_info_json_exits_2(self):
        """F-9: best_program_info.json not existing should exit 2 (infra error)."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            (tmpdir / "best_program.go").write_text(
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

    def test_extract_missing_go_file_exits_2(self, tmp_path):
        """BC-11: extract exits 2 when best_program.go is absent from the routing dir."""
        # Provide best_program_info.json but NOT best_program.go
        info = tmp_path / "best_program_info.json"
        info.write_text('{"language": "go", "metrics": {}}')
        env = {k: v for k, v in os.environ.items() if k != "CI"}
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "tools" / "transfer_cli.py"), "extract", str(tmp_path)],
            capture_output=True, text=True, env=env,
        )
        assert result.returncode == 2, f"expected exit 2, got {result.returncode}"
        assert "best_program.go not found" in result.stdout or "best_program.go not found" in result.stderr, \
            f"expected 'best_program.go not found' in output; stdout={result.stdout!r}"

    def test_extract_malformed_info_json_exits_2(self):
        """Malformed best_program_info.json (non-JSON) should exit 2."""
        import tempfile, shutil
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            shutil.copy2(str(ROUTING_DIR / "best_program.go"), str(tmpdir / "best_program.go"))
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
            (tmpdir / "best_program.go").write_text(
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
            (tmpdir / "best_program.go").write_text(
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
            (tmpdir / "best_program.go").write_text(
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
            shutil.copy2(str(ROUTING_DIR / "best_program.go"), str(tmpdir / "best_program.go"))
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
            shutil.copy2(str(ROUTING_DIR / "best_program.go"), str(tmpdir / "best_program.go"))
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
      snap.InFlightRequests (direct access)
      snap.KVUtilization (direct access)
    """

    EXPECTED_SIGNALS = {
        "InFlightRequests",
        "KVUtilization",
    }

    EXPECTED_COMPOSITES = {}

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
        reason="mapping file absent"
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
                "| InFlightRequests |",
                "| FakeSignal | int | `snap.FakeSignal` | N/A | N/A | low | 0 | Spurious test row |\n| InFlightRequests |",
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
                "| InFlightRequests |",
                "| InFlightRequests | int | `snap.InFlightRequests` | duplicate | N/A | medium | 0 | Duplicate test row |\n| InFlightRequests |",
            )
            mapping.write_text(content)
            code, output = run_cli("validate-mapping")
            assert code == 1, f"Expected failure for duplicate signal, got: {output}"
            assert any("duplicate" in e.lower() for e in output.get("errors", []))
            assert "InFlightRequests" in output.get("duplicate_signals", []), (
                f"duplicate_signals structured field should contain 'InFlightRequests': {output}"
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

    def test_validate_schema_yaml_algorithm_values(self):
        """validate-schema loads YAML files by extension."""
        alg = {
            "stack": {
                "model": {
                    "modelName": "Org/Model-7B",
                    "helmValues": {
                        "modelArtifacts": {"name": "Org/Model-7B", "uri": "pvc://model-pvc/models/Model-7B"},
                        "decode": {
                            "replicas": 4,
                            "containers": [{"image": "vllm/vllm-openai:v0.11.0"}],
                        },
                    },
                },
                "gaie": {
                    "treatment": {
                        "helmValues": {
                            "inferenceExtension": {
                                "pluginsCustomConfig": {"custom-plugins.yaml": "..."},
                            }
                        }
                    }
                },
            },
            "observe": {
                "image": "ghcr.io/inference-sim/blis:v0.6.13",
                "workloads": [{"name": "glia-prefix-heavy", "spec": "version: 1\n"}],
            },
        }
        import yaml
        yaml_file = WORKSPACE / "algorithm_values.yaml"
        yaml_file.write_text(yaml.dump(alg))
        try:
            code, output = run_cli("validate-schema", str(yaml_file))
            assert code == 0, f"Expected pass: {output}"
        finally:
            if yaml_file.exists():
                yaml_file.unlink()

    def test_validate_schema_yaml_missing_required(self):
        """validate-schema on YAML reports missing required fields."""
        import yaml
        yaml_file = WORKSPACE / "algorithm_values.yaml"
        yaml_file.write_text(yaml.dump({"stack": {"model": {"modelName": "x"}}}))
        try:
            code, output = run_cli("validate-schema", str(yaml_file))
            assert code == 1, f"Expected violations, got: {output}"
        finally:
            if yaml_file.exists():
                yaml_file.unlink()


class TestCompositeSignalConsistency:
    """Cross-validate METHOD_EXPANSIONS against the mapping artifact."""

    def test_method_expansions_match_mapping_composite_table(self):
        from tools.transfer_cli import METHOD_EXPANSIONS
        mapping = REPO_ROOT / "docs" / "transfer" / "blis_to_llmd_mapping.md"
        if not mapping.exists():
            pytest.skip("Mapping artifact not yet created")
        content = mapping.read_text()
        # Only check composite methods that are actually present in the current mapping.
        # METHOD_EXPANSIONS may contain entries (e.g. EffectiveLoad) for composite signals
        # that were removed from the mapping when the signal set was migrated.
        composites_in_mapping = {m: f for m, f in METHOD_EXPANSIONS.items() if m in content}
        if not composites_in_mapping:
            pytest.skip("No METHOD_EXPANSIONS entries present in mapping artifact (signal set migration removed composites)")
        for method, fields in composites_in_mapping.items():
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
            shutil.copy2(str(ROUTING_DIR / "best_program.go"), str(tmpdir / "best_program.go"))
            shutil.copy2(str(ROUTING_DIR / "best_program_info.json"), str(tmpdir / "best_program_info.json"))
            src = (tmpdir / "best_program.go").read_text()
            src = src.replace(
                "// EVOLVE-BLOCK-START",
                "// EVOLVE-BLOCK-START\n\t// BC-11 drift detection test modification",
            )
            (tmpdir / "best_program.go").write_text(src)
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
                shutil.copy2(str(ROUTING_DIR / "best_program.go"), str(tmpdir / "best_program.go"))
                shutil.copy2(str(ROUTING_DIR / "best_program_info.json"), str(tmpdir / "best_program_info.json"))
                src = (tmpdir / "best_program.go").read_text()
                src = src.replace(
                    "// EVOLVE-BLOCK-START",
                    "// EVOLVE-BLOCK-START\n\tunknown_val = snap.NovelMetricXYZ",
                )
                (tmpdir / "best_program.go").write_text(src)
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
                r'(\|\s*KVUtilization\s*\|[^|]*\|[^|]*\|[^|]*\|[^|]*\|)\s*high\s*(\|)',
                r'\1 low \2',
                content,
                count=1,
            )
            assert new_content != content, "KVUtilization high→low substitution failed; check mapping format"
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
        """R2-F-13: Verify *(zeroed ...)* annotation detection works against actual mapping.

        The new EVOLVE-BLOCK accesses InFlightRequests (medium) and KVUtilization (high).
        Neither is zeroed in the mapping, so fidelity_zeroed should NOT be set on either.
        """
        mapping = REPO_ROOT / "docs" / "transfer" / "blis_to_llmd_mapping.md"
        if not mapping.exists():
            pytest.skip("Mapping artifact not present")
        code, output = run_cli("extract", str(ROUTING_DIR))
        assert code == 0
        summary = json.loads((WORKSPACE / "algorithm_summary.json").read_text())
        # InFlightRequests is medium fidelity (not zeroed) in the mapping
        in_flight = [s for s in summary["signals"] if s["name"] == "InFlightRequests"]
        assert len(in_flight) == 1, "InFlightRequests must be present in extracted signals"
        assert not in_flight[0].get("fidelity_zeroed"), (
            "InFlightRequests should NOT be zeroed in the mapping"
        )
        # KVUtilization is high fidelity (not zeroed) in the mapping
        kv_util = [s for s in summary["signals"] if s["name"] == "KVUtilization"]
        assert len(kv_util) == 1, "KVUtilization must be present in extracted signals"
        assert not kv_util[0].get("fidelity_zeroed"), (
            "KVUtilization should NOT be zeroed in the mapping"
        )

    @pytest.mark.skipif(
        not (Path(__file__).parent.parent / "docs" / "transfer" / "blis_to_llmd_mapping.md").exists(),
        reason="Mapping artifact not present (pre-Task 5)"
    )
    def test_fidelity_fallback_pattern_matches_additional_signals(self):
        """R5-F-11: Verify fallback fidelity regex pattern works against signals in the mapping.

        The original test validated SessionID (removed after mapping migration). Updated to
        verify the same pattern logic against InFlightRequests (medium fidelity), which is
        present in the current mapping.
        """
        mapping = REPO_ROOT / "docs" / "transfer" / "blis_to_llmd_mapping.md"
        if not mapping.exists():
            pytest.skip("Mapping artifact not present")
        content = mapping.read_text()
        import re
        # Generalised fallback pattern (same structure as the old SessionID pattern):
        # matches any signal name followed by fidelity column (low|medium|high).
        # Verify it correctly extracts "medium" for InFlightRequests.
        pattern_alt = r'\|\s*InFlightRequests(?:\s*\([^)]*\))?\s*\|(?:[^|]*\|){4}\s*(low|medium|high)\s*(?:\*\(provisional\)\*)?\s*\|'
        match = re.search(pattern_alt, content, re.IGNORECASE)
        assert match is not None, (
            "Fallback fidelity pattern should match InFlightRequests row in the mapping"
        )
        assert match.group(1).lower() == "medium"


class TestCIStrictEnforcement:
    """F-1: Enforce --strict in CI."""

    def setup_method(self):
        WORKSPACE.mkdir(exist_ok=True)
        summary = WORKSPACE / "algorithm_summary.json"
        if summary.exists():
            summary.unlink()

    def test_ci_env_requires_strict_flag(self):
        """F-1: In CI, extract without --strict FAILS with exit 2 (invocation error)."""
        import os
        result = subprocess.run(
            [sys.executable, str(CLI), "extract", str(ROUTING_DIR)],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
            env={**os.environ, "CI": "true"},
        )
        assert result.returncode == 2
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
            shutil.copy2(str(ROUTING_DIR / "best_program.go"), str(tmpdir / "best_program.go"))
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
            shutil.copy2(str(ROUTING_DIR / "best_program.go"), str(tmpdir / "best_program.go"))
            (tmpdir / "best_program_info.json").write_text('{"metrics": [1, 2, 3]}')
            env = {k: v for k, v in os.environ.items() if k != "CI"}
            result = subprocess.run(
                [sys.executable, str(CLI), "extract", str(tmpdir)],
                capture_output=True, text=True, cwd=str(REPO_ROOT), env=env,
            )
            stdout = json.loads(result.stdout)
            assert "status" in stdout


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
                "t_eff": 0.10,
                "workload_classification": [
                    {
                        "workload": "test_workload",
                        "classification": "matched",
                        "improvement": 0.05,
                        "matched_signals": ["signal1"]
                    }
                ],
                "specificity_notes": []
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


class TestBenchmarkState:
    def _alg_summary(self, tmp_path):
        ws = tmp_path / "workspace"
        ws.mkdir()
        (ws / "algorithm_summary.json").write_text(
            '{"algorithm_name": "test-algo", "scope_validation_passed": true,'
            ' "fidelity_checked": true, "evolve_block_source": "blis_router/best/best_program.go:1-10",'
            ' "evolve_block_content_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
            ' "signals": [{"name": "KVUtilization", "type": "float64", "access_path": "kv"}],'
            ' "composite_signals": [], "metrics": {"combined_score": 1.5},'
            ' "mapping_artifact_version": "1.0"}'
        )
        return ws

    def test_creates_state_file_when_absent(self, tmp_path):
        ws = self._alg_summary(tmp_path)
        from tools.transfer_cli import cmd_benchmark_state
        import argparse
        args = argparse.Namespace(workspace=str(ws), namespace="test-ns",
                                  set_phase=None, force=False)
        rc = cmd_benchmark_state(args)
        assert rc == 0
        import json
        state = json.loads((ws / "benchmark_state.json").read_text())
        assert state["algorithm_name"] == "test-algo"
        assert state["namespace"] == "test-ns"
        assert state["phases"]["noise"]["status"] == "pending"
        assert state["phases"]["baseline"]["status"] == "pending"
        assert state["phases"]["treatment"]["status"] == "pending"

    def test_context_guard_warns_on_mismatch(self, tmp_path, monkeypatch):
        ws = self._alg_summary(tmp_path)
        import json
        state = {
            "schema_version": 1, "algorithm_name": "test-algo",
            "created_at": "2026-01-01T00:00:00Z",
            "cluster_context": "original-cluster", "namespace": "test-ns",
            "phases": {
                "noise":     {"status": "pending", "pipelinerun_name": None,
                              "submitted_at": None, "completed_at": None,
                              "results_pvc_path": "noise/", "results_local_path": None,
                              "failure_reason": None},
                "baseline":  {"status": "pending", "pipelinerun_name": None,
                              "submitted_at": None, "completed_at": None,
                              "results_pvc_path": "baseline/", "results_local_path": None,
                              "failure_reason": None},
                "treatment": {"status": "pending", "pipelinerun_name": None,
                              "submitted_at": None, "completed_at": None,
                              "results_pvc_path": "treatment/", "results_local_path": None,
                              "failure_reason": None},
            }
        }
        (ws / "benchmark_state.json").write_text(json.dumps(state))
        monkeypatch.setattr("tools.transfer_cli._kubectl_current_context",
                            lambda: "different-cluster")
        from tools.transfer_cli import cmd_benchmark_state
        import argparse
        args = argparse.Namespace(workspace=str(ws), namespace=None,
                                  set_phase=None, force=False)
        rc = cmd_benchmark_state(args)
        assert rc == 1

    def test_set_phase_updates_status(self, tmp_path):
        ws = self._alg_summary(tmp_path)
        import json, argparse
        from tools.transfer_cli import cmd_benchmark_state
        # create
        args = argparse.Namespace(workspace=str(ws), namespace="ns",
                                  set_phase=None, force=False)
        cmd_benchmark_state(args)
        # set noise to done
        args2 = argparse.Namespace(workspace=str(ws), namespace=None,
                                   set_phase="noise", status="done",
                                   pipelinerun=None, results=None,
                                   failure_reason=None, force=False)
        rc = cmd_benchmark_state(args2)
        assert rc == 0
        state = json.loads((ws / "benchmark_state.json").read_text())
        assert state["phases"]["noise"]["status"] == "done"

    def test_ordering_guard_blocks_baseline_before_noise(self, tmp_path):
        ws = self._alg_summary(tmp_path)
        import argparse
        from tools.transfer_cli import cmd_benchmark_state
        cmd_benchmark_state(argparse.Namespace(workspace=str(ws), namespace="ns",
                                               set_phase=None, force=False))
        args = argparse.Namespace(workspace=str(ws), namespace=None,
                                  set_phase="baseline", status="running",
                                  pipelinerun="pr-1", results=None,
                                  failure_reason=None, force=False)
        rc = cmd_benchmark_state(args)
        assert rc == 1  # ordering violation

    def test_missing_algorithm_summary_exits_2(self, tmp_path):
        ws = tmp_path / "workspace"
        ws.mkdir()
        import argparse
        from tools.transfer_cli import cmd_benchmark_state
        args = argparse.Namespace(workspace=str(ws), namespace="ns",
                                  set_phase=None, force=False)
        rc = cmd_benchmark_state(args)
        assert rc == 2

    def test_regression_guard_blocks_done_to_pending_without_force(self, tmp_path):
        """Regression guard: reverting 'done' phase to 'pending' without --force exits 2."""
        ws = self._alg_summary(tmp_path)
        import json, argparse
        from tools.transfer_cli import cmd_benchmark_state
        # Create state
        cmd_benchmark_state(argparse.Namespace(workspace=str(ws), namespace="ns",
                                               set_phase=None, force=False))
        # Advance noise to done
        rc_done = cmd_benchmark_state(argparse.Namespace(
            workspace=str(ws), namespace=None,
            set_phase="noise", status="done",
            pipelinerun=None, results=None, failure_reason=None, force=False,
        ))
        assert rc_done == 0
        # Attempt regression without --force
        rc = cmd_benchmark_state(argparse.Namespace(
            workspace=str(ws), namespace=None,
            set_phase="noise", status="pending",
            pipelinerun=None, results=None, failure_reason=None, force=False,
        ))
        assert rc == 2, f"Regression from done→pending without --force should be rc=2, got {rc}"

    def test_regression_guard_force_bypass(self, tmp_path):
        """--force bypasses regression guard; noise reverts from done to pending."""
        ws = self._alg_summary(tmp_path)
        import json, argparse
        from tools.transfer_cli import cmd_benchmark_state
        # Create state
        cmd_benchmark_state(argparse.Namespace(workspace=str(ws), namespace="ns",
                                               set_phase=None, force=False))
        # Advance noise to done
        cmd_benchmark_state(argparse.Namespace(
            workspace=str(ws), namespace=None,
            set_phase="noise", status="done",
            pipelinerun=None, results=None, failure_reason=None, force=False,
        ))
        # Revert with --force
        rc = cmd_benchmark_state(argparse.Namespace(
            workspace=str(ws), namespace=None,
            set_phase="noise", status="pending",
            pipelinerun=None, results=None, failure_reason=None, force=True,
        ))
        assert rc == 0, f"--force bypass should exit 0, got {rc}"
        state = json.loads((ws / "benchmark_state.json").read_text())
        assert state["phases"]["noise"]["status"] == "pending"

    def test_missing_namespace_on_first_invocation_exits_2(self, tmp_path):
        """First invocation without --namespace must exit 2 (not create a broken state file)."""
        ws = self._alg_summary(tmp_path)
        import argparse
        from tools.transfer_cli import cmd_benchmark_state
        args = argparse.Namespace(workspace=str(ws), namespace=None,
                                  set_phase=None, force=False)
        rc = cmd_benchmark_state(args)
        assert rc == 2, f"Missing --namespace on first invocation should exit 2, got {rc}"
        assert not (ws / "benchmark_state.json").exists(), (
            "No state file should be created when --namespace is absent"
        )

    def test_set_phase_failed_persists_failure_reason(self, tmp_path):
        """--status failed with --failure-reason persists reason in state file."""
        ws = self._alg_summary(tmp_path)
        import json, argparse
        from tools.transfer_cli import cmd_benchmark_state
        # Create state
        cmd_benchmark_state(argparse.Namespace(workspace=str(ws), namespace="ns",
                                               set_phase=None, force=False))
        # Set noise to failed with a reason
        rc = cmd_benchmark_state(argparse.Namespace(
            workspace=str(ws), namespace=None,
            set_phase="noise", status="failed",
            pipelinerun="pr-xyz", results=None,
            failure_reason="OOMKilled after 2h", force=False,
        ))
        assert rc == 0, f"Setting phase to failed should succeed, got {rc}"
        state = json.loads((ws / "benchmark_state.json").read_text())
        assert state["phases"]["noise"]["status"] == "failed"
        assert state["phases"]["noise"]["failure_reason"] == "OOMKilled after 2h"

    def test_corrupt_algorithm_summary_json_exits_2(self, tmp_path):
        """Corrupt algorithm_summary.json (invalid JSON) on first invocation exits 2."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        (ws / "algorithm_summary.json").write_text("{not valid json")
        import argparse
        from tools.transfer_cli import cmd_benchmark_state
        args = argparse.Namespace(workspace=str(ws), namespace="ns",
                                  set_phase=None, force=False)
        rc = cmd_benchmark_state(args)
        assert rc == 2, f"Corrupt algorithm_summary.json should exit 2, got {rc}"


import csv, textwrap


def _write_tracev2(directory, rows):
    """Write minimal TraceV2 files. rows = list of dicts with CSV fields."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "trace_header.yaml").write_text(
        "trace_version: 2\ntime_unit: microseconds\nmode: real\n"
    )
    fieldnames = ["request_id", "send_time_us", "first_chunk_time_us",
                  "last_chunk_time_us", "num_chunks", "status", "error_message"]
    with open(directory / "trace_data.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            row = {k: "" for k in fieldnames}
            row.update(r)
            w.writerow(row)


class TestConvertTrace:
    def test_baseline_single_workload(self, tmp_path):
        wl_dir = tmp_path / "baseline" / "glia-40qps"
        _write_tracev2(wl_dir, [
            {"send_time_us": "0", "first_chunk_time_us": "100000",
             "last_chunk_time_us": "200000", "num_chunks": "5", "status": "ok"},
            {"send_time_us": "0", "first_chunk_time_us": "120000",
             "last_chunk_time_us": "220000", "num_chunks": "5", "status": "ok"},
        ])
        out = tmp_path / "baseline_results.json"
        from tools.transfer_cli import cmd_convert_trace
        import argparse
        args = argparse.Namespace(input_dir=str(tmp_path / "baseline"),
                                  output=str(out))
        rc = cmd_convert_trace(args)
        assert rc == 0
        import json
        result = json.loads(out.read_text())
        assert result["workloads"][0]["name"] == "glia-40qps"
        m = result["workloads"][0]["metrics"]
        assert "ttft_p50" in m and "ttft_p99" in m
        assert m["ttft_p50"] == 100.0   # 100000 us / 1000

    def test_noise_per_run_structure(self, tmp_path):
        for i in range(3):
            wl_dir = tmp_path / "noise" / "glia-40qps" / f"run-{i}"
            _write_tracev2(wl_dir, [
                {"send_time_us": "0", "first_chunk_time_us": str(100000 + i*1000),
                 "last_chunk_time_us": str(200000 + i*1000),
                 "num_chunks": "4", "status": "ok"},
            ])
        out = tmp_path / "noise_results.json"
        from tools.transfer_cli import cmd_convert_trace
        import argparse
        args = argparse.Namespace(input_dir=str(tmp_path / "noise"),
                                  output=str(out))
        rc = cmd_convert_trace(args)
        assert rc == 0
        import json
        result = json.loads(out.read_text())
        wl = result["workloads"][0]
        assert wl["name"] == "glia-40qps"
        assert "runs" in wl
        assert len(wl["runs"]) == 3

    def test_all_failed_rows_exits_1(self, tmp_path):
        wl_dir = tmp_path / "baseline" / "broken-workload"
        _write_tracev2(wl_dir, [
            {"send_time_us": "0", "first_chunk_time_us": "0",
             "last_chunk_time_us": "0", "num_chunks": "0", "status": "timeout"},
        ])
        from tools.transfer_cli import cmd_convert_trace
        import argparse
        args = argparse.Namespace(input_dir=str(tmp_path / "baseline"),
                                  output=str(tmp_path / "out.json"))
        rc = cmd_convert_trace(args)
        assert rc == 1

    def test_missing_csv_exits_1(self, tmp_path):
        wl_dir = tmp_path / "baseline" / "glia-40qps"
        wl_dir.mkdir(parents=True)
        (wl_dir / "trace_header.yaml").write_text("trace_version: 2\n")
        # no trace_data.csv
        from tools.transfer_cli import cmd_convert_trace
        import argparse
        args = argparse.Namespace(input_dir=str(tmp_path / "baseline"),
                                  output=str(tmp_path / "out.json"))
        rc = cmd_convert_trace(args)
        assert rc == 1

    def test_underscore_directory_names_are_normalized(self, tmp_path):
        """convert-trace normalizes workload_ prefix and underscores to hyphens,
        matching _classify_workloads normalization."""
        # Directory name: glia_40qps (underscores, no workload_ prefix)
        wl_dir = tmp_path / "baseline" / "glia_40qps"
        _write_tracev2(wl_dir, [
            {"send_time_us": "0", "first_chunk_time_us": "100000",
             "last_chunk_time_us": "200000", "num_chunks": "5", "status": "ok"},
        ])
        out = tmp_path / "baseline_results.json"
        from tools.transfer_cli import cmd_convert_trace
        import argparse, json
        args = argparse.Namespace(input_dir=str(tmp_path / "baseline"), output=str(out))
        rc = cmd_convert_trace(args)
        assert rc == 0
        result = json.loads(out.read_text())
        # Name should be normalized: glia_40qps → glia-40qps
        assert result["workloads"][0]["name"] == "glia-40qps", (
            f"Expected 'glia-40qps' but got '{result['workloads'][0]['name']}'. "
            "convert-trace must normalize workload names to match _classify_workloads."
        )


class TestRenderPipelinerun:
    def test_substitutes_variables(self, tmp_path):
        stub = tmp_path / "stub.yaml"
        stub.write_text(
            "metadata:\n  name: $PIPELINERUN_NAME\n  namespace: ${NAMESPACE}\n"
        )
        out = tmp_path / "rendered.yaml"
        from tools.transfer_cli import cmd_render_pipelinerun
        import argparse
        args = argparse.Namespace(
            template=str(stub),
            vars=["PIPELINERUN_NAME=pr-123", "NAMESPACE=test-ns"],
            out=str(out),
        )
        rc = cmd_render_pipelinerun(args)
        assert rc == 0
        content = out.read_text()
        assert "pr-123" in content
        assert "test-ns" in content

    def test_exits_1_on_unresolved_placeholder(self, tmp_path):
        stub = tmp_path / "stub.yaml"
        stub.write_text("name: $PIPELINERUN_NAME\nns: $NAMESPACE\n")
        out = tmp_path / "rendered.yaml"
        from tools.transfer_cli import cmd_render_pipelinerun
        import argparse
        # Only supply one of two required vars
        args = argparse.Namespace(
            template=str(stub),
            vars=["PIPELINERUN_NAME=pr-456"],
            out=str(out),
        )
        rc = cmd_render_pipelinerun(args)
        assert rc == 1  # $NAMESPACE unresolved


class TestCompilePipeline:
    def test_exits_2_on_missing_template_dir(self, tmp_path):
        from tools.transfer_cli import cmd_compile_pipeline
        import argparse
        args = argparse.Namespace(
            template_dir=str(tmp_path / "nonexistent"),
            values=str(tmp_path / "values.yaml"),
            phase="baseline",
            out=str(tmp_path / "out"),
        )
        rc = cmd_compile_pipeline(args)
        assert rc == 2

    def test_exits_2_on_missing_values_file(self, tmp_path):
        from tools.transfer_cli import cmd_compile_pipeline
        import argparse
        tdir = tmp_path / "tekton"
        tdir.mkdir()
        (tdir / "pipeline.yaml.j2").write_text("{{ phase }}")
        args = argparse.Namespace(
            template_dir=str(tdir),
            values=str(tmp_path / "nonexistent_values.yaml"),
            phase="baseline",
            out=str(tmp_path / "out"),
        )
        rc = cmd_compile_pipeline(args)
        assert rc == 2

    def test_success_path_produces_output_file(self, tmp_path):
        """compile-pipeline exit 0 and produces output file when unified template present."""
        import argparse, unittest.mock as mock
        from tools.transfer_cli import cmd_compile_pipeline
        tdir = tmp_path / "tekton"
        tdir.mkdir()
        (tdir / "pipeline.yaml.j2").write_text("phase: {{ phase }}\n")
        vf = tmp_path / "values.yaml"
        vf.write_text("stack: {gaie: {baseline: {helmValues: {}}, treatment: {helmValues: {}}}}\n")
        out = tmp_path / "out"
        out.mkdir()
        args = argparse.Namespace(
            template_dir=str(tdir),
            values=str(vf),
            phase="baseline",
            out=str(out),
        )
        # cmd_compile_pipeline calls tektonc.py via subprocess — mock subprocess.run
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            rc = cmd_compile_pipeline(args)
        assert rc == 0

    def test_phase_and_gaie_config_injected_into_values(self, tmp_path):
        """compile-pipeline injects 'phase' and 'gaie_config' into the augmented values
        passed to tektonc. gaie_config uses baseline helmValues for noise/baseline phases
        and treatment helmValues for the treatment phase."""
        import argparse, unittest.mock as mock, yaml
        from tools.transfer_cli import cmd_compile_pipeline
        tdir = tmp_path / "tekton"
        tdir.mkdir()
        (tdir / "pipeline.yaml.j2").write_text("phase: {{ phase }}\n")
        baseline_config = {"pluginsConfigFile": "baseline.yaml"}
        treatment_config = {"pluginsConfigFile": "treatment.yaml"}
        vf = tmp_path / "values.yaml"
        vf.write_text(yaml.dump({
            "stack": {
                "gaie": {
                    "baseline": {"helmValues": baseline_config},
                    "treatment": {"helmValues": treatment_config},
                }
            }
        }))
        out = tmp_path / "out"
        out.mkdir()

        captured_calls = []

        def fake_run(cmd, **kwargs):
            # Capture the temp values file passed to tektonc (-f <file>)
            idx = cmd.index("-f") + 1
            captured_calls.append(yaml.safe_load(open(cmd[idx]).read()))
            return mock.Mock(returncode=0, stdout="", stderr="")

        # Baseline phase: gaie_config should be baseline helmValues
        with mock.patch("subprocess.run", side_effect=fake_run):
            cmd_compile_pipeline(argparse.Namespace(
                template_dir=str(tdir), values=str(vf),
                phase="baseline", out=str(out),
            ))
        assert captured_calls[-1]["phase"] == "baseline"
        assert captured_calls[-1]["gaie_config"] == baseline_config

        # Noise phase: gaie_config should also be baseline helmValues
        with mock.patch("subprocess.run", side_effect=fake_run):
            cmd_compile_pipeline(argparse.Namespace(
                template_dir=str(tdir), values=str(vf),
                phase="noise", out=str(out),
            ))
        assert captured_calls[-1]["phase"] == "noise"
        assert captured_calls[-1]["gaie_config"] == baseline_config

        # Treatment phase: gaie_config should be treatment helmValues
        with mock.patch("subprocess.run", side_effect=fake_run):
            cmd_compile_pipeline(argparse.Namespace(
                template_dir=str(tdir), values=str(vf),
                phase="treatment", out=str(out),
            ))
        assert captured_calls[-1]["phase"] == "treatment"
        assert captured_calls[-1]["gaie_config"] == treatment_config

    def test_phase_template_fallback_when_no_unified_template(self, tmp_path):
        """compile-pipeline falls back to {phase}-pipeline.yaml.j2 when pipeline.yaml.j2
        does not exist (backward compatibility)."""
        import argparse, unittest.mock as mock
        from tools.transfer_cli import cmd_compile_pipeline
        tdir = tmp_path / "tekton"
        tdir.mkdir()
        (tdir / "noise-pipeline.yaml.j2").write_text("fallback\n")
        vf = tmp_path / "values.yaml"
        vf.write_text("stack: {gaie: {baseline: {helmValues: {}}}}\n")
        out = tmp_path / "out"
        out.mkdir()
        args = argparse.Namespace(
            template_dir=str(tdir), values=str(vf),
            phase="noise", out=str(out),
        )
        captured = []

        def fake_run(cmd, **kwargs):
            # Record which template was selected (-t <template>)
            idx = cmd.index("-t") + 1
            captured.append(cmd[idx])
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch("subprocess.run", side_effect=fake_run):
            rc = cmd_compile_pipeline(args)
        assert rc == 0
        assert captured and "noise-pipeline.yaml.j2" in captured[0]

    def test_tektonc_compilation_failure_returns_1(self, tmp_path):
        """compile-pipeline exits 1 (not 2) when tektonc runs but returns non-zero.
        Exit 1 = compilation failure; exit 2 = infrastructure failure (missing files)."""
        import argparse, unittest.mock as mock
        from tools.transfer_cli import cmd_compile_pipeline
        tdir = tmp_path / "tekton"
        tdir.mkdir()
        (tdir / "pipeline.yaml.j2").write_text("{{ undefined_var }}\n")
        vf = tmp_path / "values.yaml"
        vf.write_text("stack: {gaie: {baseline: {helmValues: {}}, treatment: {helmValues: {}}}}\n")
        args = argparse.Namespace(
            template_dir=str(tdir),
            values=str(vf),
            phase="noise",
            out=str(tmp_path / "out"),
        )
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(returncode=1, stdout="", stderr="Jinja2 UndefinedError")
            rc = cmd_compile_pipeline(args)
        assert rc == 1, (
            f"tektonc compilation failure should exit 1, got {rc}. "
            "Infrastructure failures are exit 2; compilation failures are exit 1."
        )


class TestPreflight:
    def _values(self, tmp_path):
        import yaml
        v = {
            "stack": {
                "model": {
                    "helmValues": {
                        "decode": {
                            "replicas": 2,
                            "acceleratorTypes": {
                                "labelKey": "nvidia.com/gpu.product",
                                "labelValues": ["NVIDIA-H100-80GB-HBM3"],
                            }
                        }
                    }
                },
                "scorer": {
                    "baseline": {"configContent": "apiVersion: v1"},
                    "treatment": {"configContent": "apiVersion: v1"},
                }
            },
            "observe": {
                "image": "ghcr.io/inference-sim/blis:v1.0.0",
                "workloads": [{"name": "glia-40qps"}],
                "noise_runs": 5,
            }
        }
        ws = tmp_path / "workspace" / "tekton"
        ws.mkdir(parents=True)
        vf = ws / "values.yaml"
        vf.write_text(yaml.dump(v))
        return vf

    def test_unresolved_tag_fails(self, tmp_path):
        import yaml
        vf = self._values(tmp_path)
        data = yaml.safe_load(vf.read_text())
        data["observe"]["image"] = "ghcr.io/inference-sim/blis:<TAG>"
        vf.write_text(yaml.dump(data))
        from tools.transfer_cli import _preflight_check_values
        errors = _preflight_check_values(vf, "test-ns", "noise")
        assert any("<TAG>" in e for e in errors)

    def test_missing_treatment_config_fails_for_treatment(self, tmp_path):
        import yaml
        vf = self._values(tmp_path)
        data = yaml.safe_load(vf.read_text())
        data["stack"]["scorer"]["treatment"]["configContent"] = ""
        vf.write_text(yaml.dump(data))
        from tools.transfer_cli import _preflight_check_values
        errors = _preflight_check_values(vf, "test-ns", "treatment")
        assert any("treatment" in e.lower() for e in errors)

    def test_noise_phase_skips_treatment_check(self, tmp_path):
        import yaml
        vf = self._values(tmp_path)
        data = yaml.safe_load(vf.read_text())
        data["stack"]["scorer"]["treatment"]["configContent"] = ""
        vf.write_text(yaml.dump(data))
        from tools.transfer_cli import _preflight_check_values
        errors = _preflight_check_values(vf, "test-ns", "noise")
        # treatment check not run for noise phase
        assert not any("treatment" in e.lower() for e in errors)

    def test_missing_values_file_returns_oserror_message(self, tmp_path):
        """_preflight_check_values returns an OS-level error message for a missing file,
        not a YAML parse error."""
        from tools.transfer_cli import _preflight_check_values
        from pathlib import Path
        errors = _preflight_check_values(Path(tmp_path / "nonexistent.yaml"), "ns", "noise")
        assert errors, "Missing file should produce at least one error"
        assert any("read" in e.lower() or "no such" in e.lower() or "errno" in e.lower()
                   for e in errors), (
            f"Expected an OS/read error message for missing file, got: {errors}"
        )
        assert not any("parse" in e.lower() for e in errors), (
            f"Should NOT say 'parse' for a missing file — that implies a YAML syntax problem: {errors}"
        )


    def test_treatment_scorer_build_timeout_marks_failed(self, tmp_path):
        """preflight exits 1 (not hangs) when go build times out.
        subprocess is imported locally in cmd_preflight, so mock subprocess.run globally."""
        import argparse, unittest.mock as mock, subprocess as subprocess_mod
        vf = self._values(tmp_path)
        # Create fake scheduler submodule dir so the go build branch is entered
        scheduler_dir = tmp_path / "llm-d-inference-scheduler"
        scheduler_dir.mkdir()

        def fake_run(cmd, **kwargs):
            # Raise TimeoutExpired for go commands; succeed for everything else (kubectl etc.)
            if cmd and "go" in str(cmd[0]):
                raise subprocess_mod.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout", 120))
            result = mock.Mock()
            result.returncode = 0
            result.stdout = "ok"
            result.stderr = ""
            return result

        with mock.patch("tools.transfer_cli.REPO_ROOT", tmp_path), \
             mock.patch("subprocess.run", side_effect=fake_run):
            from tools.transfer_cli import cmd_preflight
            args = argparse.Namespace(phase="treatment", values=str(vf),
                                      namespace="test-ns")
            rc = cmd_preflight(args)
        # preflight must still return (not hang), and some check must have failed
        assert rc == 1, (
            f"preflight should exit 1 when go build times out, got {rc}. "
            "If it hangs, timeout is not being set on the go build subprocess call."
        )


class TestBenchmarkNew:
    def _make_noise(self, tmp_path, cv=0.05):
        """noise_results.json with controllable CV."""
        import json, math
        base = 100.0
        runs = [{"metrics": {"ttft_p50": base, "ttft_p99": base * (1 + cv * (i - 2) / 2),
                              "tpot_p50": 10.0, "tpot_p99": 15.0}}
                for i in range(5)]
        data = {"workloads": [
            {"name": "glia-40qps", "runs": runs},
            {"name": "prefix-heavy", "runs": runs},
        ]}
        p = tmp_path / "noise_results.json"
        p.write_text(json.dumps(data))
        return p

    def _make_baseline_treatment(self, tmp_path, baseline_p99=100.0, treatment_p99=85.0):
        import json
        bl = {"workloads": [
            {"name": "glia-40qps",
             "metrics": {"ttft_p50": 50.0, "ttft_p99": baseline_p99,
                         "tpot_p50": 10.0, "tpot_p99": 15.0}},
            {"name": "prefix-heavy",
             "metrics": {"ttft_p50": 55.0, "ttft_p99": baseline_p99,
                         "tpot_p50": 10.0, "tpot_p99": 15.0}},
        ]}
        tr = {"workloads": [
            {"name": "glia-40qps",
             "metrics": {"ttft_p50": 45.0, "ttft_p99": treatment_p99,
                         "tpot_p50": 9.0, "tpot_p99": 13.0}},
            {"name": "prefix-heavy",
             "metrics": {"ttft_p50": 52.0, "ttft_p99": baseline_p99,  # no improvement
                         "tpot_p50": 10.0, "tpot_p99": 15.0}},
        ]}
        bp = tmp_path / "baseline_results.json"
        tp = tmp_path / "treatment_results.json"
        bp.write_text(json.dumps(bl))
        tp.write_text(json.dumps(tr))
        return bp, tp

    def _make_signal_coverage(self, tmp_path):
        import json
        sc = {"signals": [
            {"sim_name": "KVUtilization", "prod_name": "kvUtil",
             "prod_access_path": "node.status.kv_utilization",
             "fidelity_rating": "high", "staleness_window_ms": 0, "mapped": True},
            {"sim_name": "InFlightRequests", "prod_name": "inFlight",
             "prod_access_path": "node.status.in_flight_requests",
             "fidelity_rating": "high", "staleness_window_ms": 0, "mapped": True},
        ], "unmapped_signals": [], "commit_hash": "abc123", "coverage_complete": True}
        p = tmp_path / "signal_coverage.json"
        p.write_text(json.dumps(sc))
        return p

    def _make_workloads_dir(self, tmp_path):
        """Workload YAMLs that exercise mapped signals."""
        import yaml
        wd = tmp_path / "workloads"
        wd.mkdir()
        # glia-40qps exercises kv_utilization → KVUtilization (mapped)
        (wd / "workload_glia-40qps.yaml").write_text(
            yaml.dump({"version": "1", "kv_utilization": 0.5, "aggregate_rate": 40})
        )
        # prefix-heavy exercises InFlightRequests via aggregate_rate (universal indirect driver)
        (wd / "workload_prefix-heavy.yaml").write_text(
            yaml.dump({"version": "1", "aggregate_rate": 85})
        )
        return wd

    def test_pass_verdict_with_clear_improvement(self, tmp_path):
        import json, argparse
        noise = self._make_noise(tmp_path, cv=0.02)
        bl, tr = self._make_baseline_treatment(tmp_path, 100.0, 80.0)  # 20% improvement
        sc = self._make_signal_coverage(tmp_path)
        wd = self._make_workloads_dir(tmp_path)
        out = tmp_path / "bench_out.json"
        from tools.transfer_cli import cmd_benchmark_new
        args = argparse.Namespace(noise=str(noise), baseline=str(bl), treatment=str(tr),
                                  signal_coverage=str(sc), workloads_dir=str(wd),
                                  out=str(out))
        rc = cmd_benchmark_new(args)
        assert rc == 0
        result = json.loads(out.read_text())
        assert result["mechanism_check_verdict"] == "PASS"
        assert result["passed"] is True

    def test_fail_verdict_no_improvement(self, tmp_path):
        import json, argparse
        noise = self._make_noise(tmp_path, cv=0.02)
        bl, tr = self._make_baseline_treatment(tmp_path, 100.0, 100.0)  # 0% improvement
        sc = self._make_signal_coverage(tmp_path)
        wd = self._make_workloads_dir(tmp_path)
        out = tmp_path / "bench_out.json"
        from tools.transfer_cli import cmd_benchmark_new
        args = argparse.Namespace(noise=str(noise), baseline=str(bl), treatment=str(tr),
                                  signal_coverage=str(sc), workloads_dir=str(wd),
                                  out=str(out))
        rc = cmd_benchmark_new(args)
        assert rc == 1  # FAIL
        result = json.loads(out.read_text())
        assert result["mechanism_check_verdict"] == "FAIL"

    def test_output_written_on_fail(self, tmp_path):
        """benchmark --out is always written regardless of verdict."""
        import json, argparse
        noise = self._make_noise(tmp_path)
        bl, tr = self._make_baseline_treatment(tmp_path, 100.0, 100.0)
        sc = self._make_signal_coverage(tmp_path)
        wd = self._make_workloads_dir(tmp_path)
        out = tmp_path / "bench_out.json"
        from tools.transfer_cli import cmd_benchmark_new
        args = argparse.Namespace(noise=str(noise), baseline=str(bl), treatment=str(tr),
                                  signal_coverage=str(sc), workloads_dir=str(wd),
                                  out=str(out))
        cmd_benchmark_new(args)
        assert out.exists()

    def test_inconclusive_verdict_small_improvement(self, tmp_path):
        """INCONCLUSIVE: positive improvement below t_eff exits 0."""
        import json, argparse
        # cv=0.1 → t_eff ≈ 0.22 (22%). Use 5% improvement — positive but below floor.
        noise = self._make_noise(tmp_path, cv=0.1)
        bl, tr = self._make_baseline_treatment(tmp_path, 100.0, 95.0)  # 5% improvement
        sc = self._make_signal_coverage(tmp_path)
        wd = self._make_workloads_dir(tmp_path)
        out = tmp_path / "bench_out.json"
        from tools.transfer_cli import cmd_benchmark_new
        args = argparse.Namespace(noise=str(noise), baseline=str(bl), treatment=str(tr),
                                  signal_coverage=str(sc), workloads_dir=str(wd),
                                  out=str(out))
        rc = cmd_benchmark_new(args)
        assert rc == 0, f"INCONCLUSIVE should exit 0, got {rc}"
        result = json.loads(out.read_text())
        assert result["mechanism_check_verdict"] == "INCONCLUSIVE"

    def test_noise_cv_computed_per_workload_not_pooled(self, tmp_path):
        """Two workloads with different mean latencies but identical low CVs
        should produce t_eff near 2*cv, NOT inflated by inter-workload variance."""
        import json, argparse
        # workload A: mean≈50ms, cv≈0.02; workload B: mean≈200ms, cv≈0.02
        # Pooled approach: huge CV from 50 vs 200ms difference
        # Per-workload approach: max CV ≈ 0.02 → t_eff = max(0.05, 0.04) = 0.05
        runs_a = [{"metrics": {"ttft_p50": v, "ttft_p99": v,
                                "tpot_p50": 10.0, "tpot_p99": 10.0}}
                   for v in [49.0, 50.0, 51.0, 50.5, 49.5]]   # cv ≈ 0.015
        runs_b = [{"metrics": {"ttft_p50": v, "ttft_p99": v,
                                "tpot_p50": 10.0, "tpot_p99": 10.0}}
                   for v in [196.0, 200.0, 204.0, 202.0, 198.0]]  # cv ≈ 0.015
        noise_data = {"workloads": [
            {"name": "fast-workload", "runs": runs_a},
            {"name": "slow-workload", "runs": runs_b},
        ]}
        noise = tmp_path / "noise_results.json"
        noise.write_text(json.dumps(noise_data))
        bl, tr = self._make_baseline_treatment(tmp_path, 100.0, 80.0)
        sc = self._make_signal_coverage(tmp_path)
        wd = self._make_workloads_dir(tmp_path)
        out = tmp_path / "bench_out.json"
        from tools.transfer_cli import cmd_benchmark_new
        args = argparse.Namespace(noise=str(noise), baseline=str(bl), treatment=str(tr),
                                  signal_coverage=str(sc), workloads_dir=str(wd),
                                  out=str(out))
        rc = cmd_benchmark_new(args)
        result = json.loads(out.read_text())
        # Per-workload CV ≈ 0.015 → t_eff = 0.05 (floor). Pooled CV would be >> 0.5.
        # If t_eff is > 0.5 the test will have got a wrong inflated t_eff.
        assert result["t_eff"] <= 0.10, (
            f"t_eff={result['t_eff']} is suspiciously large — noise CV is being "
            "inflated by pooling across workloads with different mean latencies"
        )

    def test_insufficient_noise_runs_exits_2(self, tmp_path):
        """benchmark exits 2 when a noise workload has fewer than 2 runs."""
        import json, argparse
        noise_data = {"workloads": [
            {"name": "glia-40qps", "runs": [
                {"metrics": {"ttft_p50": 100.0, "ttft_p99": 100.0,
                             "tpot_p50": 10.0, "tpot_p99": 15.0}}
            ]},  # only 1 run — insufficient for CV
        ]}
        noise = tmp_path / "noise_results.json"
        noise.write_text(json.dumps(noise_data))
        bl, tr = self._make_baseline_treatment(tmp_path)
        sc = self._make_signal_coverage(tmp_path)
        wd = self._make_workloads_dir(tmp_path)
        out = tmp_path / "bench_out.json"
        from tools.transfer_cli import cmd_benchmark_new
        args = argparse.Namespace(noise=str(noise), baseline=str(bl), treatment=str(tr),
                                  signal_coverage=str(sc), workloads_dir=str(wd),
                                  out=str(out))
        rc = cmd_benchmark_new(args)
        assert rc == 2, f"Insufficient noise runs should exit 2, got {rc}"

    def test_error_verdict_on_workload_name_mismatch(self, tmp_path):
        """ERROR: all workloads skipped due to name mismatch exits 2."""
        import json, argparse, yaml
        noise = self._make_noise(tmp_path, cv=0.02)
        bl, tr = self._make_baseline_treatment(tmp_path, 100.0, 80.0)
        sc = self._make_signal_coverage(tmp_path)
        # Create workloads dir whose filenames don't match any result names
        wd = tmp_path / "workloads_mismatch"
        wd.mkdir()
        (wd / "workload_completely-different.yaml").write_text(
            yaml.dump({"version": "1", "kv_utilization": 0.5, "aggregate_rate": 40})
        )
        out = tmp_path / "bench_out.json"
        from tools.transfer_cli import cmd_benchmark_new
        args = argparse.Namespace(noise=str(noise), baseline=str(bl), treatment=str(tr),
                                  signal_coverage=str(sc), workloads_dir=str(wd),
                                  out=str(out))
        rc = cmd_benchmark_new(args)
        assert rc == 2, f"Name mismatch ERROR should exit 2, got {rc}"
        result = json.loads(out.read_text())
        assert result["mechanism_check_verdict"] == "ERROR"
        assert "skipped_workloads" not in result, (
            "skipped_workloads key is not in benchmark_output.schema.json — "
            "use specificity_notes instead"
        )
        assert result["specificity_notes"], "name-mismatch error details should appear in specificity_notes"
        assert any("completely-different" in note or "skipped" in note.lower()
                   for note in result["specificity_notes"])

    def test_malformed_workload_yaml_exits_2(self, tmp_path):
        """benchmark exits 2 when a workload YAML file is malformed (not silently unmatched)."""
        import json, argparse
        noise = self._make_noise(tmp_path, cv=0.05)
        bl, tr = self._make_baseline_treatment(tmp_path)
        sc = self._make_signal_coverage(tmp_path)
        wd = tmp_path / "workloads_bad"
        wd.mkdir()
        # Write outright invalid YAML to trigger YAMLError
        (wd / "workload_glia-40qps.yaml").write_text(": {bad yaml: [unclosed")
        out = tmp_path / "bench_out.json"
        from tools.transfer_cli import cmd_benchmark_new
        args = argparse.Namespace(noise=str(noise), baseline=str(bl), treatment=str(tr),
                                  signal_coverage=str(sc), workloads_dir=str(wd),
                                  out=str(out))
        rc = cmd_benchmark_new(args)
        assert rc == 2, (
            f"Malformed workload YAML should exit 2 (infrastructure error), got {rc}. "
            "Silent reclassification to 'unmatched' is not acceptable — "
            "it would produce a wrong benchmark verdict."
        )

    def test_error_path_output_conforms_to_schema(self, tmp_path):
        """Error-path output (all workloads skipped) must not have extra keys
        beyond what benchmark_output.schema.json declares."""
        import json, argparse, yaml
        noise = self._make_noise(tmp_path, cv=0.02)
        bl, tr = self._make_baseline_treatment(tmp_path, 100.0, 80.0)
        sc = self._make_signal_coverage(tmp_path)
        wd = tmp_path / "workloads_mismatch"
        wd.mkdir()
        (wd / "workload_completely-different.yaml").write_text(
            yaml.dump({"version": "1", "kv_utilization": 0.5})
        )
        out = tmp_path / "bench_out.json"
        from tools.transfer_cli import cmd_benchmark_new
        args = argparse.Namespace(noise=str(noise), baseline=str(bl), treatment=str(tr),
                                  signal_coverage=str(sc), workloads_dir=str(wd),
                                  out=str(out))
        cmd_benchmark_new(args)
        result = json.loads(out.read_text())
        allowed_keys = {"t_eff", "noise_cv", "mechanism_check_verdict", "passed",
                        "workload_classification", "specificity_notes"}
        extra_keys = set(result.keys()) - allowed_keys
        assert not extra_keys, f"Output has extra keys not in schema: {extra_keys}"

    def test_error_verdict_when_all_workloads_unmatched_by_classification(self, tmp_path):
        """ERROR exit 2 when all workloads resolve (no name mismatch) but none match
        any signal — distinct from the name-mismatch ERROR path."""
        import json, argparse, yaml
        noise = self._make_noise(tmp_path, cv=0.02)
        bl, tr = self._make_baseline_treatment(tmp_path, 100.0, 80.0)
        sc = self._make_signal_coverage(tmp_path)
        # Workloads present in result files but with no mapped signal fields.
        # Deliberately exclude all mapping fields (aggregate_rate, kv_utilization, etc.)
        # so that _classify_workloads returns "unmatched" for every workload.
        wd = tmp_path / "workloads_no_signals"
        wd.mkdir()
        (wd / "workload_glia-40qps.yaml").write_text(
            yaml.dump({"version": "1", "duration_secs": 60})  # no mapped fields
        )
        (wd / "workload_prefix-heavy.yaml").write_text(
            yaml.dump({"version": "1", "duration_secs": 120})  # no mapped fields
        )
        out = tmp_path / "bench_out.json"
        from tools.transfer_cli import cmd_benchmark_new
        args = argparse.Namespace(noise=str(noise), baseline=str(bl), treatment=str(tr),
                                  signal_coverage=str(sc), workloads_dir=str(wd),
                                  out=str(out))
        rc = cmd_benchmark_new(args)
        assert rc == 2, f"All-unmatched classification should exit 2 (ERROR), got {rc}"
        result = json.loads(out.read_text())
        assert result["mechanism_check_verdict"] == "ERROR"
        # Confirm this is the classification-ERROR path (workload_classification is non-empty)
        assert len(result["workload_classification"]) > 0

    def test_missing_noise_file_exits_2(self, tmp_path):
        """benchmark exits 2 when --noise file does not exist."""
        import argparse
        bl, tr = self._make_baseline_treatment(tmp_path)
        sc = self._make_signal_coverage(tmp_path)
        wd = self._make_workloads_dir(tmp_path)
        from tools.transfer_cli import cmd_benchmark_new
        args = argparse.Namespace(
            noise=str(tmp_path / "nonexistent_noise.json"),
            baseline=str(bl), treatment=str(tr),
            signal_coverage=str(sc), workloads_dir=str(wd),
            out=str(tmp_path / "out.json"),
        )
        rc = cmd_benchmark_new(args)
        assert rc == 2, f"Missing noise file should exit 2, got {rc}"

    def test_malformed_json_input_exits_2(self, tmp_path):
        """benchmark exits 2 when an input JSON file is malformed."""
        import argparse
        noise = tmp_path / "bad_noise.json"
        noise.write_text("{broken json")
        bl, tr = self._make_baseline_treatment(tmp_path)
        sc = self._make_signal_coverage(tmp_path)
        wd = self._make_workloads_dir(tmp_path)
        from tools.transfer_cli import cmd_benchmark_new
        args = argparse.Namespace(
            noise=str(noise), baseline=str(bl), treatment=str(tr),
            signal_coverage=str(sc), workloads_dir=str(wd),
            out=str(tmp_path / "out.json"),
        )
        rc = cmd_benchmark_new(args)
        assert rc == 2, f"Malformed JSON input should exit 2, got {rc}"

    def test_nested_clients_keys_are_used_for_classification(self, tmp_path):
        """_classify_workloads must match signals from clients[] nested keys,
        not just top-level workload YAML keys."""
        import json, argparse, yaml
        noise = self._make_noise(tmp_path, cv=0.02)
        bl, tr = self._make_baseline_treatment(tmp_path, 100.0, 80.0)
        sc = self._make_signal_coverage(tmp_path)
        wd = tmp_path / "workloads_nested"
        wd.mkdir()
        # kv_utilization is inside clients[], not at top level
        (wd / "workload_glia-40qps.yaml").write_text(
            yaml.dump({
                "version": "1",
                "aggregate_rate": 40,
                "clients": [{"kv_utilization": 0.5, "concurrency": 10}],
            })
        )
        (wd / "workload_prefix-heavy.yaml").write_text(
            yaml.dump({"version": "1", "aggregate_rate": 85})
        )
        out = tmp_path / "bench_out.json"
        from tools.transfer_cli import cmd_benchmark_new
        args = argparse.Namespace(noise=str(noise), baseline=str(bl), treatment=str(tr),
                                  signal_coverage=str(sc), workloads_dir=str(wd),
                                  out=str(out))
        rc = cmd_benchmark_new(args)
        result = json.loads(out.read_text())
        # glia-40qps has kv_utilization inside clients[] — should be "matched"
        glia = next(w for w in result["workload_classification"] if w["workload"] == "glia-40qps")
        assert glia["classification"] == "matched", (
            f"glia-40qps has kv_utilization in clients[] — should be 'matched', "
            f"got '{glia['classification']}'. clients[] keys must be included in signal matching."
        )


class TestGenerateEvidence:
    def _make_workspace(self, tmp_path):
        import json
        ws = tmp_path / "workspace"
        ws.mkdir()
        (ws / "algorithm_summary.json").write_text(json.dumps({
            "algorithm_name": "blis-routing-v1",
            "evolve_block_source": "routing/",
        }))
        (ws / "validation_results.json").write_text(json.dumps({
            "suite_a": {"passed": True, "kendall_tau": 0.92,
                        "max_abs_error": 0.0001, "tuple_count": 150},
            "suite_b": {"passed": True, "rank_stability_tau": 1.0,
                        "threshold_crossing_pct": 0.0, "informational_only": True},
            "suite_c": {"passed": True, "deterministic": True,
                        "max_pile_on_ratio": 1.1},
            "benchmark": {
                "passed": True,
                "mechanism_check_verdict": "PASS",
                "t_eff": 0.05,
                "workload_classification": [
                    {"workload": "glia-40qps", "classification": "matched",
                     "improvement": 0.15, "matched_signals": ["KVUtilization"]},
                    {"workload": "prefix-heavy", "classification": "unmatched",
                     "improvement": 0.02, "matched_signals": []},
                ],
                "specificity_notes": [],
            },
            "overall_verdict": "PASS",
            "noise_cv": 0.03,
        }))
        return ws

    def test_generates_evidence_file(self, tmp_path):
        ws = self._make_workspace(tmp_path)
        out = tmp_path / "transfer_evidence.md"
        from tools.transfer_cli import cmd_generate_evidence
        import argparse
        args = argparse.Namespace(workspace=str(ws), out=str(out),
                                  calibration_log="docs/transfer/calibration_log.md")
        rc = cmd_generate_evidence(args)
        assert rc == 0
        content = out.read_text()
        assert "blis-routing-v1" in content
        assert "PASS" in content
        assert "glia-40qps" in content
        assert "0.92" in content  # suite_a tau

    def test_missing_validation_results_exits_1(self, tmp_path):
        ws = tmp_path / "workspace"
        ws.mkdir()
        import json
        (ws / "algorithm_summary.json").write_text(json.dumps(
            {"algorithm_name": "x", "evolve_block_source": "routing/"}
        ))
        # no validation_results.json
        from tools.transfer_cli import cmd_generate_evidence
        import argparse
        args = argparse.Namespace(workspace=str(ws), out=str(tmp_path / "out.md"),
                                  calibration_log="docs/transfer/calibration_log.md")
        rc = cmd_generate_evidence(args)
        assert rc == 1

    def test_generates_evidence_file_fail_verdict(self, tmp_path):
        """generate-evidence with overall_verdict=FAIL exits 0 and file contains FAIL narrative."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        import json
        (ws / "algorithm_summary.json").write_text(json.dumps({
            "algorithm_name": "blis-routing-v1",
            "evolve_block_source": "routing/",
        }))
        (ws / "validation_results.json").write_text(json.dumps({
            "suite_a": {"passed": False, "kendall_tau": 0.60,
                        "max_abs_error": 0.05, "tuple_count": 100},
            "suite_b": {"passed": True, "rank_stability_tau": 1.0,
                        "threshold_crossing_pct": 0.0, "informational_only": True},
            "suite_c": {"passed": True, "deterministic": True,
                        "max_pile_on_ratio": 1.1},
            "benchmark": {
                "passed": False,
                "mechanism_check_verdict": "FAIL",
                "t_eff": 0.20,
                "workload_classification": [
                    {"workload": "glia-40qps", "classification": "matched",
                     "improvement": 0.00, "matched_signals": ["KVUtilization"]},
                ],
                "specificity_notes": [],
            },
            "overall_verdict": "FAIL",
            "noise_cv": 0.10,
        }))
        out = tmp_path / "transfer_evidence.md"
        from tools.transfer_cli import cmd_generate_evidence
        import argparse
        args = argparse.Namespace(workspace=str(ws), out=str(out),
                                  calibration_log="docs/transfer/calibration_log.md")
        rc = cmd_generate_evidence(args)
        assert rc == 0, f"generate-evidence should exit 0 even on FAIL verdict, got {rc}"
        content = out.read_text()
        assert "FAIL" in content
        assert "noise floor" in content  # from the FAIL narrative string

    def test_missing_benchmark_key_exits_1(self, tmp_path):
        """generate-evidence exits 1 when validation_results.json has no 'benchmark' key."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        import json
        (ws / "algorithm_summary.json").write_text(json.dumps({
            "algorithm_name": "blis-routing-v1",
            "evolve_block_source": "routing/",
        }))
        (ws / "validation_results.json").write_text(json.dumps({
            "suite_a": {"passed": True, "kendall_tau": 0.92,
                        "max_abs_error": 0.001, "tuple_count": 100},
            "suite_b": {"passed": True, "rank_stability_tau": 1.0,
                        "threshold_crossing_pct": 0.0, "informational_only": True},
            "suite_c": {"passed": True, "deterministic": True,
                        "max_pile_on_ratio": 1.1},
            "overall_verdict": "PASS",
            "noise_cv": 0.03,
            # deliberately omit "benchmark" key
        }))
        out = tmp_path / "transfer_evidence.md"
        from tools.transfer_cli import cmd_generate_evidence
        import argparse
        args = argparse.Namespace(workspace=str(ws), out=str(out),
                                  calibration_log="docs/transfer/calibration_log.md")
        rc = cmd_generate_evidence(args)
        assert rc == 1, f"Missing 'benchmark' key should exit 1, got {rc}"


class TestEndToEndLocal:
    """Full local pipeline: convert-trace → benchmark → generate-evidence."""

    def _setup_workspace(self, tmp_path):
        import csv
        import json
        import yaml

        ws = tmp_path / "workspace"
        ws.mkdir()

        # algorithm_summary — only schema-valid fields (additionalProperties: false)
        (ws / "algorithm_summary.json").write_text(json.dumps({
            "algorithm_name": "blis-routing-v1",
            "evolve_block_source": "routing/",
        }))

        # signal_coverage (prod_access_path required by signal_coverage.schema.json)
        (ws / "signal_coverage.json").write_text(json.dumps({
            "signals": [{"sim_name": "KVUtilization", "prod_name": "kv",
                         "prod_access_path": "node.status.kv_utilization",
                         "fidelity_rating": "high", "staleness_window_ms": 0,
                         "mapped": True}],
            "unmapped_signals": [], "commit_hash": "abc", "coverage_complete": True,
        }))

        # validation_results (from Suites A/B/C — pre-existing, no benchmark yet)
        (ws / "validation_results.json").write_text(json.dumps({
            "suite_a": {"passed": True, "kendall_tau": 0.93,
                        "max_abs_error": 0.0001, "tuple_count": 120},
            "suite_b": {"passed": True, "rank_stability_tau": 1.0,
                        "threshold_crossing_pct": 0.0, "informational_only": True},
            "suite_c": {"passed": True, "deterministic": True,
                        "max_pile_on_ratio": 1.05},
        }))

        # workloads dir
        wd = tmp_path / "workloads"
        wd.mkdir()
        (wd / "workload_glia-40qps.yaml").write_text(
            yaml.dump({"version": "1", "kv_utilization": 0.5})
        )
        (wd / "workload_prefix-heavy.yaml").write_text(
            yaml.dump({"version": "1", "aggregate_rate": 85})
        )

        def write_tv2(d, ttft_us=100000, chunks=5):
            d.mkdir(parents=True)
            (d / "trace_header.yaml").write_text("trace_version: 2\n")
            with open(d / "trace_data.csv", "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["request_id", "send_time_us", "first_chunk_time_us",
                             "last_chunk_time_us", "num_chunks", "status", "error_message"])
                for i in range(10):
                    w.writerow([i, 0, ttft_us + i * 1000,
                                 ttft_us + 50000 + i * 1000, chunks, "ok", ""])

        # noise: 5 runs × 2 workloads
        raw_noise = tmp_path / "noise_raw"
        for wl in ["glia-40qps", "prefix-heavy"]:
            for r in range(5):
                write_tv2(raw_noise / wl / f"run-{r}", ttft_us=100000 + r * 500)

        # baseline and treatment (single run per workload)
        raw_bl = tmp_path / "baseline_raw"
        raw_tr = tmp_path / "treatment_raw"
        for wl, base_ttft in [("glia-40qps", 100000), ("prefix-heavy", 120000)]:
            write_tv2(raw_bl / wl, ttft_us=base_ttft)
            write_tv2(raw_tr / wl, ttft_us=int(base_ttft * 0.82))  # ~18% improvement

        return ws, wd, raw_noise, raw_bl, raw_tr

    def test_full_local_pipeline(self, tmp_path):
        import argparse
        import json

        from tools.transfer_cli import (cmd_benchmark_new, cmd_convert_trace,
                                        cmd_generate_evidence)

        ws, wd, raw_noise, raw_bl, raw_tr = self._setup_workspace(tmp_path)

        # convert-trace for all three phases
        for phase, raw_dir in [("noise", raw_noise),
                                ("baseline", raw_bl),
                                ("treatment", raw_tr)]:
            args = argparse.Namespace(
                input_dir=str(raw_dir),
                output=str(ws / f"{phase}_results.json"),
            )
            rc = cmd_convert_trace(args)
            assert rc == 0, f"convert-trace failed for {phase}"

        # benchmark
        args = argparse.Namespace(
            noise=str(ws / "noise_results.json"),
            baseline=str(ws / "baseline_results.json"),
            treatment=str(ws / "treatment_results.json"),
            signal_coverage=str(ws / "signal_coverage.json"),
            workloads_dir=str(wd),
            out=str(tmp_path / "benchmark_output.json"),
        )
        rc = cmd_benchmark_new(args)
        assert rc == 0, "benchmark failed"
        bench_out = json.loads((tmp_path / "benchmark_output.json").read_text())
        assert bench_out["mechanism_check_verdict"] == "PASS"

        # Merge benchmark output into validation_results (matches Step 5c-merge)
        val = json.loads((ws / "validation_results.json").read_text())
        val["benchmark"] = {k: v for k, v in bench_out.items() if k != "noise_cv"}
        val["overall_verdict"] = "PASS"
        val["noise_cv"] = bench_out["noise_cv"]
        (ws / "validation_results.json").write_text(json.dumps(val))

        # generate-evidence
        args_ev = argparse.Namespace(
            workspace=str(ws),
            out=str(ws / "transfer_evidence.md"),
            calibration_log="docs/transfer/calibration_log.md",
        )
        rc = cmd_generate_evidence(args_ev)
        assert rc == 0, "generate-evidence failed"
        evidence = (ws / "transfer_evidence.md").read_text()
        assert "PASS" in evidence
        assert "blis-routing-v1" in evidence


# ---------------------------------------------------------------------------
# TestMergeValues
# ---------------------------------------------------------------------------

class TestMergeValues:
    """Tests for the merge-values subcommand."""

    def _write_yaml(self, path: Path, data: dict) -> None:
        import yaml
        path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))

    def _load_yaml(self, path: Path) -> dict:
        import yaml
        return yaml.safe_load(path.read_text()) or {}

    def _minimal_algorithm_values(self, **overrides) -> dict:
        """Return a minimal valid algorithm_values dict (all required keys present)."""
        base = {
            "stack": {
                "model": {
                    "modelName": "Org/Model-7B",
                    "helmValues": {
                        "modelArtifacts": {
                            "name": "Org/Model-7B",
                            "uri": "pvc://model-pvc/models/Model-7B",
                        },
                        "decode": {
                            "replicas": 2,
                            "containers": [{"image": "vllm/vllm-openai:v0.11.0"}],
                        },
                    },
                },
                "gaie": {
                    "treatment": {
                        "helmValues": {
                            "inferenceExtension": {
                                "pluginsCustomConfig": {
                                    "custom-plugins.yaml": "treatment config"
                                }
                            }
                        }
                    }
                },
            },
            "observe": {
                "image": "ghcr.io/inference-sim/blis:v0.6.13",
                "workloads": [{"name": "wl-a", "spec": "version: '1'"}],
            },
        }
        return base

    def _minimal_env_defaults(self) -> dict:
        """Return a minimal valid env_defaults dict."""
        return {
            "stack": {
                "gateway": {
                    "helmValues": {
                        "gateway": {
                            "provider": "istio",
                            "gatewayClassName": "istio",
                        }
                    }
                }
            }
        }

    def test_basic_deep_merge(self, tmp_path):
        """Deep merge: nested dict keys are merged recursively."""
        env_file = tmp_path / "env.yaml"
        alg_file = tmp_path / "alg.yaml"
        out_file = tmp_path / "out.yaml"

        # Use minimal env_defaults (provides required gateway key) and add test key 'a'
        env = self._minimal_env_defaults()
        env["a"] = {"x": 1, "y": 2}
        self._write_yaml(env_file, env)
        # Algorithm has all required keys plus override for 'a'
        alg = self._minimal_algorithm_values()
        alg["a"] = {"y": 9, "z": 3}
        self._write_yaml(alg_file, alg)

        rc, out, err = _run_cli(
            "merge-values",
            "--env", str(env_file),
            "--algorithm", str(alg_file),
            "--out", str(out_file),
        )
        assert rc == 0, f"exit {rc}: {err}"
        result = self._load_yaml(out_file)
        assert result["a"] == {"x": 1, "y": 9, "z": 3}, (
            f"Expected deep-merged a dict, got: {result.get('a')}"
        )

    def test_pipeline_key_stripped_from_output(self, tmp_path):
        """MV-pipeline: pipeline.fast_iteration in env_defaults must not appear in values.yaml.
        Also verifies that other env_defaults keys (e.g. stack.gateway) still pass through."""
        env_file = tmp_path / "env.yaml"
        alg_file = tmp_path / "alg.yaml"
        out_file = tmp_path / "out.yaml"

        env = self._minimal_env_defaults()
        env["pipeline"] = {"fast_iteration": True}
        self._write_yaml(env_file, env)
        self._write_yaml(alg_file, self._minimal_algorithm_values())

        rc, out, err = _run_cli(
            "merge-values",
            "--env", str(env_file),
            "--algorithm", str(alg_file),
            "--out", str(out_file),
        )
        assert rc == 0, f"exit {rc}: {err}"
        result = self._load_yaml(out_file)
        assert "pipeline" not in result, (
            f"'pipeline' key must not appear in merged values.yaml, got keys: {list(result)}"
        )
        # Other env_defaults keys still pass through
        assert "stack" in result, "env_defaults 'stack' key must still be present in output"

    def test_pipeline_key_in_algorithm_values_also_stripped(self, tmp_path):
        """MV-pipeline-alg: pipeline key in algorithm_values is also stripped from merged output.
        The strip runs after _deep_merge, so it removes the key regardless of which input file
        contains it. algorithm_values should never contain 'pipeline' in practice."""
        env_file = tmp_path / "env.yaml"
        alg_file = tmp_path / "alg.yaml"
        out_file = tmp_path / "out.yaml"

        self._write_yaml(env_file, self._minimal_env_defaults())
        alg = self._minimal_algorithm_values()
        alg["pipeline"] = {"fast_iteration": False}
        self._write_yaml(alg_file, alg)

        rc, out, err = _run_cli(
            "merge-values",
            "--env", str(env_file),
            "--algorithm", str(alg_file),
            "--out", str(out_file),
        )
        assert rc == 0, f"exit {rc}: {err}"
        result = self._load_yaml(out_file)
        # pipeline is stripped from merged output regardless of source input file
        assert "pipeline" not in result, (
            "pipeline key must be absent from values.yaml — stripped after deep_merge "
            "to guard against alg_data containing it"
        )

    def test_list_replacement(self, tmp_path):
        """List in overlay replaces list in base entirely (not appended)."""
        env_file = tmp_path / "env.yaml"
        alg_file = tmp_path / "alg.yaml"
        out_file = tmp_path / "out.yaml"

        env = self._minimal_env_defaults()
        env["stack"]["model"] = {
            "helmValues": {
                "decode": {
                    "replicas": 1,
                    "containers": [{"image": "old", "modelCommand": "vllmServe"}],
                },
                "modelArtifacts": {"name": "x", "uri": "pvc://x/y"},
            }
        }
        self._write_yaml(env_file, env)

        alg = self._minimal_algorithm_values()
        # Override containers with a new list (no modelCommand)
        alg["stack"]["model"]["helmValues"]["decode"]["containers"] = [{"image": "new"}]
        self._write_yaml(alg_file, alg)

        rc, out, err = _run_cli(
            "merge-values",
            "--env", str(env_file),
            "--algorithm", str(alg_file),
            "--out", str(out_file),
        )
        assert rc == 0, f"exit {rc}: {err}"
        result = self._load_yaml(out_file)
        containers = result["stack"]["model"]["helmValues"]["decode"]["containers"]
        assert containers == [{"image": "new"}], (
            f"Expected list replacement with [{{image: new}}], got: {containers}"
        )
        # modelCommand should be gone — list was replaced, not merged
        assert "modelCommand" not in containers[0], (
            "modelCommand should be absent after list replacement"
        )

    def test_gaie_shared_flattening(self, tmp_path):
        """gaie.shared.helmValues is merged as base into both phases, then shared is removed."""
        env_file = tmp_path / "env.yaml"
        alg_file = tmp_path / "alg.yaml"
        out_file = tmp_path / "out.yaml"

        env = {
            "stack": {
                "gateway": {
                    "helmValues": {
                        "gateway": {"provider": "istio", "gatewayClassName": "istio"}
                    }
                },
                "gaie": {
                    "shared": {
                        "helmValues": {
                            "provider": {"name": "istio"},
                            "flags": [{"name": "v", "value": 1}],
                        }
                    },
                    "baseline": {
                        "helmValues": {
                            "inferenceExtension": {
                                "pluginsCustomConfig": {
                                    "custom-plugins.yaml": "baseline config"
                                }
                            }
                        }
                    },
                },
            }
        }
        self._write_yaml(env_file, env)

        alg = self._minimal_algorithm_values()
        # treatment helmValues from algorithm — should also get shared merged in
        alg["stack"]["gaie"]["treatment"]["helmValues"]["inferenceExtension"] = {
            "pluginsCustomConfig": {"custom-plugins.yaml": "treatment config"}
        }
        self._write_yaml(alg_file, alg)

        rc, out, err = _run_cli(
            "merge-values",
            "--env", str(env_file),
            "--algorithm", str(alg_file),
            "--out", str(out_file),
        )
        assert rc == 0, f"exit {rc}: {err}"
        result = self._load_yaml(out_file)
        gaie = result["stack"]["gaie"]

        # shared must be absent
        assert "shared" not in gaie, f"gaie.shared should be removed from output, got keys: {list(gaie.keys())}"

        # baseline should have shared values merged in
        bl_hv = gaie["baseline"]["helmValues"]
        assert bl_hv.get("provider") == {"name": "istio"}, (
            f"baseline.helmValues.provider should be from shared, got: {bl_hv.get('provider')}"
        )
        assert bl_hv.get("flags") == [{"name": "v", "value": 1}], (
            f"baseline.helmValues.flags should be from shared, got: {bl_hv.get('flags')}"
        )
        # baseline pluginsCustomConfig should be preserved (from env base, not overridden)
        bl_pcc = bl_hv["inferenceExtension"]["pluginsCustomConfig"]
        assert bl_pcc.get("custom-plugins.yaml") == "baseline config", (
            f"baseline pluginsCustomConfig should be preserved, got: {bl_pcc}"
        )

        # treatment should have shared values merged in
        tr_hv = gaie["treatment"]["helmValues"]
        assert tr_hv.get("provider") == {"name": "istio"}, (
            f"treatment.helmValues.provider should be from shared, got: {tr_hv.get('provider')}"
        )
        # treatment pluginsCustomConfig should come from algorithm overlay
        tr_pcc = tr_hv["inferenceExtension"]["pluginsCustomConfig"]
        assert tr_pcc.get("custom-plugins.yaml") == "treatment config", (
            f"treatment pluginsCustomConfig should be from algorithm, got: {tr_pcc}"
        )

    def test_gaie_shared_removed_from_output(self, tmp_path):
        """gaie.shared key is absent in output even when present in env_defaults."""
        env_file = tmp_path / "env.yaml"
        alg_file = tmp_path / "alg.yaml"
        out_file = tmp_path / "out.yaml"

        env = self._minimal_env_defaults()
        env["stack"]["gaie"] = {
            "shared": {"helmValues": {"provider": {"name": "istio"}}},
        }
        self._write_yaml(env_file, env)
        self._write_yaml(alg_file, self._minimal_algorithm_values())

        rc, out, err = _run_cli(
            "merge-values",
            "--env", str(env_file),
            "--algorithm", str(alg_file),
            "--out", str(out_file),
        )
        assert rc == 0, f"exit {rc}: {err}"
        result = self._load_yaml(out_file)
        assert "shared" not in result["stack"]["gaie"], (
            "gaie.shared must not appear in output"
        )

    def test_missing_required_model_name_exits_1(self, tmp_path):
        """Missing stack.model.modelName in merged output → exit 1 (validation failure)."""
        env_file = tmp_path / "env.yaml"
        alg_file = tmp_path / "alg.yaml"
        out_file = tmp_path / "out.yaml"

        self._write_yaml(env_file, self._minimal_env_defaults())

        # algorithm_values without modelName
        alg = self._minimal_algorithm_values()
        del alg["stack"]["model"]["modelName"]
        self._write_yaml(alg_file, alg)

        rc, out, err = _run_cli(
            "merge-values",
            "--env", str(env_file),
            "--algorithm", str(alg_file),
            "--out", str(out_file),
        )
        assert rc == 1, f"Expected exit 1 (validation), got {rc}. stderr: {err}"
        assert "stack.model.modelName" in err, (
            f"stderr should mention missing key, got: {err}"
        )

    def test_missing_env_file_exits_2(self, tmp_path):
        """Nonexistent --env path → exit 2 (infrastructure error)."""
        alg_file = tmp_path / "alg.yaml"
        out_file = tmp_path / "out.yaml"
        self._write_yaml(alg_file, self._minimal_algorithm_values())

        rc, out, err = _run_cli(
            "merge-values",
            "--env", str(tmp_path / "nonexistent_env.yaml"),
            "--algorithm", str(alg_file),
            "--out", str(out_file),
        )
        assert rc == 2, f"Expected exit 2 (infrastructure), got {rc}. stderr: {err}"

    def test_missing_algorithm_file_exits_2(self, tmp_path):
        """Nonexistent --algorithm path → exit 2 (infrastructure error)."""
        env_file = tmp_path / "env.yaml"
        out_file = tmp_path / "out.yaml"
        self._write_yaml(env_file, self._minimal_env_defaults())

        rc, out, err = _run_cli(
            "merge-values",
            "--env", str(env_file),
            "--algorithm", str(tmp_path / "nonexistent_alg.yaml"),
            "--out", str(out_file),
        )
        assert rc == 2, f"Expected exit 2 (infrastructure), got {rc}. stderr: {err}"

    def test_round_trip_matches_current_values_yaml(self, tmp_path):
        """Round-trip: env_defaults + algorithm_values → output has same top-level structure as values.yaml."""
        import yaml

        env_path = REPO_ROOT / "config" / "env_defaults.yaml"
        alg_path = REPO_ROOT / "workspace" / "tekton" / "algorithm_values.yaml"
        current_values_path = REPO_ROOT / "workspace" / "tekton" / "values.yaml"

        if not env_path.exists() or not current_values_path.exists():
            pytest.skip(
                f"Round-trip test skipped: missing {'config/env_defaults.yaml' if not env_path.exists() else 'workspace/tekton/values.yaml'}"
            )

        # If algorithm_values.yaml doesn't exist, derive it from values.yaml using schema-known keys
        if not alg_path.exists():
            current = yaml.safe_load(current_values_path.read_text()) or {}
            # Extract only BLIS-derived keys (model, gaie.treatment, observe)
            alg_data = {
                "stack": {
                    "model": current.get("stack", {}).get("model", {}),
                    "gaie": {
                        "treatment": current.get("stack", {}).get("gaie", {}).get("treatment", {})
                    },
                },
                "observe": current.get("observe", {}),
            }
            derived_alg_path = tmp_path / "algorithm_values.yaml"
            derived_alg_path.write_text(
                yaml.dump(alg_data, default_flow_style=False, sort_keys=False)
            )
            alg_path = derived_alg_path

        out_file = tmp_path / "values.yaml"
        rc, out, err = _run_cli(
            "merge-values",
            "--env", str(env_path),
            "--algorithm", str(alg_path),
            "--out", str(out_file),
        )
        assert rc == 0, f"exit {rc}: {err}"

        result = self._load_yaml(out_file)

        # Verify top-level sections are present
        assert "stack" in result, "output missing 'stack' key"
        assert "observe" in result, "output missing 'observe' key"
        assert "model" in result["stack"], "output missing 'stack.model'"
        assert "gaie" in result["stack"], "output missing 'stack.gaie'"
        assert "baseline" in result["stack"]["gaie"], "output missing 'stack.gaie.baseline'"
        assert "treatment" in result["stack"]["gaie"], "output missing 'stack.gaie.treatment'"

        # gaie.shared must be absent in output
        assert "shared" not in result["stack"]["gaie"], (
            "gaie.shared must be removed from output"
        )

        # observe.workloads must be a non-empty list
        workloads = result.get("observe", {}).get("workloads")
        assert isinstance(workloads, list) and len(workloads) > 0, (
            f"observe.workloads must be a non-empty list, got: {workloads}"
        )

    def test_epp_image_upstream_propagated_to_baseline(self, tmp_path):
        """merge-values sets inferenceExtension.image from epp_image.upstream in baseline."""
        env_file = tmp_path / "env.yaml"
        alg_file = tmp_path / "alg.yaml"
        out_file = tmp_path / "out.yaml"

        env = self._minimal_env_defaults()
        env.setdefault("stack", {})["gaie"] = {
            "epp_image": {
                "upstream": {
                    "hub": "ghcr.io/llm-d",
                    "name": "llm-d-inference-scheduler",
                    "tag": "v0.3.0",
                },
                "build": {
                    "hub": "ghcr.io/dev",
                    "name": "llm-d-inference-scheduler",
                    "platform": "linux/amd64",
                },
            },
            "shared": {"helmValues": {}},
            "baseline": {"helmValues": {}},
            "treatment": {"helmValues": {}},
        }
        self._write_yaml(env_file, env)
        self._write_yaml(alg_file, self._minimal_algorithm_values())

        rc, out, err = _run_cli(
            "merge-values",
            "--env", str(env_file),
            "--algorithm", str(alg_file),
            "--out", str(out_file),
        )
        assert rc == 0, f"exit {rc}: {err}"
        result = self._load_yaml(out_file)
        baseline_img = (result["stack"]["gaie"]["baseline"]["helmValues"]
                        .get("inferenceExtension", {}).get("image", {}))
        assert baseline_img == {
            "hub": "ghcr.io/llm-d",
            "name": "llm-d-inference-scheduler",
            "tag": "v0.3.0",
        }, f"Expected upstream image in baseline, got: {baseline_img}"

    def test_epp_image_treatment_preserved_if_set(self, tmp_path):
        """merge-values does not overwrite treatment.inferenceExtension.image if already set."""
        env_file = tmp_path / "env.yaml"
        alg_file = tmp_path / "alg.yaml"
        out_file = tmp_path / "out.yaml"

        env = self._minimal_env_defaults()
        env.setdefault("stack", {})["gaie"] = {
            "epp_image": {
                "upstream": {
                    "hub": "ghcr.io/llm-d",
                    "name": "llm-d-inference-scheduler",
                    "tag": "latest",
                },
                "build": {},
            },
            "shared": {"helmValues": {}},
            "baseline": {"helmValues": {}},
            "treatment": {"helmValues": {}},
        }
        self._write_yaml(env_file, env)

        alg = self._minimal_algorithm_values()
        # Pre-inject a treatment image as build-push-epp would
        (alg["stack"]["gaie"]["treatment"]["helmValues"]
         .setdefault("inferenceExtension", {})
         ["image"]) = {"hub": "ghcr.io/dev", "name": "llm-d-inference-scheduler",
                       "tag": "sim2real-abc12345"}
        self._write_yaml(alg_file, alg)

        rc, out, err = _run_cli(
            "merge-values",
            "--env", str(env_file),
            "--algorithm", str(alg_file),
            "--out", str(out_file),
        )
        assert rc == 0, f"exit {rc}: {err}"
        result = self._load_yaml(out_file)
        treatment_img = (result["stack"]["gaie"]["treatment"]["helmValues"]
                         .get("inferenceExtension", {}).get("image", {}))
        assert treatment_img == {
            "hub": "ghcr.io/dev",
            "name": "llm-d-inference-scheduler",
            "tag": "sim2real-abc12345",
        }, f"Expected preserved treatment image, got: {treatment_img}"

    def test_epp_image_removed_from_output(self, tmp_path):
        """merge-values removes the gaie.epp_image key from the output."""
        env_file = tmp_path / "env.yaml"
        alg_file = tmp_path / "alg.yaml"
        out_file = tmp_path / "out.yaml"

        env = self._minimal_env_defaults()
        env.setdefault("stack", {})["gaie"] = {
            "epp_image": {
                "upstream": {
                    "hub": "ghcr.io/llm-d",
                    "name": "llm-d-inference-scheduler",
                    "tag": "latest",
                },
                "build": {
                    "hub": "ghcr.io/dev",
                    "name": "llm-d-inference-scheduler",
                    "platform": "linux/amd64",
                },
            },
            "shared": {"helmValues": {}},
            "baseline": {"helmValues": {}},
            "treatment": {"helmValues": {}},
        }
        self._write_yaml(env_file, env)
        self._write_yaml(alg_file, self._minimal_algorithm_values())

        rc, out, err = _run_cli(
            "merge-values",
            "--env", str(env_file),
            "--algorithm", str(alg_file),
            "--out", str(out_file),
        )
        assert rc == 0, f"exit {rc}: {err}"
        result = self._load_yaml(out_file)
        assert "epp_image" not in result.get("stack", {}).get("gaie", {}), (
            "gaie.epp_image must be absent from merged output"
        )


    def test_vllm_image_override_applied(self, tmp_path):
        """When stack.model.vllm_image is set in env_defaults, decode.containers[0].image is replaced."""
        env_file = tmp_path / "env.yaml"
        alg_file = tmp_path / "alg.yaml"
        out_file = tmp_path / "out.yaml"

        env = self._minimal_env_defaults()
        env.setdefault("stack", {}).setdefault("model", {})["vllm_image"] = (
            "ghcr.io/llm-d/llm-d-cuda:v0.5.1"
        )
        self._write_yaml(env_file, env)

        alg = self._minimal_algorithm_values()
        # algorithm carries the original sim image
        alg["stack"]["model"]["helmValues"]["decode"]["containers"] = [
            {"image": "vllm/vllm-openai:v0.11.0"}
        ]
        self._write_yaml(alg_file, alg)

        rc, out, err = _run_cli(
            "merge-values",
            "--env", str(env_file),
            "--algorithm", str(alg_file),
            "--out", str(out_file),
        )
        assert rc == 0, f"exit {rc}: {err}"
        result = self._load_yaml(out_file)
        containers = result["stack"]["model"]["helmValues"]["decode"]["containers"]
        assert containers[0]["image"] == "ghcr.io/llm-d/llm-d-cuda:v0.5.1", (
            f"Expected override image, got: {containers[0]['image']}"
        )
        # vllm_image key must be absent from output
        assert "vllm_image" not in result["stack"]["model"], (
            "stack.model.vllm_image must be removed from output"
        )

    def test_vllm_image_override_absent_passthrough(self, tmp_path):
        """When stack.model.vllm_image is absent, original sim image passes through unchanged."""
        env_file = tmp_path / "env.yaml"
        alg_file = tmp_path / "alg.yaml"
        out_file = tmp_path / "out.yaml"

        # env_defaults has no vllm_image
        self._write_yaml(env_file, self._minimal_env_defaults())

        alg = self._minimal_algorithm_values()
        alg["stack"]["model"]["helmValues"]["decode"]["containers"] = [
            {"image": "vllm/vllm-openai:v0.11.0"}
        ]
        self._write_yaml(alg_file, alg)

        rc, out, err = _run_cli(
            "merge-values",
            "--env", str(env_file),
            "--algorithm", str(alg_file),
            "--out", str(out_file),
        )
        assert rc == 0, f"exit {rc}: {err}"
        result = self._load_yaml(out_file)
        containers = result["stack"]["model"]["helmValues"]["decode"]["containers"]
        assert containers[0]["image"] == "vllm/vllm-openai:v0.11.0", (
            f"Expected original sim image to pass through, got: {containers[0]['image']}"
        )


class TestBuildPushEpp:
    """Tests for the build-push-epp subcommand."""

    def _write_yaml(self, path: Path, data: dict) -> None:
        import yaml
        path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))

    def _load_yaml(self, path: Path) -> dict:
        import yaml
        return yaml.safe_load(path.read_text()) or {}

    def _env_defaults(self, hub="ghcr.io/testorg") -> dict:
        return {
            "stack": {
                "gateway": {
                    "helmValues": {
                        "gateway": {"provider": "istio", "gatewayClassName": "istio"}
                    }
                },
                "gaie": {
                    "epp_image": {
                        "upstream": {
                            "hub": "ghcr.io/llm-d",
                            "name": "llm-d-inference-scheduler",
                            "tag": "latest",
                        },
                        "build": {
                            "hub": hub,
                            "name": "llm-d-inference-scheduler",
                            "platform": "linux/amd64",
                        },
                    },
                    "shared": {"helmValues": {}},
                    "baseline": {"helmValues": {}},
                    "treatment": {"helmValues": {"inferenceExtension": {
                        "pluginsCustomConfig": {"custom-plugins.yaml": "cfg"}
                    }}},
                },
            }
        }

    def _algo_values(self) -> dict:
        return {
            "stack": {
                "model": {
                    "modelName": "Org/Model-7B",
                    "helmValues": {
                        "modelArtifacts": {"name": "Org/Model-7B",
                                           "uri": "pvc://model-pvc/models/Model-7B"},
                        "decode": {"replicas": 1,
                                   "containers": [{"image": "vllm/vllm-openai:v0.11.0"}]},
                    },
                },
                "gaie": {
                    "treatment": {"helmValues": {"inferenceExtension": {
                        "pluginsCustomConfig": {"custom-plugins.yaml": "cfg"}
                    }}}
                },
            },
            "observe": {
                "image": "ghcr.io/inference-sim/blis:v0.6.13",
                "workloads": [{"name": "wl-a", "spec": "version: '1'"}],
            },
        }

    def _make_ns(self, tmp_path, scheduler_dir, dry_run=False):
        import argparse
        return argparse.Namespace(
            scheduler_dir=str(scheduler_dir),
            env=str(tmp_path / "env.yaml"),
            values=str(tmp_path / "alg.yaml"),
            merged_values=str(tmp_path / "values.yaml"),
            dry_run=dry_run,
        )

    def _mock_run(self, sha="abcd1234", build_rc=0, push_rc=0):
        """Return a subprocess.run side_effect that mocks git + make calls."""
        from unittest.mock import MagicMock

        def _run(cmd, **kwargs):
            m = MagicMock()
            if "git" in cmd:
                m.returncode = 0
                m.stdout = f"{sha}\n"
                m.stderr = ""
            elif "make" in cmd:
                if "image-push-epp" in cmd:
                    m.returncode = push_rc
                else:
                    m.returncode = build_rc
                m.stdout = ""
                m.stderr = "build failed" if build_rc != 0 else ""
            return m

        return _run

    def test_exits_2_missing_scheduler_dir(self, tmp_path):
        """Exit 2 when --scheduler-dir does not exist."""
        import argparse
        sys.path.insert(0, str(Path(__file__).parent))
        from transfer_cli import cmd_build_push_epp

        self._write_yaml(tmp_path / "env.yaml", self._env_defaults())
        self._write_yaml(tmp_path / "alg.yaml", self._algo_values())
        ns = self._make_ns(tmp_path, tmp_path / "nonexistent")
        rc = cmd_build_push_epp(ns)
        assert rc == 2, f"Expected exit 2 for missing scheduler_dir, got {rc}"

    def test_exits_2_no_container_runtime(self, tmp_path):
        """Exit 2 when neither podman nor docker is on PATH."""
        from unittest.mock import patch
        sys.path.insert(0, str(Path(__file__).parent))
        from transfer_cli import cmd_build_push_epp

        scheduler_dir = tmp_path / "scheduler"
        scheduler_dir.mkdir()
        self._write_yaml(tmp_path / "env.yaml", self._env_defaults())
        self._write_yaml(tmp_path / "alg.yaml", self._algo_values())
        ns = self._make_ns(tmp_path, scheduler_dir)

        with patch("shutil.which", return_value=None):
            rc = cmd_build_push_epp(ns)
        assert rc == 2, f"Expected exit 2 when no container runtime found, got {rc}"

    def test_build_failure_exits_1(self, tmp_path):
        """Exit 1 when make image-build-epp fails."""
        from unittest.mock import patch
        sys.path.insert(0, str(Path(__file__).parent))
        from transfer_cli import cmd_build_push_epp

        scheduler_dir = tmp_path / "scheduler"
        scheduler_dir.mkdir()
        self._write_yaml(tmp_path / "env.yaml", self._env_defaults())
        self._write_yaml(tmp_path / "alg.yaml", self._algo_values())
        ns = self._make_ns(tmp_path, scheduler_dir)

        with patch("shutil.which", return_value="/usr/bin/podman"), \
             patch("subprocess.run", side_effect=self._mock_run(build_rc=1)):
            rc = cmd_build_push_epp(ns)
        assert rc == 1, f"Expected exit 1 on build failure, got {rc}"

    def test_dry_run_builds_but_skips_push_and_config(self, tmp_path):
        """--dry-run: build runs, push skipped, algorithm_values.yaml not modified."""
        from unittest.mock import patch
        sys.path.insert(0, str(Path(__file__).parent))
        from transfer_cli import cmd_build_push_epp

        scheduler_dir = tmp_path / "scheduler"
        scheduler_dir.mkdir()
        self._write_yaml(tmp_path / "env.yaml", self._env_defaults())
        self._write_yaml(tmp_path / "alg.yaml", self._algo_values())
        ns = self._make_ns(tmp_path, scheduler_dir, dry_run=True)

        make_calls = []

        def tracking_run(cmd, **kwargs):
            from unittest.mock import MagicMock
            m = MagicMock()
            if "git" in cmd:
                m.returncode = 0
                m.stdout = "abcd1234\n"
                m.stderr = ""
            elif "make" in cmd:
                make_calls.append(list(cmd))
                m.returncode = 0
                m.stdout = ""
                m.stderr = ""
            return m

        with patch("shutil.which", return_value="/usr/bin/podman"), \
             patch("subprocess.run", side_effect=tracking_run):
            rc = cmd_build_push_epp(ns)

        assert rc == 0, f"Expected exit 0 for dry-run, got {rc}"
        assert any("image-build-epp" in str(c) for c in make_calls), \
            "image-build-epp should be called in dry-run"
        assert not any("image-push-epp" in str(c) for c in make_calls), \
            "image-push-epp must NOT be called in dry-run"
        algo_data = self._load_yaml(tmp_path / "alg.yaml")
        img = (algo_data.get("stack", {}).get("gaie", {})
               .get("treatment", {}).get("helmValues", {})
               .get("inferenceExtension", {}).get("image"))
        assert img is None, f"algorithm_values.yaml must not be modified in dry-run, got image={img}"

    def test_tag_derived_from_scheduler_commit(self, tmp_path):
        """Tag passed to make is 'sim2real-<sha>' where sha comes from git rev-parse."""
        from unittest.mock import patch
        sys.path.insert(0, str(Path(__file__).parent))
        from transfer_cli import cmd_build_push_epp

        scheduler_dir = tmp_path / "scheduler"
        scheduler_dir.mkdir()
        self._write_yaml(tmp_path / "env.yaml", self._env_defaults())
        self._write_yaml(tmp_path / "alg.yaml", self._algo_values())
        ns = self._make_ns(tmp_path, scheduler_dir, dry_run=True)

        observed_tags = []

        def tracking_run(cmd, **kwargs):
            from unittest.mock import MagicMock
            m = MagicMock()
            if "git" in cmd:
                m.returncode = 0
                m.stdout = "deadbeef\n"
                m.stderr = ""
            elif "make" in cmd:
                for arg in cmd:
                    if arg.startswith("EPP_TAG="):
                        observed_tags.append(arg.split("=", 1)[1])
                m.returncode = 0
                m.stdout = ""
                m.stderr = ""
            return m

        with patch("shutil.which", return_value="/usr/bin/docker"), \
             patch("subprocess.run", side_effect=tracking_run):
            rc = cmd_build_push_epp(ns)

        assert rc == 0
        assert observed_tags, "EPP_TAG= should have been passed to make"
        assert observed_tags[0] == "sim2real-deadbeef", \
            f"Expected tag 'sim2real-deadbeef', got '{observed_tags[0]}'"

    def test_success_updates_algorithm_values(self, tmp_path):
        """Success: algorithm_values.yaml gains inferenceExtension.image under treatment."""
        from unittest.mock import patch
        sys.path.insert(0, str(Path(__file__).parent))
        from transfer_cli import cmd_build_push_epp

        scheduler_dir = tmp_path / "scheduler"
        scheduler_dir.mkdir()
        self._write_yaml(tmp_path / "env.yaml", self._env_defaults(hub="ghcr.io/myorg"))
        self._write_yaml(tmp_path / "alg.yaml", self._algo_values())
        ns = self._make_ns(tmp_path, scheduler_dir, dry_run=False)

        with patch("shutil.which", return_value="/usr/bin/podman"), \
             patch("subprocess.run", side_effect=self._mock_run(sha="cafebabe")):
            rc = cmd_build_push_epp(ns)

        assert rc == 0, f"Expected exit 0, got {rc}"
        algo_data = self._load_yaml(tmp_path / "alg.yaml")
        img = (algo_data.get("stack", {}).get("gaie", {})
               .get("treatment", {}).get("helmValues", {})
               .get("inferenceExtension", {}).get("image", {}))
        assert img.get("hub") == "ghcr.io/myorg", f"hub mismatch: {img}"
        assert img.get("name") == "llm-d-inference-scheduler", f"name mismatch: {img}"
        assert img.get("tag") == "sim2real-cafebabe", f"tag mismatch: {img}"
        # merge-values should have produced a values.yaml
        assert (tmp_path / "values.yaml").exists(), \
            "merged values.yaml should be written after push"


# ---------------------------------------------------------------------------
# Helpers for TestAppendCalibrationLog (added in PR6)
# ---------------------------------------------------------------------------

TOOLS_DIR = Path(__file__).parent


def _run_cli(*args, cwd=None):
    """Run transfer_cli.py; return (exit_code, stdout, stderr)."""
    cmd = [sys.executable, str(TOOLS_DIR / "transfer_cli.py")] + list(args)
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd or TOOLS_DIR.parent)
    return result.returncode, result.stdout, result.stderr


def _write_algorithm_summary(ws: Path, algorithm_name: str = "test_algo") -> None:
    # Note: pipeline_commit is NOT a field in algorithm_summary.json schema.
    # The CLI reads algorithm_name; pipeline_commit falls back to git rev-parse HEAD.
    (ws / "algorithm_summary.json").write_text(json.dumps({
        "algorithm_name": algorithm_name,
        "evolve_block_source": "blis_router/best/best_program.go:1-5",
        "evolve_block_content_hash": "abc123",
        "signals": [], "composite_signals": [],
        "metrics": {"combined_score": 0.0},
        "scope_validation_passed": True,
        "mapping_artifact_version": "1.0",
        "fidelity_checked": True,
    }))


def _write_validation_results(ws: Path, verdict: str = "PASS") -> None:
    data = {
        "suite_a": {"passed": True, "kendall_tau": 0.92, "max_abs_error": 0.01, "tuple_count": 200},
        "suite_b": {"passed": True, "rank_stability_tau": 0.95,
                    "threshold_crossing_pct": 0.0, "informational_only": True},
        "suite_c": {"passed": True, "deterministic": True, "max_pile_on_ratio": 1.1},
        "benchmark": {
            "passed": True, "mechanism_check_verdict": "PASS", "t_eff": 0.05,
            "workload_classification": [
                {"workload": "wl-a", "classification": "matched",
                 "improvement": 0.12, "matched_signals": ["queue_depth"]}
            ],
            "specificity_notes": [],
        },
        "overall_verdict": verdict,
        "noise_cv": 0.03,
    }
    if verdict == "INCONCLUSIVE":
        data["operator_notes"] = "Improvement marginally below T_eff; operator approves"
    (ws / "validation_results.json").write_text(json.dumps(data))


def _write_calibration_log(cal: Path, n_entries: int = 0) -> None:
    header = ("# Transfer Pipeline Calibration Log\n\nAppend-only.\n\n"
              "## Entries\n\n<!-- Stage 6 appends entries below this line -->\n")
    entries = "".join(
        f"\n### Transfer: prior_algo_{i}\n```yaml\ntransfer_date: 2026-01-0{i+1}\n```\n"
        for i in range(n_entries)
    )
    cal.write_text(header + entries)


class TestAppendCalibrationLog:
    def test_appends_entry_to_empty_log(self, tmp_path):
        """BC-5: happy path — appends entry when log has 0 existing entries."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        _write_algorithm_summary(ws)
        _write_validation_results(ws)
        cal = tmp_path / "calibration_log.md"
        _write_calibration_log(cal, n_entries=0)
        rc, out, err = _run_cli(
            "append-calibration-log",
            "--workspace", str(ws),
            "--calibration-log", str(cal),
        )
        assert rc == 0, f"exit {rc}: {err}"
        content = cal.read_text()
        assert content.count("### Transfer:") == 1
        assert "test_algo" in content

    def test_appends_entry_to_existing_log(self, tmp_path):
        """BC-5: appends entry when log already has N entries."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        _write_algorithm_summary(ws, "second_algo")
        _write_validation_results(ws)
        cal = tmp_path / "calibration_log.md"
        _write_calibration_log(cal, n_entries=2)
        rc, out, err = _run_cli(
            "append-calibration-log",
            "--workspace", str(ws),
            "--calibration-log", str(cal),
        )
        assert rc == 0, f"exit {rc}: {err}"
        assert cal.read_text().count("### Transfer:") == 3

    def test_missing_algorithm_summary_exits_2(self, tmp_path):
        """BC-11: exits 2 when algorithm_summary.json is absent."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        _write_validation_results(ws)
        cal = tmp_path / "calibration_log.md"
        _write_calibration_log(cal)
        rc, out, err = _run_cli(
            "append-calibration-log",
            "--workspace", str(ws),
            "--calibration-log", str(cal),
        )
        assert rc == 2, f"expected exit 2, got {rc}"
        assert "algorithm_summary.json" in err

    def test_missing_validation_results_exits_2(self, tmp_path):
        """BC-11: exits 2 when validation_results.json is absent."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        _write_algorithm_summary(ws)
        cal = tmp_path / "calibration_log.md"
        _write_calibration_log(cal)
        rc, out, err = _run_cli(
            "append-calibration-log",
            "--workspace", str(ws),
            "--calibration-log", str(cal),
        )
        assert rc == 2, f"expected exit 2, got {rc}"
        assert "validation_results.json" in err

    def test_corruption_detected_exits_1(self, tmp_path, monkeypatch):
        """BC-12: exit 1 when count mismatch detected after append.

        Uses monkeypatch on pathlib.Path.write_text to inject an extra ### Transfer:
        sentinel after the CLI's append, simulating a concurrent write to the file.
        Calls cmd_append_calibration_log directly (not via subprocess) so the
        monkeypatch takes effect in-process.
        """
        import argparse
        sys.path.insert(0, str(Path(__file__).parent))
        import transfer_cli  # the actual module under test

        ws = tmp_path / "workspace"
        ws.mkdir()
        _write_algorithm_summary(ws)
        _write_validation_results(ws)
        cal = tmp_path / "calibration_log.md"
        _write_calibration_log(cal, n_entries=1)

        # Patch Path.write_text so that after the CLI writes its entry,
        # we also inject an extra sentinel — simulating a concurrent process.
        real_write = Path.write_text
        patched = [False]

        def injecting_write(path_self, data, *args, **kwargs):
            real_write(path_self, data, *args, **kwargs)
            if not patched[0] and str(path_self) == str(cal):
                patched[0] = True
                # Overwrite with extra sentinel to trigger count mismatch
                real_write(path_self, data + "\n### Transfer: injected\n```yaml\n```\n")

        monkeypatch.setattr(Path, "write_text", injecting_write)

        args = argparse.Namespace(workspace=str(ws), calibration_log=str(cal))
        rc = transfer_cli.cmd_append_calibration_log(args)
        assert rc == 1, f"expected exit 1 (corruption detected), got {rc}"

    def test_inconclusive_with_operator_notes_succeeds(self, tmp_path):
        """BC-2: INCONCLUSIVE verdict with operator_notes is accepted."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        _write_algorithm_summary(ws)
        _write_validation_results(ws, verdict="INCONCLUSIVE")
        cal = tmp_path / "calibration_log.md"
        _write_calibration_log(cal)
        rc, out, err = _run_cli(
            "append-calibration-log",
            "--workspace", str(ws),
            "--calibration-log", str(cal),
        )
        assert rc == 0, f"exit {rc}: {err}"
        assert "INCONCLUSIVE" in cal.read_text()


class TestCompare:
    """Tests for the compare subcommand."""

    def _write_results(self, path: Path, workloads: list) -> None:
        path.write_text(json.dumps({"workloads": workloads}))

    def _workload(self, name: str, ttft_p50=100.0, ttft_p99=200.0,
                  tpot_p50=30.0, tpot_p99=50.0) -> dict:
        return {
            "name": name,
            "metrics": {
                "ttft_p50": ttft_p50, "ttft_p99": ttft_p99,
                "tpot_p50": tpot_p50, "tpot_p99": tpot_p99,
            }
        }

    def test_happy_path_single_workload(self, tmp_path):
        """CMP-1: single matched workload produces table, exit 0."""
        b = tmp_path / "baseline.json"
        t = tmp_path / "treatment.json"
        self._write_results(b, [self._workload("wl1", ttft_p50=100.0)])
        self._write_results(t, [self._workload("wl1", ttft_p50=90.0)])
        rc, out, err = _run_cli("compare", "--baseline", str(b), "--treatment", str(t))
        assert rc == 0, f"exit {rc}: {err}"
        assert "wl1" in out
        assert "TTFT p50" in out
        assert "better" in out  # 90 < 100

    def test_happy_path_writes_out_file(self, tmp_path):
        """CMP-2: --out writes comparison table to file."""
        b = tmp_path / "baseline.json"
        t = tmp_path / "treatment.json"
        out_file = tmp_path / "table.txt"
        self._write_results(b, [self._workload("wl1")])
        self._write_results(t, [self._workload("wl1", ttft_p50=110.0)])
        rc, out, err = _run_cli("compare", "--baseline", str(b), "--treatment", str(t),
                                "--out", str(out_file))
        assert rc == 0, f"exit {rc}: {err}"
        assert out_file.exists()
        assert "TTFT p50" in out_file.read_text()

    def test_worse_label_when_treatment_higher(self, tmp_path):
        """CMP-3: positive delta (higher latency) labelled 'worse'."""
        b = tmp_path / "baseline.json"
        t = tmp_path / "treatment.json"
        self._write_results(b, [self._workload("wl1", ttft_p50=100.0)])
        self._write_results(t, [self._workload("wl1", ttft_p50=120.0)])
        rc, out, err = _run_cli("compare", "--baseline", str(b), "--treatment", str(t))
        assert rc == 0, f"exit {rc}: {err}"
        assert "worse" in out

    def test_partial_mismatch_warns_and_exits_0(self, tmp_path):
        """CMP-4: partial workload mismatch — matched workloads shown, WARN for each unmatched, exit 0."""
        b = tmp_path / "baseline.json"
        t = tmp_path / "treatment.json"
        self._write_results(b, [self._workload("wl1"), self._workload("wl2")])
        self._write_results(t, [self._workload("wl1"), self._workload("wl3")])
        rc, out, err = _run_cli("compare", "--baseline", str(b), "--treatment", str(t))
        assert rc == 0, f"exit {rc}: {err}"
        assert "wl1" in out
        assert "WARN" in err
        assert "wl2" in err  # wl2 missing from treatment
        assert "wl3" in err  # wl3 missing from baseline

    def test_no_paired_workloads_exits_1(self, tmp_path):
        """CMP-5: no workloads can be paired — exit 1."""
        b = tmp_path / "baseline.json"
        t = tmp_path / "treatment.json"
        self._write_results(b, [self._workload("wl_a")])
        self._write_results(t, [self._workload("wl_b")])
        rc, out, err = _run_cli("compare", "--baseline", str(b), "--treatment", str(t))
        assert rc == 1, f"expected exit 1, got {rc}"

    def test_missing_baseline_exits_1(self, tmp_path):
        """CMP-6: missing baseline file — exit 1."""
        t = tmp_path / "treatment.json"
        self._write_results(t, [self._workload("wl1")])
        rc, out, err = _run_cli("compare",
                                "--baseline", str(tmp_path / "nope.json"),
                                "--treatment", str(t))
        assert rc == 1, f"expected exit 1, got {rc}"

    def test_malformed_treatment_exits_1(self, tmp_path):
        """CMP-7: malformed treatment JSON — exit 1."""
        b = tmp_path / "baseline.json"
        t = tmp_path / "treatment.json"
        self._write_results(b, [self._workload("wl1")])
        t.write_text("not json {{{")
        rc, out, err = _run_cli("compare", "--baseline", str(b), "--treatment", str(t))
        assert rc == 1, f"expected exit 1, got {rc}"

    def test_multiple_workloads_produces_sections(self, tmp_path):
        """CMP-8: multiple workloads produce one section each."""
        b = tmp_path / "baseline.json"
        t = tmp_path / "treatment.json"
        self._write_results(b, [self._workload("wl1"), self._workload("wl2")])
        self._write_results(t, [self._workload("wl1"), self._workload("wl2")])
        rc, out, err = _run_cli("compare", "--baseline", str(b), "--treatment", str(t))
        assert rc == 0, f"exit {rc}: {err}"
        assert out.count("=== Workload:") == 2

    def test_zero_baseline_shows_na_percent(self, tmp_path):
        """CMP-9: zero baseline value shows N/A for percentage instead of 0.0%."""
        b = tmp_path / "baseline.json"
        t = tmp_path / "treatment.json"
        self._write_results(b, [self._workload("wl1", ttft_p50=0.0)])
        self._write_results(t, [self._workload("wl1", ttft_p50=50.0)])
        rc, out, err = _run_cli("compare", "--baseline", str(b), "--treatment", str(t))
        assert rc == 0, f"exit {rc}: {err}"
        assert "N/A" in out  # percentage should be N/A, not 0.0%
        assert "0.0%" not in out.split("TTFT p50")[1].split("\n")[0]  # no fabricated 0.0%

    def test_workload_missing_name_warns(self, tmp_path):
        """CMP-10: workload entry missing 'name' key emits WARN and is skipped."""
        b = tmp_path / "baseline.json"
        t = tmp_path / "treatment.json"
        b.write_text(json.dumps({"workloads": [{"metrics": {"ttft_p50": 1, "ttft_p99": 2,
                                                             "tpot_p50": 3, "tpot_p99": 4}}]}))
        self._write_results(t, [self._workload("wl1")])
        rc, out, err = _run_cli("compare", "--baseline", str(b), "--treatment", str(t))
        # The malformed baseline entry is skipped with WARN; no paired workloads → exit 1
        assert rc == 1
        assert "WARN" in err

    def test_missing_treatment_file_exits_1(self, tmp_path):
        """CMP-11: missing treatment file — exit 1."""
        b = tmp_path / "baseline.json"
        self._write_results(b, [self._workload("wl1")])
        rc, out, err = _run_cli("compare",
                                "--baseline", str(b),
                                "--treatment", str(tmp_path / "nope.json"))
        assert rc == 1, f"expected exit 1, got {rc}"

    def test_no_change_label(self, tmp_path):
        """CMP-12: equal baseline and treatment values show 'no change' label."""
        b = tmp_path / "baseline.json"
        t = tmp_path / "treatment.json"
        self._write_results(b, [self._workload("wl1", ttft_p50=100.0)])
        self._write_results(t, [self._workload("wl1", ttft_p50=100.0)])
        rc, out, err = _run_cli("compare", "--baseline", str(b), "--treatment", str(t))
        assert rc == 0, f"exit {rc}: {err}"
        assert "no change" in out

    def test_happy_path_stdout_with_out_file(self, tmp_path):
        """CMP-13: --out writes file AND stdout still contains the table."""
        b = tmp_path / "baseline.json"
        t = tmp_path / "treatment.json"
        out_file = tmp_path / "table.txt"
        self._write_results(b, [self._workload("wl1")])
        self._write_results(t, [self._workload("wl1", ttft_p50=90.0)])
        rc, out, err = _run_cli("compare", "--baseline", str(b), "--treatment", str(t),
                                "--out", str(out_file))
        assert rc == 0, f"exit {rc}: {err}"
        assert "TTFT p50" in out          # stdout has the table
        assert out_file.exists()
        assert "TTFT p50" in out_file.read_text()  # file also has the table

    def test_malformed_baseline_exits_1(self, tmp_path):
        """CMP-14: malformed JSON in --baseline file → exit 1 (symmetric with CMP-7 for treatment)."""
        b = tmp_path / "baseline.json"
        t = tmp_path / "treatment.json"
        b.write_text("not json {{{")
        self._write_results(t, [self._workload("wl1")])
        rc, out, err = _run_cli("compare", "--baseline", str(b), "--treatment", str(t))
        assert rc == 1, f"Expected exit 1 for malformed baseline, got {rc}"
        assert "ERROR" in err

    def test_empty_workloads_list_exits_1(self, tmp_path):
        """CMP-15: baseline file with empty workloads list → exit 1 (no pairs possible)."""
        b = tmp_path / "baseline.json"
        t = tmp_path / "treatment.json"
        b.write_text(json.dumps({"workloads": []}))
        self._write_results(t, [self._workload("wl1")])
        rc, out, err = _run_cli("compare", "--baseline", str(b), "--treatment", str(t))
        assert rc == 1, f"Expected exit 1 for empty workloads, got {rc}"

    def test_missing_workloads_key_exits_1(self, tmp_path):
        """CMP-16: baseline file missing 'workloads' key entirely → exit 1."""
        b = tmp_path / "baseline.json"
        t = tmp_path / "treatment.json"
        b.write_text(json.dumps({"results": []}))
        self._write_results(t, [self._workload("wl1")])
        rc, out, err = _run_cli("compare", "--baseline", str(b), "--treatment", str(t))
        assert rc == 1, f"Expected exit 1 for missing workloads key, got {rc}"
        assert "ERROR" in err

    def test_partial_metric_keys_shows_na_for_missing(self, tmp_path):
        """CMP-17: workload with one metric key absent → that row shows N/A, exit 0, warning on stderr."""
        b = tmp_path / "baseline.json"
        t = tmp_path / "treatment.json"
        # baseline missing tpot_p99
        b.write_text(json.dumps({"workloads": [{"name": "wl1", "metrics": {
            "ttft_p50": 100.0, "ttft_p99": 200.0, "tpot_p50": 30.0
        }}]}))
        self._write_results(t, [self._workload("wl1")])
        rc, out, err = _run_cli("compare", "--baseline", str(b), "--treatment", str(t))
        assert rc == 0, f"exit {rc}: {err}"
        assert "N/A" in out
        assert "WARN" in err
        assert "tpot_p99" in err

    def test_numeric_delta_and_percent_values(self, tmp_path):
        """CMP-18: verify computed delta and percentage are arithmetically correct."""
        b = tmp_path / "baseline.json"
        t = tmp_path / "treatment.json"
        self._write_results(b, [self._workload("wl1", ttft_p50=100.0)])
        self._write_results(t, [self._workload("wl1", ttft_p50=90.0)])
        rc, out, err = _run_cli("compare", "--baseline", str(b), "--treatment", str(t))
        assert rc == 0, f"exit {rc}: {err}"
        # delta = 90.0 - 100.0 = -10.0; pct = -10.0%
        assert "-10.0" in out
        assert "-10.0%" in out


class TestMergeValuesMissingTestGaps:
    """Additional TestMergeValues tests covering gaps identified in review."""

    def _write_yaml(self, path: Path, data: dict) -> None:
        import yaml
        path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))

    def _minimal_env_defaults(self) -> dict:
        return {
            "stack": {
                "gateway": {
                    "helmValues": {
                        "gateway": {"provider": "istio", "gatewayClassName": "istio"}
                    }
                }
            }
        }

    def _minimal_algorithm_values(self) -> dict:
        return {
            "stack": {
                "model": {
                    "modelName": "Org/Model-7B",
                    "helmValues": {
                        "modelArtifacts": {"name": "Org/Model-7B", "uri": "pvc://pvc/model"},
                        "decode": {
                            "replicas": 2,
                            "containers": [{"image": "vllm/vllm-openai:v0.11.0"}],
                        },
                    },
                },
                "gaie": {
                    "treatment": {
                        "helmValues": {
                            "inferenceExtension": {
                                "pluginsCustomConfig": {"custom-plugins.yaml": "cfg"}
                            }
                        }
                    }
                },
            },
            "observe": {
                "image": "ghcr.io/inference-sim/blis:v0.6.13",
                "workloads": [{"name": "wl-a", "spec": "version: '1'"}],
            },
        }

    def test_malformed_env_yaml_exits_2(self, tmp_path):
        """MV: env file exists but contains invalid YAML → exit 2 (infrastructure error)."""
        env_file = tmp_path / "env.yaml"
        alg_file = tmp_path / "alg.yaml"
        out_file = tmp_path / "out.yaml"
        env_file.write_text(": {bad yaml: [unclosed")
        self._write_yaml(alg_file, self._minimal_algorithm_values())
        rc, out, err = _run_cli(
            "merge-values",
            "--env", str(env_file),
            "--algorithm", str(alg_file),
            "--out", str(out_file),
        )
        assert rc == 2, f"Expected exit 2 for malformed env YAML, got {rc}. stderr: {err}"
        assert "ERROR" in err

    def test_malformed_algorithm_yaml_exits_2(self, tmp_path):
        """MV: algorithm file exists but contains invalid YAML → exit 2 (infrastructure error)."""
        env_file = tmp_path / "env.yaml"
        alg_file = tmp_path / "alg.yaml"
        out_file = tmp_path / "out.yaml"
        self._write_yaml(env_file, self._minimal_env_defaults())
        alg_file.write_text(": {bad yaml: [unclosed")
        rc, out, err = _run_cli(
            "merge-values",
            "--env", str(env_file),
            "--algorithm", str(alg_file),
            "--out", str(out_file),
        )
        assert rc == 2, f"Expected exit 2 for malformed algorithm YAML, got {rc}. stderr: {err}"
        assert "ERROR" in err
