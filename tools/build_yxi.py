"""Build the installable .yxi package.

Why this exists instead of ``ayx_plugin_cli create-yxi``: that CLI targets
Python 3.8 and a Miniconda workspace layout, which is two major versions behind
the interpreter Designer 2026.1 actually runs (3.13). Rather than fight it, this
script produces the same *output* - a directory layout copied from a known-good
Alteryx-published connector - using the interpreter the tools will really use.

    python tools/build_yxi.py                # full build
    python tools/build_yxi.py --no-deps      # reuse the cached dependency set
    python tools/build_yxi.py --keep-tests   # do not prune test suites

The result is ``dist/WhatsApp_<version>.yxi``, which a user installs by
double-clicking it with Designer closed.

Layout produced, mirroring Alteryx's own connectors::

    WhatsApp_1_0_0.yxi
      Config.xml                         package manifest Designer reads first
      icon.png
      WhatsAppInput_1_0_0/
        WhatsAppInput_1_0_0Config.xml    tool manifest: anchors, GUI, icon
        WhatsAppInputGui.html            configuration panel
        icon.png
        main.pyz                         bootstrap that starts the SDK service
        manifest.json                    which package/class to load
        site-packages/                   every dependency, vendored
      WhatsAppOutput_1_0_0/
        ...

Each tool carries its own copy of site-packages, matching Alteryx's own
connectors. docs/05-architecture.md covers why sharing one folder does not
work.
"""

from __future__ import annotations

import argparse
import compileall
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
UI = ROOT / "ui"
CONFIG = ROOT / "configuration"
#: Where the finished .yxi lands. Small, and worth keeping with the project.
DIST = ROOT / "dist"


def default_build_root() -> Path:
    """Scratch space for the build, kept outside the project.

    A build stages roughly 800 MB across two copies of site-packages. If that
    happens inside a synced folder - OneDrive, Dropbox, a network home drive -
    two bad things follow: the sync client uploads every intermediate file, and
    it holds locks on them while it does, which makes the next build fail with
    a permission error partway through a copy. Both were observed on the
    machine this was developed on.

    So intermediates go to a local scratch directory and only the finished
    package (a single file) is written back into the project.
    """
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "Alteryx" / "WhatsAppConnector-build"
    return Path(os.environ.get("TEMP", ".")) / "whatsapp-connector-build"


#: Set by main() so the helpers below can stay simple.
BUILD = default_build_root()
#: Dependencies are installed once into here and reused between builds, because
#: pulling ~300 MB of wheels for every rebuild makes iteration miserable.
DEPS_CACHE = BUILD / "_deps"
STAGE = BUILD / "_stage"


def set_build_root(path: Path) -> None:
    global BUILD, DEPS_CACHE, STAGE
    BUILD = path
    DEPS_CACHE = BUILD / "_deps"
    STAGE = BUILD / "_stage"


def rmtree(path: Path) -> None:
    """Delete a tree, clearing the read-only flag that trips Windows up.

    pip marks some vendored files read-only, and shutil.rmtree turns that into
    a PermissionError rather than handling it.
    """
    if not path.exists():
        return

    def on_error(func, target, _exc):
        try:
            os.chmod(target, 0o700)
            func(target)
        except OSError:
            pass

    shutil.rmtree(path, onexc=on_error)

#: Pinned so a build is reproducible. Bump with care, then re-test in
#: Designer - these lines decide whether the connector works at all.
AYX_SDK = "ayx-python-sdk==2.6.1"
NEONIZE = "neonize==0.5.2"

#: protobuf needs pinning explicitly because the two packages above disagree.
#:
#: The SDK requires exactly 6.33.5 and its generated code declares gencode
#: 6.33.5. neonize's generated code declares gencode 7.34.1 and refuses to load
#: on an older runtime ("runtime version cannot be older than the linked gencode
#: version"). pip honours the SDK's pin, so a default resolve produces a package
#: in which `import neonize` fails - which is exactly what the build's verify
#: step caught.
#:
#: protobuf's cross-version guarantee runs the other way: a runtime supports
#: gencode from its own major version and the one before it. So a 7.x runtime
#: serves both the SDK's 6.x gencode and neonize's 7.x gencode, and installing
#: it last (overriding the SDK's pin) is the resolution that satisfies
#: everything. The verify step re-imports both stacks to prove it.
PROTOBUF = "protobuf==7.34.1"

