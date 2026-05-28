"""Tests for prepare.py — Phase 1 (Init), Phase 3 (Translation Checkpoint), validate-assembly."""
import json
import pytest
import yaml

from pipeline.lib.state_machine import StateMachine

# ── Fixtures ────────────────────────────────────────────────────────────────

MINIMAL_MANIFEST = {
    "kind": "sim2real-transfer",
    "version": 3,
    "scenario": "routing",
    "baselines": [{
        "name": "baseline",
        "scenario": None,
        "sim": {"config": "sim2real_golden/routers/policy_baseline_211.yaml"},
    }],
    "algorithms": [{
        "name": "treatment",
        "source": "sim2real_golden/routers/router_adaptive_v2.go",
        "config": "sim2real_golden/routers/policy_adaptive_v2.yaml",
        "scenario": None,
        "defaults": "baseline",
    }],
    "workloads": ["sim2real_golden/workloads/wl1.yaml"],
    "component": {
        "repo": "github.com/llm-d/llm-d-inference-scheduler",
        "path": "llm-d-inference-scheduler",
        "kind": "EndpointPickerConfig",
        "build": {"commands": [["go", "build", "./..."]]},
    },
}


def _write_yaml(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(data, default_flow_style=False))


def _write_text(path, text=""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


import subprocess as _subprocess


def _init_git_repo(path):
    """Initialize a real git repo with an initial empty commit at path."""
    _subprocess.run(["git", "init"], cwd=path, capture_output=True)
    _subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=path, capture_output=True)
    _subprocess.run(["git", "config", "user.name", "T"], cwd=path, capture_output=True)
    _subprocess.run(["git", "commit", "--allow-empty", "-m", "init"], cwd=path, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    """Set up a minimal repo layout for prepare.py testing."""
    # Config files
    manifest_data = dict(MINIMAL_MANIFEST)
    _write_yaml(tmp_path / "config" / "transfer.yaml", manifest_data)

    # Algorithm files referenced by manifest
    _write_text(tmp_path / "sim2real_golden" / "routers" / "router_adaptive_v2.go", "package main")
    _write_text(tmp_path / "sim2real_golden" / "routers" / "policy_adaptive_v2.yaml", "policy: v2")
    _write_text(tmp_path / "sim2real_golden" / "routers" / "policy_baseline_211.yaml", "policy: baseline")
    _write_text(tmp_path / "sim2real_golden" / "workloads" / "wl1.yaml",
                yaml.dump({"name": "wl1", "num_requests": 100}))

    # Target repo
    (tmp_path / "llm-d-inference-scheduler" / "pkg" / "plugins" / "scorer").mkdir(parents=True)
    _write_text(tmp_path / "llm-d-inference-scheduler" / "pkg" / "plugins" / "register.go",
                'func init() {\n\tRegister("test-scorer", NewTestScorer)\n}')

    # inference-sim submodule: real git repo so git rev-parse HEAD succeeds
    inf_sim = tmp_path / "inference-sim"
    inf_sim.mkdir()
    _init_git_repo(inf_sim)

    return tmp_path


# ── Test helpers ────────────────────────────────────────────────────────────

# We test prepare.py functions by importing them with a patched REPO_ROOT.
# This avoids running the full CLI which has side effects (sys.exit, input).

def _import_prepare_with_root(repo_root):
    """Import prepare module with REPO_ROOT patched to tmp_path."""
    import importlib
    import pipeline.prepare as mod
    importlib.reload(mod)
    mod.REPO_ROOT = repo_root
    mod.EXPERIMENT_ROOT = repo_root
    return mod


# ── Phase 1: Init ──────────────────────────────────────────────────────────

class TestPhaseInit:
    def test_init_creates_state_machine(self, repo):
        mod = _import_prepare_with_root(repo)
        manifest = dict(MINIMAL_MANIFEST)
        run_dir = repo / "workspace" / "runs" / "test-run"

        class Args:
            force = False
            run = "test-run"
            manifest = None
            rebuild_context = False

        state = mod._phase_init(Args(), manifest, run_dir)
        assert state.is_done("init")
        assert state.run_name == "test-run"
        assert state.scenario == "routing"

    def test_init_skips_when_done(self, repo):
        mod = _import_prepare_with_root(repo)
        manifest = dict(MINIMAL_MANIFEST)
        run_dir = repo / "workspace" / "runs" / "test-run"

        # Pre-create state
        sm = StateMachine("test-run", "routing", run_dir)
        sm.mark_done("init")

        class Args:
            force = False
            run = "test-run"
            manifest = None
            rebuild_context = False

        state = mod._phase_init(Args(), manifest, run_dir)
        assert state.is_done("init")

    def test_init_force_recreates(self, repo):
        mod = _import_prepare_with_root(repo)
        manifest = dict(MINIMAL_MANIFEST)
        run_dir = repo / "workspace" / "runs" / "test-run"

        # Pre-create state
        sm = StateMachine("test-run", "routing", run_dir)
        sm.mark_done("init")
        sm.mark_done("context")

        class Args:
            force = True
            run = "test-run"
            manifest = None
            rebuild_context = False

        state = mod._phase_init(Args(), manifest, run_dir)
        assert state.is_done("init")
        # Force creates a new state machine, context should not be done
        assert not state.is_done("context")

    def test_init_missing_algorithm_source_exits(self, repo):
        mod = _import_prepare_with_root(repo)
        manifest = dict(MINIMAL_MANIFEST)
        manifest["algorithms"] = [{
            "name": "treatment",
            "source": "nonexistent/file.go",
            "config": "sim2real_golden/routers/policy_adaptive_v2.yaml",
            "defaults": "baseline",
        }]
        run_dir = repo / "workspace" / "runs" / "test-run"

        class Args:
            force = True
            run = "test-run"
            manifest = None
            rebuild_context = False

        with pytest.raises(SystemExit):
            mod._phase_init(Args(), manifest, run_dir)

    def test_init_missing_workload_exits(self, repo):
        mod = _import_prepare_with_root(repo)
        manifest = dict(MINIMAL_MANIFEST)
        manifest["workloads"] = ["nonexistent/workload.yaml"]
        run_dir = repo / "workspace" / "runs" / "test-run"

        class Args:
            force = True
            run = "test-run"
            manifest = None
            rebuild_context = False

        with pytest.raises(SystemExit):
            mod._phase_init(Args(), manifest, run_dir)

    def test_init_unknown_scenario_succeeds(self, repo):
        """Unknown scenario no longer requires matching env_defaults entry."""
        mod = _import_prepare_with_root(repo)
        manifest = dict(MINIMAL_MANIFEST)
        manifest["scenario"] = "unknown_scenario"
        run_dir = repo / "workspace" / "runs" / "test-run"

        class Args:
            force = True
            run = "test-run"
            manifest = None
            rebuild_context = False

        state = mod._phase_init(Args(), manifest, run_dir)
        assert state is not None

    def test_init_ref_match_succeeds(self, repo):
        """Phase 1 passes when component.ref matches checked-out SHA."""
        comp = repo / "llm-d-inference-scheduler"
        _init_git_repo(comp)
        import subprocess
        sha = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                            text=True, cwd=comp).stdout.strip()

        mod = _import_prepare_with_root(repo)
        manifest = dict(MINIMAL_MANIFEST)
        manifest["component"] = {**manifest["component"], "ref": sha}
        run_dir = repo / "workspace" / "runs" / "test-run"

        class Args:
            force = True
            run = "test-run"
            manifest = None
            rebuild_context = False

        state = mod._phase_init(Args(), manifest, run_dir)
        assert state.is_done("init")

    def test_init_ref_mismatch_warns(self, repo, capsys):
        """Phase 1 warns (does not exit) when component.ref doesn't match checked-out SHA."""
        comp = repo / "llm-d-inference-scheduler"
        _init_git_repo(comp)

        mod = _import_prepare_with_root(repo)
        manifest = dict(MINIMAL_MANIFEST)
        manifest["component"] = {**manifest["component"], "ref": "deadbeef" * 5}
        run_dir = repo / "workspace" / "runs" / "test-run"

        class Args:
            force = True
            run = "test-run"
            manifest = None
            rebuild_context = False

        state = mod._phase_init(Args(), manifest, run_dir)
        assert state.is_done("init")
        captured = capsys.readouterr()
        assert "Component ref mismatch" in captured.out
        assert "deadbeef" * 5 in captured.out
        assert "git checkout" in captured.out

    def test_init_ref_missing_submodule_exits_with_command(self, repo, capsys):
        """Phase 1 errors with init command when submodule missing and ref set."""
        import shutil
        comp = repo / "llm-d-inference-scheduler"
        if comp.exists():
            shutil.rmtree(comp)

        mod = _import_prepare_with_root(repo)
        manifest = dict(MINIMAL_MANIFEST)
        manifest["component"] = {**manifest["component"], "ref": "a" * 40}
        run_dir = repo / "workspace" / "runs" / "test-run"

        class Args:
            force = True
            run = "test-run"
            manifest = None
            rebuild_context = False

        with pytest.raises(SystemExit):
            mod._phase_init(Args(), manifest, run_dir)
        captured = capsys.readouterr()
        assert "git submodule update --init" in captured.err or "git submodule update --init" in captured.out

    def test_init_ref_not_git_repo_exits(self, repo, capsys):
        """Phase 1 errors when component.ref is set but directory is not a git repo."""
        import shutil
        comp = repo / "llm-d-inference-scheduler"
        if comp.exists():
            shutil.rmtree(comp)
        comp.mkdir()
        (comp / "somefile.txt").write_text("not a git repo")

        mod = _import_prepare_with_root(repo)
        manifest = dict(MINIMAL_MANIFEST)
        manifest["component"] = {**manifest["component"], "ref": "a" * 40}
        run_dir = repo / "workspace" / "runs" / "test-run"

        class Args:
            force = True
            run = "test-run"
            manifest = None
            rebuild_context = False

        with pytest.raises(SystemExit):
            mod._phase_init(Args(), manifest, run_dir)
        captured = capsys.readouterr()
        assert "not a git repository" in captured.err

    def test_init_no_ref_skips_validation(self, repo):
        """Phase 1 does not check ref when component.ref is absent."""
        mod = _import_prepare_with_root(repo)
        manifest = dict(MINIMAL_MANIFEST)
        # No ref field — should pass without error
        run_dir = repo / "workspace" / "runs" / "test-run"

        class Args:
            force = True
            run = "test-run"
            manifest = None
            rebuild_context = False

        state = mod._phase_init(Args(), manifest, run_dir)
        assert state.is_done("init")


