"""Context document assembly with SHA-256 caching."""
import hashlib
from pathlib import Path


def compute_context_hash(
    files: list[Path], submodule_shas: dict[str, str]
) -> str:
    """SHA-256 of file contents + submodule SHAs. Notes excluded."""
    h = hashlib.sha256()
    for f in sorted(files):
        h.update(f.read_bytes())
    for key in sorted(submodule_shas):
        h.update(f"{key}={submodule_shas[key]}".encode())
    return h.hexdigest()[:12]


def build_context(
    context_files: list[Path],
    submodule_shas: dict[str, str],
    scenario: str,
    cache_dir: Path,
) -> tuple[Path, bool]:
    """Assemble context.md from files. Returns (path, was_cached).

    Cache key is SHA-256 of file contents + submodule SHAs.
    Notes are NOT included in the hash (delivered via skill_input.json).
    """
    for f in context_files:
        if not f.exists():
            raise FileNotFoundError(f"Context file not found: {f}")

    content_hash = compute_context_hash(context_files, submodule_shas)
    cache_path = cache_dir / scenario / f"{content_hash}.md"

    if cache_path.exists():
        return cache_path, True

    sha_summary = " | ".join(
        f"{k}@{v[:7]}" for k, v in sorted(submodule_shas.items())
    )
    lines = ["# Translation Context", f"Scenario: {scenario} | {sha_summary}", ""]
    for f in context_files:
        lines.append(f"## {f}")
        lines.append(f.read_text())
        lines.append("")

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text("\n".join(lines))
    return cache_path, False
