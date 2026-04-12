# sim2real-prepare Script Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the `/sim2real-prepare` Claude skill with a standalone `scripts/prepare.py` that orchestrates Extract → Translate → Generate → Build/Test → Review without requiring Claude as an interactive agent.

**Architecture:** A single orchestrator script (`prepare.py`) with two helper library modules (`llm.py` for LiteLLM HTTP calls, `consensus.py` for the interactive loop). LLM stages call three models in parallel via the same `OPENAI_API_KEY` / `OPENAI_BASE_URL` environment variables used by `review.sh`. The Generate and Review stages each run a consensus loop that pauses after `--reviews N` rounds and prompts the user to continue, add more rounds, accept, or quit.

**Tech Stack:** Python 3.10+ stdlib + `requests` (HTTP) + `PyYAML` (already in requirements.txt). No new runtime dependencies.

---

## LLM Interaction Summary

The script informs the user upfront:

```
━━━ sim2real-prepare ━━━

Stages: Extract → Translate → Generate → Build/Test → Review

LLM interactions (3 total):
  1. Translate  single call    maps BLIS signals to production equivalents
  2. Generate   consensus loop  3 models write scorer plugin, majority vote
  3. Review     consensus loop  3 models verify translation fidelity

--reviews N (default: 2) sets rounds per loop. After N rounds each loop
pauses and you choose: continue, add more rounds, accept, or quit.
```

## File Structure

| File | Role |
|------|------|
| `scripts/prepare.py` | Main orchestrator — arg parsing, stage sequencing, metadata |
| `scripts/lib/__init__.py` | Empty package marker |
| `scripts/lib/llm.py` | LiteLLM HTTP client — `call_model()`, `call_models_parallel()` |
| `scripts/lib/consensus.py` | Consensus loop — `run_consensus_loop()`, user prompting |
| `tests/test_llm.py` | Unit tests for llm.py (mock HTTP) |
| `tests/test_consensus.py` | Unit tests for consensus.py (mock LLM responses) |

`tests/` is a new top-level directory (existing `tools/` tests cover the CLI; `tests/` covers scripts-level modules).

---

## Chunk 1: LLM Client + Consensus Infrastructure

### Task 1: LLM client library

**Files:**
- Create: `scripts/lib/__init__.py`
- Create: `scripts/lib/llm.py`
- Create: `tests/__init__.py`
- Create: `tests/test_llm.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_llm.py
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from unittest.mock import patch, MagicMock
from lib.llm import call_model, call_models_parallel, LLMError

def make_ok_response(content):
    m = MagicMock()
    m.status_code = 200
    m.json.return_value = {"choices": [{"message": {"content": content}}]}
    return m

def make_err_response(status, body):
    m = MagicMock()
    m.status_code = status
    m.json.return_value = body
    return m

def test_call_model_returns_content():
    with patch("lib.llm.requests.post", return_value=make_ok_response("hello")) as mock:
        result = call_model("Azure/gpt-4o", [{"role": "user", "content": "hi"}])
        assert result == "hello"
        assert mock.called

def test_call_model_raises_on_http_error():
    with patch("lib.llm.requests.post", return_value=make_err_response(401, {"error": "unauthorized"})):
        try:
            call_model("Azure/gpt-4o", [{"role": "user", "content": "hi"}])
            assert False, "should raise"
        except LLMError as e:
            assert "401" in str(e)

def test_call_models_parallel_returns_all():
    with patch("lib.llm.requests.post", return_value=make_ok_response("response")):
        results = call_models_parallel(
            ["Azure/gpt-4o", "GCP/gemini-2.5-flash", "aws/claude-opus-4-6"],
            [{"role": "user", "content": "hi"}]
        )
        assert len(results) == 3
        assert all(r == "response" for r in results.values())

def test_call_models_parallel_captures_partial_failure():
    responses = {
        "Azure/gpt-4o": make_ok_response("ok"),
        "GCP/gemini-2.5-flash": make_err_response(500, {"error": "internal"}),
        "aws/claude-opus-4-6": make_ok_response("ok2"),
    }
    def side_effect(*args, **kwargs):
        model = kwargs.get("json", {}).get("model", "")
        return responses.get(model, make_ok_response("fallback"))
    with patch("lib.llm.requests.post", side_effect=side_effect):
        results = call_models_parallel(
            ["Azure/gpt-4o", "GCP/gemini-2.5-flash", "aws/claude-opus-4-6"],
            [{"role": "user", "content": "hi"}]
        )
        assert results["Azure/gpt-4o"] == "ok"
        assert isinstance(results["GCP/gemini-2.5-flash"], LLMError)
        assert results["aws/claude-opus-4-6"] == "ok2"
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
cd /path/to/sim2real
python -m pytest tests/test_llm.py -v
```
Expected: `ModuleNotFoundError: No module named 'lib'`

- [ ] **Step 3: Implement llm.py**

