"""Test entry point that works with Designer's embedded Python.

The interpreter Alteryx ships is an *embedded* distribution: it has no pip, no
site-packages of its own and ignores PYTHONPATH because of its ``._pth`` file.
So rather than expecting a configured environment, this script puts ``src`` on
``sys.path`` itself and runs the suite.

Usage (from the project root)::

    "C:\\Program Files\\Alteryx\\bin\\Python\\python-3.13.11-embed-amd64\\python.exe" ^
        tests\\run_tests.py

Any other CPython 3.10+ works too; the tests do not import neonize.
"""

from __future__ import annotations

import os
import site
import sys
import unittest
from pathlib import Path

# Do not leave .pyc files behind.
#
# A .pyc records the absolute path it was compiled from, so running the tests
# scatters the build account's username and directory layout through
# src/**/__pycache__. .gitignore covers those files, but only once the project
# is a git repository - publishing the folder any other way (a zip, a drag into
# a web upload) would ship them. Never creating them is one less thing to
# remember before a release.
sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parent.parent


def add_dependencies() -> str:
    """Put pyarrow and the Alteryx SDK on the path, if they can be found.

    The plugin tests need them; the engine tests do not and are written to run
    without them. Three sources are tried, newest-wins, so the suite works
    whether you have just built the package, have it installed in Designer, or
    are using an ordinary virtualenv.
    """
    override = os.environ.get("WHATSAPP_SITE_PACKAGES")
    candidates = [Path(override)] if override else []

    local = os.environ.get("LOCALAPPDATA")
    if local:
        # Wherever tools/build_yxi.py staged its payload.
        candidates.append(
            Path(local) / "Alteryx" / "WhatsAppConnector-build" / "_stage"
        )
    appdata = os.environ.get("APPDATA")
    if appdata:
        tools = Path(appdata) / "Alteryx" / "Tools"
        candidates.extend(sorted(tools.glob("WhatsAppInput_*/site-packages"), reverse=True))

    for candidate in candidates:
        if candidate.is_dir():
            site.addsitedir(str(candidate))
            return str(candidate)
    return ""


# The connector's own source comes first, so tests always exercise the working
# copy rather than a stale build.
dependencies = add_dependencies()
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "src"))


def main() -> int:
    print(f"source:       {ROOT / 'src'}")
    print(f"dependencies: {dependencies or '(none found - plugin tests will skip)'}")
    print()
    loader = unittest.TestLoader()
    suite = loader.discover(str(ROOT / "tests"), pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