#: Shown in Designer's tool metadata and on the package card.
AUTHOR = "Alejandro Ferrandis del Valle"

PACKAGE_NAME = "WhatsApp"

TOOLS = (
    {
        "name": "WhatsAppInput",
        "class": "WhatsAppInput",
        "label": "WhatsApp Input",
        "gui": UI / "WhatsAppInput" / "WhatsAppInputGui.html",
        "config": CONFIG / "WhatsAppInput",
        "description": "Read messages from a linked WhatsApp account.",
        "inputs": (),
        "outputs": (
            ("Messages", "M", False),
            ("Chats", "C", True),
        ),
    },
    {
        "name": "WhatsAppOutput",
        "class": "WhatsAppOutput",
        "label": "WhatsApp Output",
        "gui": UI / "WhatsAppOutput" / "WhatsAppOutputGui.html",
        "config": CONFIG / "WhatsAppOutput",
        "description": "Send a WhatsApp message for every incoming row.",
        "inputs": (("Input", False),),
        "outputs": (("Results", "R", True),),
    },
)

# --------------------------------------------------------------------------
# pruning
# --------------------------------------------------------------------------

#: Directories that exist only to build or test a package. Removing them cuts
#: the payload roughly in half and cannot affect runtime behaviour.
PRUNE_DIRS = (
    "tests", "test", "testing", "__pycache__",
    "pyarrow/include", "pyarrow/tests", "pyarrow/src",
    "pandas/tests",
    "numpy/tests", "numpy/_core/include", "numpy/_core/lib", "numpy/f2py",
    # Optional phonenumbers datasets - 42 MB of them. neonize only parses and
    # formats numbers; it never asks which region, carrier or timezone a number
    # belongs to. Note that `shortdata` and `data` are NOT optional: the package
    # __init__ imports shortnumberinfo eagerly, so removing them breaks
    # `import phonenumbers` outright.
    "phonenumbers/geodata", "phonenumbers/carrierdata", "phonenumbers/timezonedata",
    # The SDK ships its own Sphinx manual and example plugins - 31 MB per tool,
    # 62 MB across the package. Nothing imports them, and they document a
    # different product than the one being installed.
    "ayx_python_sdk/docs", "ayx_python_sdk/examples",
)

#: The thin wrappers over the datasets above. Deleted alongside their data so
#: no module is left that imports something that is no longer there.
PRUNE_FILES = (
    "phonenumbers/geocoder.py", "phonenumbers/carrier.py", "phonenumbers/timezone.py",
)

PRUNE_GLOBS = ("*.pyi", "*.c", "*.h", "*.pyx", "*.pxd", "*.a", "*.lib", "*.exp")

#: Directories removed from the TOP of site-packages only, never nested ones.
#:
#: `bin` holds pip's console-script launchers (tqdm.exe, httpx.exe and friends).
#: Nothing here ever runs them - main.pyz imports the SDK entry point directly -
#: and each one embeds the ABSOLUTE PATH OF THE BUILD MACHINE'S INTERPRETER in a
#: plain-text shebang. That path leaks the build account's username and whatever
#: directory the build venv happened to live in, into every copy a customer
#: receives. They are dead weight and an information leak, so they go.
PRUNE_ROOT_DIRS = ("bin", "Scripts", "share")

#: Never prune inside these, whatever the rules above say. neonize's .pyi files
#: are irrelevant but its proto package must stay whole, and the SDK ships type
#: stubs that some of its own code imports.
PRUNE_EXEMPT = ("neonize/proto",)


#: Replacement for neonize/download.py in the packaged build.
#:
#: Stock neonize fetches its Go core (neonize-windows-amd64.dll) from GitHub
#: whenever the bundled copy is missing or reports an unexpected version. The
#: wheel already contains the right DLL, so that path should never run - but
#: "should never" is not "cannot", and an antivirus quarantine or a partial
#: install would silently turn this connector into something that reaches out
#: to the internet on first use. This product ships everything it needs, so the
#: fallback is replaced with a clear error instead.
OFFLINE_DOWNLOAD_STUB = '''\
"""Offline replacement for neonize's runtime downloader.

The WhatsApp connector for Alteryx bundles neonize's native core inside the
.yxi, so there is never anything to fetch. Upstream neonize would download it
from GitHub on demand; that is disabled here so the connector cannot make an
unexpected outbound request, and so a damaged installation fails loudly instead
of quietly repairing itself from the internet.

Patched at build time by tools/build_yxi.py. Do not edit in place.
"""

import os

__GONEONIZE_VERSION__ = "{version}"
__GIT_RELEASE_URL__ = "https://github.com/krypton-byte/neonize"


class UnsupportedPlatform(Exception):
    pass


def download():
    """Never downloads. Raises with an actionable message instead."""
    here = os.path.dirname(__file__)
    raise RuntimeError(
        "The WhatsApp connector's native library is missing from "
        f"{{here}}.\\n"
        "This connector ships every component it needs and never downloads "
        "anything, so this means the installation is damaged - most often "
        "because antivirus quarantined the file.\\n"
        "Fix: reinstall the connector from the .yxi, and allow-list the Alteryx "
        "tools directory in your security software."
    )
'''


