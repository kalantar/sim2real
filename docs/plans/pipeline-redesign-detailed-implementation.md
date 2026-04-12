# Detailed Implementation Plan: Pipeline Redesign

**Based on:** Design spec and high-level implementation plan  
**Created:** 2026-04-09  
**Status:** Ready for review

---

## Implementation Checklist

This document provides granular, file-level implementation tasks for the pipeline redesign.

---

## Phase 1: Foundation & Data Structures

### 1.1 Transfer Manifest Schema

**Files to create:**
- `scripts/lib/manifest.py` - Manifest schema, validation, and loading

**Implementation details:**
```python
# scripts/lib/manifest.py
class TransferManifest:
    """Transfer manifest schema and validation"""
    
    REQUIRED_FIELDS = ['scenario', 'algorithm', 'baseline', 'workloads', 'llm_config']
    VALID_SCENARIOS = ['routing', 'admission_control']
    
    def __init__(self, manifest_path: str):
        """Load and validate transfer.yaml"""
        
    def validate(self) -> List[str]:
        """Validate manifest, return list of errors"""
        
    @property
    def scenario(self) -> str:
        """Get scenario name"""
        
    @property
    def algorithm_source(self) -> str:
        """Get algorithm source file path"""
        
    @property
    def algorithm_config(self) -> str:
        """Get algorithm config file path"""
        
    @property
    def baseline_config(self) -> str:
        """Get baseline config file path"""
        
    @property
    def workloads(self) -> List[str]:
        """Get workload file paths"""
        
    @property
    def llm_config(self) -> str:
        """Get LLM config file path"""
        
    @property
    def context_files(self) -> List[str]:
        """Get optional context files"""
        
    @property
    def context_notes(self) -> Optional[str]:
        """Get optional context notes"""
```

**Tasks:**
- [ ] Create `scripts/lib/manifest.py`
- [ ] Implement `TransferManifest` class with validation
- [ ] Add unit tests in `tests/test_manifest.py`
- [ ] Create example manifests:
  - `examples/transfer-routing.yaml`
  - `examples/transfer-admission-control.yaml`

---

### 1.2 Environment Defaults Restructuring

**Files to modify:**
- `config/env_defaults.yaml` - Restructure with common + scenarios sections

**New structure:**
```yaml
common:
  observe:
    request_multiplier: 10
  stack:
    model:
      vllm_image: ""
    gaie:
      epp_image:
        upstream:
          hub: ghcr.io/llm-d
          name: llm-d-inference-scheduler
          tag: latest
          pullPolicy: IfNotPresent
        build:
          hub: ""
          name: llm-d-inference-scheduler
          tag: ""
          platform: linux/amd64
          pullPolicy: Always
  pipeline:
    fast_iteration: true

scenarios:
  routing:
    gaie:
      shared:
        helmValues:
          inferenceExtension:
            gatewayType: istio
            connectionPool:
              maxRequestsPerConnection: 256000
      baseline:
        helmValues:
          inferenceExtension:
            pluginsCustomConfig:
              custom-plugins.yaml: |
                apiVersion: inference.networking.x-k8s.io/v1alpha1
                kind: EndpointPickerConfig
                plugins:
                - type: load-aware-scorer
                - type: decode-filter
                - type: max-score-picker
                - type: single-profile-handler
                schedulingProfiles:
                - name: default
                  plugins:
                  - pluginRef: decode-filter
                  - pluginRef: max-score-picker
                  - pluginRef: load-aware-scorer
                    weight: 1
    treatment_config_template: |
      apiVersion: inference.networking.x-k8s.io/v1alpha1
      kind: EndpointPickerConfig
      plugins:
      - type: {plugin_type}
      - type: decode-filter
      - type: max-score-picker
      - type: single-profile-handler
      schedulingProfiles:
      - name: default
        plugins:
        - pluginRef: decode-filter
        - pluginRef: max-score-picker
        - pluginRef: {plugin_type}
          weight: 1

  admission_control:
    gaie:
      inferenceObjectives:
        - name: critical
          priority: 100
        - name: standard
          priority: 0
        - name: sheddable
          priority: -10
        - name: batch
          priority: -50
    treatment_config_template: |
      apiVersion: inference.networking.x-k8s.io/v1alpha1
      kind: AdmissionPolicyConfig
      policies:
      - type: {plugin_type}
    baseline_config_template: |
      apiVersion: inference.networking.x-k8s.io/v1alpha1
      kind: AdmissionPolicyConfig
      policies:
      - type: always-admit
```

