"""SSH/single-host deployment backend for testbeds.

See the project design memory for the full design (plan-as-derivation,
`nix copy` transfer, systemd-generation activation/rollback). This
package currently implements only the bootstrap step
(`rigx.deploy.bootstrap`); plan compilation, transfer, and activation
are not implemented yet.
"""
