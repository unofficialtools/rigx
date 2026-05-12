"""Per-command implementations for the rigx CLI.

`rigx.cli` does argparse wiring; each `cmd_<name>` function here
handles one subcommand's logic. Shared helpers (`_load`,
`_report_build_error`, hint formatting, byte formatting) live in
`commands.helpers`.
"""