**Files to create:**
- `scripts/lib/env_config.py` - Environment config loading and merging

**Implementation details:**
```python
# scripts/lib/env_config.py
class EnvConfig:
    """Environment defaults with scenario-based merging"""
    
    def __init__(self, env_defaults_path: str):
        """Load env_defaults.yaml"""
        
    def get_merged_config(self, scenario: str) -> dict:
        """Deep merge common + scenario section"""
        
    def get_treatment_config_template(self, scenario: str) -> str:
        """Get treatment config template for scenario"""
        
    def get_baseline_config_template(self, scenario: str) -> Optional[str]:
        """Get baseline config template for scenario"""
        
    @staticmethod
    def deep_merge(base: dict, overlay: dict) -> dict:
        """Deep merge two dictionaries"""
```

**Tasks:**
- [ ] Backup current `config/env_defaults.yaml` to `config/env_defaults.yaml.legacy`
- [ ] Restructure `config/env_defaults.yaml` with new format
- [ ] Create `scripts/lib/env_config.py`
- [ ] Implement deep merge logic
- [ ] Add unit tests in `tests/test_env_config.py`
- [ ] Update `scripts/setup.py` to write to `common.stack.gaie.epp_image.build`

---

## Phase 2: Context Caching System

### 2.1 Context Hash Computation

**Files to create:**
- `scripts/lib/context_cache.py` - Context hashing and cache management

**Implementation details:**
```python
# scripts/lib/context_cache.py
import hashlib
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple

class ContextCache:
    """Context caching with content-based hashing"""
    
    def __init__(self, workspace_dir: str):
        self.workspace_dir = Path(workspace_dir)
        self.cache_dir = self.workspace_dir / "context"
        
    def compute_hash(
        self,
        scenario: str,
        context_files: List[str],
        inference_sim_path: str,
        llmd_path: str
    ) -> str:
        """
        Compute content-based hash from:
        - context_files content
        - inference-sim submodule SHA
        - llm-d-inference-scheduler submodule SHA
        """
        
    def get_cache_path(self, scenario: str, hash_value: str) -> Path:
        """Get cache file path for scenario + hash"""
        
    def cache_exists(self, scenario: str, hash_value: str) -> bool:
        """Check if cache exists"""
        
    def get_submodule_sha(self, submodule_path: str) -> str:
        """Get git commit SHA for submodule"""
        
    def read_cache(self, scenario: str, hash_value: str) -> str:
        """Read cached context.md"""
        
    def write_cache(
        self,
        scenario: str,
        hash_value: str,
        content: str,
        metadata: dict
    ) -> None:
        """Write context.md to cache with metadata"""
        
    def list_cached_contexts(self, scenario: Optional[str] = None) -> List[dict]:
        """List all cached contexts with metadata"""
        
    def cleanup_old_caches(self, scenario: str, keep_count: int = 5) -> None:
        """Remove old cache files, keep most recent N"""
```

**Tasks:**
- [ ] Create `scripts/lib/context_cache.py`
- [ ] Implement hash computation with SHA256
- [ ] Implement cache directory structure (`workspace/context/<scenario>/<hash>.md`)
- [ ] Add metadata file (`workspace/context/<scenario>/<hash>.meta.json`)
- [ ] Add unit tests in `tests/test_context_cache.py`