def patch_offline(target: Path) -> None:
    """Replace neonize's downloader and confirm the native core is present."""
    dll = target / "neonize" / "neonize-windows-amd64.dll"
    if not dll.is_file():
        raise SystemExit(
            f"The neonize wheel did not provide {dll.name}. The package would try "
            "to download it at runtime, which this build does not allow."
        )

    downloader = target / "neonize" / "download.py"
    if not downloader.is_file():
        raise SystemExit(f"Expected to find {downloader} to patch.")
    downloader.write_text(
        OFFLINE_DOWNLOAD_STUB.format(version=NEONIZE.split("==")[-1]), encoding="utf-8"
    )
    log(f"offline guard applied; native core bundled ({dll.stat().st_size / 1048576:.0f} MB)")


def drop_superseded_metadata(target: Path) -> None:
    """Remove dist-info left behind by an overridden package.

    protobuf is installed twice on purpose (see PROTOBUF): the SDK's pin first,
    then the newer runtime over the top. ``--target`` overwrites the modules but
    leaves the older ``.dist-info`` in place, so the package ends up claiming a
    version it does not contain - ``importlib.metadata.version('protobuf')``
    answers 6.33.5 while the code is 7.34.1. Anything gating on the metadata
    reads the wrong answer.
    """
    kept = PROTOBUF.split("==")[-1]
    for info in sorted(target.glob("protobuf-*.dist-info")):
        if not info.name.startswith(f"protobuf-{kept}"):
            rmtree(info)
            log(f"removed stale metadata {info.name}")


def version() -> str:
    text = (SRC / "whatsapp_core" / "version.py").read_text(encoding="utf-8")
    match = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    if not match:
        raise SystemExit("Could not read __version__ from whatsapp_core/version.py")
    return match.group(1)


def sdk_version() -> str:
    """The ayx-python-sdk version actually installed, for the tool Config.xml."""
    for item in DEPS_CACHE.glob("ayx_python_sdk-*.dist-info"):
        name = item.name.replace("ayx_python_sdk-", "").replace(".dist-info", "")
        return name.split("+")[0]
    return AYX_SDK.split("==")[-1]


def log(message: str) -> None:
    print(f"[build] {message}", flush=True)


def install_dependencies(force: bool) -> None:
    if DEPS_CACHE.exists() and not force:
        log(f"reusing cached dependencies in {DEPS_CACHE}")
        return
    rmtree(DEPS_CACHE)
    DEPS_CACHE.mkdir(parents=True, exist_ok=True)
    log(f"installing {AYX_SDK} and {NEONIZE} (this downloads ~300 MB once)")
    subprocess.run(
        [
            sys.executable, "-m", "pip", "install",
            "--no-warn-script-location", "--disable-pip-version-check",
            "--target", str(DEPS_CACHE),
            AYX_SDK, NEONIZE,
        ],
        check=True,
    )

    # Second pass, kept separate: pip would otherwise refuse the
    # combination because the SDK pins protobuf==6.33.5. See the PROTOBUF
    # comment for why the newer runtime is the correct answer rather than a
    # workaround.
    log(f"overriding {PROTOBUF} to satisfy both generated-code versions")
    subprocess.run(
        [
            sys.executable, "-m", "pip", "install",
            "--no-warn-script-location", "--disable-pip-version-check",
            "--no-deps", "--upgrade",
            "--target", str(DEPS_CACHE),
            PROTOBUF,
        ],
        check=True,
    )


