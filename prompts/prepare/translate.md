---
stage: prepare
version: "2.0"
description: "Translation writer prompt — generic, scenario-agnostic"
---

# Translation Writer

Translate a simulation-discovered algorithm into a production plugin for
llm-d-inference-scheduler. This prompt is invoked by the /sim2real-translate
skill after `prepare.py` writes `skill_input.json`.

## Inputs

Read `skill_input.json` from the run directory. It contains:

| Field              | Description                                          |
|--------------------|------------------------------------------------------|
| `run_name`         | Current run identifier                               |
| `run_dir`          | Path to the run directory (relative to repo root)    |
| `scenario`         | Transfer scenario (e.g., `routing`, `admission_control`) |
| `context_path`     | Path to the cached context.md document               |
| `context_notes`    | Free-text notes from the manifest author             |
| `algorithm_source` | Path to the source algorithm file                    |
| `algorithm_config` | Path to the algorithm's policy config                |
| `target`           | Target repo info (repo, plugin_dir, register_file, package) |
| `build_commands`   | List of build/test commands to validate the plugin   |
| `config_kind`      | Expected kind for the treatment config YAML          |

## Context

Read the context document at `context_path`. It contains:
- Mapping documents (sim signals to production equivalents)
- Submodule commit SHAs for traceability

Also read `context_notes` for scenario-specific translation hints from the
manifest author.

## Translation Loop

1. **Read** the algorithm source and config to understand the scoring/admission logic
2. **Read** the context document for signal mapping and production patterns
3. **Write** the production plugin code in `target.plugin_dir`
4. **Register** the plugin in `target.register_file`
5. **Write** `treatment_config.yaml` in the run directory with `kind: {config_kind}`
6. **Build + Test** using `build_commands` (run in the target repo directory)
7. If tests fail, diagnose and fix. Repeat from step 3.

## Output Artifacts

After successful translation, write to the run directory:

### `translation_output.json`
```json
{
  "plugin_type": "<registered-plugin-name>",
  "description": "<one-line description of the algorithm>",
  "files_created": ["<relative paths within target repo>"],
  "files_modified": ["<relative paths within target repo>"],
  "review_rounds": 0,
  "consensus": "N/A"
}
```

### `treatment_config.yaml`
The treatment EPP/admission config. Must have `kind: {config_kind}`.

### `generated/`
Copy all created/modified files into `{run_dir}/generated/` for inspectability.
This allows prepare.py to verify and include them in the run summary.

## Review

If multi-model review is configured, the reviewer prompt at
`prompts/prepare/review.md` will evaluate the translation. Address any
NEEDS_CHANGES feedback and update `translation_output.json` with the
final `review_rounds` and `consensus`.

## Completion

After writing all output artifacts, the user re-runs `prepare.py` which
detects `translation_output.json` and continues through Assembly, Summary,
and Gate phases.