```python
# scripts/lib/llm.py
"""LiteLLM-compatible HTTP client. Uses OPENAI_API_KEY + OPENAI_BASE_URL."""
import os
import concurrent.futures
import requests  # already in .venv via pip

MODELS = [
    "Azure/gpt-4o",
    "GCP/gemini-2.5-flash",
    "aws/claude-opus-4-6",
]

class LLMError(Exception):
    pass

def _get_endpoint() -> tuple[str, str]:
    """Returns (api_key, base_url). Raises LLMError if not configured."""
    api_key = os.environ.get("OPENAI_API_KEY", "")
    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com")
    if not api_key:
        # Fallback to ANTHROPIC_AUTH_TOKEN (same as review.sh)
        api_key = os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
        base_url = os.environ.get("ANTHROPIC_BASE_URL", base_url)
    if not api_key:
        raise LLMError(
            "No API key found. Set OPENAI_API_KEY + OPENAI_BASE_URL, "
            "or ANTHROPIC_AUTH_TOKEN + ANTHROPIC_BASE_URL."
        )
    return api_key, base_url.rstrip("/")

def call_model(model: str, messages: list[dict],
               timeout: int = 300) -> str:
    """Call a single model. Returns response text. Raises LLMError on failure."""
    api_key, base_url = _get_endpoint()
    url = f"{base_url}/v1/chat/completions"
    payload = {"model": model, "messages": messages}
    try:
        resp = requests.post(
            url, json=payload,
            headers={"Authorization": f"Bearer {api_key}",
                     "Content-Type": "application/json"},
            timeout=timeout,
        )
    except requests.Timeout:
        raise LLMError(f"{model}: request timed out after {timeout}s")
    except requests.ConnectionError as e:
        raise LLMError(f"{model}: connection error — {e}")
    if resp.status_code != 200:
        raise LLMError(f"{model}: HTTP {resp.status_code} — {resp.text[:200]}")
    try:
        return resp.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as e:
        raise LLMError(f"{model}: unexpected response shape — {e}: {resp.text[:200]}")

def call_models_parallel(models: list[str], messages: list[dict],
                         timeout: int = 300) -> dict[str, str | LLMError]:
    """Call multiple models in parallel. Returns {model: response_text | LLMError}."""
    results: dict[str, str | LLMError] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(models)) as ex:
        futures = {ex.submit(call_model, m, messages, timeout): m for m in models}
        for future in concurrent.futures.as_completed(futures):
            model = futures[future]
            try:
                results[model] = future.result()
            except LLMError as e:
                results[model] = e
    return results
```

- [ ] **Step 4: Run tests — confirm pass**

```bash
python -m pytest tests/test_llm.py -v
```
Expected: 4 tests pass.

- [ ] **Step 5: Commit**

```bash
git add scripts/lib/__init__.py scripts/lib/llm.py tests/__init__.py tests/test_llm.py
git commit -m "feat: add LLM client library for prepare.py"
```

---

### Task 2: Consensus loop

**Files:**
- Create: `scripts/lib/consensus.py`
- Create: `tests/test_consensus.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_consensus.py
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from unittest.mock import patch
from lib.consensus import run_consensus_loop, ConsensusResult

# A check_fn that returns True (consensus) when all responses are "ok"
def check_ok(responses): return all(v == "ok" for v in responses.values())
def check_never(responses): return False

def summarize(round_num, responses, consensus):
    return f"Round {round_num}: consensus={consensus}"

def test_consensus_reached_within_rounds():
    """Consensus on round 1 → loop exits, no user prompt needed."""
    call_count = [0]
    def fake_call(messages):
        call_count[0] += 1
        return {"Azure/gpt-4o": "ok", "GCP/gemini-2.5-flash": "ok", "aws/claude-opus-4-6": "ok"}

    result = run_consensus_loop(
        messages=[],
        call_fn=fake_call,
        check_fn=check_ok,
        summarize_fn=summarize,
        max_rounds=2,
        accept_on_consensus=True,
    )
    assert result.consensus is True
    assert result.rounds_run == 1
    assert call_count[0] == 1

def test_no_consensus_prompts_user_after_max_rounds():
    """No consensus after max_rounds → user is prompted."""
    fake_responses = {"Azure/gpt-4o": "a", "GCP/gemini-2.5-flash": "b", "aws/claude-opus-4-6": "c"}
    def fake_call(messages): return fake_responses

    with patch("builtins.input", return_value="a"):  # user accepts
        result = run_consensus_loop(
            messages=[],
            call_fn=fake_call,
            check_fn=check_never,
            summarize_fn=summarize,
            max_rounds=2,
            accept_on_consensus=False,
        )
    assert result.rounds_run == 2
    assert result.accepted_by_user is True

def test_user_quit_raises():
    """User types 'q' → StopIteration raised."""
    def fake_call(messages): return {"Azure/gpt-4o": "x"}
    with patch("builtins.input", return_value="q"):
        try:
            run_consensus_loop(
                messages=[],
                call_fn=fake_call,
                check_fn=check_never,
                summarize_fn=summarize,
                max_rounds=1,
            )
            assert False, "should raise"
        except SystemExit:
            pass  # quit maps to sys.exit

def test_user_plus_n_adds_rounds():
    """User types '+2' → runs 2 more rounds, then prompts again."""
    call_count = [0]
    def fake_call(messages):
        call_count[0] += 1
        return {"m": "x"}
    # First prompt: +1; second prompt: accept
    inputs = iter(["+1", "a"])
    with patch("builtins.input", side_effect=lambda _: next(inputs)):
        result = run_consensus_loop(
            messages=[],
            call_fn=fake_call,
            check_fn=check_never,
            summarize_fn=summarize,
            max_rounds=1,
        )
    assert result.rounds_run == 2  # 1 initial + 1 from +1
    assert result.accepted_by_user is True
```

- [ ] **Step 2: Run tests — confirm fail**

```bash
python -m pytest tests/test_consensus.py -v
```
Expected: `ModuleNotFoundError`

- [ ] **Step 3: Implement consensus.py**