---

### 2.2 Context Assembly

**Files to create:**
- `scripts/lib/context_assembler.py` - Context document assembly logic

**Implementation details:**
```python
# scripts/lib/context_assembler.py
class ContextAssembler:
    """Assemble context.md from various sources"""
    
    def __init__(
        self,
        inference_sim_path: str,
        llmd_path: str,
        scenario: str
    ):
        self.inference_sim_path = Path(inference_sim_path)
        self.llmd_path = Path(llmd_path)
        self.scenario = scenario
        
    def assemble(
        self,
        context_files: List[str],
        context_notes: Optional[str] = None
    ) -> str:
        """
        Assemble complete context.md:
        1. Header with submodule SHAs
        2. Mapping document
        3. Production interfaces (scenario-specific)
        4. Example files from context_files
        5. Plugin registration pattern
        6. Context notes (appended, not cached)
        """
        
    def _read_mapping_doc(self) -> str:
        """Read sim2real mapping document"""
        
    def _read_production_interfaces(self) -> str:
        """Read relevant production interfaces for scenario"""
        
    def _read_context_files(self, files: List[str]) -> str:
        """Read and format context files"""
        
    def _read_registration_pattern(self) -> str:
        """Read plugin registration pattern from register.go"""
```

**Tasks:**
- [ ] Create `scripts/lib/context_assembler.py`
- [ ] Implement context assembly logic
- [ ] Add scenario-specific interface selection
- [ ] Add unit tests in `tests/test_context_assembler.py`

---

## Phase 3: Core Skill Architecture

### 3.1 State Management

**Files to create:**
- `scripts/lib/state.py` - Run state tracking and persistence

**Implementation details:**
```python
# scripts/lib/state.py
from enum import Enum
from typing import Optional, Dict, Any
from pathlib import Path
import json

class StepStatus(Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"

class RunState:
    """Track prepare skill execution state"""
    
    STEPS = [
        "context",
        "translate",
        "values",
        "cluster",
        "ai_check",
        "summary",
        "gate"
    ]
    
    def __init__(self, run_dir: str):
        self.run_dir = Path(run_dir)
        self.state_file = self.run_dir / ".state.json"
        self.state = self._load_or_initialize()
        
    def _load_or_initialize(self) -> dict:
        """Load existing state or create new"""
        
    def get_step_status(self, step: str) -> StepStatus:
        """Get status of a step"""
        
    def set_step_status(
        self,
        step: str,
        status: StepStatus,
        metadata: Optional[Dict[str, Any]] = None
    ) -> None:
        """Set step status and optional metadata"""
        
    def get_step_metadata(self, step: str) -> Dict[str, Any]:
        """Get step metadata"""
        
    def get_next_pending_step(self) -> Optional[str]:
        """Get first pending step, or None if all done"""
        
    def mark_step_done(self, step: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        """Mark step as done"""
        
    def mark_step_failed(self, step: str, error: str) -> None:
        """Mark step as failed with error"""
        
    def save(self) -> None:
        """Persist state to disk"""
        
    def reset_from_step(self, step: str) -> None:
        """Reset state from given step onwards"""
```

**Tasks:**
- [ ] Create `scripts/lib/state.py`
- [ ] Implement state persistence with JSON
- [ ] Add state validation
- [ ] Add unit tests in `tests/test_state.py`

---

### 3.2 Translation Output Schema

**Files to create:**
- `scripts/lib/translation_output.py` - Translation output schema

