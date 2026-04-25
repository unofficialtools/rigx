"""Tests for builder attribute resolution logic (no Nix invocation)."""

import unittest
from pathlib import Path
from unittest import mock

from rigx import builder
from rigx.builder import BuildError, NixNotFoundError
from rigx.config import Project, Target, Variant


def _project_with(**targets) -> Project:
    return Project(
        name="p",
        version="0.1.0",
        nixpkgs_ref="nixos-24.11",
        git_deps={},
        targets=targets,
        root=Path("/tmp/fake"),
    )


class ResolveAttr(unittest.TestCase):
    def test_plain_target(self):
        t = Target(name="hello", kind="executable", sources=["m.cpp"])
        proj = _project_with(hello=t)
        self.assertEqual(builder._resolve_attr(proj, "hello"), "hello")

    def test_variant(self):
        t = Target(
            name="hello",
            kind="executable",
            sources=["m.cpp"],
            variants={
                "debug": Variant(name="debug"),
                "release": Variant(name="release"),
            },
        )
        proj = _project_with(hello=t)
        self.assertEqual(builder._resolve_attr(proj, "hello@debug"), "hello-debug")

    def test_unknown_target(self):
        proj = _project_with()
        with self.assertRaisesRegex(BuildError, "no such target"):
            builder._resolve_attr(proj, "missing")

    def test_unknown_variant(self):
        t = Target(
            name="h",
            kind="executable",
            sources=["m.cpp"],
            variants={"debug": Variant(name="debug")},
        )
        proj = _project_with(h=t)
        with self.assertRaisesRegex(BuildError, "has no variant"):
            builder._resolve_attr(proj, "h@asan")


class ResolveAttrCrossFlake(unittest.TestCase):
    @staticmethod
    def _parent_with_sub() -> Project:
        from rigx.config import LocalDep
        sub = Project(
            name="sub",
            version="0.1.0",
            nixpkgs_ref="nixos-24.11",
            git_deps={},
            targets={
                "app": Target(name="app", kind="executable", sources=["m.cpp"]),
                "lib": Target(
                    name="lib",
                    kind="executable",
                    sources=["m.cpp"],
                    variants={
                        "debug": Variant(name="debug"),
                        "release": Variant(name="release"),
                    },
                ),
            },
            root=Path("/tmp/sub"),
        )
        return Project(
            name="parent",
            version="0.1.0",
            nixpkgs_ref="nixos-24.11",
            git_deps={},
            targets={},
            root=Path("/tmp/parent"),
            local_deps={"sub": LocalDep(name="sub", path=Path("/tmp/sub"), sub_project=sub)},
        )

    def test_dotted_no_variant(self):
        proj = self._parent_with_sub()
        self.assertEqual(builder._resolve_attr(proj, "sub.app"), "sub_app")

    def test_dotted_with_variant(self):
        proj = self._parent_with_sub()
        # Variant suffix uses `-` because Nix allows hyphens in identifiers;
        # only the dot in the qualified name gets sanitized to `_`.
        self.assertEqual(
            builder._resolve_attr(proj, "sub.lib@release"), "sub_lib-release"
        )

    def test_unknown_dotted_target(self):
        proj = self._parent_with_sub()
        with self.assertRaisesRegex(BuildError, "no such target"):
            builder._resolve_attr(proj, "sub.missing")

    def test_unknown_variant_on_dotted(self):
        proj = self._parent_with_sub()
        with self.assertRaisesRegex(BuildError, "has no variant"):
            builder._resolve_attr(proj, "sub.lib@asan")

    def test_own_target_still_unsanitized(self):
        # Regression: own-target hyphenated variant must not get sanitized
        # — the rec block emits it with a hyphen.
        from rigx.config import LocalDep
        proj = Project(
            name="p",
            version="0.1.0",
            nixpkgs_ref="nixos-24.11",
            git_deps={},
            targets={
                "h": Target(
                    name="h",
                    kind="executable",
                    sources=["m.cpp"],
                    variants={"release": Variant(name="release")},
                ),
            },
            root=Path("/tmp"),
        )
        self.assertEqual(builder._resolve_attr(proj, "h@release"), "h-release")

    def test_b_merged_module_target_sanitized(self):
        # B-merged (qualified) name: `frontend.greet` resolves directly
        # against project.targets (no local-deps needed) and the result is
        # sanitized for Nix.
        proj = Project(
            name="p",
            version="0.1.0",
            nixpkgs_ref="nixos-24.11",
            git_deps={},
            targets={
                "frontend.greet": Target(
                    name="greet",
                    namespace="frontend",
                    kind="executable",
                    sources=["frontend/m.cpp"],
                    variants={"release": Variant(name="release")},
                ),
            },
            root=Path("/tmp"),
        )
        self.assertEqual(builder._resolve_attr(proj, "frontend.greet"), "frontend_greet")
        self.assertEqual(
            builder._resolve_attr(proj, "frontend.greet@release"),
            "frontend_greet-release",
        )


