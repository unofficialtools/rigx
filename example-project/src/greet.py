import sys

from rich.console import Console
from rich.panel import Panel


def main() -> int:
    who = sys.argv[1] if len(sys.argv) > 1 else "world"
    console = Console()
    console.print(Panel.fit(f"Hello, [bold cyan]{who}[/]!", title="from Python"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
