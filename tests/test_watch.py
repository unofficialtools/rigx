"""Tests for `rigx watch` scan logic (no polling loops, no sleeps)."""

import os
import tempfile
import unittest
from pathlib import Path

from rigx.commands import watch


class ScanAll(unittest.TestCase):
    """--watch-all mode: rglob scan of the whole project tree."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _write(self, rel: str, mtime: float | None = None) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")
        if mtime is not None:
            os.utime(p, (mtime, mtime))
        return p

    def test_skips_flake_nix_and_lock(self):
        # Builds regenerate flake.nix/flake.lock; if the scan saw them,
        # every rebuild would trigger the next one — an infinite loop.
        src = self._write("src/main.cpp", mtime=1_000_000.0)
        self._write("flake.nix")     # fresh mtime (now)
        self._write("flake.lock")    # fresh mtime (now)
        latest = watch._scan_all(self.root)
        self.assertEqual(latest, src.stat().st_mtime)

    def test_skips_generated_dirs(self):
        src = self._write("src/main.cpp", mtime=1_000_000.0)
        self._write("output/foo/bin", mtime=2_000_000.0)
        self._write(".rigx/cache", mtime=2_000_000.0)
        self.assertEqual(watch._scan_all(self.root), src.stat().st_mtime)

    def test_sees_ordinary_files(self):
        self._write("src/a.cpp", mtime=1_000_000.0)
        newer = self._write("src/b.cpp", mtime=2_000_000.0)
        self.assertEqual(watch._scan_all(self.root), newer.stat().st_mtime)

    def test_survives_file_vanishing_mid_scan(self):
        # An editor temp file can disappear between the is_file() check
        # and the stat() call; the scan must skip it, not crash.
        from unittest import mock
        self._write("src/a.cpp", mtime=1_000_000.0)
        ghost = self._write("src/.a.cpp.swp", mtime=2_000_000.0)
        real_stat = Path.stat
        calls = {"n": 0}

        def flaky_stat(path_self, **kw):
            if path_self.name == ghost.name:
                calls["n"] += 1
                if calls["n"] >= 2:  # first call is is_file()'s
                    raise FileNotFoundError(str(path_self))
            return real_stat(path_self, **kw)

        with mock.patch.object(Path, "stat", flaky_stat):
            latest = watch._scan_all(self.root)
        self.assertEqual(latest, (self.root / "src/a.cpp").stat().st_mtime)


if __name__ == "__main__":
    unittest.main()