```python
# scripts/lib/consensus.py
"""Consensus loop with interactive user prompting."""
import sys
from dataclasses import dataclass, field
from typing import Callable

@dataclass
class ConsensusResult:
    consensus: bool
    rounds_run: int
    responses: dict          # last round's {model: response_text | LLMError}
    accepted_by_user: bool = False

def run_consensus_loop(
    messages: list[dict],
    call_fn: Callable[[list[dict]], dict],   # returns {model: response}
    check_fn: Callable[[dict], bool],         # returns True if consensus
    summarize_fn: Callable[[int, dict, bool], str],  # returns display string
    max_rounds: int = 2,
    accept_on_consensus: bool = True,         # auto-accept when consensus reached
) -> ConsensusResult:
    """
    Run LLM calls in rounds until consensus or user accepts.

    Returns ConsensusResult. Raises SystemExit if user quits.
    """
    rounds_run = 0
    responses: dict = {}
    remaining = max_rounds

    while True:
        rounds_run += 1
        remaining -= 1
        responses = call_fn(messages)
        consensus = check_fn(responses)
        print(summarize_fn(rounds_run, responses, consensus))

        if consensus and accept_on_consensus:
            return ConsensusResult(consensus=True, rounds_run=rounds_run,
                                   responses=responses)

        if remaining > 0:
            # More rounds to run before prompting
            continue

        # Prompt user
        if consensus:
            prompt = f"\n  Consensus reached. [a]ccept / [c]ontinue / [q]uit: "
        else:
            prompt = f"\n  No consensus. [c]ontinue 1 more / [+N] N more / [a]ccept-anyway / [q]uit: "

        while True:
            choice = input(prompt).strip().lower()
            if choice == "a":
                return ConsensusResult(consensus=consensus, rounds_run=rounds_run,
                                       responses=responses, accepted_by_user=True)
            elif choice == "q":
                print("Aborted.")
                sys.exit(0)
            elif choice == "c":
                remaining = 1
                break
            elif choice.startswith("+"):
                try:
                    n = int(choice[1:])
                    if n < 1:
                        raise ValueError
                    remaining = n
                    break
                except ValueError:
                    print(f"  Invalid input '{choice}'. Enter 'c', '+N' (e.g. +3), 'a', or 'q'.")
            else:
                print(f"  Invalid input '{choice}'. Enter 'c', '+N', 'a', or 'q'.")
```

- [ ] **Step 4: Run tests — confirm pass**

```bash
python -m pytest tests/test_consensus.py -v
```
Expected: 4 tests pass.

- [ ] **Step 5: Run all lib tests**

```bash
python -m pytest tests/ -v
```
Expected: 8 tests pass.

- [ ] **Step 6: Commit**

```bash
git add scripts/lib/consensus.py tests/test_consensus.py
git commit -m "feat: add consensus loop library for prepare.py"
```

---

## Chunk 2: Main Script — Prerequisites, Extract, Translate

### Task 3: Script skeleton + prerequisite checks

**Files:**
- Create: `scripts/prepare.py`

- [ ] **Step 1: Create prepare.py with parser + helpers**

Structure mirrors `scripts/setup.py`. Key sections:

```python
#!/usr/bin/env python3
"""sim2real prepare — Extract, Translate, Generate, Build/Test, Review."""

import argparse, json, os, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MODELS = ["Azure/gpt-4o", "GCP/gemini-2.5-flash", "aws/claude-opus-4-6"]

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="prepare.py",
        description="sim2real prepare: Extract → Translate → Generate → Build/Test → Review",
        epilog="""
Environment variables:
  OPENAI_API_KEY, OPENAI_BASE_URL   (or ANTHROPIC_AUTH_TOKEN, ANTHROPIC_BASE_URL)

Examples:
  python scripts/prepare.py
  python scripts/prepare.py --reviews 3
""",
    )
    p.add_argument("--reviews", type=int, default=2, metavar="N",
                   help="Number of consensus loop rounds per LLM loop [default: 2]")
    return p

# Color helpers (same pattern as setup.py)
_tty = sys.stdout.isatty()
def _c(code, text): return f"\033[{code}m{text}\033[0m" if _tty else text
def info(msg): print(_c("34", "[INFO]  ") + msg)
def ok(msg):   print(_c("32", "[OK]    ") + msg)
def warn(msg): print(_c("33", "[WARN]  ") + msg)
def err(msg):  print(_c("31", "[ERROR] ") + msg, file=sys.stderr)
def step(n, title): print("\n" + _c("36", f"━━━ Step {n}: {title} ━━━"))

def run(cmd, *, check=True, capture=False, input=None, cwd=None):
    return subprocess.run(cmd, check=check, text=True,
                          capture_output=capture, input=input, cwd=cwd)
```

- [ ] **Step 2: Implement print_intro(reviews)**

```python
def print_intro(reviews: int) -> None:
    print(_c("36", "\n━━━ sim2real-prepare ━━━\n"))
    print("Stages: Extract → Translate → Generate → Build/Test → Review\n")
    print("LLM interactions (3 total):")
    print(f"  1. Translate  single call    maps BLIS signals to production equivalents")
    print(f"  2. Generate   consensus loop  3 models write scorer plugin, majority vote")
    print(f"  3. Review     consensus loop  3 models verify translation fidelity")
    print(f"\n--reviews {reviews}: each loop runs {reviews} round(s), then pauses for your input.")
    print("After each loop you choose: [c]ontinue / [+N] N more rounds / [a]ccept / [q]uit\n")
```

- [ ] **Step 3: Implement check_prerequisites()**

Checks required artifacts + submodules. Exits on missing items.

```python
def check_prerequisites() -> None:
    step(0, "Checking prerequisites")
    required_files = [
        REPO_ROOT / "blis_router/best/best_program.go",
        REPO_ROOT / "blis_router/best/best_program_info.json",
        REPO_ROOT / "docs/transfer/blis_to_llmd_mapping.md",
        REPO_ROOT / "docs/transfer/scorer_template.go.md",
        REPO_ROOT / "workspace/setup_config.json",
    ]
    required_dirs = [
        REPO_ROOT / "inference-sim/sim",
        REPO_ROOT / "llm-d-inference-scheduler/pkg",
    ]
    missing = [str(p) for p in required_files if not p.exists()]
    missing += [str(p) for p in required_dirs if not p.is_dir()]
    if missing:
        err("Missing prerequisites:")
        for m in missing: print(f"  • {m}")
        err("Run /sim2real-setup first, and ensure submodules are initialized.")
        sys.exit(1)
    ok("Prerequisites satisfied")
```

- [ ] **Step 4: Implement load_setup_config()**

```python
def load_setup_config() -> dict:
    cfg_path = REPO_ROOT / "workspace/setup_config.json"
    cfg = json.loads(cfg_path.read_text())
    run_name = cfg["run_name"]
    run_dir = REPO_ROOT / "workspace/runs" / run_name
    run_dir.mkdir(parents=True, exist_ok=True)
    ok(f"Run: {run_name}  ({run_dir})")
    return cfg
```

