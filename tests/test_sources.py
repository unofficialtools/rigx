"""Tests for the per-target source-set computation in rigx.sources."""

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from textwrap import dedent

from rigx import config, sources
from rigx.sources import (
    _glob_to_regex,
    ancestor_dirs,
    compute_project_files,
    compute_target_files,
    project_filtering_enabled,
)


class GlobToRegex(unittest.TestCase):
    """White-box tests for the glob translator. The `**` semantics are
    where bugs lurk; verify against the path-level expectations callers
    rely on."""

    def assertMatches(self, pattern: str, path: str) -> None:
        rx = _glob_to_regex(pattern)
        self.assertIsNotNone(
            rx.match(path), f"{pattern!r} should match {path!r} (regex {rx.pattern})"
        )

    def assertDoesNotMatch(self, pattern: str, path: str) -> None:
        rx = _glob_to_regex(pattern)
        self.assertIsNone(
            rx.match(path), f"{pattern!r} should NOT match {path!r} (regex {rx.pattern})"
        )

    def test_literal_path(self):
        self.assertMatches("src/main.cpp", "src/main.cpp")
        self.assertDoesNotMatch("src/main.cpp", "src/main.cppx")
        self.assertDoesNotMatch("src/main.cpp", "x/src/main.cpp")

    def test_star_one_component(self):
        self.assertMatches("src/*.cpp", "src/main.cpp")
        self.assertDoesNotMatch("src/*.cpp", "src/sub/main.cpp")

    def test_question_mark(self):
        self.assertMatches("file?.txt", "fileA.txt")
        self.assertDoesNotMatch("file?.txt", "file.txt")
        self.assertDoesNotMatch("file?.txt", "fileAB.txt")

    def test_double_star_at_start(self):
        self.assertMatches("**/*.nim", "a.nim")
        self.assertMatches("**/*.nim", "src/a.nim")
        self.assertMatches("**/*.nim", "src/sub/deep/a.nim")
        self.assertDoesNotMatch("**/*.nim", "a.go")

    def test_double_star_at_end(self):
        self.assertMatches("media/**", "media/a")
        self.assertMatches("media/**", "media/a/b/c")
        self.assertMatches("media/**", "media")
        self.assertDoesNotMatch("media/**", "other/a")

    def test_double_star_in_middle(self):
        self.assertMatches("src/**/*.nim", "src/a.nim")
        self.assertMatches("src/**/*.nim", "src/sub/a.nim")
        self.assertMatches("src/**/*.nim", "src/a/b/c.nim")
        self.assertDoesNotMatch("src/**/*.nim", "other/a.nim")

    def test_bracket_class(self):
        self.assertMatches("file[abc].txt", "filea.txt")
        self.assertMatches("file[abc].txt", "fileb.txt")
        self.assertDoesNotMatch("file[abc].txt", "filed.txt")

    def test_dot_is_literal(self):
        self.assertMatches("foo.txt", "foo.txt")
        self.assertDoesNotMatch("foo.txt", "fooXtxt")

    def test_leading_dot_slash(self):
        self.assertMatches("./src/main.cpp", "src/main.cpp")


class ProjectFixture:
    """Build a temp project with a populated source tree."""

    def __init__(self, toml_body: str, files: dict[str, str]):
        self.toml_body = dedent(toml_body).lstrip()
        self.files = files

    def __enter__(self) -> Path:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        for rel, body in self.files.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body)
        (root / "rigx.toml").write_text(self.toml_body)
        # Bypass any per-project caches that earlier tests may have populated.
        sources._project_files_cached.cache_clear()
        return root

    def __exit__(self, *exc) -> None:
        sources._project_files_cached.cache_clear()
        self._tmp.cleanup()


class ProjectBaseline(unittest.TestCase):
    def test_filtering_disabled_when_project_sources_unset(self):
        body = """
            [project]
            name = "p"
        """
        with ProjectFixture(body, {"src/a.cpp": "", "src/b.h": ""}) as root:
            proj = config.load(root)
            self.assertFalse(project_filtering_enabled(proj))
            self.assertEqual(compute_project_files(proj), [])

    def test_baseline_intersects_includes(self):
        body = """
            [project]
            name = "p"
            sources = ["**/*.nim", "**/*.toml"]
        """
        files = {
            "src/a.nim": "", "src/sub/b.nim": "",
            "src/c.go": "", "config.toml": "",
        }
        with ProjectFixture(body, files) as root:
            proj = config.load(root)
            base = compute_project_files(proj)
        self.assertEqual(
            sorted(base),
            sorted(["config.toml", "rigx.toml", "src/a.nim", "src/sub/b.nim"]),
        )

    def test_excludes_subtract(self):
        body = """
            [project]
            name = "p"
            sources = ["**/*.nim"]
            excludes = ["**/*_generated.nim"]
            respect_gitignore = false
        """
        files = {
            "src/a.nim": "",
            "src/a_generated.nim": "",
            "src/sub/b_generated.nim": "",
        }
        with ProjectFixture(body, files) as root:
            proj = config.load(root)
            base = compute_project_files(proj)
        self.assertEqual(base, ["src/a.nim"])

    def test_skips_always_skip_dirs(self):
        body = """
            [project]
            name = "p"
            sources = ["**/*"]
            respect_gitignore = false
        """
        files = {
            "src/a.txt": "",
            "output/should-skip": "",
            "result/should-skip": "",
            ".rigx/cache": "",
            "__pycache__/x.pyc": "",
        }
        with ProjectFixture(body, files) as root:
            proj = config.load(root)
            base = compute_project_files(proj)
        self.assertIn("src/a.txt", base)
        self.assertNotIn("output/should-skip", base)
        self.assertNotIn("result/should-skip", base)
        self.assertNotIn(".rigx/cache", base)
        self.assertFalse(any(f.startswith("__pycache__/") for f in base))


