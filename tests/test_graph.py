"""Tests for the Mermaid dep-graph generator."""

import unittest
from pathlib import Path

from rigx import graph
from rigx.config import GitDep, LocalDep, Project, Target, TargetDeps


def _project(targets, **kwargs) -> Project:
    return Project(
        name="p",
        version="0.1.0",
        nixpkgs_ref="nixos-24.11",
        git_deps=kwargs.get("git_deps", {}),
        targets=targets,
        root=Path("/tmp"),
        local_deps=kwargs.get("local_deps", {}),
    )


class Mermaid(unittest.TestCase):
    def test_single_target_no_deps(self):
        out = graph.mermaid(
            _project({"hello": Target(name="hello", kind="executable", sources=["m.cpp"])}),
            "hello",
        )
        self.assertIn("graph TD", out)
        self.assertIn('hello["hello [executable]"]', out)

    def test_internal_dep_recursed(self):
        proj = _project({
            "greet": Target(name="greet", kind="static_library", sources=["g.cpp"]),
            "hello": Target(
                name="hello", kind="executable", sources=["m.cpp"],
                deps=TargetDeps(internal=["greet"]),
            ),
        })
        out = graph.mermaid(proj, "hello")
        self.assertIn('hello["hello [executable]"]', out)
        self.assertIn('greet["greet [static_library]"]', out)
        self.assertIn("hello --> greet", out)

    def test_nixpkgs_dep_emitted_as_pkg_node(self):
        proj = _project({
            "hello": Target(
                name="hello", kind="executable", sources=["m.cpp"],
                deps=TargetDeps(nixpkgs=["fmt"]),
            ),
        })
        out = graph.mermaid(proj, "hello")
        self.assertIn('pkg_fmt(["pkgs.fmt"])', out)
        self.assertIn("hello --> pkg_fmt", out)

    def test_git_dep_emitted_as_git_node(self):
        dep = GitDep(name="mylib", url="https://example/x", rev="HEAD")
        proj = _project(
            {
                "hello": Target(
                    name="hello", kind="executable", sources=["m.cpp"],
                    deps=TargetDeps(git=["mylib"]),
                ),
            },
            git_deps={"mylib": dep},
        )
        out = graph.mermaid(proj, "hello")
        self.assertIn('git_mylib(["git:mylib"])', out)
        self.assertIn("hello --> git_mylib", out)

    def test_cross_flake_ref_is_opaque_leaf(self):
        # A-style cross-flake ref: render as a `local-dep` node, do NOT recurse.
        sub = Project(
            name="sub", version="0.1.0", nixpkgs_ref="nixos-24.11",
            git_deps={}, targets={
                "app": Target(name="app", kind="executable", sources=["m.cpp"]),
            },
            root=Path("/tmp/sub"),
        )
        proj = _project(
            {
                "bundle": Target(
                    name="bundle", kind="custom", install_script="true",
                    deps=TargetDeps(internal=["sub.app"]),
                ),
            },
            local_deps={"sub": LocalDep(name="sub", path=Path("/tmp/sub"), sub_project=sub)},
        )
        out = graph.mermaid(proj, "bundle")
        self.assertIn('sub_app(["sub.app [local-dep]"])', out)
        self.assertIn("bundle --> sub_app", out)
        # We did NOT recurse into the sub-project — `app`'s own deps don't appear.
        self.assertNotIn('"app [executable]"', out)

    def test_run_target_field_treated_as_implicit_dep(self):
        proj = _project({
            "tool": Target(name="tool", kind="executable", sources=["t.cpp"]),
            "r": Target(
                name="r", kind="run", run="tool", outputs=["x.txt"],
            ),
        })
        out = graph.mermaid(proj, "r")
        self.assertIn("r --> tool", out)
        self.assertIn('tool["tool [executable]"]', out)

    def test_variant_suffix_is_stripped(self):
        proj = _project({
            "hello": Target(
                name="hello", kind="executable", sources=["m.cpp"],
                variants={"release": __import__("rigx.config", fromlist=["Variant"]).Variant(name="release")},
            ),
        })
        out = graph.mermaid(proj, "hello@release")
        self.assertIn('hello["hello [executable]"]', out)

    def test_unknown_target_raises(self):
        proj = _project({})
        with self.assertRaisesRegex(ValueError, "no such target"):
            graph.mermaid(proj, "missing")

    def test_classes_assigned(self):
        proj = _project({
            "hello": Target(
                name="hello", kind="executable", sources=["m.cpp"],
                deps=TargetDeps(nixpkgs=["fmt"]),
            ),
        })
        out = graph.mermaid(proj, "hello")
        self.assertIn("class hello internal", out)
        self.assertIn("class pkg_fmt nixpkgs", out)


if __name__ == "__main__":
    unittest.main()