def prune(target: Path) -> None:
    """Delete build-time and test-only files from a staged site-packages."""
    before = folder_size(target)

    for relative in PRUNE_DIRS:
        if "/" in relative:
            candidates = [target / relative]
        else:
            candidates = [p for p in target.rglob(relative) if p.is_dir()]
        for path in candidates:
            if path.is_dir() and not _exempt(path, target):
                shutil.rmtree(path, ignore_errors=True)

    for relative in PRUNE_ROOT_DIRS:
        rmtree(target / relative)

    for relative in PRUNE_FILES:
        (target / relative).unlink(missing_ok=True)

    for pattern in PRUNE_GLOBS:
        for path in target.rglob(pattern):
            if path.is_file() and not _exempt(path, target):
                path.unlink(missing_ok=True)

    after = folder_size(target)
    log(f"pruned {(before - after) / 1048576:.0f} MB -> {after / 1048576:.0f} MB")


def _exempt(path: Path, root: Path) -> bool:
    relative = path.relative_to(root).as_posix()
    return any(relative.startswith(prefix) for prefix in PRUNE_EXEMPT)


def folder_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def stage_site_packages(keep_tests: bool) -> Path:
    """Assemble the one site-packages tree every tool will get a copy of."""
    rmtree(STAGE)
    STAGE.mkdir(parents=True, exist_ok=True)

    log("staging dependencies")
    shutil.copytree(DEPS_CACHE, STAGE, dirs_exist_ok=True)

    log("staging connector source")
    for package in ("whatsapp_core", "ayx_plugins"):
        shutil.copytree(
            SRC / package, STAGE / package,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )

    patch_offline(STAGE)
    drop_superseded_metadata(STAGE)

    if not keep_tests:
        prune(STAGE)

    # Byte-compiling here means Designer never pays the compile cost on the
    # first run of a workflow, and never needs write access to the tools folder.
    #
    # stripdir/prependdir rewrite the source path baked into every .pyc. Without
    # them each of the ~4,200 compiled files records the absolute path it was
    # compiled from - exposing the build account's username to anyone who opens
    # the bytecode, and printing it in any traceback the tool shows at runtime.
    # The rewritten path is also simply more useful: "site-packages/x/y.py"
    # rather than a directory that exists on nobody else's machine.
    log("byte-compiling with neutral source paths")
    compileall.compile_dir(
        str(STAGE), quiet=2, force=True, workers=0,
        stripdir=str(STAGE), prependdir="site-packages",
    )
    return STAGE


