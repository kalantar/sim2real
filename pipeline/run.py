#!/usr/bin/env python3
"""sim2real run — list, inspect, and switch pipeline runs."""

import argparse
import json
import sys
from pathlib import Path

# Ensure repo root is on sys.path when run as a script (python pipeline/run.py)
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from pipeline.lib.run_manager import (
    list_runs, inspect_run, switch_run,
    RunNotFoundError, TranslationOutputError, SwitchAborted,
)

# ── Repo layout ───────────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parent.parent
WORKSPACE_DIR = REPO_ROOT / "workspace"
SETUP_CONFIG = WORKSPACE_DIR / "setup_config.json"
SUBMODULE_DIR = REPO_ROOT / "llm-d-inference-scheduler"

# ── Color helpers ─────────────────────────────────────────────────────────────
_tty = sys.stdout.isatty()

def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _tty else text

def info(msg: str) -> None: print(_c("34", "[INFO]  ") + msg)
def ok(msg: str)   -> None: print(_c("32", "[OK]    ") + msg)
def warn(msg: str) -> None: print(_c("33", "[WARN]  ") + msg)
def err(msg: str)  -> None: print(_c("31", "[ERROR] ") + msg, file=sys.stderr)


# ── Subcommand handlers ───────────────────────────────────────────────────────

def cmd_list(_args) -> None:
    runs = list_runs(WORKSPACE_DIR, SETUP_CONFIG)
    fmt = "{:<14} {:<28} {:<12} {:<22} {}"
    print(fmt.format("NAME", "SCENARIO", "PHASE", "VERDICT", "ACTIVE"))
    for r in runs:
        print(fmt.format(r.name, r.scenario, r.last_phase, r.verdict,
                         "*" if r.active else ""))


def cmd_inspect(args) -> None:
    active_run = ""
    if SETUP_CONFIG.exists():
        try:
            active_run = json.loads(SETUP_CONFIG.read_text()).get("current_run", "")
        except (json.JSONDecodeError, OSError):
            pass

    run_dir = WORKSPACE_DIR / "runs" / args.name
    try:
        detail = inspect_run(run_dir, active_run=active_run)
    except RunNotFoundError as e:
        err(str(e))
        sys.exit(1)

    active_marker = "  [ACTIVE]" if detail.active else ""
    print(f"Run: {detail.name}{active_marker}")
    print(f"Scenario: {detail.scenario}")
    print()
    print("Phases:")
    for p in detail.phases:
        notes_part = f"  ({p.notes})" if p.notes else ""
        verdict_part = f"  → {p.verdict}" if p.verdict else ""
        print(f"  {p.name:<20} {p.status}{notes_part}{verdict_part}")

    if detail.files_created or detail.files_modified:
        print()
        print("Generated files:")
        for f in detail.files_modified:
            print(f"  {f}  (modified)")
        for f in detail.files_created:
            print(f"  {f}  (created)")

    if detail.deploy_stages:
        print()
        print("Deploy:")
        for stage, status in detail.deploy_stages.items():
            extra = (f"  (last: {detail.deploy_last_step})"
                     if stage == "deploy" and detail.deploy_last_step else "")
            print(f"  {stage:<10} {status}{extra}")


def cmd_switch(args) -> None:
    def confirm_fn(_dirty):
        warn("llm-d-inference-scheduler has uncommitted changes that will be discarded.")
        answer = input("Reset all uncommitted changes and switch? [y/N] ").strip().lower()
        return answer == "y"

    try:
        result = switch_run(
            args.name, WORKSPACE_DIR, SUBMODULE_DIR, SETUP_CONFIG, confirm_fn
        )
    except (RunNotFoundError, TranslationOutputError, ValueError) as e:
        err(str(e))
        sys.exit(1)
    except SwitchAborted:
        info("Switch aborted — no changes made.")
        sys.exit(0)
    except OSError as e:
        err(str(e))
        sys.exit(1)

    ok(f"Switched to run: {result.active_run}")
    for f in result.files_written:
        info(f"  wrote {f}")


# ── Argument parser ───────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run.py",
        description="sim2real run — list, inspect, and switch pipeline runs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python pipeline/run.py list              # show all runs with status
  python pipeline/run.py inspect adaptive6 # show run details
  python pipeline/run.py switch admin5     # switch active run, sync submodule
""",
    )
    sub = p.add_subparsers(dest="command")

    sub.add_parser("list", help="List all conforming runs")

    inspect_p = sub.add_parser("inspect", help="Show full details of a run")
    inspect_p.add_argument("name", metavar="NAME", help="Run name")

    switch_p = sub.add_parser("switch", help="Switch active run and sync submodule artifacts")
    switch_p.add_argument("name", metavar="NAME", help="Run name to switch to")

    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "list":
        cmd_list(args)
    elif args.command == "inspect":
        cmd_inspect(args)
    elif args.command == "switch":
        cmd_switch(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
