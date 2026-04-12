# sim2real-translate Skill Redesign Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Redesign the sim2real-translate skill to use state machine integration, context caching with subagent delegation, parallel multi-model review, and the new 9-field `translation_output.json` schema.

**Architecture:** `SKILL.md` uses `pipeline/lib/state_machine.py` for resumability; `review.py` consolidates `build_review_request.py` + `review_translation.py` with parallel `concurrent.futures` dispatch; writer writes `translation_output.json` (9 fields) before the build/test gate, which reads `test_commands` from it. Context assembly is delegated to a subagent on cache miss.

**Tech Stack:** Python 3.10+ stdlib + `concurrent.futures`, Claude Code Agent tool for context subagent, `pipeline/lib` (state_machine, context_builder).

**Spec:** `docs/superpowers/specs/2026-04-09-sim2real-translate-skill-design.md`

---

## File Structure

### New Files
| File | Responsibility |
|------|----------------|
| `.claude/skills/sim2real-translate/scripts/review.py` | Parallel multi-model reviewer; inlines prompt building; reads `prompts/prepare/review.md` |
| `.claude/skills/sim2real-translate/tests/__init__.py` | Empty package marker |
| `.claude/skills/sim2real-translate/tests/test_review.py` | Unit tests: consensus logic, JSON extraction, prompt building |

### Modified Files
| File | Change |
|------|--------|
| `.claude/skills/sim2real-translate/SKILL.md` | Full rewrite: state machine resumability, context subagent, 9-field schema, parallel review, `--rebuild-context` flag |

### Deleted Files
| File | Reason |
|------|--------|
| `.claude/skills/sim2real-translate/scripts/build_review_request.py` | Merged into `review.py` |
| `.claude/skills/sim2real-translate/scripts/review_translation.py` | Replaced by `review.py` |
| `.claude/skills/sim2real-translate/scripts/dashboard.py` | Replaced by Claude Code TaskCreate/TaskUpdate |

### No Changes Needed
- `pipeline/lib/state_machine.py` — already implemented
- `pipeline/lib/context_builder.py` — already implemented
- `prompts/prepare/translate.md` — out of scope (separate design task)
- `prompts/prepare/review.md` — out of scope (separate design task)

---

## Chunk 1: review.py — Parallel Multi-Model Reviewer

### Task 1: Failing tests for review.py

**Files:**
- Create: `.claude/skills/sim2real-translate/tests/__init__.py`
- Create: `.claude/skills/sim2real-translate/tests/test_review.py`

- [ ] **Step 1: Create empty package init**

```bash
touch .claude/skills/sim2real-translate/tests/__init__.py
```

- [ ] **Step 2: Write failing test file**

Create `.claude/skills/sim2real-translate/tests/test_review.py`:

```python
"""Tests for review.py — consensus logic, JSON extraction, prompt building."""
import json
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import review as rv


# ── check_consensus ────────────────────────────────────────────────────────────

def _r(verdict, model="m1"):
    return {"model": model, "verdict": verdict, "issues": [], "summary": ""}


def test_consensus_all_approve():
    reviews = [_r("APPROVE", "m1"), _r("APPROVE", "m2"), _r("APPROVE", "m3")]
    has_consensus, approve, total = rv.check_consensus(reviews)
    assert has_consensus is True
    assert approve == 3
    assert total == 3


def test_consensus_majority_approve():
    reviews = [_r("APPROVE", "m1"), _r("APPROVE", "m2"), _r("NEEDS_CHANGES", "m3")]
    has_consensus, approve, total = rv.check_consensus(reviews)
    assert has_consensus is True
    assert approve == 2
    assert total == 3


def test_consensus_approve_with_error():
    """2 APPROVE + 1 ERROR → 2/2 successful = consensus."""
    reviews = [_r("APPROVE", "m1"), _r("APPROVE", "m2"), _r("ERROR", "m3")]
    has_consensus, approve, total = rv.check_consensus(reviews)
    assert has_consensus is True
    assert approve == 2
    assert total == 2


def test_no_consensus_split_with_error():
    """1 APPROVE + 1 NEEDS_CHANGES + 1 ERROR → 1/2 successful = no consensus."""
    reviews = [_r("APPROVE", "m1"), _r("NEEDS_CHANGES", "m2"), _r("ERROR", "m3")]
    has_consensus, approve, total = rv.check_consensus(reviews)
    assert has_consensus is False
    assert approve == 1
    assert total == 2


def test_no_consensus_all_needs_changes_with_error():
    """2 NEEDS_CHANGES + 1 ERROR → 0/2 successful = no consensus."""
    reviews = [_r("NEEDS_CHANGES", "m1"), _r("NEEDS_CHANGES", "m2"), _r("ERROR", "m3")]
    has_consensus, approve, total = rv.check_consensus(reviews)
    assert has_consensus is False
    assert approve == 0
    assert total == 2


def test_no_consensus_all_errors():
    """All errors → 0 successful reviews."""
    reviews = [_r("ERROR", "m1"), _r("ERROR", "m2"), _r("ERROR", "m3")]
    has_consensus, approve, total = rv.check_consensus(reviews)
    assert has_consensus is False
    assert approve == 0
    assert total == 0


def test_dev_mode_single_approve():
    """1/1 in dev mode → consensus."""
    reviews = [_r("APPROVE")]
    has_consensus, approve, total = rv.check_consensus(reviews)
    assert has_consensus is True


def test_dev_mode_single_needs_changes():
    reviews = [_r("NEEDS_CHANGES")]
    has_consensus, approve, total = rv.check_consensus(reviews)
    assert has_consensus is False


# ── extract_json_from_content ──────────────────────────────────────────────────

def test_extract_plain_json():
    content = '{"verdict": "APPROVE", "issues": [], "summary": "ok"}'
    result = rv.extract_json_from_content(content)
    assert result is not None
    assert result["verdict"] == "APPROVE"


def test_extract_json_in_code_fence():
    content = '```json\n{"verdict": "NEEDS_CHANGES", "issues": [{"category": "fidelity", "description": "x", "file": "f.go", "fix": "y"}], "summary": "needs work"}\n```'
    result = rv.extract_json_from_content(content)
    assert result is not None
    assert result["verdict"] == "NEEDS_CHANGES"
    assert len(result["issues"]) == 1


def test_extract_json_with_prose():
    content = 'Here is my review:\n{"verdict": "APPROVE", "issues": [], "summary": "good"}\nDone.'
    result = rv.extract_json_from_content(content)
    assert result is not None
    assert result["verdict"] == "APPROVE"


def test_extract_json_trailing_comma():
    """Gemini sometimes produces trailing commas."""
    content = '{"verdict": "APPROVE", "issues": [], "summary": "ok",}'
    result = rv.extract_json_from_content(content)
    assert result is not None


def test_extract_json_returns_none_on_garbage():
    result = rv.extract_json_from_content("no json here at all")
    assert result is None


# ── build_system_prompt ────────────────────────────────────────────────────────

def test_build_system_prompt_includes_json_schema(tmp_path, monkeypatch):
    """build_system_prompt reads review.md and appends JSON format section."""
    fake_prompt = tmp_path / "review.md"
    fake_prompt.write_text("# Review Criteria\nCheck fidelity.\n")
    monkeypatch.setattr(rv, "REVIEW_PROMPT", fake_prompt)

    prompt = rv.build_system_prompt()
    assert "# Review Criteria" in prompt
    assert "verdict" in prompt
    assert "NEEDS_CHANGES" in prompt


def test_build_system_prompt_file_missing(tmp_path, monkeypatch):
    """Missing review.md raises FileNotFoundError."""
    monkeypatch.setattr(rv, "REVIEW_PROMPT", tmp_path / "nonexistent.md")
    with pytest.raises(FileNotFoundError):
        rv.build_system_prompt()


# ── build_user_message ─────────────────────────────────────────────────────────

def test_build_user_message_round_number():
    msg = rv.build_user_message("package p", "func algo(){}", "thresh: 0.5",
                                 "# Context", "kind: Scorer", 3)
    assert "Round 3" in msg


def test_build_user_message_includes_all_sections():
    msg = rv.build_user_message("package p", "func algo(){}", "thresh: 0.5",
                                 "# Context", "kind: Scorer", 1)
    assert "Generated Plugin Code" in msg
    assert "Algorithm Source" in msg
    assert "Algorithm Config" in msg
    assert "Translation Context" in msg
    assert "Treatment Config" in msg


# ── collect_issues ─────────────────────────────────────────────────────────────

def test_collect_issues_from_needs_changes():
    reviews = [
        {"verdict": "NEEDS_CHANGES", "issues": [
            {"category": "fidelity", "description": "issue A", "file": "f.go", "fix": "fix A"}
        ]},
        {"verdict": "APPROVE", "issues": []},
    ]
    issues = rv.collect_issues(reviews)
    assert len(issues) == 1
    assert issues[0]["description"] == "issue A"


def test_collect_issues_string_coercion():
    """Models that return string issues are coerced to dicts."""
    reviews = [{"verdict": "NEEDS_CHANGES", "issues": ["simple string issue"]}]
    issues = rv.collect_issues(reviews)
    assert len(issues) == 1
    assert issues[0]["description"] == "simple string issue"


def test_collect_issues_ignores_errors():
    reviews = [{"verdict": "ERROR", "issues": []}]
    assert rv.collect_issues(reviews) == []
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
cd /Users/jchen/go/src/inference-sim/sim2real
python -m pytest .claude/skills/sim2real-translate/tests/test_review.py -v
```

