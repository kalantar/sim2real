"""Tests for context builder."""
import pytest

from pipeline.lib.context_builder import build_context, compute_context_hash


def _write_file(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def test_compute_hash_deterministic(tmp_path):
    f1 = tmp_path / "a.md"
    f1.write_text("content A")
    h1 = compute_context_hash([f1], {"sim": "abc", "sched": "def"})
    h2 = compute_context_hash([f1], {"sim": "abc", "sched": "def"})
    assert h1 == h2


def test_hash_changes_on_file_content(tmp_path):
    f1 = tmp_path / "a.md"
    f1.write_text("v1")
    h1 = compute_context_hash([f1], {"sim": "abc", "sched": "def"})
    f1.write_text("v2")
    h2 = compute_context_hash([f1], {"sim": "abc", "sched": "def"})
    assert h1 != h2


def test_hash_changes_on_submodule_sha(tmp_path):
    f1 = tmp_path / "a.md"
    f1.write_text("same")
    h1 = compute_context_hash([f1], {"sim": "abc", "sched": "def"})
    h2 = compute_context_hash([f1], {"sim": "abc", "sched": "xyz"})
    assert h1 != h2


def test_build_context_creates_file(tmp_path):
    f1 = tmp_path / "docs" / "mapping.md"
    _write_file(f1, "# Mapping\nSignal A \u2192 Signal B")
    cache_dir = tmp_path / "cache"
    path, cached = build_context(
        context_files=[f1],
        submodule_shas={"sim": "abc123def456", "sched": "def456abc123"},
        scenario="routing",
        cache_dir=cache_dir,
    )
    assert path.exists()
    assert not cached
    content = path.read_text()
    assert "# Translation Context" in content
    assert "Signal A" in content
    assert "routing" in content


def test_build_context_includes_full_path(tmp_path):
    f1 = tmp_path / "docs" / "mapping.md"
    _write_file(f1, "content")
    cache_dir = tmp_path / "cache"
    path, _ = build_context([f1], {"sim": "a", "sched": "b"}, "routing", cache_dir)
    content = path.read_text()
    # Should include full path, not just filename
    assert str(f1) in content


def test_build_context_cache_hit(tmp_path):
    f1 = tmp_path / "docs" / "mapping.md"
    _write_file(f1, "# Mapping")
    cache_dir = tmp_path / "cache"
    shas = {"sim": "abc", "sched": "def"}
    _, cached1 = build_context([f1], shas, "routing", cache_dir)
    _, cached2 = build_context([f1], shas, "routing", cache_dir)
    assert not cached1
    assert cached2


def test_build_context_cache_miss_after_change(tmp_path):
    f1 = tmp_path / "mapping.md"
    f1.write_text("v1")
    cache_dir = tmp_path / "cache"
    shas = {"sim": "abc", "sched": "def"}
    _, c1 = build_context([f1], shas, "routing", cache_dir)
    f1.write_text("v2")
    _, c2 = build_context([f1], shas, "routing", cache_dir)
    assert not c1
    assert not c2


def test_missing_context_file_raises(tmp_path):
    cache_dir = tmp_path / "cache"
    with pytest.raises(FileNotFoundError):
        build_context(
            [tmp_path / "nonexistent.md"],
            {"sim": "abc", "sched": "def"},
            "routing",
            cache_dir,
        )


def test_multiple_context_files(tmp_path):
    f1 = tmp_path / "a.md"
    f2 = tmp_path / "b.md"
    f1.write_text("file A")
    f2.write_text("file B")
    cache_dir = tmp_path / "cache"
    path, _ = build_context([f1, f2], {"sim": "a", "sched": "b"}, "routing", cache_dir)
    content = path.read_text()
    assert "file A" in content
    assert "file B" in content


def test_empty_context_files(tmp_path):
    cache_dir = tmp_path / "cache"
    path, _ = build_context([], {"sim": "a", "sched": "b"}, "routing", cache_dir)
    assert path.exists()
    assert "# Translation Context" in path.read_text()
