"""Import every module under support_agent/, scripts/ and tests/ and print the ones that fail.

    python -X utf8 scripts/check_imports.py

Prints nothing and exits 0 when every module imports; otherwise prints one line per failure
("module: ExceptionType: first line of the message") and exits 1. __main__ modules are skipped.
tests/ is also put on sys.path, as `unittest discover -s tests` does, because some test modules import
their siblings by bare name.
Modules are imported in sorted order in this one process; only module top level runs (entry points
are guarded by __name__ == '__main__'), so no test, replay or API call is executed.
"""
from __future__ import annotations

import argparse
import importlib
from pathlib import Path
import sys

PROJECT = Path(__file__).resolve().parents[1]
PACKAGES = ("support_agent", "scripts", "tests")


def module_names(root=PROJECT, packages=PACKAGES):
    names = []
    for package in packages:
        for path in sorted((Path(root) / package).rglob("*.py")):
            parts = path.relative_to(root).with_suffix("").parts
            if "__pycache__" in parts or parts[-1] == "__main__":
                continue
            names.append(".".join(parts[:-1] if parts[-1] == "__init__" else parts))
    return names


def failures(names):
    found = []
    for name in names:
        try:
            importlib.import_module(name)
        except (Exception, SystemExit) as error:
            message = str(error).splitlines()[0][:200] if str(error) else ""
            found.append(f"{name}: {type(error).__name__}: {message}")
    return found


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=PROJECT, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    for folder in (root / "tests", root):
        if str(folder) not in sys.path:
            sys.path.insert(0, str(folder))
    found = failures(module_names(root))
    for line in found:
        print(line)
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
