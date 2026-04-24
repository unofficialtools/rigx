import pathlib
import sys

# Put repo root on sys.path so tests can `import rigx` when invoked via
# `python -m unittest discover tests` without needing an installed package.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