#: Import-and-exercise check run against the staged payload before packaging.
#:
#: Pruning is the one build step that can produce a package which looks perfect
#: and fails on a customer's machine - deleting a directory that turns out to be
#: imported eagerly breaks the tool with a ModuleNotFoundError nobody sees until
#: Designer runs it. So the build refuses to package a tree it cannot import.
VERIFY_SCRIPT = '''\
import site, sys
site.addsitedir(sys.argv[1])

import pyarrow, pandas, numpy                                   # noqa: F401
import google.protobuf
print("protobuf runtime", google.protobuf.__version__)

from ayx_python_sdk.core import PluginV2, Field, FieldType, Metadata   # noqa: F401
from ayx_python_sdk.providers.amp_provider.__main__ import start_sdk_tool_service  # noqa: F401

# Load the SDK's own generated protobuf modules. The build overrides the
# protobuf version the SDK pins, so proving the SDK's 6.x gencode still loads on
# the newer runtime is the whole point of that override - and a plain
# `import ayx_python_sdk` does not reach these modules.
import importlib, pkgutil
import ayx_python_sdk.providers.amp_provider.resources.generated as _generated
_loaded = 0
for _finder, _name, _ispkg in pkgutil.iter_modules(_generated.__path__):
    if _name.endswith("_pb2") or _name.endswith("_pb2_grpc"):
        importlib.import_module(f"{_generated.__name__}.{_name}")
        _loaded += 1
assert _loaded, "no generated SDK protobuf modules were found to verify"
print(f"SDK generated protobuf modules loaded: {_loaded}")

# The native WhatsApp core and everything it pulls in at import time. This is
# the check that catches an over-eager prune of phonenumbers or protobuf.
import neonize                                                   # noqa: F401
from neonize.client import NewClient                             # noqa: F401
from neonize.utils import build_jid                              # noqa: F401
from neonize.utils import enum as _enum
from neonize import events as _events
assert _enum.ClientName.WINDOWS and _enum.ClientType.CHROME
assert _events.MessageEv and _events.OfflineSyncCompletedEv

import ayx_plugins
assert ayx_plugins.WhatsAppInput and ayx_plugins.WhatsAppOutput

# Construct both plugins against a provider that behaves like the real one.
#
# Importing the classes is not enough: a plugin can import perfectly and still
# fail in Designer the moment it is dropped on a canvas. That is exactly what
# happened with push_outgoing_metadata, which wants a pyarrow.Schema and calls
# .serialize() on it - passing the SDK's Metadata object imported fine and died
# at runtime with "'Metadata' object has no attribute 'serialize'".
#
# So the fake does what the provider does, rather than accepting anything.
import tempfile

class _IO:
    def info(self, m): pass
    def warn(self, m): pass
    def error(self, m): raise AssertionError(f"plugin reported an error: {m}")
    def update_progress(self, f): pass
    def translate_msg(self, m, *a): return m

class _Env:
    update_only = False
    designer_version = "2026.1"

class _Provider:
    def __init__(self, config):
        self.tool_config = config
        self.io = _IO()
        self.environment = _Env()
        self.anchors = {}
    def push_outgoing_metadata(self, anchor, metadata):
        metadata.serialize().to_pybytes()      # what the real provider does
        self.anchors[anchor] = metadata
    def write_to_anchor(self, anchor, table): pass

with tempfile.TemporaryDirectory() as _tmp:
    _in = ayx_plugins.WhatsAppInput(
        _Provider({"Profile": "verify", "DataDir": _tmp, "Mode": "ArchiveOnly"})
    )
    _out = ayx_plugins.WhatsAppOutput(
        _Provider({"Profile": "verify", "DataDir": _tmp, "ToSource": "Fixed",
                   "ToFixed": "+15550100", "MessageSource": "Fixed",
                   "MessageFixed": "x"})
    )
    assert set(_in.provider.anchors) == {"Messages", "Chats"}, _in.provider.anchors
    assert set(_out.provider.anchors) == {"Results"}, _out.provider.anchors
print("plugins construct and publish anchor metadata")

# Building each anchor's table proves the schema, the Alteryx field metadata
# and the Arrow type mapping all line up.
from ayx_plugins import _arrow
from whatsapp_core.schema import MESSAGE_COLUMNS, CHAT_COLUMNS, RESULT_COLUMNS
for columns in (MESSAGE_COLUMNS, CHAT_COLUMNS, RESULT_COLUMNS):
    table = _arrow.empty_table(columns)
    assert table.num_columns == len(columns)
    assert list(table.schema.names) == [c.name for c in columns]

from whatsapp_core import cli, runner, sender, messages, store, jid   # noqa: F401

# The offline guard must be in place: a packaged build may never reach out to
# GitHub for its native core.
import neonize.download as _dl
try:
    _dl.download()
except RuntimeError as exc:
    assert "never downloads anything" in str(exc), str(exc)
else:
    raise AssertionError("neonize.download.download() is not the offline stub")

import pathlib
_dll = pathlib.Path(neonize.__file__).parent / "neonize-windows-amd64.dll"
assert _dll.is_file(), f"native core missing at {_dll}"

print("verify: ok (offline guard active, native core bundled)")
'''


MAIN_PYZ = '''\
"""Bootstrap for an Alteryx Python SDK tool.

Designer launches this file with the tool package and class name as arguments.
Its only job is to put the bundled dependencies on the import path before
handing control to the SDK's tool service.

The .pyz extension is what Designer expects; the file is ordinary Python.
"""

import json
import os
import pathlib
import site
import sys

os.environ["PYTHONUNBUFFERED"] = "1"

# Force UTF-8 on the standard streams.
#
# WhatsApp content is full of emoji and accented characters, and the bundled
# protocol library logs chat names and message ids to stdout/stderr. Python
# picks the *locale* encoding for those streams, which on a Spanish or German
# Windows is cp1252 - and a single emoji in a group name then raises
# UnicodeEncodeError from inside a logging call, killing the tool for reasons
# that have nothing to do with the workflow. errors="replace" means an
# unprintable character degrades to "?" instead of failing.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass  # Not a reconfigurable stream; nothing to do.

HERE = pathlib.Path(__file__).parent.resolve()

# Everything the tool needs is vendored beside this file, so the connector is
# unaffected by whatever else is installed on the machine.
if (HERE / "site-packages").is_dir():
    site.addsitedir(str(HERE / "site-packages"))


if __name__ == "__main__":
    manifest_path = HERE / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"Could not read {manifest_path}: {exc}") from exc

    for extra in manifest.get("additional_pythonpaths", []):
        site.addsitedir(extra)

    tool_package = sys.argv[2]
    tool_name = sys.argv[3]

    from ayx_python_sdk.providers.amp_provider.__main__ import start_sdk_tool_service

    print(f"ListenPort: {os.getenv('TOOL_SERVICE_ADDRESS')}")
    start_sdk_tool_service(tool_package, tool_name, os.getenv("TOOL_SERVICE_ADDRESS"))
'''


