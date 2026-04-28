"""Interactive testbed: writer + viewer cooperating through a shared volume.

Run with:

    rigx build note_writer note_viewer
    rigx run notes_demo

Then open http://localhost:8000 in a browser; refresh to watch new
lines appear in `log`. Press Enter in the terminal to tear down.

The scenario exercises three v0.4 features at once:

1. `kind = "capsule"` with declarative `volumes = [{ host, container, mode }]`
   so the runners bake `-v <host>:<container>:<mode>` defaults.
2. `Network.shared_volume()` so the testbed allocates a single tempdir,
   passes it to both capsules at the same container path, and cleans it
   up on exit.
3. `Network.declare(expose=[("0.0.0.0", 8000)])` so the viewer is
   reachable to a browser on the host's external interface (the
   testbed's loopback alias would only be reachable from inside the
   subnet).
"""

from rigx.capsule import start
from rigx.testbed import Network


def main() -> None:
    with Network() as net:
        # One shared tempdir, attached to both capsules at /shared.
        # The writer mounts rw; the viewer mounts ro so accidental
        # writes from the HTTP server can't corrupt the log.
        notes = net.shared_volume("notes")
        net.declare(
            "note_writer",
            volumes={notes: ("/shared", "rw")},
        )
        net.declare(
            "note_viewer",
            listens_on=[8000],
            volumes={notes: ("/shared", "ro")},
            expose=[("0.0.0.0", 8000)],
        )

        with start("note_writer", **net.bindings("note_writer")) as wr, \
             start("note_viewer", **net.bindings("note_viewer")) as vw:
            vw.wait_for_port(8000, timeout=15)
            print()
            print("note_writer is appending a tick to /shared/log every second.")
            print("note_viewer is serving /shared at http://localhost:8000")
            print("press Enter to tear down…")
            try:
                input()
            except (EOFError, KeyboardInterrupt):
                pass


if __name__ == "__main__":
    main()