**Implementation details:**
```python
# scripts/lib/translation_output.py
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field

class TranslationOutput(BaseModel):
    """Schema for translation_output.json"""
    
    plugin_type: str = Field(..., description="Plugin type name (e.g., 'adaptive-v2-scorer')")
    files_created: List[str] = Field(..., description="List of created file paths")
    files_modified: List[str] = Field(..., description="List of modified file paths")
    package: str = Field(..., description="Go package name (e.g., 'scorer')")
    test_commands: List[List[str]] = Field(..., description="Test commands to run")
    config_kind: str = Field(..., description="Config kind (EndpointPickerConfig or AdmissionPolicyConfig)")
    helm_path: str = Field(..., description="Helm values path for config")
    needs_custom_config: bool = Field(False, description="Whether custom config generation needed")
    suggested_config: Optional[str] = Field(None, description="Suggested config structure")
    
    def save(self, output_path: str) -> None:
        """Save to JSON file"""
        
    @classmethod
    def load(cls, output_path: str) -> "TranslationOutput":
        """Load from JSON file"""
```

**Tasks:**
- [ ] Create `scripts/lib/translation_output.py`
- [ ] Implement Pydantic schema
- [ ] Add validation
- [ ] Add unit tests in `tests/test_translation_output.py`

---

### 3.3 Skill Orchestrator

**Files to create:**
- `.claude/skills/sim2real-prepare.skill` - Main skill definition
- `scripts/lib/skill_orchestrator.py` - Skill orchestration logic

**Skill file structure:**
```markdown
# sim2real-prepare

Orchestrates the sim2real transfer pipeline prepare stage.

## Usage

```bash
bob skill run sim2real-prepare
```

## Steps

1. Load setup config and transfer manifest
2. Check context cache (spawn subagent if needed)
3. Interactive translate + review loop
4. Assemble values
5. Assemble cluster YAMLs
6. Run AI config checker (subagent)
7. Generate run summary
8. Human review gate

## State Management

State tracked in `workspace/runs/<name>/.state.json`. Resume from any step.
```

**Implementation details:**
```python
# scripts/lib/skill_orchestrator.py
class SkillOrchestrator:
    """Orchestrate prepare skill execution"""
    
    def __init__(self, workspace_dir: str):
        self.workspace_dir = Path(workspace_dir)
        
    def run(self) -> None:
        """Main orchestration loop"""
        # Step 0: Load config
        # Step 1: Context check
        # Step 2: Translate + review
        # Step 3: Values assembly
        # Step 4: Cluster YAML assembly
        # Step 5: AI config checker
        # Step 6: Run summary
        # Step 7: Human review gate
        
    def step_0_load_config(self) -> Tuple[dict, TransferManifest]:
        """Load setup_config.json and transfer.yaml"""
        
    def step_1_context_check(
        self,
        manifest: TransferManifest,
        state: RunState
    ) -> str:
        """Check context cache, spawn subagent if needed"""
        
    def step_2_translate_and_review(
        self,
        manifest: TransferManifest,
        context_path: str,
        state: RunState
    ) -> TranslationOutput:
        """Interactive translate + review loop (stays in main session)"""
        
    def step_3_values_assembly(
        self,
        manifest: TransferManifest,
        translation: TranslationOutput,
        state: RunState
    ) -> None:
        """Assemble values.yaml"""
        
    def step_4_cluster_yaml_assembly(
        self,
        state: RunState
    ) -> None:
        """Assemble cluster YAMLs"""
        
    def step_5_ai_config_checker(
        self,
        translation: TranslationOutput,
        state: RunState
    ) -> None:
        """Spawn AI config checker subagent"""
        
    def step_6_run_summary(
        self,
        manifest: TransferManifest,
        translation: TranslationOutput,
        state: RunState
    ) -> None:
        """Generate run_summary.md"""
        
    def step_7_human_review_gate(
        self,
        state: RunState
    ) -> None:
        """Human review gate"""
```

**Tasks:**
- [ ] Create `.claude/skills/sim2real-prepare.skill`
- [ ] Create `scripts/lib/skill_orchestrator.py`
- [ ] Implement orchestration loop with state management
- [ ] Add resume logic
- [ ] Add error handling

---

### 3.4 Build/Test Gate

**Files to create:**
- `scripts/lib/build_test_gate.py` - Build and test execution