- [ ] **Step 5: Implement update_run_metadata() helper**

```python
def update_run_metadata(run_dir: Path, stage: str, **fields) -> None:
    meta_path = run_dir / "run_metadata.json"
    if not meta_path.exists(): return
    meta = json.loads(meta_path.read_text())
    meta["stages"].setdefault(stage, {}).update(fields)
    meta_path.write_text(json.dumps(meta, indent=2))
```

- [ ] **Step 6: Add main() skeleton and wire up**

```python
def main() -> int:
    args = build_parser().parse_args()
    print_intro(args.reviews)
    check_prerequisites()
    cfg = load_setup_config()
    run_dir = REPO_ROOT / "workspace/runs" / cfg["run_name"]
    update_run_metadata(run_dir, "prepare", status="in_progress",
                        started_at=datetime.now(timezone.utc).isoformat())
    # stages follow in subsequent tasks
    return 0

if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 7: Smoke test**

```bash
python scripts/prepare.py --help
```
Expected: help text with --reviews flag.

- [ ] **Step 8: Commit**

```bash
git add scripts/prepare.py
git commit -m "feat: add prepare.py skeleton with prerequisites and setup"
```

---

### Task 4: Stage 1 — Extract

**Files:**
- Modify: `scripts/prepare.py` (add `stage_extract()`)

- [ ] **Step 1: Implement stage_extract(run_dir)**

```python
def stage_extract(run_dir: Path) -> Path:
    step(1, "Extract")
    out = run_dir / "prepare_algorithm_summary.json"
    out.unlink(missing_ok=True)  # stale guard

    venv_python = REPO_ROOT / ".venv/bin/python"
    cli = str(REPO_ROOT / "tools/transfer_cli.py")

    result = run([str(venv_python), cli, "extract", "--strict",
                  str(REPO_ROOT / "blis_router/best/")],
                 check=False, capture=True, cwd=REPO_ROOT)
    if result.returncode != 0:
        err(f"extract failed:\n{result.stderr}")
        sys.exit(1)

    # transfer_cli writes to workspace/algorithm_summary.json — copy to run_dir
    src = REPO_ROOT / "workspace/algorithm_summary.json"
    if not src.exists():
        err("extract succeeded but workspace/algorithm_summary.json not found")
        sys.exit(1)
    import shutil
    shutil.copy(src, out)

    # Validate
    run([str(venv_python), cli, "validate-schema", str(out)], cwd=REPO_ROOT)
    scope_ok = run([str(venv_python), "-c",
                    f"import json,sys; d=json.load(open('{out}')); "
                    "sys.exit(0 if d.get('scope_validation_passed') is True else 1)"],
                   check=False).returncode == 0
    if not scope_ok:
        err("scope_validation_passed is not true — HALT"); sys.exit(1)

    ok(f"Extract complete → {out}")
    return out
```

- [ ] **Step 2: Wire into main()**

```python
algo_summary_path = stage_extract(run_dir)
```

- [ ] **Step 3: Smoke test (--no-cluster skips kubectl, extract still runs)**

Run from sim2real root with valid blis_router artifacts present:
```bash
python scripts/prepare.py  # ctrl+C after extract step prints OK
```
Expected: `[OK]    Extract complete → workspace/runs/.../prepare_algorithm_summary.json`

- [ ] **Step 4: Commit**

```bash
git add scripts/prepare.py
git commit -m "feat: add stage 1 (extract) to prepare.py"
```

---

### Task 5: Stage 2 — Translate (single LLM call)

**Files:**
- Modify: `scripts/prepare.py` (add `stage_translate()`)

This is a single LLM call (not a loop) to one model. The model reads the mapping doc and algorithm summary, then produces a `signal_coverage.json`. We call the same model used for generation (gpt-4o) since this is a structured extraction task.

- [ ] **Step 1: Implement build_translate_prompt(algo_summary, mapping_doc)**

```python
def build_translate_prompt(algo_summary: dict, mapping_doc: str,
                            submodule_head: str) -> list[dict]:
    system = (
        "You are a precise technical translator. Your task is to map simulation "
        "signals from an algorithm summary to their production equivalents, "
        "following the provided mapping document exactly. "
        "Respond ONLY with valid JSON matching the signal_coverage schema. "
        "No prose before or after the JSON."
    )
    user = f"""Map the signals in this algorithm summary to their production equivalents.

## Algorithm Summary
```json
{json.dumps(algo_summary, indent=2)}
```

## Mapping Document
{mapping_doc}

## Instructions
Follow prompts/translate.md logic:
- For each signal in algorithm_summary signals[], look it up in the mapping document.
- Record: sim_name, prod_name, prod_access_path, fidelity_rating, staleness_window_ms=0, mapped=true
- Propagate normalization and fidelity_provisional fields from the algorithm summary.
- Check F-10 double-counting if composite_signals is non-empty.
- Set commit_hash to: {submodule_head}
- Set coverage_complete to true if unmapped_signals is empty.

Respond with ONLY the JSON object for signal_coverage.json (no markdown fences, no prose).
"""
    return [{"role": "system", "content": system},
            {"role": "user", "content": user}]
