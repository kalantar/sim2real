# DEPRECATED: This script is retired as of 2026-04-10.
# The multi-model external LLM review loop has been replaced by the agent-team
# design in SKILL.md. The reviewer is now a spawned Claude agent using
# prompts/prepare/agent-reviewer.md.
# This file is kept for git history only — do not invoke it.
#
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
    hints_json: "str | None" = None,
) -> str:
    base = (
        f"## Review Round {round_num}\n\n"
        f"## Generated Plugin Code\n```go\n{plugin_code}\n```\n\n"
        f"## Algorithm Source (Ground Truth)\n```go\n{algorithm_source}\n```\n\n"
        f"## Algorithm Config\n```yaml\n{algorithm_config}\n```\n\n"
        f"## Translation Context\n{context_doc}\n\n"
        f"## Treatment Config\n```yaml\n{treatment_config}\n```\n"
    )
    if hints_json:
        try:
            hints = json.loads(hints_json)
            hints_text = hints.get("text", "")
            hints_files = hints.get("files", [])
            if hints_text or hints_files:
                parts = ["\n## Transfer Hints (user's mandate for this run)\n"]
                if hints_text:
                    parts.append(hints_text)
                for f in hints_files:
                    parts.append(f"\n### {f.get('path', 'hint')}\n{f.get('content', '')}")
                base = base + "\n".join(parts)
        except (json.JSONDecodeError, AttributeError):
            pass  # hints are optional; don't fail review if malformed
    return base


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
        # Validate schema
        if review.get("verdict") not in ("APPROVE", "NEEDS_CHANGES"):
            return None, f"invalid verdict '{review.get('verdict')}' (must be APPROVE or NEEDS_CHANGES)"
        if not isinstance(review.get("issues"), list):
            return None, f"issues must be a list, got {type(review.get('issues'))}"
        if "summary" not in review:
            return None, "missing required field: summary"
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
    hints_json: "str | None" = None,
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
            hints_json=hints_json,
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
    parser.add_argument(
        "--hints-json", dest="hints_json", default=None,
        help="Optional JSON string with hints {text, files:[{path, content}]}. "
             "Used to evaluate whether translation honored the user's mandate."
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
        hints_json=getattr(args, "hints_json", None),
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
