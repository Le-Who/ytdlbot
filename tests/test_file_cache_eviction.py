from pathlib import Path

from app.core.cache import FileTTLCache


def test_file_cache_eviction_removes_file(tmp_path):
    deleted = []

    def on_evict(path):
        deleted.append(path)
        Path(path).unlink(missing_ok=True)

    cache = FileTTLCache(maxsize=1, ttl=60, on_eviction=on_evict)
    file1 = tmp_path / "a.tmp"
    file1.write_text("x")
    file2 = tmp_path / "b.tmp"
    file2.write_text("y")

    cache["a"] = str(file1)
    cache["b"] = str(file2)

    assert str(file1) in deleted
    assert not file1.exists()