class AllAttrs(unittest.TestCase):
    def test_expands_variants(self):
        proj = _project_with(
            plain=Target(name="plain", kind="executable", sources=["m.cpp"]),
            variadic=Target(
                name="variadic",
                kind="executable",
                sources=["m.cpp"],
                variants={
                    "debug": Variant(name="debug"),
                    "release": Variant(name="release"),
                },
            ),
        )
        attrs = builder._all_attrs(proj)
        self.assertIn("plain", attrs)
        self.assertIn("variadic-debug", attrs)
        self.assertIn("variadic-release", attrs)
        # Variadic target itself (without variant suffix) is not listed — its
        # alias is reachable, but _all_attrs emits the concrete variants.
        self.assertNotIn("variadic", attrs)

    def test_script_targets_excluded_from_build_all(self):
        proj = _project_with(
            hello=Target(name="hello", kind="executable", sources=["m.cpp"]),
            publish=Target(name="publish", kind="script", script="uv publish"),
        )
        attrs = builder._all_attrs(proj)
        self.assertIn("hello", attrs)
        self.assertNotIn("publish", attrs)


class NixMissing(unittest.TestCase):
    def test_raises_specific_error_with_instructions(self):
        with mock.patch("rigx.builder.shutil.which", return_value=None):
            with self.assertRaises(NixNotFoundError) as ctx:
                builder._nix_bin()
        msg = str(ctx.exception)
        self.assertIn("Nix is required", msg)
        self.assertIn("nixos.org", msg)
        self.assertIn("install.determinate.systems", msg)

    def test_is_subclass_of_builderror(self):
        self.assertTrue(issubclass(NixNotFoundError, BuildError))