**Implementation details:**
```python
# scripts/lib/build_test_gate.py
import subprocess
from typing import List, Tuple, Optional
from pathlib import Path

class BuildTestGate:
    """Execute build and test commands"""
    
    def __init__(self, repo_path: str):
        self.repo_path = Path(repo_path)
        
    def run_commands(
        self,
        commands: List[List[str]],
        timeout: int = 600
    ) -> Tuple[bool, Optional[str]]:
        """
        Run commands sequentially, stop on first failure.
        Returns (success, error_message)
        """
        
    def format_error(
        self,
        command: List[str],
        returncode: int,
        stdout: str,
        stderr: str
    ) -> str:
        """Format structured error message"""
```

**Tasks:**
- [ ] Create `scripts/lib/build_test_gate.py`
- [ ] Implement command execution with timeout
- [ ] Add structured error formatting
- [ ] Add unit tests in `tests/test_build_test_gate.py`

---

### 3.5 Reviewer Loop

**Files to create:**
- `scripts/lib/reviewer.py` - Reviewer consensus logic

**Implementation details:**
```python
# scripts/lib/reviewer.py
from typing import List, Dict, Tuple
from enum import Enum
import asyncio

class ReviewDecision(Enum):
    APPROVE = "approve"
    NEEDS_CHANGES = "needs_changes"

class ReviewResult:
    """Single reviewer result"""
    model: str
    decision: ReviewDecision
    feedback: str
    
class ReviewerLoop:
    """Multi-model reviewer consensus"""
    
    def __init__(
        self,
        run_dir: str,
        dev_mode: bool = False
    ):
        self.run_dir = Path(run_dir)
        self.review_dir = self.run_dir / "review"
        self.dev_mode = dev_mode
        
    async def run_round(
        self,
        round_num: int,
        algorithm_source: str,
        context_md: str,
        generated_code: str
    ) -> List[ReviewResult]:
        """
        Run one review round with parallel API calls.
        Dev mode: aws/claude-opus-4-6 only
        Prod mode: Azure/gpt-4o, GCP/gemini-2.5-flash, aws/claude-opus-4-6
        """
        
    def aggregate_feedback(
        self,
        results: List[ReviewResult]
    ) -> Tuple[int, int, str]:
        """
        Aggregate reviewer feedback.
        Returns (approved_count, total_count, aggregated_feedback)
        """
        
    def has_consensus(self, results: List[ReviewResult]) -> bool:
        """Check if majority consensus reached (≥2/3)"""
        
    def save_round_results(
        self,
        round_num: int,
        results: List[ReviewResult]
    ) -> None:
        """Save round results to review/round_N.json"""
```

**Tasks:**
- [ ] Create `scripts/lib/reviewer.py`
- [ ] Implement parallel API calls using existing `lib/llm.py`
- [ ] Implement consensus logic
- [ ] Add dev mode (single model)
- [ ] Add unit tests in `tests/test_reviewer.py`

---

## Phase 4: Values & Cluster YAML Assembly

### 4.1 Config Generation

**Files to create:**
- `scripts/lib/config_generator.py` - Treatment/baseline config generation

**Implementation details:**
```python
# scripts/lib/config_generator.py
from typing import Optional
import asyncio

class ConfigGenerator:
    """Generate treatment and baseline configs"""
    
    def __init__(self, run_dir: str):
        self.run_dir = Path(run_dir)
        
    def generate_from_template(
        self,
        template: str,
        plugin_type: str
    ) -> str:
        """Fill template with plugin_type"""
        
    async def generate_custom(
        self,
        plugin_code: str,
        template: str,
        suggested_config: Optional[str]
    ) -> str:
        """Generate custom config via LLM API call"""
        
    def generate_treatment_config(
        self,
        env_config: EnvConfig,
        scenario: str,
        translation: TranslationOutput
    ) -> str:
        """Generate treatment config (template or custom)"""
        
    def generate_baseline_config(
        self,
        env_config: EnvConfig,
        scenario: str
    ) -> str:
        """Generate baseline config from template"""
```

