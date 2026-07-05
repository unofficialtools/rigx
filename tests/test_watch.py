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


class RefreshProject(unittest.TestCase):
    """After a rigx.toml edit, rebuilds must use the reloaded config —
    not the stale in-memory project loaded at watch startup."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.toml = self.root / "rigx.toml"
        self.toml.write_text('[project]\nname = "p"\n')

    def _load(self):
        from rigx import config
        return config.load(self.root)

    def test_unchanged_mtime_keeps_same_project(self):
        proj = self._load()
        mtime = self.toml.stat().st_mtime
        proj2, tm = watch._refresh_project(proj, mtime)
        self.assertIs(proj2, proj)
        self.assertEqual(tm, mtime)

    def test_edited_toml_reloads_project(self):
        proj = self._load()
        self.toml.write_text(
            '[project]\nname = "p"\n\n'
            '[targets.task]\nkind = "script"\nscript = "true"\n'
        )
        os.utime(self.toml, (2_000_000_000.0, 2_000_000_000.0))
        proj2, tm = watch._refresh_project(proj, 1.0)
        self.assertIsNot(proj2, proj)
        self.assertIn("task", proj2.targets)
        self.assertEqual(tm, self.toml.stat().st_mtime)

    def test_broken_edit_warns_and_keeps_previous_project(self):
        import io
        from contextlib import redirect_stdout
        proj = self._load()
        self.toml.write_text("[project\nbroken")
        out = io.StringIO()
        with redirect_stdout(out):
            proj2, tm = watch._refresh_project(proj, 1.0)
        self.assertIs(proj2, proj)
        # mtime advances so the same broken file isn't re-parsed every poll.
        self.assertEqual(tm, self.toml.stat().st_mtime)
        self.assertIn("config reload failed", out.getvalue())


if __name__ == "__main__":
    unittest.main()
