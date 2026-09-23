# Building from source

---

## Requirements

- **Windows x64.**
- **Any CPython 3.13 with `pip`** to run the build. Designer's embedded
  interpreter has no pip, so it cannot build — but it is used automatically to
  *verify* the result, which is the check that matters.
- **Alteryx Designer 2026.1** to test.
- Internet access on the first build, to download wheels (~300 MB, cached
  afterwards).

If you have no standalone Python 3.13, make one from Designer's copy:

```bash
mkdir %LOCALAPPDATA%\py313-build
xcopy /E /I "C:\Program Files\Alteryx\bin\Python\python-3.13.11-embed-amd64" %LOCALAPPDATA%\py313-build
cd %LOCALAPPDATA%\py313-build
:: let the embedded build see site-packages
echo python313.zip> python313._pth
echo .>> python313._pth
echo Lib\site-packages>> python313._pth
echo.>> python313._pth
echo import site>> python313._pth
curl -o get-pip.py https://bootstrap.pypa.io/get-pip.py
python.exe get-pip.py
```

That gives you the exact interpreter version the tools will run on, with pip.
Never modify the copy inside `C:\Program Files\Alteryx`.

---

## Build

```bash
python tools\build_yxi.py
```

Output: `dist/WhatsApp_<version>.yxi`.

| Flag | Effect |
| --- | --- |
| `--no-deps` | Reuse the cached dependency download. Use this for every rebuild after the first. |
| `--refresh-deps` | Force a fresh download. |
| `--keep-tests` | Skip pruning. Much larger, occasionally useful for diagnosis. |
| `--skip-verify` | Package without verifying. Not recommended — see below. |
| `--build-dir PATH` | Override the scratch directory. |
| `--out PATH` | Override the output path. |

### What it does