def scan_for_build_traces(target: Path) -> None:
    """Refuse to package anything carrying traces of the build machine.

    Everything the build writes on purpose is easy to review. The dangerous
    material is what the *tools* write: pip bakes the absolute path of the
    building interpreter into every console-script launcher, and CPython bakes
    the absolute source path into every .pyc. Neither is visible in a diff, and
    both end up in the customer's copy.

    That is not a theoretical risk - an early build of this connector shipped 22
    launchers containing the build account's home directory, and 4,224 .pyc
    files containing its username. The prune and compile steps now prevent both,
    but a prevention nobody checks is one refactor away from silently lapsing.
    So the build looks for the evidence directly, and fails if it finds any.

    The markers are derived from the machine doing the building, so this works
    on any developer's machine without being told what to look for.
    """
    home = Path.home()
    markers: list[tuple[str, bytes]] = [
        ("build account's home directory", str(home)),
        ("build interpreter path", str(Path(sys.executable).parent)),
        ("scratch build directory", str(BUILD)),
        ("project directory", str(ROOT)),
    ]

    # Every marker is a *path*, never a bare name. A username on its own is far
    # too short to search for in binaries: a five-letter account name matched 19
    # vendored files, purely because those letters recur inside unrelated
    # identifiers such as IllegalExportName and ephemeralExpiration. False alarms of that kind are worse than no
    # check, because the habit they teach is to ignore the gate. A full path is
    # long enough that a coincidental match is not a realistic concern, and a
    # username only discloses anything when it appears *as part of a path*
    # anyway, which these markers cover.
    #
    # Case-insensitive, because Windows paths are.
    markers = [
        (label, value.lower().encode("utf-8"))
        for label, value in markers
        if len(value) > 12
    ]

    offenders: dict[str, list[str]] = {}
    scanned = 0
    for path in target.rglob("*"):
        if not path.is_file():
            continue
        scanned += 1
        try:
            blob = path.read_bytes().lower()
        except OSError:
            continue
        for label, value in markers:
            if value in blob:
                offenders.setdefault(label, []).append(
                    str(path.relative_to(target))
                )

    if offenders:
        lines = [
            "",
            "The staged payload contains traces of THIS MACHINE and has NOT been "
            "packaged.",
            "Shipping it would disclose the build account and its directories to "
            "every customer.",
            "",
        ]
        for label, files in offenders.items():
            lines.append(f"  {label}: {len(files)} file(s)")
            for name in sorted(files)[:5]:
                lines.append(f"      {name}")
            if len(files) > 5:
                lines.append(f"      ... and {len(files) - 5} more")
        lines += [
            "",
            "Usual causes: a directory pip populated that PRUNE_ROOT_DIRS does not "
            "remove (console-script launchers under bin/ embed the building "
            "interpreter's full path), or byte-compilation without stripdir/"
            "prependdir (every .pyc records the path it was compiled from).",
        ]
        raise SystemExit("\n".join(lines))

    log(f"no build-machine traces in {scanned} staged files")


def designer_python() -> Path | None:
    """Designer's embedded interpreter, if this machine has Designer installed.

    Verifying with the interpreter that will actually run the tools is worth
    more than verifying with the one doing the build, because they can differ
    in ways that matter (embedded distributions ignore PYTHONPATH, for one).
    """
    base = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Alteryx" / "bin" / "Python"
    if not base.is_dir():
        return None
    for candidate in sorted(base.glob("python-3.13*-embed-amd64/python.exe"), reverse=True):
        return candidate
    return None