class TargetIntersection(unittest.TestCase):
    def test_target_without_sources_inherits_baseline(self):
        body = """
            [project]
            name = "p"
            sources = ["**/*.txt"]
            respect_gitignore = false

            [targets.bundle]
            kind = "custom"
            install_script = "true"
        """
        files = {"a.txt": "", "b.txt": "", "c.md": ""}
        with ProjectFixture(body, files) as root:
            proj = config.load(root)
            tgt = proj.targets["bundle"]
            files_for_target = compute_target_files(proj, tgt)
        self.assertEqual(sorted(files_for_target), ["a.txt", "b.txt"])

    def test_target_sources_narrow_to_listed_files(self):
        body = """
            [project]
            name = "p"
            sources = ["**/*.cpp", "**/*.h", "**/*.toml"]
            respect_gitignore = false

            [targets.hello]
            kind = "executable"
            sources = ["src/main.cpp"]
            language = "cxx"
        """
        files = {"src/main.cpp": "", "src/other.cpp": "", "include/x.h": ""}
        with ProjectFixture(body, files) as root:
            proj = config.load(root)
            tgt = proj.targets["hello"]
            files_for_target = compute_target_files(proj, tgt)
        # Only src/main.cpp; other.cpp dropped, x.h dropped (no includes set).
        self.assertEqual(files_for_target, ["src/main.cpp"])

    def test_target_includes_pull_in_headers(self):
        body = """
            [project]
            name = "p"
            sources = ["**/*.cpp", "**/*.h", "**/*.toml"]
            respect_gitignore = false

            [targets.hello]
            kind = "executable"
            sources = ["src/main.cpp"]
            includes = ["include"]
            language = "cxx"
        """
        files = {
            "src/main.cpp": "",
            "include/a.h": "",
            "include/sub/b.h": "",
            "elsewhere/c.h": "",
        }
        with ProjectFixture(body, files) as root:
            proj = config.load(root)
            tgt = proj.targets["hello"]
            files_for_target = compute_target_files(proj, tgt)
        self.assertEqual(
            files_for_target,
            ["include/a.h", "include/sub/b.h", "src/main.cpp"],
        )

    def test_target_sources_outside_baseline_is_error(self):
        body = """
            [project]
            name = "p"
            sources = ["**/*.cpp"]
            respect_gitignore = false

            [targets.hello]
            kind = "executable"
            sources = ["src/main.cpp", "include/x.h"]
            language = "cxx"
        """
        # Files exist; the issue is `include/x.h` doesn't match the project
        # baseline globs, so the build would silently lose it from `src`.
        files = {"src/main.cpp": "", "include/x.h": ""}
        with ProjectFixture(body, files) as root:
            proj = config.load(root)
            tgt = proj.targets["hello"]
            with self.assertRaises(ValueError) as cm:
                compute_target_files(proj, tgt)
            self.assertIn("include/x.h", str(cm.exception))


class GitignoreIntersection(unittest.TestCase):
    """End-to-end test of `respect_gitignore` against a real git checkout
    in a tempdir. Skipped when git isn't available — same fallback the
    runtime uses."""

    def setUp(self):
        try:
            subprocess.run(
                ["git", "--version"], check=True,
                capture_output=True,
            )
        except (FileNotFoundError, subprocess.CalledProcessError):
            self.skipTest("git not available")

    def test_gitignore_filters_baseline(self):
        body = """
            [project]
            name = "p"
            sources = ["**/*.txt"]
        """
        files = {
            "keep.txt": "",
            "drop.txt": "",
            ".gitignore": "drop.txt\n",
        }
        with ProjectFixture(body, files) as root:
            subprocess.run(
                ["git", "-c", "init.defaultBranch=main", "init", "-q"],
                cwd=root, check=True,
            )
            subprocess.run(
                ["git", "-c", "user.email=a@b", "-c", "user.name=A",
                 "add", "."], cwd=root, check=True,
            )
            proj = config.load(root)
            base = compute_project_files(proj)
        self.assertIn("keep.txt", base)
        self.assertNotIn("drop.txt", base)

    def test_gitignore_disabled_keeps_ignored_files(self):
        body = """
            [project]
            name = "p"
            sources = ["**/*.txt"]
            respect_gitignore = false
        """
        files = {
            "keep.txt": "",
            "would-be-ignored.txt": "",
            ".gitignore": "would-be-ignored.txt\n",
        }
        with ProjectFixture(body, files) as root:
            subprocess.run(
                ["git", "-c", "init.defaultBranch=main", "init", "-q"],
                cwd=root, check=True,
            )
            proj = config.load(root)
            base = compute_project_files(proj)
        self.assertIn("would-be-ignored.txt", base)


class AncestorDirs(unittest.TestCase):
    def test_collects_unique_dirs(self):
        rels = ["src/sub/a.nim", "src/b.nim", "include/x.h"]
        self.assertEqual(
            ancestor_dirs(rels),
            ["include", "src", "src/sub"],
        )

    def test_root_files_have_no_ancestors(self):
        self.assertEqual(ancestor_dirs(["a.txt", "b.txt"]), [])


if __name__ == "__main__":
    unittest.main()