```

- [ ] **Step 2: Implement stage_translate(run_dir, algo_summary_path)**

```python
def stage_translate(run_dir: Path, algo_summary_path: Path) -> Path:
    step(2, "Translate (single LLM call — gpt-4o)")
    out = run_dir / "prepare_signal_coverage.json"
    out.unlink(missing_ok=True)

    # Submodule staleness check (deterministic)
    submodule_head = run(
        ["git", "-C", str(REPO_ROOT / "llm-d-inference-scheduler"), "rev-parse", "HEAD"],
        capture=True).stdout.strip()
    mapping_hash = _extract_mapping_hash()
    if not submodule_head.startswith(mapping_hash):
        err(f"Stale submodule: mapping pinned at {mapping_hash}, "
            f"submodule at {submodule_head[:7]}")
        sys.exit(1)

    algo_summary = json.loads(algo_summary_path.read_text())
    mapping_doc = (REPO_ROOT / "docs/transfer/blis_to_llmd_mapping.md").read_text()

    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from lib.llm import call_model, LLMError

    info("Calling gpt-4o for signal translation...")
    messages = build_translate_prompt(algo_summary, mapping_doc, submodule_head)
    try:
        response = call_model("Azure/gpt-4o", messages)
    except LLMError as e:
        err(f"LLM call failed: {e}"); sys.exit(1)

    # Parse and validate
    try:
        coverage = json.loads(response)
    except json.JSONDecodeError as e:
        err(f"Model returned invalid JSON: {e}\nResponse:\n{response[:500]}")
        sys.exit(1)

    out.write_text(json.dumps(coverage, indent=2))
    venv_python = str(REPO_ROOT / ".venv/bin/python")
    cli = str(REPO_ROOT / "tools/transfer_cli.py")
    run([venv_python, cli, "validate-schema", str(out)], cwd=REPO_ROOT)

    if not coverage.get("coverage_complete") or coverage.get("unmapped_signals"):
        err(f"coverage_complete is not true or unmapped_signals is non-empty")
        sys.exit(1)

    # Also write to workspace/ for downstream CLI compatibility
    (REPO_ROOT / "workspace/signal_coverage.json").write_text(json.dumps(coverage, indent=2))

    ok(f"Translate complete → {out}")
    return out

def _extract_mapping_hash() -> str:
    import re
    text = (REPO_ROOT / "docs/transfer/blis_to_llmd_mapping.md").read_text()
    m = re.search(r'Pinned commit hash:\s*([0-9a-f]{7,40})', text)
    if not m:
        err("Could not extract pinned commit hash from mapping artifact"); sys.exit(1)
    return m.group(1)[:7]
```

- [ ] **Step 3: Wire into main()**

```python
signal_coverage_path = stage_translate(run_dir, algo_summary_path)
```

- [ ] **Step 4: Commit**

```bash
git add scripts/prepare.py
git commit -m "feat: add stage 2 (translate) to prepare.py"
```

---

## Chunk 3: Stage 3 — Generate Consensus Loop

### Task 6: Generate loop

**Files:**
- Modify: `scripts/prepare.py` (add `stage_generate()` and helpers)

The Generate loop is the most complex stage. Three models each produce scorer Go code. We extract key structural properties from each output, check if 2+/3 agree (majority vote), and run `go build` to validate the winning output.

- [ ] **Step 1: Implement build_generate_prompt()**

```python
def build_generate_prompt(algo_summary: dict, signal_coverage: dict,
                           mapping_doc: str, scorer_template: str,
                           existing_scorers: str) -> list[dict]:
    system = (
        "You are an expert Go engineer implementing a production scorer plugin. "
        "Follow the template and constraints exactly. "
        "Respond with EXACTLY two fenced Go code blocks: "
        "first the scorer implementation, then the test file. "
        "Label them with file paths as comments on the first line:\n"
        "  // FILE: llm-d-inference-scheduler/pkg/plugins/scorer/<name>.go\n"
        "  // FILE: llm-d-inference-scheduler/pkg/plugins/scorer/<name>_test.go\n"
        "No prose before or after the code blocks."
    )
    user = f"""Generate a production scorer plugin implementing the evolved routing algorithm.

## Algorithm Summary
```json
{json.dumps(algo_summary, indent=2)}
```

## Signal Coverage (use these production access paths)
```json
{json.dumps(signal_coverage, indent=2)}
```

## Scorer Template
{scorer_template}

## Mapping Document (reference for composite signal expansion)
{mapping_doc[:3000]}

## Existing Scorers (follow these patterns)
{existing_scorers[:2000]}

## Critical Requirements (from prompts/generate.md)
1. observe.image must use ghcr.io/inference-sim/blis:TAG (full registry path)
2. acceleratorTypes must use labelValues array, not GPU product name as key
3. Each decode container MUST have readinessProbe and startupProbe
4. Do NOT include --port=8000 in container args
5. Register the scorer in llm-d-inference-scheduler/pkg/plugins/register.go
"""
    return [{"role": "system", "content": system},
            {"role": "user", "content": user}]
```

- [ ] **Step 2: Implement extract_scorer_properties(code) and check_generate_consensus()**

```python
import re

def extract_scorer_properties(code: str) -> dict:
    """Extract key structural properties from generated scorer Go code."""
    props = {}
    # Signal access paths used
    props["access_paths"] = sorted(set(
        re.findall(r'endpoint\.GetMetrics\(\)\.\w+', code)))
    # Normalization patterns (÷100 for KV)
    props["has_kv_norm"] = "/ 100" in code or "/100" in code
    # Weights (float literals near signal reads)
    props["float_literals"] = sorted(set(re.findall(r'\b0\.\d+\b', code)))
    return props

def check_generate_consensus(responses: dict) -> tuple[bool, dict | None]:
    """
    Returns (consensus: bool, winning_response: dict | None).
    Consensus = 2+/3 models agree on key properties.
    """
    from lib.llm import LLMError
    parsed = {}
    for model, resp in responses.items():
        if isinstance(resp, LLMError):
            continue
        scorer_code = _extract_code_block(resp, index=0)
        if scorer_code:
            parsed[model] = (resp, extract_scorer_properties(scorer_code))

    if len(parsed) < 2:
        return False, None

    # Find most common property set (majority vote)
    from collections import Counter
    props_list = [json.dumps(p, sort_keys=True) for _, p in parsed.values()]
    most_common, count = Counter(props_list).most_common(1)[0]
    if count >= 2:
        # Pick the first response matching the majority props
        for model, (resp, props) in parsed.items():
            if json.dumps(props, sort_keys=True) == most_common:
                return True, {"model": model, "response": resp, "properties": props}
    return False, None

def _extract_code_block(text: str, index: int = 0) -> str:
    """Extract the Nth fenced Go code block from text."""
    blocks = re.findall(r'```(?:go)?\n(.*?)```', text, re.DOTALL)
    return blocks[index] if index < len(blocks) else ""
