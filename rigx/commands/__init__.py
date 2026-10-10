"""Per-command implementations for the rigx CLI.

`rigx.cli` does argparse wiring; each `cmd_<name>` function here
handles one subcommand's logic. Shared helpers (error reporting, hint
and byte formatting) live in `commands.helpers`; project loading is
`rigx.cli._load`, so tests can patch it in one place.
"""