**Tasks:**
- [ ] Create `scripts/lib/config_generator.py`
- [ ] Implement template-based generation
- [ ] Implement custom generation with LLM API
- [ ] Add unit tests in `tests/test_config_generator.py`

---

### 4.2 Values Merge

**Files to create:**
- `scripts/lib/values_merger.py` - Values assembly and merging

**Implementation details:**
```python
# scripts/lib/values_merger.py
class ValuesMerger:
    """Assemble and merge values.yaml"""
    
    def __init__(self, run_dir: str):
        self.run_dir = Path(run_dir)
        
    def merge(
        self,
        env_config: EnvConfig,
        scenario: str,
        algorithm_config: str,
        treatment_config: str,
        baseline_config: str,
        llm_config: str
    ) -> dict:
        """
        Merge all configs:
        1. env_defaults (common + scenario)
        2. algorithm config
        3. treatment/baseline configs
        4. llm_config
        """
        
    def validate(self, values: dict) -> List[str]:
        """Validate merged values against schema"""
        
    def save(self, values: dict, output_path: str) -> None:
        """Save values.yaml"""
```

**Tasks:**
- [ ] Create `scripts/lib/values_merger.py`
- [ ] Implement merge logic (reuse existing merge-values if possible)
- [ ] Add validation
- [ ] Add unit tests in `tests/test_values_merger.py`

---

### 4.3 Cluster YAML Assembly

**Files to create:**
- `scripts/lib/cluster_yaml_assembler.py` - Cluster YAML generation

**Implementation details:**
```python
# scripts/lib/cluster_yaml_assembler.py
class ClusterYAMLAssembler:
    """Assemble cluster YAMLs from values"""
    
    def __init__(self, run_dir: str):
        self.run_dir = Path(run_dir)
        self.cluster_dir = self.run_dir / "cluster"
        
    def extract_epp_configs(self, values: dict) -> Tuple[str, str]:
        """Extract baseline and treatment EPP configs"""
        
    def compile_pipeline_runs(self, values: dict) -> Tuple[str, str]:
        """
        Run compile-pipeline to generate PipelineRun YAMLs.
        Returns (baseline_yaml, treatment_yaml)
        """
        
    def assemble_all(self, values_path: str) -> None:
        """
        Assemble all cluster YAMLs:
        - cluster/epp-baseline.yaml
        - cluster/epp-treatment.yaml
        - cluster/pipelinerun-baseline.yaml
        - cluster/pipelinerun-treatment.yaml
        """
```

**Tasks:**
- [ ] Create `scripts/lib/cluster_yaml_assembler.py`
- [ ] Implement EPP config extraction
- [ ] Integrate with existing compile-pipeline
- [ ] Add unit tests in `tests/test_cluster_yaml_assembler.py`

---

## Phase 5: AI Config Checker & Run Summary

### 5.1 AI Config Checker

**Files to create:**
- `.claude/skills/ai-config-checker.skill` - AI checker skill
- `scripts/lib/ai_config_checker.py` - Config validation logic

**Implementation details:**
```python
# scripts/lib/ai_config_checker.py
class AIConfigChecker:
    """AI-powered config consistency checker"""
    
    def __init__(self, run_dir: str):
        self.run_dir = Path(run_dir)
        
    def check(
        self,
        translation: TranslationOutput,
        epp_config_path: str,
        values_path: str
    ) -> Tuple[bool, List[str]]:
        """
        Check config consistency:
        1. plugin_type matches in code and EPP config
        2. All pluginRef names resolve to declared plugins
        3. plugin_type registered in register.go
        4. helm_path matches config placement in values.yaml
        
        Returns (passed, issues)
        """
        
    def check_endpoint_picker_config(
        self,
        plugin_type: str,
        epp_config: dict
    ) -> List[str]:
        """Validate EndpointPickerConfig"""
        
    def check_admission_policy_config(
        self,
        plugin_type: str,
        epp_config: dict
    ) -> List[str]:
        """Validate AdmissionPolicyConfig"""
        
    def check_registration(
        self,
        plugin_type: str,
        register_go_path: str
    ) -> bool:
        """Check if plugin_type is registered"""
        
    def check_helm_path(
        self,
        helm_path: str,
        values: dict
    ) -> bool:
        """Check if helm_path exists in values"""
```