```

- [ ] **Step 3: Implement summarize_generate_round()**

```python
def summarize_generate_round(round_num: int, responses: dict, consensus: bool) -> str:
    from lib.llm import LLMError
    lines = [f"\n━━━ Generate Round {round_num} ━━━"]
    for model, resp in responses.items():
        if isinstance(resp, LLMError):
            lines.append(f"  {model:<28} ✗  error: {str(resp)[:60]}")
        else:
            props = extract_scorer_properties(_extract_code_block(resp) or "")
            norm = "KV÷100" if props.get("has_kv_norm") else "no-KV-norm"
            paths = len(props.get("access_paths", []))
            lines.append(f"  {model:<28} ✓  signals={paths}  {norm}  weights={props.get('float_literals', [])}")
    lines.append(f"\n  {'Consensus reached' if consensus else 'No consensus yet'}")
    return "\n".join(lines)
```

- [ ] **Step 4: Implement stage_generate()**

```python
def stage_generate(run_dir: Path, algo_summary_path: Path,
                   signal_coverage_path: Path, reviews: int) -> Path:
    step(3, "Generate (consensus loop)")
    out_dir = run_dir / "prepare_tekton"
    out_dir.mkdir(parents=True, exist_ok=True)
    stage3_out = run_dir / "prepare_stage3_output.json"
    stage3_out.unlink(missing_ok=True)

    algo_summary = json.loads(algo_summary_path.read_text())
    signal_coverage = json.loads(signal_coverage_path.read_text())
    mapping_doc = (REPO_ROOT / "docs/transfer/blis_to_llmd_mapping.md").read_text()
    scorer_template = (REPO_ROOT / "docs/transfer/scorer_template.go.md").read_text()
    existing = (REPO_ROOT / "llm-d-inference-scheduler/pkg/plugins/scorer/load_aware.go").read_text()

    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from lib.llm import call_models_parallel, LLMError
    from lib.consensus import run_consensus_loop

    messages = build_generate_prompt(algo_summary, signal_coverage,
                                      mapping_doc, scorer_template, existing)

    def call_fn(msgs):
        return call_models_parallel(MODELS, msgs)

    def check_fn(responses):
        consensus, _ = check_generate_consensus(responses)
        return consensus

    def summarize_fn(round_num, responses, consensus):
        return summarize_generate_round(round_num, responses, consensus)

    result = run_consensus_loop(
        messages=messages, call_fn=call_fn,
        check_fn=check_fn, summarize_fn=summarize_fn,
        max_rounds=reviews,
    )

    # Get winning response (consensus or user-accepted)
    _, winner = check_generate_consensus(result.responses)
    if winner is None:
        # User accepted despite no consensus — pick first buildable response
        winner = _pick_first_buildable(result.responses, run_dir)
    if winner is None:
        err("No buildable scorer output — HALT"); sys.exit(1)

    scorer_path = _write_scorer_files(winner["response"], algo_summary, run_dir)
    _validate_build(scorer_path)

    # Generate Tekton artifacts and merge
    algo_values_path = _generate_algorithm_values(algo_summary, signal_coverage, out_dir)
    values_path = _run_merge_values(algo_values_path, out_dir)

    # Write stage3_output.json
    scorer_type = _scorer_type_from_name(algo_summary.get("algorithm_name", "evolved"))
    stage3_out.write_text(json.dumps({
        "scorer_file": str(scorer_path.relative_to(REPO_ROOT)),
        "test_file": str(scorer_path.with_name(scorer_path.stem + "_test.go").relative_to(REPO_ROOT)),
        "register_file": "llm-d-inference-scheduler/pkg/plugins/register.go",
        "scorer_type": scorer_type,
        "tekton_artifacts": {"values_yaml": str(values_path.relative_to(REPO_ROOT))},
    }, indent=2))

    venv_python = str(REPO_ROOT / ".venv/bin/python")
    cli = str(REPO_ROOT / "tools/transfer_cli.py")
    run([venv_python, cli, "validate-schema", str(stage3_out)], cwd=REPO_ROOT)

    ok(f"Generate complete → {scorer_path}")
    return stage3_out
```

Helper stubs (implement inline):
- `_pick_first_buildable(responses, run_dir)` — write each response to a temp file and try `go build`, return first success
- `_write_scorer_files(response, algo_summary, run_dir)` — parse code blocks from response, write `.go` and `_test.go` files
- `_validate_build(scorer_path)` — run `go build ./...` in llm-d-inference-scheduler
- `_generate_algorithm_values(algo_summary, signal_coverage, out_dir)` — generates Tekton algorithm_values.yaml following the critical requirements from the skill (labelValues, probes, image path, no --port)
- `_run_merge_values(algo_values_path, out_dir)` — calls `transfer_cli.py merge-values`
- `_scorer_type_from_name(name)` — sanitize to `lowercase-hyphenated-scorer` format

- [ ] **Step 5: Wire into main()**

```python
stage3_path = stage_generate(run_dir, algo_summary_path, signal_coverage_path, args.reviews)
```

- [ ] **Step 6: Commit**

```bash
git add scripts/prepare.py
git commit -m "feat: add stage 3 (generate) consensus loop to prepare.py"
```

---

## Chunk 4: Build/Test Gate + Review Loop + Completion

### Task 7: Stage 4 — Build, Test, Equivalence Gate (deterministic)

**Files:**
- Modify: `scripts/prepare.py` (add `stage_build_test()`, `stage_equivalence_gate()`)

- [ ] **Step 1: Implement stage_build_test()**

```python
def stage_build_test(run_dir: Path, stage3_path: Path) -> None:
    step(4, "Build & Test")
    sched = REPO_ROOT / "llm-d-inference-scheduler"

    for label, cmd in [
        ("go build",  ["go", "build", "./..."]),
        ("go vet",    ["go", "vet", "./..."]),
        ("go test",   ["go", "test", "-timeout", "10m",
                       "./pkg/plugins/scorer/...", "-v"]),
    ]:
        result = run(cmd, check=False, capture=True, cwd=sched,
                     # GOWORK=off keeps the workspace from pulling in sim2real go.work
                     **{"env": {**os.environ, "GOWORK": "off"}})
        if result.returncode != 0:
            err(f"{label} failed:\n{result.stdout[-2000:]}\n{result.stderr[-500:]}")
            sys.exit(1)
        ok(f"{label} passed")