class RunNamedScript(unittest.TestCase):
    def test_rejects_unknown_target(self):
        proj = _project_with()
        with self.assertRaisesRegex(BuildError, "no such target"):
            builder.run_named_script(proj, "missing")

    def test_rejects_non_script_target(self):
        proj = _project_with(
            hello=Target(name="hello", kind="executable", sources=["m.cpp"]),
        )
        with self.assertRaisesRegex(BuildError, "is not a script target"):
            builder.run_named_script(proj, "hello")

    def test_extra_args_forwarded_to_bash_as_positional(self):
        proj = _project_with(
            deploy=Target(name="deploy", kind="script", script='echo "$@"'),
        )
        with mock.patch("rigx.builder._nix_bin", return_value="/usr/bin/nix"), \
             mock.patch("rigx.builder.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0)
            builder.run_named_script(proj, "deploy", ["--dry-run", "prod"])
        cmd = run.call_args.args[0]
        # `bash -eo pipefail -c <script> $0 $1 $2 ...`
        self.assertIn("bash", cmd)
        i = cmd.index("-c")
        # Script body sits at -c's value; target name is $0; user args follow.
        self.assertEqual(cmd[i + 1 : i + 5], ['echo "$@"', "deploy", "--dry-run", "prod"])

    def test_no_extra_args_means_no_positional_after_target_name(self):
        proj = _project_with(
            deploy=Target(name="deploy", kind="script", script="true"),
        )
        with mock.patch("rigx.builder._nix_bin", return_value="/usr/bin/nix"), \
             mock.patch("rigx.builder.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0)
            builder.run_named_script(proj, "deploy")
        cmd = run.call_args.args[0]
        i = cmd.index("-c")
        self.assertEqual(cmd[i + 1 : i + 3], ["true", "deploy"])
        self.assertEqual(len(cmd), i + 3)


class BuildRejectsScript(unittest.TestCase):
    def test_build_points_at_rigx_run(self):
        proj = _project_with(
            publish=Target(name="publish", kind="script", script="echo"),
        )
        with self.assertRaisesRegex(BuildError, "use `rigx run publish`"):
            builder.build(proj, ["publish"])


class FlakeRef(unittest.TestCase):
    def test_shape(self):
        proj = _project_with()
        ref = builder._flake_ref(proj, "hello")
        self.assertTrue(ref.startswith("path:"))
        self.assertTrue(ref.endswith("#hello"))

    def test_without_attr(self):
        proj = _project_with()
        ref = builder._flake_ref(proj)
        self.assertTrue(ref.startswith("path:"))
        self.assertNotIn("#", ref)


class DashNamedTargets(unittest.TestCase):
    """Regression: dash-named targets must round-trip identically through
    `_resolve_attr` and the flake. Previously rigx rewrote
    `actarus-test-runner` → `actarus_test_runner` in the flake but asked
    `nix build .#actarus-test-runner`. Names are now verbatim — `_nix_id`
    only handles `.` (the actually-illegal Nix bare-attr char), and Nix
    accepts hyphens natively (we've been emitting `hello-debug` variant
    attrs since day one)."""

    def test_resolve_attr_preserves_hyphens(self):
        proj = _project_with(**{
            "actarus-test-runner": Target(
                name="actarus-test-runner",
                kind="executable", sources=["m.cpp"],
            ),
        })
        self.assertEqual(
            builder._resolve_attr(proj, "actarus-test-runner"),
            "actarus-test-runner",
        )

    def test_resolve_attr_preserves_hyphens_with_variant(self):
        proj = _project_with(**{
            "actarus-test-runner": Target(
                name="actarus-test-runner",
                kind="executable", sources=["m.cpp"],
                variants={"release": Variant(name="release")},
            ),
        })
        self.assertEqual(
            builder._resolve_attr(proj, "actarus-test-runner@release"),
            "actarus-test-runner-release",
        )

    def test_flake_attr_matches_resolve(self):
        # End-to-end: flake.nix declares the same attr name `_resolve_attr`
        # asks for, so `nix build .#<name>` finds the derivation.
        from rigx import nix_gen
        proj = _project_with(**{
            "actarus-test-runner": Target(
                name="actarus-test-runner",
                kind="executable", sources=["m.cpp"],
            ),
        })
        out = nix_gen.generate(proj)
        attr = builder._resolve_attr(proj, "actarus-test-runner")
        self.assertIn(f"{attr} = pkgs.stdenv.mkDerivation", out)


class BuildGlob(unittest.TestCase):
    """Glob specs select targets by name (variants ignored when matching)
    and expand all variants of each match."""

    def _proj(self) -> Project:
        return Project(
            name="p", version="0.1.0", nixpkgs_ref="nixos-24.11",
            git_deps={}, root=Path("/tmp"),
            targets={
                "hello":      Target(name="hello", kind="executable", sources=["m.cpp"],
                                     variants={
                                         "debug":   Variant(name="debug"),
                                         "release": Variant(name="release"),
                                     }),
                "hello_go":   Target(name="hello_go", kind="executable", sources=["g.go"]),
                "hello_rust": Target(name="hello_rust", kind="executable", sources=["r.rs"]),
                "tool":       Target(name="tool", kind="executable", sources=["t.cpp"]),
                "publish":    Target(name="publish", kind="script", script="echo"),
                "smoke":      Target(name="smoke", kind="test", script="exit 0"),
            },
        )

    def test_glob_expands_to_matched_targets_and_variants(self):
        proj = self._proj()
        attrs = builder._expand_build_spec(proj, "hello*")
        # `hello` has variants, expand to hello-debug + hello-release.
        # hello_go and hello_rust are plain.
        self.assertEqual(
            sorted(attrs),
            sorted(["hello-debug", "hello-release", "hello_go", "hello_rust"]),
        )

    def test_glob_skips_script_and_test_targets(self):
        proj = self._proj()
        # `*` would technically include publish/smoke, but glob-mode skips
        # non-buildables silently (cf. literal naming, which errors).
        attrs = builder._expand_build_spec(proj, "*")
        self.assertNotIn("publish", attrs)
        self.assertNotIn("smoke", attrs)
        self.assertIn("hello-debug", attrs)
        self.assertIn("tool", attrs)

    def test_glob_no_match_errors(self):
        proj = self._proj()
        with self.assertRaisesRegex(BuildError, "matched no targets"):
            builder._expand_build_spec(proj, "nonexistent*")

    def test_glob_with_variant_suffix_errors(self):
        proj = self._proj()
        with self.assertRaisesRegex(BuildError, "cannot include @variant"):
            builder._expand_build_spec(proj, "hello*@release")

    def test_glob_matching_only_unbuildables_errors(self):
        proj = self._proj()
        # Only matches `publish` (script) and `smoke` (test) — both skipped.
        # Build the project with just those reachable so the empty-result
        # path triggers.
        only_unbuildable = Project(
            name="p", version="0.1.0", nixpkgs_ref="nixos-24.11",
            git_deps={}, root=Path("/tmp"),
            targets={
                "publish": Target(name="publish", kind="script", script="echo"),
                "smoke":   Target(name="smoke", kind="test", script="exit 0"),
            },
        )
        with self.assertRaisesRegex(BuildError, "non-buildable"):
            builder._expand_build_spec(only_unbuildable, "*")

    def test_literal_name_unchanged(self):
        # Non-glob spec → existing _resolve_attr path (alias, no variant
        # expansion).
        proj = self._proj()
        attrs = builder._expand_build_spec(proj, "hello")
        self.assertEqual(attrs, ["hello"])

    def test_literal_with_variant_unchanged(self):
        proj = self._proj()
        attrs = builder._expand_build_spec(proj, "hello@release")
        self.assertEqual(attrs, ["hello-release"])


class TestGlob(unittest.TestCase):
    """run_tests treats each filter entry as an fnmatch pattern."""

    def _proj(self) -> Project:
        return Project(
            name="p", version="0.1.0", nixpkgs_ref="nixos-24.11",
            git_deps={}, root=Path("/tmp"),
            targets={
                "unit_a":  Target(name="unit_a", kind="test", script="exit 0", sandbox=False),
                "unit_b":  Target(name="unit_b", kind="test", script="exit 0", sandbox=False),
                "integ":   Target(name="integ",  kind="test", script="exit 0", sandbox=False),
            },
        )

    def test_glob_filter(self):
        with mock.patch("rigx.builder.run_script_target", return_value=0):
            results = builder.run_tests(self._proj(), filters=["unit_*"])
        self.assertEqual([n for n, _ in results], ["unit_a", "unit_b"])

    def test_literal_filter_still_exact(self):
        with mock.patch("rigx.builder.run_script_target", return_value=0):
            results = builder.run_tests(self._proj(), filters=["integ"])
        self.assertEqual([n for n, _ in results], ["integ"])

    def test_mixed_literal_and_glob(self):
        with mock.patch("rigx.builder.run_script_target", return_value=0):
            results = builder.run_tests(self._proj(), filters=["integ", "unit_a"])
        self.assertEqual(sorted(n for n, _ in results), ["integ", "unit_a"])

    def test_star_filter_matches_all(self):
        with mock.patch("rigx.builder.run_script_target", return_value=0):
            results = builder.run_tests(self._proj(), filters=["*"])
        self.assertEqual(
            sorted(n for n, _ in results), ["integ", "unit_a", "unit_b"]
        )


class BuildPerAttrIsolation(unittest.TestCase):
    """`builder.build` invokes `nix build` once per attr so a failure in
    one target doesn't cancel the others (the Nix daemon dedupes derivation
    builds across concurrent client calls). With `-j N`, the calls are
    dispatched through a `ThreadPoolExecutor` of size N."""

    def _proj(self) -> Project:
        return Project(
            name="p", version="0.1.0", nixpkgs_ref="nixos-24.11",
            git_deps={}, root=Path("/tmp/proj"),
            targets={
                "a": Target(name="a", kind="executable", sources=["m.cpp"]),
                "b": Target(name="b", kind="executable", sources=["m.cpp"]),
                "c": Target(name="c", kind="executable", sources=["m.cpp"]),
            },
        )

    def _run_build(self, jobs=None, attrs=("a", "b", "c"), exit_codes=None):
        proj = self._proj()
        # subprocess.run gets called once per attr; default = all succeed.
        codes = exit_codes or [0] * len(attrs)
        results_iter = iter(mock.Mock(returncode=rc) for rc in codes)
        with mock.patch("rigx.builder.write_flake"), \
             mock.patch("rigx.builder._nix_bin", return_value="/usr/bin/nix"), \
             mock.patch(
                 "rigx.builder.subprocess.run",
                 side_effect=lambda *a, **kw: next(results_iter),
             ) as run, \
             mock.patch("pathlib.Path.mkdir"):
            try:
                built = builder.build(proj, list(attrs), jobs=jobs)
            except BuildError as e:
                return run, [], e
        return run, built, None

    def test_one_invocation_per_attr(self):
        run, built, err = self._run_build()
        self.assertIsNone(err)
        self.assertEqual(run.call_count, 3)
        self.assertEqual(sorted(a for a, _ in built), ["a", "b", "c"])
        # Each call carries exactly one flake ref.
        for call in run.call_args_list:
            cmd = call.args[0]
            refs = [t for t in cmd if t.startswith("path:")]
            self.assertEqual(len(refs), 1)

    def test_partial_failure_does_not_cancel_others(self):
        # Middle attr fails; the others should still build, and the
        # BuildError summarizes only the failures.
        run, _, err = self._run_build(exit_codes=[0, 1, 0])
        self.assertEqual(run.call_count, 3)
        self.assertIsNotNone(err)
        msg = str(err)
        self.assertIn("1/3 target(s) failed", msg)
        self.assertIn("b (exit 1)", msg)
        self.assertIn("2 succeeded", msg)

    def test_jobs_dispatches_via_thread_pool(self):
        # With -j > 1, the same number of calls are made (one per attr);
        # we just dispatch via ThreadPoolExecutor. The end-to-end behavior
        # (per-attr `nix build`, isolated failures) is identical.
        run, built, err = self._run_build(jobs=4)
        self.assertIsNone(err)
        self.assertEqual(run.call_count, 3)
        self.assertEqual(sorted(a for a, _ in built), ["a", "b", "c"])


class RunTestsParallel(unittest.TestCase):
    """`run_tests` with `jobs > 1` runs `exclusive = true` tests sequentially
    first, then the remaining tests in a thread pool with output captured
    per-test."""

    def _proj_with(self, *test_specs: tuple[str, bool]) -> Project:
        # Host-side tests: `sandbox=False` so the parallel/sequential
        # ThreadPoolExecutor + `run_script_target` path is exercised.
        targets = {}
        for name, exclusive in test_specs:
            targets[name] = Target(
                name=name, kind="test", script=f"echo {name}",
                sandbox=False, exclusive=exclusive,
            )
        return Project(
            name="p", version="0.1.0", nixpkgs_ref="nixos-24.11",
            git_deps={}, root=Path("/tmp"), targets=targets,
        )

    def test_jobs_le_1_runs_sequential(self):
        proj = self._proj_with(("a", False), ("b", False))
        with mock.patch("rigx.builder.run_script_target", return_value=0) as r:
            results = builder.run_tests(proj, jobs=1)
        # Sequential path uses the streaming runner — no capture variant.
        self.assertEqual(r.call_count, 2)
        self.assertEqual([n for n, _ in results], ["a", "b"])

    def test_exclusive_runs_before_parallel(self):
        proj = self._proj_with(("ex", True), ("p1", False), ("p2", False))
        call_order: list[str] = []
        def stream(_p, t):
            call_order.append(("stream", t.name))
            return 0
        def capture(_p, t):
            call_order.append(("capture", t.name))
            return 0, ""
        with mock.patch("rigx.builder.run_script_target", side_effect=stream), \
             mock.patch("rigx.builder._run_test_capture", side_effect=capture):
            results = builder.run_tests(proj, jobs=4)
        # First call must be the exclusive (streaming).
        self.assertEqual(call_order[0], ("stream", "ex"))
        # The remaining two are the parallel pair (captured).
        captured_names = sorted(n for kind, n in call_order[1:] if kind == "capture")
        self.assertEqual(captured_names, ["p1", "p2"])
        self.assertEqual(sorted(n for n, _ in results), ["ex", "p1", "p2"])

    def test_only_exclusives_use_streaming(self):
        proj = self._proj_with(("a", True), ("b", True))
        with mock.patch("rigx.builder.run_script_target", return_value=0) as stream, \
             mock.patch("rigx.builder._run_test_capture") as capture:
            builder.run_tests(proj, jobs=4)
        self.assertEqual(stream.call_count, 2)
        capture.assert_not_called()

    def test_single_test_runs_sequential_even_with_jobs(self):
        # No point spinning up a pool for one test.
        proj = self._proj_with(("solo", False))
        with mock.patch("rigx.builder.run_script_target", return_value=0) as r, \
             mock.patch("rigx.builder._run_test_capture") as cap:
            builder.run_tests(proj, jobs=8)
        r.assert_called_once()
        cap.assert_not_called()

    def test_parallel_returns_all_results_with_exit_codes(self):
        proj = self._proj_with(("p1", False), ("p2", False))
        with mock.patch(
            "rigx.builder._run_test_capture",
            side_effect=[(0, "out1"), (1, "out2")],
        ):
            results = builder.run_tests(proj, jobs=2)
        # Order is completion-order (mock returns deterministically); verify
        # the contents irrespective of ordering.
        codes = dict(results)
        self.assertEqual(codes, {"p1": 0, "p2": 1})


class SandboxedTests(unittest.TestCase):
    """`kind = "test"` defaults to `sandbox = true` — runs as its own Nix
    derivation via `nix build`, automatically cached. `sandbox = false`
    flips it to host-side execution."""

    def _proj(self, **targets) -> Project:
        return Project(
            name="p", version="0.1.0", nixpkgs_ref="nixos-24.11",
            git_deps={}, root=Path("/tmp/proj"), targets=targets,
        )

    def test_sandboxed_test_excluded_from_all_attrs(self):
        proj = self._proj(
            built=Target(name="built", kind="executable", sources=["m.cpp"]),
            verify=Target(name="verify", kind="test", script="exit 0"),
        )
        attrs = builder._all_attrs(proj)
        self.assertIn("built", attrs)
        self.assertNotIn("verify", attrs)

    def test_build_rejects_test_with_pointer_to_rigx_test(self):
        proj = self._proj(
            verify=Target(name="verify", kind="test", script="exit 0"),
        )
        with self.assertRaisesRegex(BuildError, "use `rigx test verify`"):
            builder._expand_build_spec(proj, "verify")

    def test_sandboxed_dispatches_via_nix_build(self):
        proj = self._proj(
            verify=Target(name="verify", kind="test", script="exit 0"),
        )
        with mock.patch("rigx.builder.write_flake"), \
             mock.patch("rigx.builder._nix_bin", return_value="/usr/bin/nix"), \
             mock.patch("rigx.builder.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0)
            results = builder.run_tests(proj)
        self.assertEqual(results, [("verify", 0)])
        cmd = run.call_args.args[0]
        self.assertIn("build", cmd)
        self.assertTrue(any(c.endswith("#verify") for c in cmd))
        self.assertIn("--no-link", cmd)

    def test_sandbox_false_dispatches_via_nix_shell(self):
        # When sandbox=false, the host-side path is used — `run_script_target`
        # is invoked, NOT `nix build`.
        proj = self._proj(
            host_t=Target(
                name="host_t", kind="test", script="exit 0", sandbox=False,
            ),
        )
        with mock.patch("rigx.builder.run_script_target", return_value=0) as r, \
             mock.patch("rigx.builder.subprocess.run") as nix_run:
            results = builder.run_tests(proj)
        r.assert_called_once()
        nix_run.assert_not_called()
        self.assertEqual(results, [("host_t", 0)])

    def test_mixed_sandboxed_and_host(self):
        proj = self._proj(
            sand=Target(name="sand", kind="test", script="exit 0"),  # default sandbox=True
            host=Target(name="host", kind="test", script="exit 0", sandbox=False),
        )
        with mock.patch("rigx.builder.write_flake"), \
             mock.patch("rigx.builder._nix_bin", return_value="/usr/bin/nix"), \
             mock.patch(
                 "rigx.builder.subprocess.run",
                 return_value=mock.Mock(returncode=0),
             ), \
             mock.patch("rigx.builder.run_script_target", return_value=0):
            results = dict(builder.run_tests(proj))
        self.assertEqual(results, {"sand": 0, "host": 0})

    def test_sandboxed_failure_propagates(self):
        proj = self._proj(
            verify=Target(name="verify", kind="test", script="exit 1"),
        )
        with mock.patch("rigx.builder.write_flake"), \
             mock.patch("rigx.builder._nix_bin", return_value="/usr/bin/nix"), \
             mock.patch(
                 "rigx.builder.subprocess.run",
                 return_value=mock.Mock(returncode=1),
             ):
            results = builder.run_tests(proj)
        self.assertEqual(results, [("verify", 1)])


class HintCommitGenerated(unittest.TestCase):
    """The reminder is opt-in by environment: only inside a git work-tree,
    only on stderr, never staged. We mock the git probe so the test stays
    fast and platform-independent."""

    def _proj(self) -> Project:
        return Project(
            name="p", version="0.1.0", nixpkgs_ref="nixos-24.11",
            git_deps={}, targets={}, root=Path("/tmp"),
        )

    def test_silent_outside_git(self):
        import io
        with mock.patch("rigx.builder._is_git_work_tree", return_value=False), \
             mock.patch("rigx.builder.sys.stderr", new=io.StringIO()) as err:
            builder._hint_commit_generated(self._proj(), ["flake.nix"])
        self.assertEqual(err.getvalue(), "")

    def test_prints_inside_git(self):
        import io
        with mock.patch("rigx.builder._is_git_work_tree", return_value=True), \
             mock.patch("rigx.builder.sys.stderr", new=io.StringIO()) as err:
            builder._hint_commit_generated(self._proj(), ["flake.nix", "flake.lock"])
        out = err.getvalue()
        self.assertIn("regenerated flake.nix flake.lock", out)
        self.assertIn("commit when stable", out)


if __name__ == "__main__":
    unittest.main()
