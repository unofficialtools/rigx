"""Tests for pinning nixpkgs by commit: `[nixpkgs] rev` and `rigx lock --pin`."""

import json
import os
import tempfile
import tomllib
import unittest
from pathlib import Path
from textwrap import dedent
from unittest import mock

from rigx import builder, config, nix_gen
from rigx.builder import BuildError
from rigx.config import ConfigError, Project
from rigx.nixpkgs_pin import PinError, set_rev

REV = "50ab793786d9de88ee30ec4e4c24fb4236fc2674"
OLD = "0" * 40


def _project(root: Path, rev: str | None = None) -> Project:
    return Project(
        name="p", version="0.1.0", nixpkgs_ref="nixos-24.11", nixpkgs_rev=rev,
        git_deps={}, targets={}, root=root,
    )


class SetRev(unittest.TestCase):
    def test_adds_rev_after_ref_and_keeps_comments(self):
        text = dedent("""\
            # my project
            [project]
            name = "p"  # keep me

            [nixpkgs]
            ref = "nixos-24.11"
            # trailing comment

            [targets.a]
            kind = "executable"
        """)
        new = set_rev(text, REV, "nixos-24.11", note="nixos-24.11 on 2026-10-09")
        self.assertIn(
            f'ref = "nixos-24.11"\nrev = "{REV}"  # nixos-24.11 on 2026-10-09\n',
            new,
        )
        self.assertIn('name = "p"  # keep me', new)
        self.assertIn("# trailing comment", new)

    def test_replaces_an_existing_rev(self):
        text = f'[nixpkgs]\nref = "nixos-24.11"\nrev = "{OLD}"  # old\n'
        new = set_rev(text, REV, "nixos-24.11")
        self.assertEqual(new, f'[nixpkgs]\nref = "nixos-24.11"\nrev = "{REV}"\n')

    def test_adds_a_table_when_there_is_none(self):
        text = '[project]\nname = "p"'
        new = set_rev(text, REV, "nixos-24.11")
        self.assertEqual(
            tomllib.loads(new)["nixpkgs"], {"ref": "nixos-24.11", "rev": REV},
        )

    def test_header_without_ref(self):
        new = set_rev("[nixpkgs]\n[project]\nname = 'p'\n", REV, "nixos-24.11")
        self.assertEqual(tomllib.loads(new)["nixpkgs"], {"rev": REV})

    def test_a_rev_in_another_table_is_left_alone(self):
        text = f'[nixpkgs]\nref = "x"\n\n[dependencies.git.lib]\nrev = "{OLD}"\n'
        new = set_rev(text, REV, "x")
        data = tomllib.loads(new)
        self.assertEqual(data["dependencies"]["git"]["lib"]["rev"], OLD)
        self.assertEqual(data["nixpkgs"]["rev"], REV)

    def test_dotted_form_is_refused(self):
        with self.assertRaises(PinError):
            set_rev('nixpkgs.ref = "nixos-24.11"\n', REV, "nixos-24.11")

    def test_an_edit_that_changes_other_tables_is_refused(self):
        # The [nixpkgs] header sits inside a multi-line string, so a line
        # edit would land in the string; the round-trip check catches it.
        text = 's = """\n[nixpkgs]\nref = "x"\n"""\n'
        with self.assertRaises(PinError):
            set_rev(text, REV, "x")


class ConfigRev(unittest.TestCase):
    def _load(self, nixpkgs_body: str) -> Project:
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "rigx.toml").write_text(
                '[project]\nname = "p"\n\n[nixpkgs]\n' + dedent(nixpkgs_body)
            )
            return config.load(Path(d))

    def test_rev_is_loaded(self):
        proj = self._load(f'ref = "nixos-24.11"\nrev = "{REV}"\n')
        self.assertEqual(proj.nixpkgs_rev, REV)

    def test_rev_defaults_to_none(self):
        self.assertIsNone(self._load('ref = "nixos-24.11"\n').nixpkgs_rev)

    def test_short_or_bad_rev_is_rejected(self):
        for bad in ('"50ab793"', f'"{REV.upper()}"', "42"):
            with self.subTest(bad=bad), self.assertRaises(ConfigError):
                self._load(f"rev = {bad}\n")

    def test_unknown_key_is_rejected(self):
        with self.assertRaises(ConfigError):
            self._load('ref = "nixos-24.11"\nrevision = "x"\n')


class RevIsUsed(unittest.TestCase):
    def test_flake_input_is_the_rev(self):
        out = nix_gen.generate(_project(Path("/tmp"), REV))
        self.assertIn(f'nixpkgs.url = "github:NixOS/nixpkgs/{REV}";', out)

    def test_flake_input_is_the_ref_without_a_rev(self):
        out = nix_gen.generate(_project(Path("/tmp")))
        self.assertIn('nixpkgs.url = "github:NixOS/nixpkgs/nixos-24.11";', out)

    def test_host_tools_use_the_rev_even_without_a_lock(self):
        with tempfile.TemporaryDirectory() as d:
            ref = builder._nixpkgs_pkg_ref(_project(Path(d), REV), "jq")
        self.assertEqual(ref, f"github:NixOS/nixpkgs/{REV}#jq")

    def test_offline_hint_drops_the_no_lock_warning_when_pinned(self):
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch("rigx.builder._network_down", return_value=True):
            hint = builder.network_hint(_project(Path(d), REV))
        self.assertNotIn("no flake.lock", hint)


class PinNixpkgs(unittest.TestCase):
    def test_writes_the_resolved_rev(self):
        meta = json.dumps({"locked": {"rev": REV, "type": "github"}})
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "rigx.toml").write_text(
                '[project]\nname = "p"\n\n[nixpkgs]\nref = "nixos-24.11"\n'
            )
            proj = _project(root)
            with mock.patch.dict(os.environ, {}, clear=True), \
                 mock.patch("rigx.builder._nix_bin", return_value="/usr/bin/nix"), \
                 mock.patch("rigx.builder.subprocess.run") as run:
                run.return_value = mock.Mock(returncode=0, stdout=meta)
                self.assertEqual(builder.pin_nixpkgs(proj), REV)
            cmd = run.call_args.args[0]
            self.assertIn("--refresh", cmd)
            self.assertEqual(cmd[-1], "github:NixOS/nixpkgs/nixos-24.11")
            self.assertEqual(proj.nixpkgs_rev, REV)
            self.assertEqual(config.load(root).nixpkgs_rev, REV)

    def test_refuses_offline(self):
        with mock.patch.dict(os.environ, {"RIGX_OFFLINE": "1"}):
            with self.assertRaises(BuildError):
                builder.pin_nixpkgs(_project(Path("/tmp")))

    def test_lock_pin_pins_then_relocks(self):
        from rigx import cli
        order = []
        with mock.patch("rigx.cli._load", return_value=_project(Path("/tmp"))), \
             mock.patch("rigx.builder.pin_nixpkgs",
                        side_effect=lambda p: order.append("pin") or REV), \
             mock.patch("rigx.builder.update_lock",
                        side_effect=lambda p: order.append("lock")), \
             mock.patch("sys.stdout"):
            self.assertEqual(cli.main(["lock", "--pin"]), 0)
        self.assertEqual(order, ["pin", "lock"])


if __name__ == "__main__":
    unittest.main()