```

- [ ] **Step 2: Implement stage_equivalence_gate()**

```python
def stage_equivalence_gate(run_dir: Path) -> Path:
    step("4.5", "Equivalence Gate (Suite A/B/C)")
    sched = REPO_ROOT / "llm-d-inference-scheduler"
    env = {**os.environ, "GOWORK": "off"}

    suite_results = {}
    for suite, tag, fatal in [("A", "suitea", True), ("B", "suiteb", False), ("C", "suitec", True)]:
        cmd = ["go", "test", f"-tags={tag}", "-json", "-v",
               "-timeout", "10m", "./pkg/plugins/scorer/..."]
        if suite == "C":
            cmd.append("-race")
        result = run(cmd, check=False, capture=True, cwd=sched,
                     **{"env": env})
        passed = result.returncode == 0
        suite_results[f"suite_{suite.lower()}"] = {
            "passed": passed, "output": result.stdout[-3000:]
        }
        if not passed and fatal:
            err(f"Suite {suite} FAILED — HALT")
            sys.exit(1)
        status = "PASS" if passed else ("WARN" if not fatal else "FAIL")
        ok(f"Suite {suite}: {status}")

    # Extract tau from suite A output
    tau_match = re.search(r'tau[=: ]+([0-9.]+)', suite_results["suite_a"]["output"])
    if tau_match:
        suite_results["suite_a"]["kendall_tau"] = float(tau_match.group(1))

    out = run_dir / "prepare_equivalence_results.json"
    out.write_text(json.dumps(suite_results, indent=2))
    venv_python = str(REPO_ROOT / ".venv/bin/python")
    cli = str(REPO_ROOT / "tools/transfer_cli.py")
    run([venv_python, cli, "validate-schema", str(out)], cwd=REPO_ROOT)
    ok(f"Equivalence gate → {out}")
    return out
```

- [ ] **Step 3: Wire into main()**

```python
stage_build_test(run_dir, stage3_path)
eq_path = stage_equivalence_gate(run_dir)
```

- [ ] **Step 4: Commit**

```bash
git add scripts/prepare.py
git commit -m "feat: add stage 4 (build/test/equivalence gate) to prepare.py"
```

---

### Task 8: Stage 5 — Review Consensus Loop

**Files:**
- Modify: `scripts/prepare.py` (add `stage_review()` and helpers)

- [ ] **Step 1: Implement build_review_prompt()**

```python
def build_review_prompt(scorer_code: str, algo_summary: dict,
                         signal_coverage: dict, evolve_block: str) -> list[dict]:
    system = (
        "You are an expert code reviewer verifying that a Go scorer plugin faithfully "
        "implements an evolved routing algorithm. Be precise and technical. "
        "Respond with JSON: {\"verdict\": \"consistent\"|\"inconsistent\", "
        "\"issues\": [...], \"per_signal\": {...}, \"summary\": \"...\"}"
    )
    user = f"""Review the scorer Go code for translation fidelity.

For EACH signal, verify:
- The correct production field is read (per signal_coverage)
- Normalization matches algorithm_summary (e.g. KVUtil ÷100)
- Weight/coefficient matches the EVOLVE-BLOCK source
- Scoring logic (comparison, threshold, combination) is faithful

## Generated Scorer
```go
{scorer_code}
```

## Algorithm Summary
```json
{json.dumps(algo_summary, indent=2)}
```

## Signal Coverage
```json
{json.dumps(signal_coverage, indent=2)}
```

## EVOLVE-BLOCK Source
```go
{evolve_block}
```

Respond with ONLY the JSON object (no markdown fences, no prose).
"""
    return [{"role": "system", "content": system},
            {"role": "user", "content": user}]
```

- [ ] **Step 2: Implement check_review_consensus() and summarize_review_round()**

```python
def check_review_consensus(responses: dict) -> bool:
    from lib.llm import LLMError
    verdicts = []
    for model, resp in responses.items():
        if isinstance(resp, LLMError): continue
        try:
            data = json.loads(resp)
            verdicts.append(data.get("verdict", "unknown"))
        except json.JSONDecodeError:
            pass
    return all(v == "consistent" for v in verdicts) and len(verdicts) >= 2

def summarize_review_round(round_num: int, responses: dict, consensus: bool) -> str:
    from lib.llm import LLMError
    lines = [f"\n━━━ Review Round {round_num} ━━━"]
    all_issues = []
    for model, resp in responses.items():
        if isinstance(resp, LLMError):
            lines.append(f"  {model:<28} ✗  error: {str(resp)[:60]}")
            continue
        try:
            data = json.loads(resp)
            verdict = data.get("verdict", "unknown")
            issues = data.get("issues", [])
            all_issues.extend(issues)
            icon = "✓" if verdict == "consistent" else "✗"
            issue_str = f"  issues: {issues[:2]}" if issues else ""
            lines.append(f"  {model:<28} {icon}  {verdict}{issue_str}")
        except json.JSONDecodeError:
            lines.append(f"  {model:<28} ?  could not parse response")
    if all_issues:
        lines.append(f"\n  Remaining issues: {list(set(str(i) for i in all_issues[:3]))}")
    lines.append(f"\n  {'All reviewers consistent ✓' if consensus else 'Reviewers disagree'}")
    return "\n".join(lines)