def verify_stage(staged: Path) -> None:
    """Import the staged payload in a fresh interpreter; fail the build if it breaks."""
    script = BUILD / "_verify.py"
    script.write_text(VERIFY_SCRIPT, encoding="utf-8")

    interpreter = designer_python()
    log(
        f"verifying payload with {'Designer' if interpreter else 'the build'} "
        f"interpreter: {interpreter or sys.executable}"
    )
    result = subprocess.run(
        [str(interpreter or sys.executable), str(script), str(staged)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(result.stdout, file=sys.stderr)
        print(result.stderr, file=sys.stderr)
        raise SystemExit(
            "\nThe staged payload does not import cleanly, so it has NOT been "
            "packaged.\nThis usually means a prune rule in PRUNE_DIRS removed "
            "something that is imported at start-up."
        )
    log(result.stdout.strip() or "verify: ok")


def tool_config_xml(tool: dict, tool_version: str, sdk: str) -> str:
    inputs = "".join(
        f'\n\t\t\t<Connection Name="{name}" Type="Connection" '
        f'Optional="{"True" if optional else "False"}"/>'
        for name, optional in tool["inputs"]
    )
    input_block = (
        f"<InputConnections>{inputs}\n\t\t</InputConnections>"
        if inputs else "<InputConnections/>"
    )

    outputs = "".join(
        f'\n\t\t\t<Connection AllowMultiple="False" Label="{label}" Name="{name}" '
        f'Optional="{"True" if optional else "False"}" Type="Connection"/>'
        for name, label, optional in tool["outputs"]
    )

    return f"""<?xml version="1.0" encoding="utf-8"?>
<!-- Generated by tools/build_yxi.py. Edit that script; changes here are overwritten. -->
<AlteryxJavaScriptPlugin>
\t<EngineSettings EngineDll="SdkEnginePlugin.dll" EngineDllEntryPoint="SdkEnginePlugin" SDKVersion="10.1"/>
\t<SdkSettings Manifest="True" Language="Python">
\t\t<CustomParameters>
\t\t\t<Version Value="{sdk}"/>
\t\t\t<ToolVersion Value="{tool_version}"/>
\t\t</CustomParameters>
\t</SdkSettings>
\t<GuiSettings Html="{tool['gui'].name}" Icon="icon.png" SDKVersion="10.1">
\t\t{input_block}
\t\t<OutputConnections>{outputs}
\t\t</OutputConnections>
\t</GuiSettings>
\t<Properties>
\t\t<MetaInfo>
\t\t\t<Name>{tool['label']}</Name>
\t\t\t<RootToolName>{tool['label']}</RootToolName>
\t\t\t<Description>{tool['description']} (v{tool_version})</Description>
\t\t\t<ToolVersion>{tool_version}</ToolVersion>
\t\t\t<CategoryName>Connectors</CategoryName>
\t\t\t<SearchTags>whatsapp, chat, message, messaging, sms, notification, connector</SearchTags>
\t\t\t<Author>{AUTHOR}</Author>
\t\t\t<Company>{AUTHOR}</Company>
\t\t\t<Copyright>2026</Copyright>
\t\t</MetaInfo>
\t</Properties>
</AlteryxJavaScriptPlugin>
"""


def package_config_xml(tool_version: str) -> str:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<!-- Generated by tools/build_yxi.py. Edit that script; changes here are overwritten. -->
<AlteryxJavaScriptPlugin>
\t<EngineSettings EngineDLL="Python" SDKVersion="10.1"/>
\t<Properties>
\t\t<MetaInfo>
\t\t\t<Name>{PACKAGE_NAME}</Name>
\t\t\t<RootToolName>{PACKAGE_NAME}</RootToolName>
\t\t\t<Description>Read and send WhatsApp messages from an Alteryx workflow.</Description>
\t\t\t<CategoryName>Connectors</CategoryName>
\t\t\t<ToolVersion>{tool_version}</ToolVersion>
\t\t\t<Author>{AUTHOR}</Author>
\t\t\t<Company>{AUTHOR}</Company>
\t\t\t<Copyright>2026</Copyright>
\t\t\t<Icon>icon.png</Icon>
\t\t</MetaInfo>
\t</Properties>
</AlteryxJavaScriptPlugin>
"""


def build_tool(tool: dict, package_root: Path, staged: Path, tool_version: str, sdk: str) -> Path:
    folder_name = f"{tool['name']}_{tool_version.replace('.', '_')}"
    target = package_root / folder_name
    target.mkdir(parents=True, exist_ok=True)

    (target / f"{folder_name}Config.xml").write_text(
        tool_config_xml(tool, tool_version, sdk), encoding="utf-8"
    )
    (target / "main.pyz").write_text(MAIN_PYZ, encoding="utf-8")
    (target / "manifest.json").write_text(
        # Backslashes because Designer builds a Windows path from this verbatim.
        '{\n'
        '    "version": "3.13",\n'
        f'    "entry_point": "{folder_name}\\\\main.pyz",\n'
        '    "tool_package": "ayx_plugins",\n'
        f'    "tool_name": "{tool["class"]}",\n'
        '    "additional_pythonpaths": []\n'
        '}\n',
        encoding="utf-8",
    )
    shutil.copy2(tool["gui"], target / tool["gui"].name)
    # Both ship with every tool: MPL-2.0 requires the notice to travel with
    # the binary, and a licence nobody can find is no licence at all.
    for name in ("LICENSE", "THIRD-PARTY-NOTICES.md"):
        source = ROOT / name
        if source.is_file():
            shutil.copy2(source, target / name)
    shutil.copy2(tool["config"] / "icon.png", target / "icon.png")
    # The support CLI travels with the tool. The documentation makes it the
    # first troubleshooting step ("run doctor"), and the plugins point at it in
    # their own error messages, so a package without it sends every customer
    # down a path that dead-ends. It already resolves the installed tool's
    # site-packages at runtime, so it works from wherever it is copied to.
    shutil.copy2(ROOT / "tools" / "whatsapp.cmd", target / "whatsapp.cmd")

    log(f"copying site-packages into {folder_name}")
    shutil.copytree(staged, target / "site-packages", dirs_exist_ok=True)
    return target


def make_zip(package_root: Path, destination: Path) -> None:
    log(f"zipping {destination.name}")
    with zipfile.ZipFile(
        destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
    ) as archive:
        for path in sorted(package_root.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(package_root).as_posix())


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the WhatsApp connector .yxi")
    parser.add_argument("--no-deps", action="store_true",
                        help="Reuse the cached dependency download")
    parser.add_argument("--refresh-deps", action="store_true",
                        help="Force a fresh dependency download")
    parser.add_argument("--keep-tests", action="store_true",
                        help="Do not prune test suites and headers (larger package)")
    parser.add_argument("--skip-verify", action="store_true",
                        help="Package without importing the staged payload first "
                             "(not recommended)")
    parser.add_argument("--out", default="", help="Output path for the .yxi")
    parser.add_argument("--build-dir", default="",
                        help="Scratch directory for intermediates "
                             f"(default: {default_build_root()})")
    args = parser.parse_args()

    if args.build_dir:
        set_build_root(Path(args.build_dir).expanduser().resolve())

    tool_version = version()
    log(f"building {PACKAGE_NAME} {tool_version} with {sys.executable}")
    log(f"scratch directory: {BUILD}")

    if not args.no_deps or not DEPS_CACHE.exists():
        install_dependencies(force=args.refresh_deps)
    sdk = sdk_version()
    log(f"Alteryx SDK {sdk}")

    staged = stage_site_packages(keep_tests=args.keep_tests)
    # Privacy gate first: a payload that leaks the build machine must not be
    # packaged even if it imports perfectly.
    scan_for_build_traces(staged)
    if not args.skip_verify:
        verify_stage(staged)

    package_root = BUILD / f"{PACKAGE_NAME}_{tool_version.replace('.', '_')}"
    rmtree(package_root)
    package_root.mkdir(parents=True, exist_ok=True)

    (package_root / "Config.xml").write_text(package_config_xml(tool_version), encoding="utf-8")
    shutil.copy2(CONFIG / "package" / "icon.png", package_root / "icon.png")

    for tool in TOOLS:
        build_tool(tool, package_root, staged, tool_version, sdk)

    # Only the finished package comes back into the project; everything else
    # stays in the scratch directory. See default_build_root().
    DIST.mkdir(parents=True, exist_ok=True)
    destination = Path(args.out).expanduser() if args.out else (
        DIST / f"{PACKAGE_NAME}_{tool_version.replace('.', '_')}.yxi"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.unlink(missing_ok=True)
    make_zip(package_root, destination)

    size_mb = destination.stat().st_size / 1048576
    log(f"done: {destination}  ({size_mb:.0f} MB)")
    log("install it by closing Designer and double-clicking the .yxi")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