Expected: `ModuleNotFoundError: No module named 'review'` (file doesn't exist yet)

- [ ] **Step 4: Commit failing tests**

```bash
git add .claude/skills/sim2real-translate/tests/
git commit -m "test(sim2real-translate): add failing tests for review.py redesign"
```

---

### Task 2: Write review.py

**Files:**
- Create: `.claude/skills/sim2real-translate/scripts/review.py`

- [ ] **Step 1: Write review.py**

Create `.claude/skills/sim2real-translate/scripts/review.py`:

```python
#!/usr/bin/env python3
"""Multi-model translation review (v2).

Consolidates build_review_request.py + review_translation.py.
Reads prompts/prepare/review.md as system prompt base; appends JSON format instruction.
Calls models in parallel via concurrent.futures.ThreadPoolExecutor.

Usage:
    python3 .claude/skills/sim2real-translate/scripts/review.py \
        --plugin-files FILE [FILE ...] \
        --algorithm FILE \
        --algorithm-config FILE \
        --context FILE \
        --treatment-config FILE \
        --round N \
        --out FILE \
        [--dev]

Environment:
    OPENAI_API_KEY / OPENAI_BASE_URL  (primary)
    ANTHROPIC_AUTH_TOKEN / ANTHROPIC_BASE_URL (fallback)

Exit codes:
    0 — consensus reached (majority of successful reviews APPROVE)
    1 — no consensus (NEEDS_CHANGES or mixed verdicts)
    2 — all models errored (no successful reviews)
"""

import argparse
import json
import os
import re
import ssl
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# Repo root is 4 levels up from .claude/skills/sim2real-translate/scripts/
_REPO_ROOT = Path(__file__).parents[4]
REVIEW_PROMPT = _REPO_ROOT / "prompts" / "prepare" / "review.md"

ALL_MODELS = ["Azure/gpt-4o", "GCP/gemini-2.5-flash", "aws/claude-opus-4-6"]
DEV_MODELS = ["aws/claude-opus-4-6"]

_JSON_FORMAT_SUFFIX = """

## Required Output Format

Respond ONLY with a valid JSON object. No markdown fences, no prose outside the JSON.

Schema:
{
  "verdict": "APPROVE" | "NEEDS_CHANGES",
  "issues": [
    {
      "category": "fidelity" | "config" | "code-quality" | "registration",
      "description": "what is wrong",
      "file": "path/to/file.go",
      "fix": "suggested correction"
    }
  ],
  "summary": "brief overall assessment (under 200 chars)"
}

If no issues: {"verdict": "APPROVE", "issues": [], "summary": "..."}
"""

RED = "\033[0;31m"
GREEN = "\033[0;32m"
YELLOW = "\033[1;33m"
BLUE = "\033[0;34m"
NC = "\033[0m"


def build_system_prompt() -> str:
    """Read review.md and append JSON format instruction."""
    return REVIEW_PROMPT.read_text() + _JSON_FORMAT_SUFFIX


def build_user_message(
    plugin_code: str,
    algorithm_source: str,
    algorithm_config: str,
    context_doc: str,
    treatment_config: str,
    round_num: int,
) -> str:
    return (
        f"## Review Round {round_num}\n\n"
        f"## Generated Plugin Code\n```go\n{plugin_code}\n```\n\n"
        f"## Algorithm Source (Ground Truth)\n```go\n{algorithm_source}\n```\n\n"
        f"## Algorithm Config\n```yaml\n{algorithm_config}\n```\n\n"
        f"## Translation Context\n{context_doc}\n\n"
        f"## Treatment Config\n```yaml\n{treatment_config}\n```\n"
    )


def extract_json_from_content(content: str) -> "dict | None":
    """Extract JSON object from LLM response content.

    Handles: markdown code fences, prose before/after JSON,
    truncated responses, trailing commas (Gemini).
    """
    text = content.strip()

    # 1. Extract from code fences
    fence_matches = list(re.finditer(r"```(?:json)?\s*\n(.*?)```", text, re.DOTALL))
    if fence_matches:
        text = max(fence_matches, key=lambda m: len(m.group(1))).group(1).strip()
    elif re.match(r"^```(?:json)?\s*\n", text):
        text = re.sub(r"^```(?:json)?\s*\n", "", text).strip()

    # 2. Try direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 3. Find outermost { ... } via brace matching
    start = text.find("{")
    if start == -1:
        return None
    depth, in_string, escape, end = 0, False, False, start
    for i in range(start, len(text)):
        c = text[i]
        if escape:
            escape = False
            continue
        if c == "\\" and in_string:
            escape = True
            continue
        if c == '"' and not escape:
            in_string = not in_string
            continue
        if in_string:
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break

    if depth == 0 and end > start:
        candidate = text[start:end]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass
        cleaned = re.sub(r",\s*([}\]])", r"\1", candidate)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass

    # 4. Last resort: close unclosed braces
    candidate = text[start:]
    candidate = re.sub(r",\s*([}\]])", r"\1", candidate)
    open_braces = candidate.count("{") - candidate.count("}")
    open_brackets = candidate.count("[") - candidate.count("]")
    quote_count = len(re.findall(r'(?<!\\)"', candidate))
    if quote_count % 2 == 1:
        candidate += '"'
    candidate += "]" * max(0, open_brackets)
    candidate += "}" * max(0, open_braces)
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        return None


def check_consensus(reviews: list) -> "tuple[bool, int, int]":
    """Return (has_consensus, approve_count, total_successful).

    Consensus = majority of successful (non-error) responses APPROVE.
    Error responses are non-votes.
    """
    successful = [r for r in reviews if r.get("verdict") != "ERROR"]
    if not successful:
        return False, 0, 0
    approve_count = sum(1 for r in successful if r.get("verdict") == "APPROVE")
    return approve_count > len(successful) / 2, approve_count, len(successful)


def collect_issues(reviews: list) -> list:
    """Gather all issues from NEEDS_CHANGES reviews."""
    issues = []
    for r in reviews:
        if r.get("verdict") == "NEEDS_CHANGES":
            for issue in r.get("issues", []):
                if isinstance(issue, dict):
                    issues.append(issue)
                else:
                    issues.append({
                        "description": str(issue),
                        "category": "unknown",
                        "file": "",
                        "fix": "",
                    })
    return issues


def resolve_api_credentials() -> "tuple[str, str]":
    api_key = os.environ.get("OPENAI_API_KEY")
    base_url = os.environ.get(
        "OPENAI_BASE_URL", os.environ.get("OPENAI_URL", "https://api.openai.com")
    )
    if not api_key:
        api_key = os.environ.get("ANTHROPIC_AUTH_TOKEN")
        base_url = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    if not api_key:
        print(
            f"{RED}[ERROR]{NC} No API key. Set OPENAI_API_KEY or ANTHROPIC_AUTH_TOKEN.",
            file=sys.stderr,
        )
        sys.exit(1)
    return api_key, base_url


def _call_model_raw(
    model: str, messages: list, api_key: str, base_url: str
) -> "tuple[dict | None, str | None]":
    """Call one model. Returns (review_dict, error_str)."""
    payload = json.dumps({
        "model": model,
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": 8192,
    }).encode()
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    endpoint = f"{base_url}/v1/chat/completions"
    try:
        req = urllib.request.Request(endpoint, data=payload, headers=headers)
        ctx = ssl.create_default_context()
        resp = urllib.request.urlopen(req, timeout=300, context=ctx)
        body = resp.read().decode()
    except urllib.error.HTTPError as e:
        preview = e.read().decode()[:200] if hasattr(e, "read") else str(e)
        return None, f"HTTP {e.code}: {preview}"
    except (urllib.error.URLError, TimeoutError) as e:
        return None, str(e)

    try:
        response = json.loads(body)
        content = response["choices"][0]["message"]["content"]
        if response["choices"][0].get("finish_reason") == "length":
            print(f"{YELLOW}[WARN]{NC} {model} response truncated", file=sys.stderr)
        review = extract_json_from_content(content)
        if review is None:
            return None, f"could not extract JSON (preview: {content[:200]})"
        return review, None
    except (json.JSONDecodeError, KeyError, IndexError) as e:
        return None, str(e)


def run_review_round(
    round_num: int,
    plugin_files: list,
    algorithm: str,
    algorithm_config: str,
    context: str,
    treatment_config: str,
    models: list,
    api_key: str,
    base_url: str,
) -> list:
    """Run one review round across all models in parallel."""
    plugin_parts = []
    for pf in plugin_files:
        p = Path(pf)
        plugin_parts.append(f"// --- {p.name} ---\n{p.read_text()}")
    plugin_code = "\n\n".join(plugin_parts)

    messages = [
        {"role": "system", "content": build_system_prompt()},
        {"role": "user", "content": build_user_message(
            plugin_code,
            Path(algorithm).read_text(),
            Path(algorithm_config).read_text(),
            Path(context).read_text(),
            Path(treatment_config).read_text(),
            round_num,
        )},
    ]

    print(f"\n{BLUE}--- Review Round {round_num} ({len(models)} models in parallel) ---{NC}")
    reviews = []
    with ThreadPoolExecutor(max_workers=len(models)) as executor:
        future_to_model = {
            executor.submit(_call_model_raw, m, messages, api_key, base_url): m
            for m in models
        }
        for future in as_completed(future_to_model):
            model = future_to_model[future]
            print(f"  {model}... ", end="", flush=True)
            review, error = future.result()
            if review is None:
                print(f"{RED}ERROR: {error}{NC}")
                reviews.append({
                    "model": model, "round": round_num,
                    "verdict": "ERROR", "issues": [],
                    "summary": f"Error: {error}",
                })
            else:
                review["model"] = model
                review["round"] = round_num
                verdict = review.get("verdict", "UNKNOWN")
                if verdict == "APPROVE":
                    print(f"{GREEN}{verdict}{NC}")
                else:
                    issue_count = len(review.get("issues", []))
                    print(f"{YELLOW}{verdict} ({issue_count} issues){NC}")
                reviews.append(review)
    return reviews


def main():
    parser = argparse.ArgumentParser(description="Multi-model translation review (v2)")
    parser.add_argument(
        "--plugin-files", nargs="+", required=True, help="Paths to generated .go files"
    )
    parser.add_argument("--algorithm", required=True, help="Path to algorithm source file")
    parser.add_argument(
        "--algorithm-config", required=True, help="Path to algorithm config YAML"
    )
    parser.add_argument("--context", required=True, help="Path to context.md document")
    parser.add_argument(
        "--treatment-config", required=True, help="Path to treatment_config.yaml"
    )
    parser.add_argument("--round", type=int, default=1, help="Round number")
    parser.add_argument("--out", required=True, help="Output path for round JSON result")
    parser.add_argument(
        "--dev", action="store_true", help="Dev mode: only aws/claude-opus-4-6"
    )
    args = parser.parse_args()

    for pf in args.plugin_files:
        if not Path(pf).is_file():
            print(f"{RED}[ERROR]{NC} Plugin file not found: {pf}", file=sys.stderr)
            sys.exit(1)
    for attr, label in [
        ("algorithm", "algorithm source"),
        ("algorithm_config", "algorithm config"),
        ("context", "context document"),
        ("treatment_config", "treatment config"),
    ]:
        path = getattr(args, attr)
        if not Path(path).is_file():
            print(f"{RED}[ERROR]{NC} {label} not found: {path}", file=sys.stderr)
            sys.exit(1)

    api_key, base_url = resolve_api_credentials()
    models = DEV_MODELS if args.dev else ALL_MODELS

    reviews = run_review_round(
        args.round,
        args.plugin_files,
        args.algorithm,
        args.algorithm_config,
        args.context,
        args.treatment_config,
        models,
        api_key,
        base_url,
    )
    has_consensus, approve_count, total_successful = check_consensus(reviews)

    if total_successful == 0:
        print(f"\n{RED}Round {args.round}: All models errored (0 successful reviews){NC}")
    elif has_consensus:
        print(
            f"\n{GREEN}Round {args.round}: Consensus reached "
            f"({approve_count}/{total_successful} APPROVE){NC}"
        )
    else:
        issues = collect_issues(reviews)
        print(
            f"\n{YELLOW}Round {args.round}: No consensus "
            f"({approve_count}/{total_successful} APPROVE){NC}"
        )
        if issues:
            print(f"{YELLOW}Issues:{NC}")
            for issue in issues:
                cat = issue.get("category", "unknown")
                desc = issue.get("description", str(issue))
                print(f"  [{cat}] {desc}")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    round_result = {
        "round": args.round,
        "models": models,
        "reviews": reviews,
        "consensus": has_consensus,
        "approve_count": approve_count,
        "total_successful": total_successful,
        "issues": collect_issues(reviews),
    }
    out_path.write_text(json.dumps(round_result, indent=2))
    print(f"Results written to {args.out}")

    if total_successful == 0:
        sys.exit(2)
    elif has_consensus:
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run tests — verify they pass**

```bash
cd /Users/jchen/go/src/inference-sim/sim2real
python -m pytest .claude/skills/sim2real-translate/tests/test_review.py -v
```

Expected: All 20 tests PASS

- [ ] **Step 3: Verify REVIEW_PROMPT path resolves correctly**

```bash
python3 -c "
from pathlib import Path
script = Path('.claude/skills/sim2real-translate/scripts/review.py').resolve()
repo_root = script.parents[4]
prompt = repo_root / 'prompts' / 'prepare' / 'review.md'
print(f'Repo root: {repo_root}')
print(f'Prompt exists: {prompt.exists()}')
"
```

Expected: `Prompt exists: True`

- [ ] **Step 4: Commit**

```bash
git add .claude/skills/sim2real-translate/scripts/review.py
git commit -m "feat(sim2real-translate): add review.py with parallel dispatch and inline prompt assembly"
```

---

## Chunk 2: SKILL.md — Full Rewrite

### Task 3: Write SKILL.md

**Files:**
- Modify: `.claude/skills/sim2real-translate/SKILL.md`

The rewrite replaces the current SKILL.md with: state machine resumability, context subagent on cache miss, `translation_output.json` written by the writer before the build gate, `test_commands` read from that file, `--rebuild-context` flag, and TaskCreate/TaskUpdate progress tracking.

- [ ] **Step 1: Write the complete SKILL.md**

Replace the full contents of `.claude/skills/sim2real-translate/SKILL.md`:

```markdown
---
name: sim2real-translate
description: |
  Translates a simulation-discovered algorithm into a production plugin for
  llm-d-inference-scheduler. Reads skill_input.json (written by prepare.py),
  produces Go code + treatment config, runs build/test gate, multi-model
  reviewer loop, snapshots, and writes translation_output.json.
  Use after prepare.py reaches the translation checkpoint.
argument-hint: "[--rounds N] [--dev] [--rebuild-context]"
user-invocable: true
allowed-tools:
  - Agent
  - TaskCreate
  - TaskUpdate
  - Bash(**/review.py *)
  - Bash(python3 *)
  - Bash(cd * && *)
  - Bash(test *)
  - Bash(git *)
  - Bash(GOWORK=off go *)
  - Glob
  - Read
  - Edit
  - Write
  - Grep
---

# sim2real-translate

Translate a simulation algorithm into a production plugin, with build/test
gates and multi-model AI review.

## CRITICAL: Working Directory

**All commands in this skill must run from the `sim2real/` root directory.**
The target production repo (e.g., `llm-d-inference-scheduler/`) is a submodule.
Run commands there via subshell: `(cd llm-d-inference-scheduler && ...)`

Before each major step, verify:
```bash
test -f config/env_defaults.yaml || { echo "ERROR: not in sim2real root"; exit 1; }
```

## Arguments

- `--rounds N` — Max multi-model review rounds (default: 3)
- `--dev` — Dev mode: only use `aws/claude-opus-4-6` for reviews (faster)
- `--rebuild-context` — Force context reassembly even if cache exists

Parse from skill invocation arguments. Defaults: `REVIEW_ROUNDS=3`,
`DEV_MODE=false`, `REBUILD_CONTEXT=false`.

## Prerequisites

Use TaskCreate to create an overall progress task:
```
TaskCreate subject="sim2real-translate: $RUN_NAME" description="Running translation skill"
```

Read current run directory:

```bash
RUN_DIR=$(python3 -c "
import json, sys, os
config_path = 'workspace/setup_config.json'
if not os.path.exists(config_path):
    print('HALT: workspace/setup_config.json not found. Run /sim2real-setup first.', file=sys.stderr)
    sys.exit(1)
config = json.load(open(config_path))
run_name = config.get('current_run', '')
if not run_name:
    print('HALT: No current_run in setup_config.json.', file=sys.stderr)
    sys.exit(1)
run_dir = f'workspace/runs/{run_name}'
if not os.path.exists(f'{run_dir}/skill_input.json'):
    print(f'HALT: {run_dir}/skill_input.json not found. Run prepare.py first.', file=sys.stderr)
    sys.exit(1)
print(run_dir)
")
test -n "$RUN_DIR" || exit 1
```

Validate and load `skill_input.json`:

```bash
python3 -c "
import json, sys
si = json.load(open('$RUN_DIR/skill_input.json'))
required = ['run_name', 'run_dir', 'scenario', 'context_path', 'manifest_path',
            'algorithm_source', 'algorithm_config', 'target', 'build_commands', 'config_kind']
missing = [f for f in required if f not in si]
if missing:
    print(f'HALT: skill_input.json missing fields: {missing}')
    sys.exit(1)
for f in ['repo', 'plugin_dir', 'register_file', 'package']:
    if f not in si['target']:
        print(f'HALT: skill_input.json target.{f} missing')
        sys.exit(1)
print(f'Loaded: run={si[\"run_name\"]} scenario={si[\"scenario\"]} config_kind={si[\"config_kind\"]}')
" || exit 1
```

Load into shell variables:

```bash
RUN_NAME=$(python3 -c "import json; print(json.load(open('$RUN_DIR/skill_input.json'))['run_name'])")
SCENARIO=$(python3 -c "import json; print(json.load(open('$RUN_DIR/skill_input.json'))['scenario'])")
CONTEXT_PATH=$(python3 -c "import json; print(json.load(open('$RUN_DIR/skill_input.json'))['context_path'])")
MANIFEST_PATH=$(python3 -c "import json; print(json.load(open('$RUN_DIR/skill_input.json'))['manifest_path'])")
ALGO_SOURCE=$(python3 -c "import json; print(json.load(open('$RUN_DIR/skill_input.json'))['algorithm_source'])")
ALGO_CONFIG=$(python3 -c "import json; print(json.load(open('$RUN_DIR/skill_input.json'))['algorithm_config'])")
TARGET_REPO=$(python3 -c "import json; print(json.load(open('$RUN_DIR/skill_input.json'))['target']['repo'])")
PLUGIN_DIR=$(python3 -c "import json; print(json.load(open('$RUN_DIR/skill_input.json'))['target']['plugin_dir'])")
REGISTER_FILE=$(python3 -c "import json; print(json.load(open('$RUN_DIR/skill_input.json'))['target']['register_file'])")
PACKAGE=$(python3 -c "import json; print(json.load(open('$RUN_DIR/skill_input.json'))['target']['package'])")
CONFIG_KIND=$(python3 -c "import json; print(json.load(open('$RUN_DIR/skill_input.json'))['config_kind'])")
CONTEXT_NOTES=$(python3 -c "import json; print(json.load(open('$RUN_DIR/skill_input.json')).get('context_notes', ''))")
```

Verify source files exist:

```bash
test -f "$ALGO_SOURCE" || { echo "HALT: algorithm source not found: $ALGO_SOURCE"; exit 1; }
test -f "$ALGO_CONFIG" || { echo "HALT: algorithm config not found: $ALGO_CONFIG"; exit 1; }
test -d "$TARGET_REPO" || { echo "HALT: target repo not found: $TARGET_REPO"; exit 1; }
```

## Resumability Check

Load `.state.json` to detect completed phases:

```bash
python3 -c "
import json, os, sys
from pathlib import Path
state_path = Path('$RUN_DIR') / '.state.json'
if not state_path.exists():
    print('State: fresh run')
    sys.exit(0)
state = json.loads(state_path.read_text())
phases = state.get('phases', {})
ctx = phases.get('context', {})
tr = phases.get('translate', {})
print(f'context: {ctx.get(\"status\", \"pending\")}' + (f' (hash={ctx[\"hash\"]})' if ctx.get('hash') else ''))
print(f'translate: {tr.get(\"status\", \"pending\")}')
if tr.get('status') == 'done':
    print(f'  files={tr.get(\"files\", [])}')
    print(f'  review_rounds={tr.get(\"review_rounds\", 0)} consensus={tr.get(\"consensus\", \"?\")}')
"
```

**If `translate` phase is `done` and `translation_output.json` exists:**
- Read `translation_output.json` and verify all `files_created` + `files_modified` exist
  in `$RUN_DIR/generated/`
- If complete: print the summary (see Step 6) and HALT with:
  `Translation already complete. Re-run prepare.py to continue.`
- If `generated/` is missing files: jump directly to Step 6 to re-copy

**If `context` phase is `done` AND `$CONTEXT_PATH` exists AND `REBUILD_CONTEXT=false`:**
- Skip Step 1 entirely

**If snapshots or review rounds exist but `translate` is not `done`:**
- Resume the loop: check which snapshot versions exist and which review rounds exist,
  then start from the appropriate point

## Step 1: Context Check

Use TaskCreate: `"Step 1: Context Check"` → TaskUpdate in_progress

Check if the context file is already assembled:

```bash
if [ "$REBUILD_CONTEXT" = "false" ] && [ -f "$CONTEXT_PATH" ]; then
    echo "Context cache hit: $CONTEXT_PATH"
    # TaskUpdate Step 1 → completed
else
    echo "Context cache miss — assembling context document via subagent..."
    # Read manifest to get context.files list
    python3 -c "
import json, yaml
from pathlib import Path
manifest = yaml.safe_load(Path('$MANIFEST_PATH').read_text())
ctx_files = manifest.get('context', {}).get('files', [])
print('Context files to assemble:')
for f in ctx_files:
    print(f'  {f}')
print(f'Output path: $CONTEXT_PATH')
"
    # Spawn subagent — see below
fi
```

**On cache miss: spawn context-assembly subagent**

Use the Agent tool to spawn a subagent with a concrete prompt. Build the prompt
using the actual values loaded above (no placeholders). The subagent gets a fresh
context window and must NOT receive any part of the current session's history.

Construct the subagent prompt using the concrete values from the manifest:

```
You are assembling a translation context document for the sim2real pipeline.

Read the following files and write a single context.md document to:
  <CONTEXT_PATH>

1. Mapping document: docs/transfer/blis_to_llmd_mapping.md
   (or any additional context.files entries from the manifest)

2. Production interfaces for the "<SCENARIO>" scenario from:
   <TARGET_REPO>/<PLUGIN_DIR>
   - For routing: read the Scorer interface, EndpointPickerConfig, SchedulingContext
   - For admission_control: read the Admission interface, AdmissionPolicyConfig
   Read these types from the actual Go files in that directory.

3. One or two existing production plugin examples from the same directory.
   Choose the most representative (e.g., load_aware.go for routing,
   always_admit.go for admission_control). Read the complete file.

4. Plugin registration pattern from: <TARGET_REPO>/<REGISTER_FILE>
   Read the complete file.

5. Any additional files listed in the manifest's context.files beyond the
   mapping document.

Format the output as:

# Translation Context
Scenario: <SCENARIO> | inference-sim@<sha> | llm-d@<sha>

## Signal Mapping
[full contents of mapping document]

## Production Interfaces
[Scorer/Admission interface definition, EndpointPickerConfig, SchedulingContext
 — extracted from the actual source files, not paraphrased]

## Example Plugin: <filename>
[full contents of example plugin]

## Plugin Registration
[full contents of register.go]

## <additional file name>
[full contents if any additional files]

CRITICAL: Do NOT include context notes — those are held in the main session.
Write the file to <CONTEXT_PATH> and report the path when done.
```

After the subagent writes the file, update `.state.json`:

```bash
python3 -c "
import sys, re
sys.path.insert(0, '.')
from pipeline.lib.state_machine import StateMachine
from pathlib import Path
m = re.search(r'([a-f0-9]{12})\.md$', '$CONTEXT_PATH')
hash_val = m.group(1) if m else 'unknown'
state = StateMachine.load('$RUN_DIR')
state.mark_done('context', hash=hash_val)
print(f'State updated: context done (hash={hash_val})')
"
```

TaskUpdate Step 1 → completed

## Step 2: Translate

Use TaskCreate: `"Step 2: Translate Algorithm"` → TaskUpdate in_progress

Read these files (use the Read tool):

1. `$CONTEXT_PATH` — cached context document (signal mapping, interfaces, examples)
2. `$ALGO_SOURCE` — simulated algorithm source (Go)
3. `$ALGO_CONFIG` — algorithm policy config (YAML with weights/thresholds)
4. `prompts/prepare/translate.md` — writer guidance
5. `$TARGET_REPO/$REGISTER_FILE` — current plugin registrations (to find the pattern)
6. `config/env_defaults.yaml` — look at `scenarios.<SCENARIO>.gaie.baseline.helmValues`
   for treatment config structural reference

Also hold in mind: `$CONTEXT_NOTES` (from skill_input.json, NOT written to context.md)

Based on your reading, write the production plugin:

**1. Create the plugin Go file** in `$TARGET_REPO/$PLUGIN_DIR`
   - Implement the correct interface based on `$SCENARIO`:
     - routing → `Scorer` interface
     - admission_control → `Admission` interface
   - Map ALL signals from the context document using the production equivalents
   - Preserve ALL weights and thresholds from `$ALGO_CONFIG` exactly
   - Follow patterns from the example plugins in the context document
   - Use `$PACKAGE` as the Go package name

**2. Create a test file** in the same directory (`<plugin_name>_test.go`)
   - At minimum: compile-time interface assertion + basic smoke test

**3. Register the plugin** in `$TARGET_REPO/$REGISTER_FILE`
   - Choose a kebab-case plugin type name (e.g., `adaptive-v2-scorer`)
   - Add factory registration matching the existing pattern in that file

**4. Write `$RUN_DIR/treatment_config.yaml`**
   - Must have `kind: $CONFIG_KIND`
   - Reference the registered plugin type name
   - For routing: include scheduling profile with the treatment plugin entry
   - If algorithm requires non-standard wiring: set `needs_custom_config: true` (next step)

**5. Write `$RUN_DIR/translation_output.json`** with exactly these 9 fields:

```json
{
  "plugin_type": "<kebab-case name matching Go registration>",
  "files_created": ["<paths relative to target repo root>"],
  "files_modified": ["<paths relative to target repo root>"],
  "package": "<Go package name>",
  "test_commands": [
    ["go", "build", "./..."],
    ["go", "vet", "./..."],
    ["go", "test", "-timeout", "10m", "./pkg/plugins/<package>/...", "-v"]
  ],
  "config_kind": "<value of $CONFIG_KIND>",
  "helm_path": "gaie.treatment.helmValues.inferenceExtension.pluginsCustomConfig.custom-plugins.yaml",
  "needs_custom_config": false,
  "suggested_config": null
}
```

Field notes:
- `test_commands` — determine the right commands for what you generated. Seed from
  `build_commands` in skill_input.json but scope the test path to exactly your package.
  Do not run the full repo test suite.
- `needs_custom_config: true` when treatment config uses non-standard wiring (multi-profile,
  non-standard plugin composition). Set `suggested_config` to your config structure in that case.
- `files_modified` typically includes `$REGISTER_FILE`

Track all files you created/modified — you need this for Steps 3–6.

## Step 3: Build/Test Gate

Use TaskCreate: `"Step 3: Build/Test"` → TaskUpdate in_progress

Read `test_commands` from `$RUN_DIR/translation_output.json`:

```bash
python3 -c "
import json
o = json.load(open('$RUN_DIR/translation_output.json'))
print(f'Running {len(o[\"test_commands\"])} test commands:')
for i, cmd in enumerate(o['test_commands']):
    print(f'  {i+1}: {\" \".join(cmd)}')
"
```

Run each command sequentially with CWD = target repo root:

```bash
# For each cmd in test_commands:
(cd $TARGET_REPO && GOWORK=off <cmd>)
```

**On failure:** Read the error, diagnose (missing import? interface method mismatch?
test assertion failure?), fix the Go code or test file, then retry from command 1.
Max 6 retry attempts. After 6: HALT and report to operator with the exact error.

**On success:** proceed to Step 4.

## Step 4: Snapshot

After EVERY successful build/test pass (including the first pass before any review):

```bash
SNAP_NUM=$(python3 -c "
from pathlib import Path
snaps = [d for d in (Path('$RUN_DIR') / 'snapshots').glob('v*') if d.is_dir()]
print(len(snaps) + 1)
" 2>/dev/null || echo 1)
SNAP_DIR="$RUN_DIR/snapshots/v${SNAP_NUM}"
mkdir -p "$SNAP_DIR"
```

Copy all files from `files_created` + `files_modified` plus `treatment_config.yaml`:

```bash
python3 -c "
import json, shutil
from pathlib import Path
o = json.load(open('$RUN_DIR/translation_output.json'))
snap = Path('$SNAP_DIR')
target = Path('$TARGET_REPO')
for f in o['files_created'] + o.get('files_modified', []):
    src = target / f
    dst = snap / Path(f).name
    shutil.copy2(src, dst)
    print(f'  {Path(f).name} → snapshots/v$SNAP_NUM/')
shutil.copy2('$RUN_DIR/treatment_config.yaml', snap / 'treatment_config.yaml')
print(f'Snapshot v$SNAP_NUM saved')
"
```

## Step 5: Review

Use TaskCreate: `"Step 5: Review (Round N)"` → TaskUpdate in_progress

Collect plugin files:

```bash
PLUGIN_FILES=$(python3 -c "
import json
from pathlib import Path
o = json.load(open('$RUN_DIR/translation_output.json'))
files = [str(Path('$TARGET_REPO') / f) for f in o['files_created']]
print(' '.join(files))
")
```

Run the reviewer:

```bash
python3 .claude/skills/sim2real-translate/scripts/review.py \
    --plugin-files $PLUGIN_FILES \
    --algorithm "$ALGO_SOURCE" \
    --algorithm-config "$ALGO_CONFIG" \
    --context "$CONTEXT_PATH" \
    --treatment-config "$RUN_DIR/treatment_config.yaml" \
    --round $ROUND_NUM \
    --out "$RUN_DIR/review/round_${ROUND_NUM}.json" \
    $([ "$DEV_MODE" = "true" ] && echo "--dev")
```

Read `$RUN_DIR/review/round_${ROUND_NUM}.json` to see issues.

**Exit 0 (consensus):** proceed to Step 6.

**Exit 1 (no consensus):** Read the specific issues. Fix the Go code and/or
`$RUN_DIR/treatment_config.yaml` in response. Then: → Step 3 → Step 4 → Step 5
(increment ROUND_NUM). Repeat up to `REVIEW_ROUNDS` rounds.

**Exit 2 (all errors):** Prompt operator:
```
All models errored in round N. Options:
  [r] Retry this round
  [a] Accept anyway
  [q] Quit
```

**Rounds exhausted without consensus:** Prompt:
```
N rounds completed without consensus. Options:
  [c] Continue for more rounds (enter new N)
  [a] Accept anyway
  [q] Quit
```

## Loop Instruction

If NEEDS_CHANGES after any review round: return to Step 2, address the issues
in the plugin code and/or treatment_config.yaml, then Step 3, then Step 4, then
Step 5 (next round). The loop never exits silently — the operator makes the call.

## Step 6: Output

Use TaskCreate: `"Step 6: Output Artifacts"` → TaskUpdate in_progress

Determine final review outcome from the most recent round file:

```bash
FINAL_CONSENSUS=$(python3 -c "
import json
from pathlib import Path
rounds = sorted((Path('$RUN_DIR/review')).glob('round_*.json'))
if not rounds:
    print('none')
else:
    last = json.loads(rounds[-1].read_text())
    if last.get('consensus'):
        print(f'{last[\"approve_count\"]}/{last[\"total_successful\"]}')
    else:
        print('accepted_without_consensus')
" 2>/dev/null || echo "none")

REVIEW_ROUNDS_DONE=$(python3 -c "
from pathlib import Path
print(len(list((Path('$RUN_DIR/review')).glob('round_*.json'))))
" 2>/dev/null || echo 0)
```

Copy all created/modified files + treatment_config.yaml into `$RUN_DIR/generated/`:

```bash
python3 -c "
import json, shutil
from pathlib import Path
o = json.load(open('$RUN_DIR/translation_output.json'))
gen = Path('$RUN_DIR/generated')
gen.mkdir(parents=True, exist_ok=True)
target = Path('$TARGET_REPO')
for f in o['files_created'] + o.get('files_modified', []):
    src = target / f
    dst = gen / Path(f).name
    shutil.copy2(src, dst)
    print(f'  {Path(f).name} → generated/')
shutil.copy2('$RUN_DIR/treatment_config.yaml', gen / 'treatment_config.yaml')
print('Generated artifacts ready.')
"
```

Update `.state.json`:

```bash
python3 -c "
import json, sys
sys.path.insert(0, '.')
from pipeline.lib.state_machine import StateMachine
o = json.load(open('$RUN_DIR/translation_output.json'))
state = StateMachine.load('$RUN_DIR')
state.mark_done('translate',
    files=o['files_created'],
    review_rounds=$REVIEW_ROUNDS_DONE,
    consensus='$FINAL_CONSENSUS')
print('State updated: translate done')
"
```

Verify outputs:

```bash
python3 -c "
import json, sys
from pathlib import Path
o = json.load(open('$RUN_DIR/translation_output.json'))
required = ['plugin_type', 'files_created', 'files_modified', 'package',
            'test_commands', 'config_kind', 'helm_path', 'needs_custom_config',
            'suggested_config']
missing = [f for f in required if f not in o]
if missing:
    print(f'ERROR: translation_output.json missing: {missing}')
    sys.exit(1)
gen = Path('$RUN_DIR/generated')
for f in o['files_created'] + o.get('files_modified', []):
    dst = gen / Path(f).name
    if not dst.exists():
        print(f'ERROR: generated/ missing: {Path(f).name}')
        sys.exit(1)
print(f'Verified: plugin_type={o[\"plugin_type\"]}, '
      f'{len(o[\"files_created\"])} created, '
      f'{len(o.get(\"files_modified\", []))} modified')
" || exit 1
```

TaskUpdate all open tasks → completed.

Print completion summary:

```
━━━ /sim2real-translate complete ━━━

Plugin:   <plugin_type>
Package:  <package>
Files:    <files_created> (created)
          <files_modified> (modified)
Review:   <consensus> after <N> rounds
Snapshots: <N> versions in $RUN_DIR/snapshots/

Output artifacts:
  $RUN_DIR/translation_output.json
  $RUN_DIR/treatment_config.yaml
  $RUN_DIR/generated/
  $RUN_DIR/snapshots/
  $RUN_DIR/review/

Next: re-run prepare.py to continue through Assembly, Summary, and Gate.
  python pipeline/prepare.py
```

## What This Skill Does NOT Do

- Does not assemble values.yaml or cluster YAMLs (prepare.py Phase 4)
- Does not touch env_defaults.yaml or cluster config
- Does not generate run_summary.md or perform the human gate
- Job: produce working, reviewed Go code + treatment config + metadata in generated/
```

- [ ] **Step 2: Verify YAML frontmatter parses**

```bash
python3 -c "
import re
text = open('.claude/skills/sim2real-translate/SKILL.md').read()
m = re.match(r'^---\n(.*?)\n---', text, re.DOTALL)
if not m:
    print('ERROR: no frontmatter found')
    exit(1)
import yaml
meta = yaml.safe_load(m.group(1))
print(f'name: {meta[\"name\"]}')
print(f'argument-hint: {meta[\"argument-hint\"]}')
print(f'allowed-tools: {len(meta[\"allowed-tools\"])} entries')
assert '--rebuild-context' in meta['argument-hint']
assert 'Agent' in meta['allowed-tools']
assert 'TaskCreate' in meta['allowed-tools']
print('Frontmatter OK')
"
```

Expected: `Frontmatter OK`

- [ ] **Step 3: Verify all referenced scripts exist**

```bash
python3 -c "
from pathlib import Path
files = [
    '.claude/skills/sim2real-translate/scripts/review.py',
    'prompts/prepare/translate.md',
    'prompts/prepare/review.md',
    'pipeline/lib/state_machine.py',
    'pipeline/lib/context_builder.py',
]
for f in files:
    p = Path(f)
    status = 'OK' if p.exists() else 'MISSING'
    print(f'  {status}: {f}')
"
```

Expected: All `OK`

- [ ] **Step 4: Verify review.py is no longer referenced from old allowed-tools**

```bash
grep "review_translation\|build_review_request\|dashboard" .claude/skills/sim2real-translate/SKILL.md
```

Expected: no output

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/sim2real-translate/SKILL.md
git commit -m "feat(sim2real-translate): rewrite SKILL.md with state machine, context subagent, 9-field schema"
```

---

## Chunk 3: Cleanup

### Task 4: Delete old scripts

**Files:**
- Delete: `.claude/skills/sim2real-translate/scripts/build_review_request.py`
- Delete: `.claude/skills/sim2real-translate/scripts/review_translation.py`
- Delete: `.claude/skills/sim2real-translate/scripts/dashboard.py`

- [ ] **Step 1: Verify no dangling references before deleting**

```bash
grep -r "build_review_request\|review_translation\|dashboard\.py" \
    .claude/skills/sim2real-translate/ \
    prompts/ \
    pipeline/ \
    tools/ \
    scripts/ \
    2>/dev/null
```

Expected: no output (if any output found, resolve the references first)

- [ ] **Step 2: Delete old scripts**

```bash
rm .claude/skills/sim2real-translate/scripts/build_review_request.py
rm .claude/skills/sim2real-translate/scripts/review_translation.py
rm .claude/skills/sim2real-translate/scripts/dashboard.py
```

- [ ] **Step 3: Verify scripts/ directory is clean**

```bash
ls .claude/skills/sim2real-translate/scripts/
```

Expected: only `review.py`

- [ ] **Step 4: Run full test suite to confirm nothing is broken**

```bash
cd /Users/jchen/go/src/inference-sim/sim2real
python -m pytest .claude/skills/sim2real-translate/tests/ -v
python -m pytest tools/ -v
```

Expected: all tests pass

- [ ] **Step 5: Commit**

```bash
git add -u .claude/skills/sim2real-translate/scripts/
git commit -m "chore(sim2real-translate): remove build_review_request.py, review_translation.py, dashboard.py"
```

---

## Final Verification

- [ ] **Confirm skill structure**

```bash
find .claude/skills/sim2real-translate -type f | sort
```

Expected:
```
.claude/skills/sim2real-translate/SKILL.md
.claude/skills/sim2real-translate/scripts/review.py
.claude/skills/sim2real-translate/tests/__init__.py
.claude/skills/sim2real-translate/tests/test_review.py
```

- [ ] **Confirm translation_output.json schema change is documented**

Verify the new 9-field schema is defined in SKILL.md Step 2:

```bash
python3 -c "
text = open('.claude/skills/sim2real-translate/SKILL.md').read()
fields = ['plugin_type', 'files_created', 'files_modified', 'package',
          'test_commands', 'config_kind', 'helm_path', 'needs_custom_config', 'suggested_config']
missing = [f for f in fields if f not in text]
if missing:
    print(f'ERROR: SKILL.md missing field docs: {missing}')
    exit(1)
print('All 9 translation_output.json fields documented in SKILL.md')
"
```

Expected: `All 9 translation_output.json fields documented in SKILL.md`