# ── Phase 3: Translation Checkpoint ────────────────────────────────────────

class TestPhaseTranslate:
    def test_writes_skill_input_json(self, repo):
        mod = _import_prepare_with_root(repo)
        manifest = dict(MINIMAL_MANIFEST)
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

        # Should exit cleanly at checkpoint (no translation_output.json)
        with pytest.raises(SystemExit) as exc_info:
            mod._phase_translate(Args(), state, manifest, run_dir, resolved, context_path)
        assert exc_info.value.code == 0

        # Verify skill_input.json was written
        si_path = run_dir / "skill_input.json"
        assert si_path.exists()
        si = json.loads(si_path.read_text())
        assert si["run_name"] == "test-run"
        assert si["scenario"] == "routing"
        assert si["algorithm_source"] == "sim2real_golden/routers/router_adaptive_v2.go"
        assert si["config_kind"] == "EndpointPickerConfig"
        assert isinstance(si["build_commands"], list)
        # Test should not append, skill determines test scope
        assert si["build_commands"] == ["go build ./..."]

    def test_skips_when_done(self, repo):
        mod = _import_prepare_with_root(repo)
        manifest = dict(MINIMAL_MANIFEST)
        run_dir = repo / "workspace" / "runs" / "test-run"
        run_dir.mkdir(parents=True, exist_ok=True)

        resolved = mod._load_resolved_config(manifest)
        state = StateMachine("test-run", "routing", run_dir)
        state.mark_done("init")
        state.mark_done("translate", plugin_type="test-scorer", files_created=[])

        context_path = run_dir / "context.md"
        context_path.write_text("# Context")

        class Args:
            force = False
            manifest = None

        # Should not raise — just skip
        mod._phase_translate(Args(), state, manifest, run_dir, resolved, context_path)

    def test_detects_translation_output(self, repo):
        mod = _import_prepare_with_root(repo)
        manifest = dict(MINIMAL_MANIFEST)
        run_dir = repo / "workspace" / "runs" / "test-run"
        run_dir.mkdir(parents=True, exist_ok=True)

        resolved = mod._load_resolved_config(manifest)
        state = StateMachine("test-run", "routing", run_dir)
        state.mark_done("init")

        context_path = run_dir / "context.md"
        context_path.write_text("# Context")

        # Write translation_output.json with all required fields
        output = {
            "plugin_type": "adaptive-v2-scorer",
            "files_created": ["pkg/plugins/scorer/adaptive_v2.go"],
            "files_modified": ["pkg/plugins/register.go"],
            "package": "scorer",
            "test_commands": [["go", "test", "./..."]],
            "config_kind": "EndpointPickerConfig",
            "treatment_config_generated": True,
            "description": "Adaptive v2 scorer",
            "register_file": "pkg/plugins/register.go",
        }
        (run_dir / "translation_output.json").write_text(json.dumps(output))

        class Args:
            force = False
            manifest = None

        # Should complete (not exit at checkpoint)
        mod._phase_translate(Args(), state, manifest, run_dir, resolved, context_path)
        assert state.is_done("translate")
        assert state.get_phase("translate")["plugin_type"] == "adaptive-v2-scorer"

    def test_missing_required_field_in_output_exits(self, repo):
        mod = _import_prepare_with_root(repo)
        manifest = dict(MINIMAL_MANIFEST)
        run_dir = repo / "workspace" / "runs" / "test-run"
        run_dir.mkdir(parents=True, exist_ok=True)

        resolved = mod._load_resolved_config(manifest)
        state = StateMachine("test-run", "routing", run_dir)
        state.mark_done("init")

        context_path = run_dir / "context.md"
        context_path.write_text("# Context")

        # Missing files_modified
        output = {
            "plugin_type": "test-scorer",
            "files_created": [],
        }
        (run_dir / "translation_output.json").write_text(json.dumps(output))

        class Args:
            force = False
            manifest = None

        with pytest.raises(SystemExit):
            mod._phase_translate(Args(), state, manifest, run_dir, resolved, context_path)

    def test_checkpoint_increments_hits(self, repo):
        mod = _import_prepare_with_root(repo)
        manifest = dict(MINIMAL_MANIFEST)
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

        # First checkpoint hit
        with pytest.raises(SystemExit):
            mod._phase_translate(Args(), state, manifest, run_dir, resolved, context_path)
        assert state.get_phase("translate").get("checkpoint_hits") == 1

        # Second checkpoint hit
        with pytest.raises(SystemExit):
            mod._phase_translate(Args(), state, manifest, run_dir, resolved, context_path)
        assert state.get_phase("translate").get("checkpoint_hits") == 2

    def test_skill_input_contains_context_empty(self, repo):
        """When manifest has no context.text, skill_input.json has context={text:''}."""
        mod = _import_prepare_with_root(repo)
        manifest = dict(MINIMAL_MANIFEST)
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
        assert "context" in si
        assert si["context"]["text"] == ""
        assert "hints" not in si
        # target has only repo
        assert set(si["target"].keys()) == {"repo"}

    def test_skill_input_contains_context_text_from_manifest(self, repo):
        """When manifest has context.text, it is written to skill_input.json."""
        mod = _import_prepare_with_root(repo)
        manifest = dict(MINIMAL_MANIFEST)
        manifest["context"] = {"text": "Modify scorer X", "files": []}
        run_dir = repo / "workspace" / "runs" / "test-run2"
        run_dir.mkdir(parents=True, exist_ok=True)

        resolved = mod._load_resolved_config(manifest)
        state = StateMachine("test-run2", "routing", run_dir)
        state.mark_done("init")

        context_path = run_dir / "context.md"
        context_path.write_text("# Context")

        class Args:
            force = False
            manifest = None

        with pytest.raises(SystemExit):
            mod._phase_translate(Args(), state, manifest, run_dir, resolved, context_path)

        si = json.loads((run_dir / "skill_input.json").read_text())
        assert si["context"]["text"] == "Modify scorer X"
        assert "hints" not in si

    def test_translation_output_validates_all_required_fields(self, repo):
        """translation_output.json must have all required fields."""
        mod = _import_prepare_with_root(repo)
        manifest = dict(MINIMAL_MANIFEST)
        run_dir = repo / "workspace" / "runs" / "test-run"
        run_dir.mkdir(parents=True, exist_ok=True)

        resolved = mod._load_resolved_config(manifest)
        state = StateMachine("test-run", "routing", run_dir)
        state.mark_done("init")

        context_path = run_dir / "context.md"
        context_path.write_text("# Context")

        # Missing test_commands (one of the new required fields)
        output = {
            "plugin_type": "test-scorer",
            "files_created": [],
            "files_modified": [],
            "package": "scorer",
            "config_kind": "EndpointPickerConfig",
            "treatment_config_generated": True,
            "description": "A test scorer",
            "register_file": None,
            # missing test_commands
        }
        (run_dir / "translation_output.json").write_text(json.dumps(output))

        class Args:
            force = False
            manifest = None

        with pytest.raises(SystemExit):
            mod._phase_translate(Args(), state, manifest, run_dir, resolved, context_path)

    def test_translation_output_validates_register_file_present(self, repo):
        """register_file must be present even if null."""
        mod = _import_prepare_with_root(repo)
        manifest = dict(MINIMAL_MANIFEST)
        run_dir = repo / "workspace" / "runs" / "test-run"
        run_dir.mkdir(parents=True, exist_ok=True)

        resolved = mod._load_resolved_config(manifest)
        state = StateMachine("test-run", "routing", run_dir)
        state.mark_done("init")

        context_path = run_dir / "context.md"
        context_path.write_text("# Context")

        # Missing register_file field entirely
        output = {
            "plugin_type": "test-scorer",
            "files_created": [],
            "files_modified": [],
            "package": "scorer",
            "test_commands": [["go", "test", "./..."]],
            "config_kind": "EndpointPickerConfig",
            "treatment_config_generated": True,
            "description": "A test scorer",
            # missing register_file
        }
        (run_dir / "translation_output.json").write_text(json.dumps(output))

        class Args:
            force = False
            manifest = None

        with pytest.raises(SystemExit):
            mod._phase_translate(Args(), state, manifest, run_dir, resolved, context_path)

    def test_translation_output_saves_register_file_and_treatment_config_generated(self, repo):
        """State saves register_file and treatment_config_generated from output."""
        mod = _import_prepare_with_root(repo)
        manifest = dict(MINIMAL_MANIFEST)
        run_dir = repo / "workspace" / "runs" / "test-run"
        run_dir.mkdir(parents=True, exist_ok=True)

        resolved = mod._load_resolved_config(manifest)
        state = StateMachine("test-run", "routing", run_dir)
        state.mark_done("init")

        context_path = run_dir / "context.md"
        context_path.write_text("# Context")

        # Complete valid output
        output = {
            "plugin_type": "test-scorer",
            "files_created": [],
            "files_modified": [],
            "package": "scorer",
            "test_commands": [["go", "test", "./..."]],
            "config_kind": "EndpointPickerConfig",
            "treatment_config_generated": True,
            "description": "A test scorer",
            "register_file": "pkg/plugins/register.go",
        }
        (run_dir / "translation_output.json").write_text(json.dumps(output))

        class Args:
            force = False
            manifest = None

        mod._phase_translate(Args(), state, manifest, run_dir, resolved, context_path)
        assert state.is_done("translate")
        phase = state.get_phase("translate")
        assert phase["register_file"] == "pkg/plugins/register.go"
        assert phase["treatment_config_generated"] is True

    def test_build_commands_excludes_test_scope(self, repo):
        """build_commands should not include test scope appended by prepare.py."""
        mod = _import_prepare_with_root(repo)
        manifest = dict(MINIMAL_MANIFEST)
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
        # Should only have the build commands from manifest, not test appended
        build_cmds = si["build_commands"]
        assert len(build_cmds) == 1
        assert build_cmds[0] == "go build ./..."
        # Should NOT have go test appended
        assert not any("test" in str(cmd) for cmd in build_cmds)

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

    def test_skill_input_includes_baseline_real_config_when_present(self, repo):
        """skill_input.json includes baseline_real_config when v3 provides it."""
        mod = _import_prepare_with_root(repo)

        # Write a v3 manifest with baseline.real fields
        v3_data = {
            "kind": "sim2real-transfer",
            "version": 3,
            "scenario": "routing",
            "baselines": [{
                "name": "baseline",
                "scenario": None,
                "sim": {"config": "sim2real_golden/routers/policy_baseline_211.yaml"},
                "real": {
                    "config": "sim2real_golden/routers/baseline_epp_template.yaml",
                    "notes": "Use EndpointPickerConfig",
                },
            }],
            "algorithms": [{
                "name": "treatment",
                "source": "sim2real_golden/routers/router_adaptive_v2.go",
                "defaults": "baseline",
            }],
            "workloads": ["sim2real_golden/workloads/wl1.yaml"],
            "component": {
                "repo": "github.com/llm-d/llm-d-inference-scheduler",
                "path": "llm-d-inference-scheduler",
                "kind": "EndpointPickerConfig",
            },
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


# ── validate-assembly ───────────────────────────────────────────────────────

class TestValidateAssembly:
    def test_passes_when_consistent(self, repo):
        mod = _import_prepare_with_root(repo)
        run_dir = repo / "workspace" / "runs" / "test-run"
        run_dir.mkdir(parents=True, exist_ok=True)

        resolved = {"repo": "github.com/llm-d/llm-d-inference-scheduler", "path": "llm-d-inference-scheduler", "kind": "EndpointPickerConfig"}

        # Set up translation output
        output = {
            "plugin_type": "test-scorer",
            "files_created": ["pkg/plugins/scorer/test_scorer.go"],
            "files_modified": ["pkg/plugins/register.go"],
            "register_file": "pkg/plugins/register.go",
            "treatment_config_generated": True,
        }
        (run_dir / "translation_output.json").write_text(json.dumps(output))

        # Make register.go contain the plugin type
        register_path = repo / "llm-d-inference-scheduler" / "pkg" / "plugins" / "register.go"
        register_path.write_text('Register("test-scorer", NewTestScorer)')

        # Create treatment.yaml with plugin type
        cluster_dir = run_dir / "cluster"
        cluster_dir.mkdir(parents=True, exist_ok=True)
        (cluster_dir / "treatment.yaml").write_text("- type: test-scorer\n")

        # Create treatment_config with correct kind
        (run_dir / "generated").mkdir(parents=True, exist_ok=True)
        _write_yaml(run_dir / "generated" / "treatment_config.yaml", {"kind": "EndpointPickerConfig"})

        # Create the files_created file
        scorer_path = repo / "llm-d-inference-scheduler" / "pkg" / "plugins" / "scorer" / "test_scorer.go"
        scorer_path.write_text("package scorer")

        # Should not raise
        mod._validate_assembly(run_dir, resolved)

    def test_fails_plugin_type_not_in_register(self, repo):
        mod = _import_prepare_with_root(repo)
        run_dir = repo / "workspace" / "runs" / "test-run"
        run_dir.mkdir(parents=True, exist_ok=True)

        resolved = {"repo": "github.com/llm-d/llm-d-inference-scheduler", "path": "llm-d-inference-scheduler", "kind": "EndpointPickerConfig"}

        output = {
            "plugin_type": "missing-scorer",
            "files_created": [],
            "files_modified": [],
            "register_file": "pkg/plugins/register.go",
            "treatment_config_generated": True,
        }
        (run_dir / "translation_output.json").write_text(json.dumps(output))

        # register.go doesn't contain "missing-scorer"
        register_path = repo / "llm-d-inference-scheduler" / "pkg" / "plugins" / "register.go"
        register_path.write_text('Register("test-scorer", NewTestScorer)')

        # Create treatment.yaml with the plugin type so Check 2 passes
        cluster_dir = run_dir / "cluster"
        cluster_dir.mkdir(parents=True, exist_ok=True)
        (cluster_dir / "treatment.yaml").write_text("- type: missing-scorer\n")

        with pytest.raises(SystemExit):
            mod._validate_assembly(run_dir, resolved)

    def test_fails_plugin_type_not_in_pipeline(self, repo):
        """Check 2 reads cluster/treatment.yaml for plugin_type."""
        mod = _import_prepare_with_root(repo)
        run_dir = repo / "workspace" / "runs" / "test-run"
        run_dir.mkdir(parents=True, exist_ok=True)

        resolved = {"repo": "github.com/llm-d/llm-d-inference-scheduler", "path": "llm-d-inference-scheduler", "kind": "EndpointPickerConfig"}

        output = {
            "plugin_type": "test-scorer",
            "files_created": [],
            "files_modified": [],
            "register_file": "pkg/plugins/register.go",
            "treatment_config_generated": True,
        }
        (run_dir / "translation_output.json").write_text(json.dumps(output))

        # register.go contains the plugin type
        register_path = repo / "llm-d-inference-scheduler" / "pkg" / "plugins" / "register.go"
        register_path.write_text('Register("test-scorer", NewTestScorer)')

        # treatment.yaml does NOT contain the plugin type
        cluster_dir = run_dir / "cluster"
        cluster_dir.mkdir(parents=True, exist_ok=True)
        (cluster_dir / "treatment.yaml").write_text("- type: other-scorer\n")

        with pytest.raises(SystemExit):
            mod._validate_assembly(run_dir, resolved)

    def test_fails_files_created_missing(self, repo):
        mod = _import_prepare_with_root(repo)
        run_dir = repo / "workspace" / "runs" / "test-run"
        run_dir.mkdir(parents=True, exist_ok=True)

        resolved = {"repo": "github.com/llm-d/llm-d-inference-scheduler", "path": "llm-d-inference-scheduler", "kind": "EndpointPickerConfig"}

        output = {
            "plugin_type": "test-scorer",
            "files_created": ["pkg/plugins/scorer/nonexistent.go"],
            "files_modified": [],
            "register_file": "pkg/plugins/register.go",
            "treatment_config_generated": True,
        }
        (run_dir / "translation_output.json").write_text(json.dumps(output))

        # register.go contains the plugin type
        register_path = repo / "llm-d-inference-scheduler" / "pkg" / "plugins" / "register.go"
        register_path.write_text('Register("test-scorer", NewTestScorer)')

        # treatment.yaml contains the plugin type
        cluster_dir = run_dir / "cluster"
        cluster_dir.mkdir(parents=True, exist_ok=True)
        (cluster_dir / "treatment.yaml").write_text("- type: test-scorer\n")

        with pytest.raises(SystemExit):
            mod._validate_assembly(run_dir, resolved)

    def test_check1_skipped_when_register_file_null(self, repo):
        """register_file=null (rewrite mode) — Check 1 skipped."""
        mod = _import_prepare_with_root(repo)
        run_dir = repo / "workspace" / "runs" / "test-run"
        run_dir.mkdir(parents=True, exist_ok=True)

        resolved = {"repo": "github.com/llm-d/llm-d-inference-scheduler", "path": "llm-d-inference-scheduler", "kind": "EndpointPickerConfig"}
        output = {
            "plugin_type": "test-scorer",
            "files_created": [],
            "files_modified": ["pkg/plugins/scorer/precise_prefix_cache.go"],
            "register_file": None,
            "treatment_config_generated": True,
        }
        (run_dir / "translation_output.json").write_text(json.dumps(output))

        cluster_dir = run_dir / "cluster"
        cluster_dir.mkdir(parents=True, exist_ok=True)
        (cluster_dir / "treatment.yaml").write_text("type: test-scorer\n")
        (run_dir / "generated").mkdir(parents=True, exist_ok=True)
        _write_yaml(run_dir / "generated" / "treatment_config.yaml", {"kind": "EndpointPickerConfig"})

        # register.go does NOT contain plugin_type — but should be ignored
        register = repo / "llm-d-inference-scheduler" / "pkg" / "plugins" / "register.go"
        register.write_text("no scorer here")

        mod._validate_assembly(run_dir, resolved)  # should not raise

    def test_check2_uses_treatment_yaml(self, repo):
        """Check 2 reads cluster/treatment.yaml for plugin_type."""
        mod = _import_prepare_with_root(repo)
        run_dir = repo / "workspace" / "runs" / "test-run"
        run_dir.mkdir(parents=True, exist_ok=True)

        resolved = {"repo": "github.com/llm-d/llm-d-inference-scheduler", "path": "llm-d-inference-scheduler", "kind": "EndpointPickerConfig"}
        output = {
            "plugin_type": "test-scorer",
            "files_created": [],
            "files_modified": [],
            "register_file": "pkg/plugins/register.go",
            "treatment_config_generated": True,
        }
        (run_dir / "translation_output.json").write_text(json.dumps(output))

        register = repo / "llm-d-inference-scheduler" / "pkg" / "plugins" / "register.go"
        register.write_text('Register("test-scorer", NewTestScorer)')

        cluster_dir = run_dir / "cluster"
        cluster_dir.mkdir(parents=True, exist_ok=True)
        (cluster_dir / "treatment.yaml").write_text("type: test-scorer\n")
        (run_dir / "generated").mkdir(parents=True, exist_ok=True)
        _write_yaml(run_dir / "generated" / "treatment_config.yaml", {"kind": "EndpointPickerConfig"})

        mod._validate_assembly(run_dir, resolved)  # should not raise



# ── _get_submodule_shas ────────────────────────────────────────────────────

class TestGetSubmoduleShas:
    def test_returns_component_sha_from_manifest_path(self, repo):
        """_get_submodule_shas uses component_path for the component entry."""
        comp = repo / "my-scheduler"
        comp.mkdir()
        _init_git_repo(comp)

        mod = _import_prepare_with_root(repo)
        shas = mod._get_submodule_shas(component_path="my-scheduler")
        assert shas.get("component") != "unknown"
        assert len(shas["component"]) == 40  # SHA-1 hex

    def test_unknown_when_component_path_missing(self, repo):
        """_get_submodule_shas returns 'unknown' when component dir doesn't exist."""
        mod = _import_prepare_with_root(repo)
        shas = mod._get_submodule_shas(component_path="nonexistent")
        assert shas["component"] == "unknown"

    def test_framework_submodules_still_resolved(self, repo):
        """inference-sim and llm-d-benchmark are still in the result."""
        mod = _import_prepare_with_root(repo)
        shas = mod._get_submodule_shas(component_path="llm-d-inference-scheduler")
        assert "inference-sim" in shas
        assert "llm-d-benchmark" in shas


# ── Config resolution ───────────────────────────────────────────────────────

class TestConfigResolution:
    def test_load_resolved_config(self, repo):
        mod = _import_prepare_with_root(repo)
        manifest = dict(MINIMAL_MANIFEST)
        resolved = mod._load_resolved_config(manifest)
        # Should return component section fields
        assert resolved["path"] == "llm-d-inference-scheduler"
        assert resolved["kind"] == "EndpointPickerConfig"




class TestExperimentRootSeparation:
    """Verify --experiment-root correctly separates experiment paths from framework paths."""

    def _make_framework(self, tmp_path):
        """Create a minimal framework directory (no experiment files)."""
        fw = tmp_path / "sim2real"
        # inference-sim stub
        (fw / "inference-sim" / ".git").mkdir(parents=True)
        _write_text(fw / "inference-sim" / ".git" / "config", "[core]\n")
        # pipeline/pipeline.yaml (static Pipeline resource)
        (fw / "pipeline").mkdir(parents=True, exist_ok=True)
        _write_text(fw / "pipeline" / "pipeline.yaml", "apiVersion: tekton.dev/v1\nkind: Pipeline\nmetadata:\n  name: sim2real\n")
        return fw

    def _make_experiment(self, tmp_path):
        """Create a minimal experiment repo (transfer.yaml at root)."""
        exp = tmp_path / "admission-control"
        manifest_data = {
            "kind": "sim2real-transfer",
            "version": 3,
            "scenario": "admission_control",
            "baselines": [{"name": "baseline", "scenario": None}],
            "algorithms": [{"name": "treatment", "source": "algorithm/admission.go", "defaults": "baseline"}],
            "workloads": ["workloads/w1.yaml"],
            "component": {"repo": "github.com/llm-d/llm-d-inference-scheduler", "path": "llm-d-inference-scheduler", "kind": "AdmissionConfig"},
        }
        _write_yaml(exp / "transfer.yaml", manifest_data)
        _write_text(exp / "algorithm" / "admission.go", "package main\n")
        _write_text(exp / "workloads" / "w1.yaml",
                    "version: '2'\nnum_requests: 10\naggregate_rate: 10\n")
        (exp / "llm-d-inference-scheduler" / "pkg").mkdir(parents=True)
        _write_text(exp / "llm-d-inference-scheduler" / "pkg" / "register.go", "")
        (exp / "workspace").mkdir(parents=True)
        return exp

    def _patch(self, mod, fw, exp):
        """Patch both REPO_ROOT and EXPERIMENT_ROOT to separate framework and experiment."""
        mod.REPO_ROOT = fw
        mod.EXPERIMENT_ROOT = exp

    def test_init_resolves_files_from_experiment_root(self, tmp_path):
        fw = self._make_framework(tmp_path)
        exp = self._make_experiment(tmp_path)

        import importlib
        import pipeline.prepare as mod
        importlib.reload(mod)
        self._patch(mod, fw, exp)

        from pipeline.lib.manifest import load_manifest
        manifest = load_manifest(exp / "transfer.yaml")
        run_dir = exp / "workspace" / "runs" / "test-run"

        class Args:
            force = False
            run = "test-run"
            manifest = str(exp / "transfer.yaml")
            rebuild_context = False
            experiment_root = str(exp)
            pipeline_template = None

        state = mod._phase_init(Args(), manifest, run_dir)
        assert state.is_done("init")
        assert state.scenario == "admission_control"

    def test_env_defaults_resolved_from_experiment_root(self, tmp_path):
        fw = self._make_framework(tmp_path)
        exp = self._make_experiment(tmp_path)

        import importlib
        import pipeline.prepare as mod
        importlib.reload(mod)
        self._patch(mod, fw, exp)

        from pipeline.lib.manifest import load_manifest
        manifest = load_manifest(exp / "transfer.yaml")
        resolved = mod._load_resolved_config(manifest)
        assert resolved["path"] == "llm-d-inference-scheduler"

    def test_workspace_created_in_experiment_root(self, tmp_path):
        """run_dir should be under EXPERIMENT_ROOT, not REPO_ROOT."""
        fw = self._make_framework(tmp_path)
        exp = self._make_experiment(tmp_path)

        import importlib
        import pipeline.prepare as mod
        importlib.reload(mod)
        self._patch(mod, fw, exp)

        from pipeline.lib.manifest import load_manifest
        manifest = load_manifest(exp / "transfer.yaml")
        run_dir = exp / "workspace" / "runs" / "test-run"

        class Args:
            force = False
            run = "test-run"
            manifest = str(exp / "transfer.yaml")
            rebuild_context = False
            experiment_root = str(exp)
            pipeline_template = None

        mod._phase_init(Args(), manifest, run_dir)
        # State file should exist in experiment root, not framework root
        assert (exp / "workspace" / "runs" / "test-run" / ".state.json").exists()
        assert not (fw / "workspace").exists()


# ── Baseline-only assembly ─────────────────────────────────────────────────

class TestBaselineOnlyAssembly:
    """Tests for baseline-only mode: no translation_output.json present.

    These tests document the expected behavior when prepare.py assemble is
    invoked without a prior translation step — producing only baseline
    PipelineRuns and skipping treatment-specific logic.
    """

    def _setup_baseline_only_repo(self, repo):
        """Set up repo with everything needed for assembly EXCEPT translation_output.json."""
        run_dir = repo / "workspace" / "runs" / "test-run"
        run_dir.mkdir(parents=True, exist_ok=True)

        # setup_config.json (written by setup.py)
        setup_config = {"namespace": "sim2real-test", "workspaces": {}}
        (repo / "workspace" / "setup_config.json").write_text(json.dumps(setup_config))

        # baseline.yaml bundle (experiment root)
        baseline_bundle = {
            "scenario": [{"name": "baseline-scenario", "model": {"name": "test-model"}}]
        }
        _write_yaml(repo / "baseline.yaml", baseline_bundle)

        # llm-d-benchmark submodule (git repo — needed for SHA detection)
        benchmark = repo / "llm-d-benchmark"
        benchmark.mkdir(parents=True, exist_ok=True)
        _init_git_repo(benchmark)

        return run_dir

    def test_assembly_baseline_only_produces_only_baseline_pipelineruns(self, repo):
        """When no translation_output.json exists, _phase_assembly should produce
        only pipelinerun-*-baseline.yaml files, no cluster/treatment.yaml, and
        mark_done('assembly', packages=['baseline']).
        """
        mod = _import_prepare_with_root(repo)
        run_dir = self._setup_baseline_only_repo(repo)

        manifest = dict(MINIMAL_MANIFEST)
        resolved = mod._load_resolved_config(manifest)
        state = StateMachine("test-run", "routing", run_dir)
        state.mark_done("init")

        class Args:
            force = False

        mod._phase_assembly(Args(), state, manifest, run_dir, resolved)

        # Should mark done with baseline-only packages
        assert state.is_done("assembly")
        assert state.get_phase("assembly")["packages"] == ["baseline"]

        # Should produce only baseline PipelineRuns
        cluster_dir = run_dir / "cluster"
        pr_files = list(cluster_dir.glob("pipelinerun-*.yaml"))
        assert all("-baseline.yaml" in f.name for f in pr_files)
        assert not any("-treatment.yaml" in f.name for f in pr_files)

        # Should NOT produce cluster/treatment.yaml
        assert not (cluster_dir / "treatment.yaml").exists()

    def test_assembly_baseline_only_skips_epp_validation(self, repo):
        """When run_metadata.json has NO registry key but no translation_output.json
        exists, _phase_assembly should still succeed (EPP validation skipped).
        """
        mod = _import_prepare_with_root(repo)
        run_dir = self._setup_baseline_only_repo(repo)

        # Write run_metadata.json WITHOUT registry (would fail in full mode)
        meta = {"repo_name": "llm-d-inference-scheduler"}
        (run_dir / "run_metadata.json").write_text(json.dumps(meta))

        manifest = dict(MINIMAL_MANIFEST)
        resolved = mod._load_resolved_config(manifest)
        state = StateMachine("test-run", "routing", run_dir)
        state.mark_done("init")

        class Args:
            force = False

        # Should succeed — EPP injection should be skipped in baseline-only mode
        mod._phase_assembly(Args(), state, manifest, run_dir, resolved)
        assert state.is_done("assembly")

    def test_summary_baseline_only_does_not_crash(self, repo):
        """_phase_summary should produce a reduced summary with 'Baseline-only'
        and 'Translation skipped' text when translation_output.json is absent.
        """
        mod = _import_prepare_with_root(repo)
        run_dir = self._setup_baseline_only_repo(repo)

        manifest = dict(MINIMAL_MANIFEST)
        resolved = mod._load_resolved_config(manifest)
        state = StateMachine("test-run", "routing", run_dir)
        state.mark_done("init")
        state.mark_done("assembly", packages=["baseline"])

        # Do NOT create translation_output.json

        # Should NOT crash with FileNotFoundError
        mod._phase_summary(state, manifest, run_dir, resolved)

        assert state.is_done("summary")
        summary_path = run_dir / "run_summary.md"
        assert summary_path.exists()
        content = summary_path.read_text()
        assert "Baseline-only" in content or "baseline-only" in content.lower()
        assert "Translation skipped" in content or "translation skipped" in content.lower()

    def test_summary_full_mode_unchanged(self, repo):
        """_phase_summary still works normally when translation_output.json IS present."""
        mod = _import_prepare_with_root(repo)
        run_dir = self._setup_baseline_only_repo(repo)

        manifest = dict(MINIMAL_MANIFEST)
        resolved = mod._load_resolved_config(manifest)
        state = StateMachine("test-run", "routing", run_dir)
        state.mark_done("init")
        state.mark_done("translate", plugin_type="test-scorer", files_created=[])
        state.mark_done("assembly", packages=["baseline", "treatment"])

        # Write translation_output.json (full mode)
        output = {
            "plugin_type": "test-scorer",
            "files_created": ["pkg/plugins/scorer/test.go"],
            "files_modified": ["pkg/plugins/register.go"],
            "package": "scorer",
            "test_commands": [["go", "test", "./..."]],
            "config_kind": "EndpointPickerConfig",
            "treatment_config_generated": True,
            "description": "Test scorer plugin",
            "register_file": "pkg/plugins/register.go",
        }
        (run_dir / "translation_output.json").write_text(json.dumps(output))

        # Should work normally
        mod._phase_summary(state, manifest, run_dir, resolved)

        assert state.is_done("summary")
        summary_path = run_dir / "run_summary.md"
        assert summary_path.exists()
        content = summary_path.read_text()
        assert "test-scorer" in content
        assert "Test scorer plugin" in content

    def test_validate_assembly_baseline_only_exits_cleanly(self, repo):
        """_validate_assembly should return cleanly (not raise/exit) when
        translation_output.json is absent.
        """
        mod = _import_prepare_with_root(repo)
        run_dir = repo / "workspace" / "runs" / "test-run"
        run_dir.mkdir(parents=True, exist_ok=True)

        resolved = {"repo": "github.com/llm-d/llm-d-inference-scheduler", "path": "llm-d-inference-scheduler", "kind": "EndpointPickerConfig"}

        # Do NOT create translation_output.json
        # Should return without raising or sys.exit
        mod._validate_assembly(run_dir, resolved)

    def test_cmd_assemble_no_translation_proceeds(self, repo):
        """_cmd_assemble should NOT sys.exit(1) when translation_output.json is
        missing; should proceed and produce baseline-only output.
        """
        mod = _import_prepare_with_root(repo)
        run_dir = self._setup_baseline_only_repo(repo)

        manifest = dict(MINIMAL_MANIFEST)

        # Create state with init done (cmd_assemble loads from disk)
        state = StateMachine("test-run", "routing", run_dir)
        state.mark_done("init")
        # Do NOT mark translate as done — that's the point of this test

        class Args:
            force = False

        # _cmd_assemble proceeds in baseline-only mode without translation.
        mod._cmd_assemble(Args(), manifest, run_dir)

        # Verify baseline-only output
        cluster_dir = run_dir / "cluster"
        prs = list(cluster_dir.glob("pipelinerun-*.yaml"))
        assert all("-baseline.yaml" in f.name for f in prs)

    def test_cmd_assemble_errors_when_translation_attempted_but_missing(self, repo):
        """_cmd_assemble should sys.exit(1) when translate phase was attempted
        (checkpoint_hits > 0) but translation_output.json is absent.
        """
        mod = _import_prepare_with_root(repo)
        run_dir = self._setup_baseline_only_repo(repo)

        manifest = dict(MINIMAL_MANIFEST)

        # State with translate checkpoint_hits (translation was attempted)
        state = StateMachine("test-run", "routing", run_dir)
        state.mark_done("init")
        state.increment("translate", "checkpoint_hits")

        class Args:
            force = False

        with pytest.raises(SystemExit):
            mod._cmd_assemble(Args(), manifest, run_dir)


# ── Baseline-only (no algorithm in manifest) ──────────────────────────────

class TestBaselineOnlyNoAlgorithm:
    """Tests for full prepare flow when manifest has no algorithm field."""

    def _no_algo_manifest(self):
        """Manifest without algorithm section."""
        m = {k: v for k, v in MINIMAL_MANIFEST.items() if k not in ("algorithm", "algorithms")}
        m["algorithms"] = []
        return m

    def test_phase_init_no_algorithm(self, repo):
        """_phase_init succeeds when manifest has no algorithm field."""
        mod = _import_prepare_with_root(repo)
        manifest = self._no_algo_manifest()
        run_dir = repo / "workspace" / "runs" / "test-run"

        class Args:
            force = True
            run = "test-run"
            manifest = None
            rebuild_context = False

        state = mod._phase_init(Args(), manifest, run_dir)
        assert state.is_done("init")

    def test_phase_translate_skips_no_algorithm(self, repo):
        """_phase_translate marks done immediately when no algorithm in manifest."""
        mod = _import_prepare_with_root(repo)
        manifest = self._no_algo_manifest()
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

        mod._phase_translate(Args(), state, manifest, run_dir, resolved, context_path)
        assert state.is_done("translate")
        assert not (run_dir / "skill_input.json").exists()

    def test_cmd_run_baseline_only_no_algorithm(self, repo):
        """Full _cmd_run succeeds with no algorithm — baseline-only flow."""
        mod = _import_prepare_with_root(repo)
        manifest = self._no_algo_manifest()
        # Empty workloads to skip PipelineRun generation (avoids setup_config dep)
        manifest["workloads"] = []

        # baseline.yaml must exist for assembly (needs scenario list for HF injection)
        (repo / "baseline.yaml").write_text(yaml.dump({"scenario": [{"name": "test", "model": {"name": "test"}}]}))

        # setup_config.json required for HF secret injection
        ws_dir = repo / "workspace"
        ws_dir.mkdir(parents=True, exist_ok=True)
        (ws_dir / "setup_config.json").write_text(json.dumps({"namespace": "default"}))

        run_dir = repo / "workspace" / "runs" / "test-run"
        run_dir.mkdir(parents=True, exist_ok=True)

        class Args:
            force = False
            run = "test-run"
            manifest = None
            rebuild_context = False

        # Mock _phase_gate to avoid interactive input
        original_gate = mod._phase_gate
        mod._phase_gate = lambda *a, **kw: None
        try:
            mod._cmd_run(Args(), manifest, run_dir)
        finally:
            mod._phase_gate = original_gate

        # Verify: translate was skipped, summary was written
        assert (run_dir / "run_summary.md").exists()
        assert not (run_dir / "skill_input.json").exists()
        summary = (run_dir / "run_summary.md").read_text()
        assert "Baseline-only" in summary

    def test_phase_summary_with_stale_translation_output(self, repo):
        """_phase_summary doesn't crash when translation_output.json exists but algorithm is absent."""
        mod = _import_prepare_with_root(repo)
        manifest = self._no_algo_manifest()
        run_dir = repo / "workspace" / "runs" / "test-run"
        run_dir.mkdir(parents=True, exist_ok=True)

        # Simulate stale translation_output.json from a prior run
        output = {
            "plugin_type": "old-scorer",
            "files_created": ["old.go"],
            "files_modified": [],
            "description": "Stale translation",
        }
        (run_dir / "translation_output.json").write_text(json.dumps(output))

        state = StateMachine("test-run", "routing", run_dir)
        state.mark_done("init")
        state.mark_done("translate", mode="baseline-only")
        state.mark_done("assembly", packages=["baseline"])

        resolved = {}

        # Should not crash even with stale translation_output.json
        mod._phase_summary(state, manifest, run_dir, resolved)
        summary = (run_dir / "run_summary.md").read_text()
        assert "Source: `N/A`" in summary