```

- [ ] **Step 3: Implement stage_review()**

```python
def stage_review(run_dir: Path, stage3_path: Path,
                  algo_summary_path: Path, signal_coverage_path: Path,
                  reviews: int) -> Path:
    step(5, "Review (consensus loop)")

    stage3 = json.loads(stage3_path.read_text())
    scorer_file = REPO_ROOT / stage3["scorer_file"]
    scorer_code = scorer_file.read_text()

    algo_summary = json.loads(algo_summary_path.read_text())
    signal_coverage = json.loads(signal_coverage_path.read_text())
    evolve_block_path = algo_summary.get("evolve_block_file", "")
    evolve_block = (REPO_ROOT / evolve_block_path).read_text() if evolve_block_path else ""

    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from lib.llm import call_models_parallel
    from lib.consensus import run_consensus_loop

    messages = build_review_prompt(scorer_code, algo_summary, signal_coverage, evolve_block)

    result = run_consensus_loop(
        messages=messages,
        call_fn=lambda msgs: call_models_parallel(MODELS, msgs),
        check_fn=check_review_consensus,
        summarize_fn=summarize_review_round,
        max_rounds=reviews,
    )

    # Collect review data
    reviews_data = {}
    for model, resp in result.responses.items():
        from lib.llm import LLMError
        if isinstance(resp, LLMError):
            reviews_data[model] = {"error": str(resp)}
        else:
            try:
                reviews_data[model] = json.loads(resp)
            except json.JSONDecodeError:
                reviews_data[model] = {"raw": resp}

    out = run_dir / "prepare_translation_reviews.json"
    out.write_text(json.dumps({
        "rounds": result.rounds_run,
        "consensus": result.consensus,
        "accepted_by_user": result.accepted_by_user,
        "reviews": reviews_data,
    }, indent=2))
    ok(f"Review complete → {out}")
    return out
```

- [ ] **Step 4: Wire into main()**

```python
review_path = stage_review(run_dir, stage3_path, algo_summary_path,
                            signal_coverage_path, args.reviews)
```

- [ ] **Step 5: Commit**

```bash
git add scripts/prepare.py
git commit -m "feat: add stage 5 (review) consensus loop to prepare.py"
```

---

### Task 9: Completion + metadata

**Files:**
- Modify: `scripts/prepare.py` (add `write_outputs()`, finalize `main()`)

- [ ] **Step 1: Implement write_outputs()**

```python
def write_outputs(run_dir: Path, cfg: dict, stage3_path: Path) -> None:
    stage3 = json.loads(stage3_path.read_text())
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    update_run_metadata(run_dir, "prepare",
        status="completed",
        completed_at=now,
        summary="Extract, translate, generate, build/test, and AI review completed",
        artifacts=[
            "prepare_algorithm_summary.json",
            "prepare_signal_coverage.json",
            "prepare_stage3_output.json",
            "prepare_translation_reviews.json",
        ])

    print()
    print(_c("32", "━━━ sim2real-prepare complete ━━━"))
    print()
    print(f"Run:      {cfg['run_name']}")
    print(f"Run dir:  {run_dir}")
    print()
    print("Artifacts produced:")
    for name in ["prepare_algorithm_summary.json", "prepare_signal_coverage.json",
                 "prepare_stage3_output.json", "prepare_translation_reviews.json"]:
        print(f"  {run_dir / name}")
    scorer = stage3.get("scorer_file", "")
    if scorer:
        print(f"  {REPO_ROOT / scorer}")
    print()
    print("Next: python scripts/deploy.py  OR  /sim2real-deploy in Claude")
```

- [ ] **Step 2: Finalize main()**

```python
def main() -> int:
    args = build_parser().parse_args()
    print_intro(args.reviews)
    check_prerequisites()
    cfg = load_setup_config()
    run_dir = REPO_ROOT / "workspace/runs" / cfg["run_name"]
    update_run_metadata(run_dir, "prepare", status="in_progress",
                        started_at=datetime.now(timezone.utc).isoformat())
    try:
        algo_summary_path  = stage_extract(run_dir)
        signal_coverage_path = stage_translate(run_dir, algo_summary_path)
        stage3_path        = stage_generate(run_dir, algo_summary_path,
                                            signal_coverage_path, args.reviews)
        stage_build_test(run_dir, stage3_path)
        stage_equivalence_gate(run_dir)
        stage_review(run_dir, stage3_path, algo_summary_path,
                     signal_coverage_path, args.reviews)
        write_outputs(run_dir, cfg, stage3_path)
    except SystemExit:
        update_run_metadata(run_dir, "prepare", status="failed",
                            failed_at=datetime.now(timezone.utc).isoformat())
        raise
    return 0
```

- [ ] **Step 3: Run all unit tests**

```bash
python -m pytest tests/ -v
```
Expected: all 8 tests pass.

- [ ] **Step 4: Smoke test help and --reviews flag**

```bash
python scripts/prepare.py --help
python scripts/prepare.py --reviews 1  # ctrl+C early is fine
```
Expected: intro message shows correct round count.

- [ ] **Step 5: Final commit**

```bash
git add scripts/prepare.py tests/
git commit -m "feat: complete prepare.py with all stages and consensus loops"
```

---

## Testing Checklist

- [ ] `python -m pytest tests/ -v` — 8 unit tests pass (llm client + consensus loop)
- [ ] `python scripts/prepare.py --help` — shows usage with --reviews flag
- [ ] LLM connectivity: `bash /path/to/review.sh --check-models` — all 3 models reachable
- [ ] End-to-end: run `python scripts/prepare.py --reviews 1` with valid blis_router artifacts

## Notes

- The `requests` library is likely already in `.venv` (review.sh uses curl, but Python's stdlib `urllib` could substitute if needed — use `requests` for cleaner code).
- `_generate_algorithm_values()` is the most complex helper — it must follow all the critical requirements from the skill: `ghcr.io/inference-sim/blis:TAG` image path, `labelValues` array for acceleratorTypes, readiness/startup probes on decode containers, no `--port=8000` in args.
- The consensus loop `accept_on_consensus=True` is set for the Generate stage so it exits immediately when all 3 models agree, without prompting the user. For Review, same behavior.