1. `pip install` the pinned dependencies into a cache.
2. Install `protobuf==7.34.1` on top — see [Pinned versions](#pinned-versions).
3. Copy the cache plus `src/whatsapp_core` and `src/ayx_plugins` into a staging
   tree.
4. Replace `neonize/download.py` with an offline stub, and confirm the native
   DLL is present.
5. Prune test suites, C headers, the SDK's own manual and unused datasets
   (~165 MB). The build prints the exact figure.
6. Byte-compile, so Designer never compiles on first run or needs write access
   to the tools folder.
7. **Verify** — see below.
8. Assemble the two tool folders, each with its own copy of the staged tree, and
   zip.

Intermediates go to `%LOCALAPPDATA%\Alteryx\WhatsAppConnector-build`, never into
the project. A build stages ~800 MB; doing that inside OneDrive or any synced
folder makes the sync client upload every intermediate and hold locks on them,
which breaks the next build partway through a copy.

### The verification step

Pruning is the one step that can produce a package which looks perfect and fails
on a customer's machine. So the build imports its own staged payload in a fresh
interpreter — **Designer's own** when this machine has Designer — and checks:

- pyarrow, pandas, numpy, the SDK and its 48 generated protobuf modules load;
- `neonize`, its native DLL, and the event and enum types the connector uses
  load;
- both plugin classes import and every anchor's table builds with the right
  columns;
- `neonize.download.download()` is the offline stub, so a packaged build cannot
  reach out to GitHub.

**A payload that does not import is not packaged.** This gate caught two real
defects during development; see
[How it works → Packaging](05-architecture.md#packaging).

---

## Test

```bash
"C:\Program Files\Alteryx\bin\Python\python-3.13.11-embed-amd64\python.exe" tests\run_tests.py
```

142 tests, about a second, no network and no WhatsApp account needed.

- `tests/test_core.py` — JID and phone normalisation, configuration coercion and
  validation, the archive's queries and watermark, profiles and locking. Needs
  nothing but the standard library.
- `tests/test_plugins.py` — the Alteryx-facing layer, driven by a stand-in
  provider: metadata publication, invalid configuration handling, update-only
  mode, the archive-only read path, row buffering and per-row error reporting.
  Needs pyarrow and the SDK.

The runner finds pyarrow and the SDK automatically: `WHATSAPP_SITE_PACKAGES` if
set, then the build's staging tree, then an installed copy under
`%APPDATA%\Alteryx\Tools`. If none is found, the plugin tests skip and the rest
still run.

Running with Designer's interpreter means the tests exercise exactly the Python
the tools will use.

---

## Install a local build

**With the installer** — close Designer, double-click `dist\WhatsApp_1_0_0.yxi`.

**By hand**, which is faster when iterating:

```bash
xcopy /E /I /Y ^
  "%LOCALAPPDATA%\Alteryx\WhatsAppConnector-build\WhatsApp_1_0_0\WhatsAppInput_1_0_0" ^
  "%APPDATA%\Alteryx\Tools\WhatsAppInput_1_0_0"
xcopy /E /I /Y ^
  "%LOCALAPPDATA%\Alteryx\WhatsAppConnector-build\WhatsApp_1_0_0\WhatsAppOutput_1_0_0" ^
  "%APPDATA%\Alteryx\Tools\WhatsAppOutput_1_0_0"
```

Designer must be closed. It reads the tool folders at start-up.

> Editing a configuration panel only? Copy the single `.html` file into the
> installed tool folder and restart Designer. No rebuild needed.

---

## Using the CLI

Every code path the plugins use is reachable from a terminal, which is the
fastest way to test against a real account:

```bash
set TOOL=%APPDATA%\Alteryx\Tools\WhatsAppInput_1_0_0
set PY=C:\Program Files\Alteryx\bin\Python\python-3.13.11-embed-amd64\python.exe

"%PY%" -c "import site; site.addsitedir(r'%TOOL%\site-packages'); import sys; sys.argv=['cli','doctor']; from whatsapp_core.cli import main; raise SystemExit(main())"
```

Commands: `doctor`, `status`, `link`, `unlink`, `sync`, `read`, `chats`, `send`,
`prune`. Run `doctor` first — it is the single most useful thing to attach to a
bug report.

---

## Pinned versions

In `tools/build_yxi.py`:

```python
AYX_SDK  = "ayx-python-sdk==2.6.1"
NEONIZE  = "neonize==0.5.2"
PROTOBUF = "protobuf==7.34.1"
```

**protobuf is pinned separately and installed last, on purpose.** The SDK
requires exactly `protobuf==6.33.5` and its generated code declares gencode
6.33.5. neonize's generated code declares gencode 7.34.1 and refuses to load on
an older runtime. pip honours the SDK's pin, so a default resolve produces a
package where `import neonize` fails.

protobuf's compatibility guarantee runs the other way round: a runtime supports
gencode from its own major version and the one before it. So a 7.x runtime
serves both. Installing it last with `--no-deps` is the resolution; the verify
step re-imports both stacks to prove it.

**Bumping neonize** is the change most likely to matter — it carries the
whatsmeow version, and WhatsApp's protocol moves. After bumping: rebuild, check
the verify step passes, then link a real account and exchange messages both
ways. Check whether the new gencode version needs a different protobuf pin.

---

## Releasing

### Publish by pushing, not by zipping

Use `git push`, or `git archive` from a clean checkout. Do **not** publish by
zipping the working folder or dragging it into a web upload.

The reason is `__pycache__`. A `.pyc` records the absolute path it was compiled
from, so one that is built on your machine carries your username and directory
layout. `.gitignore` excludes them and git will silently leave them out — but a
zip of the folder will not.

`tests/run_tests.py` sets `sys.dont_write_bytecode`, so the documented commands
create none. A plain `import whatsapp_core.store` while poking at the source
does, which is exactly what a contributor is likely to do. If you must archive
the folder by hand:

```bash
rmdir /s /q src\whatsapp_core\__pycache__ src\ayx_plugins\__pycache__ tests\__pycache__
```

The built `.yxi` is not affected either way: its bytecode is compiled with
stripped paths, and `scan_for_build_traces` refuses to package anything
carrying the build machine's directories.

### Steps

1. Update `src/whatsapp_core/version.py`. Everything else derives from it: the
   `.yxi` name, the tool folder names, both `Config.xml` files.
2. Update `CHANGELOG.md`.
3. `python tools\build_yxi.py --refresh-deps` — a release build should resolve
   dependencies from scratch.
4. Run the tests.
5. Install the `.yxi` on a clean machine, link an account, send and receive.
6. Ship `dist/WhatsApp_<version>.yxi`.

Installing a new version leaves the old tool folder in place (the folder name
carries the version). Existing workflows keep pointing at the old tool until
their node is replaced; remove the old folder once nothing uses it.

Profiles and archives live outside the tool folders and survive upgrades
untouched.

---

## Regenerating the icons

```bash
python tools\make_icons.py
```

Needs Pillow. Rewrites the three PNGs under `configuration/`. They are generated
from code rather than checked in as opaque binaries so they can be reviewed and
restyled without a design tool.