**Tasks:**
- [ ] Create `.claude/skills/ai-config-checker.skill`
- [ ] Create `scripts/lib/ai_config_checker.py`
- [ ] Implement validation rules for both config kinds
- [ ] Add unit tests in `tests/test_ai_config_checker.py`

---

### 5.2 Run Summary Generator

**Files to create:**
- `scripts/lib/run_summary_generator.py` - Run summary assembly

**Implementation details:**
```python
# scripts/lib/run_summary_generator.py
class RunSummaryGenerator:
    """Generate run_summary.md"""
    
    def __init__(self, run_dir: str):
        self.run_dir = Path(run_dir)
        
    def generate(
        self,
        manifest: TransferManifest,
        translation: TranslationOutput,
        state: RunState,
        values: dict
    ) -> str:
        """
        Generate complete run summary with sections:
        - Algorithm
        - Translation
        - Baseline vs Treatment Setup (comparison table)
        - EPP Configuration (both baseline and treatment)
        - vLLM
        - Workloads
        - Cluster
        - Checklist
        """
        
    def _format_algorithm_section(self, manifest: TransferManifest) -> str:
        """Format algorithm section"""
        
    def _format_translation_section(
        self,
        translation: TranslationOutput,
        state: RunState
    ) -> str:
        """Format translation section with review consensus"""
        
    def _format_comparison_table(self, values: dict) -> str:
        """Format baseline vs treatment comparison table"""
        
    def _format_epp_configs(self, cluster_dir: Path) -> str:
        """Format EPP config sections"""
        
    def save(self, content: str) -> None:
        """Save run_summary.md"""
```

**Tasks:**
- [ ] Create `scripts/lib/run_summary_generator.py`
- [ ] Implement markdown generation
- [ ] Add comparison table logic
- [ ] Add unit tests in `tests/test_run_summary_generator.py`

---

### 5.3 Human Review Gate

**Files to create:**
- `scripts/lib/human_review_gate.py` - Interactive review gate

**Implementation details:**
```python
# scripts/lib/human_review_gate.py
from enum import Enum

class ReviewGateDecision(Enum):
    DEPLOY = "deploy"
    EDIT = "edit"
    QUIT = "quit"

class HumanReviewGate:
    """Interactive human review gate"""
    
    def __init__(self, run_dir: str):
        self.run_dir = Path(run_dir)
        self.summary_path = self.run_dir / "run_summary.md"
        
    def present_and_wait(self) -> ReviewGateDecision:
        """
        Display run_summary.md and wait for operator decision.
        Returns operator's choice.
        """
        
    def mark_ready_to_deploy(self) -> None:
        """Write 'READY TO DEPLOY' into run_summary.md"""
        
    def open_for_edit(self) -> None:
        """Open run_summary.md in editor"""
```

**Tasks:**
- [ ] Create `scripts/lib/human_review_gate.py`
- [ ] Implement interactive prompt
- [ ] Add editor integration
- [ ] Add unit tests in `tests/test_human_review_gate.py`

---

## Phase 6: Deploy.py Updates

### 6.1 Pre-Built YAML Application

**Files to modify:**
- `scripts/deploy.py` - Update to use pre-built YAMLs

**Changes:**
```python
# scripts/deploy.py

def check_ready_to_deploy(run_dir: str) -> bool:
    """Check if run_summary.md contains 'READY TO DEPLOY'"""
    summary_path = Path(run_dir) / "run