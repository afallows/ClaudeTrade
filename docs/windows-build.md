# Building a Windows Executable (PyInstaller)

**Status: authored, not yet executed or validated on Windows.** Everything in
this document — `claudetrade.spec` (repo root), `scripts/build-windows.bat`,
and this write-up — was produced by reading `src/claudetrade/**` on a Linux
development machine. PyInstaller does not cross-compile: there is no way to
produce or test a Windows `.exe` from Linux, and `pyinstaller` was
deliberately never invoked while writing this. Treat the spec as a
well-reasoned first draft that **requires a real Windows machine to build,
run, and fix** before anyone should trust the resulting executable — not as
a working artifact. The [Known caveats](#known-caveats) section below
describes a specific, load-bearing problem this build is expected to hit,
found by reading the code rather than by running it.

If you just want to run ClaudeTrade and don't need a standalone `.exe`,
use [docs/windows-install.md](windows-install.md) instead (`pip install`
into a virtual environment) — that path is the one this repository's own
tests exercise, and does not have the caveats below.

---

## Why onedir, not onefile

`claudetrade.spec` builds in **onedir** mode (`COLLECT`, a folder of files)
rather than **onefile** mode (a single self-extracting `.exe`). Reasons:

1. **Onefile self-extracts to a fresh temp directory on every launch.** For a
   plain CLI that's just slower startup; for an app that shells out to
   Streamlit (see [Known caveats](#known-caveats)) and bundles Streamlit's
   compiled JS/CSS frontend as data files, it means re-extracting a
   non-trivial amount of static frontend content every single run, and
   Streamlit does some of its own file-mtime-based caching that behaves
   oddly against a temp directory that gets wiped and rebuilt each time.
2. **Antivirus and SmartScreen heuristics flag onefile executables more
   often** than onedir folders, because a self-extracting single binary
   matches a lot of malware's shape. A first trial on an unfamiliar Windows
   machine is exactly the situation where you don't want to be troubleshooting
   a Defender quarantine on top of everything else.
3. **Onedir keeps everything on disk in one place**, which makes it possible
   to actually diagnose the caveat below (checking whether a given file — like
   `ui\app.py` — really did land in the frozen build, and where) instead of
   inspecting a temp directory that disappears when the process exits.

The trade-off is a folder (`dist\claudetrade\`) instead of a single file to
hand someone. For a first trial that is the right trade.

## Prerequisites

- A **Windows** machine (10/11). This cannot be done from Linux/macOS.
- Python 3.11+ installed and a working ClaudeTrade venv — complete
  [docs/windows-install.md](windows-install.md) Steps 1–4 first
  (`pip install -r requirements.txt` and `pip install -e .`) so the app
  itself runs correctly *before* trying to freeze it. A build of a broken
  app is still a broken app, just harder to debug.
- The `build` extra, which adds PyInstaller:
  ```
  pip install -e ".[build]"
  ```
  (`scripts\build-windows.bat` does this for you.)

## Building

```
scripts\build-windows.bat
```

This activates the venv, installs the `build` extra, clears any previous
`build\` and `dist\claudetrade\` output, and runs
`pyinstaller claudetrade.spec`. Expect it to take several minutes the first
time — collecting Streamlit's frontend assets is the slow part. Output lands
in `dist\claudetrade\`, with `claudetrade.exe` at its root.

To run PyInstaller directly instead (e.g. to pass extra flags):
```
pyinstaller claudetrade.spec
```

## What the spec does, and why

`claudetrade.spec` is heavily commented; the summary:

- **Entry point**: `scripts/pyinstaller_entry.py`, a two-line script that
  calls `claudetrade.cli:main` — the exact function
  `pyproject.toml`'s `[project.scripts]` maps the `claudetrade` command to.
  PyInstaller needs a concrete `.py` file to start tracing imports from; it
  can't be pointed at a console-script name directly the way `pip install` can.
- **`collect_all()` for `streamlit`, `plotly`, `keyring`** — these three are
  the well-known problem packages for PyInstaller, each for a different
  reason:
  - **streamlit** ships a compiled JS/CSS frontend as package *data*, and
    does some dynamic imports of its own component modules. Static-analysis
    import tracing alone reliably misses both.
  - **plotly** dynamically imports renderer/backend submodules depending on
    what environment it detects at runtime.
  - **keyring** picks its backend (Windows Credential Manager, on a Windows
    build) via `importlib.metadata` entry points at runtime
    (`claudetrade.secrets._keyring_backend` calls `keyring.get_keyring()`) —
    there is never a literal `import keyring.backends.Windows` anywhere in
    claudetrade's or keyring's own source for PyInstaller's scanner to find.
- **`copy_metadata()` for streamlit, plotly, keyring, typer, click, rich,
  pydantic, pydantic-settings, httpx, sqlalchemy** — several of these check
  their own installed version via `importlib.metadata` at import or startup
  time; `collect_all()` does not copy a package's `dist-info` metadata by
  itself, only its code and data files, so this is a separate step.
- **`win32ctypes` submodules** (via `collect_submodules`) — keyring's Windows
  Credential Manager backend depends on `pywin32-ctypes`, which `pip` only
  installs on Windows (it's gated by an environment marker in keyring's own
  dependency list). This line will find nothing if you inspect the spec on
  Linux/macOS — that's expected — but **must** resolve to real submodules
  when actually built on Windows, or credential storage will silently stop
  working in the frozen build even though `claudetrade secrets set` worked
  fine from the venv.
- **Hidden imports for `sqlalchemy.dialects.sqlite*`** — selected by the
  `"sqlite+pysqlite://"` URL string in
  `claudetrade.config.DatabaseConfig.effective_url`, not a static import
  PyInstaller's scanner would otherwise find.
- **`config.example.toml` bundled as data** next to the executable, so a
  non-developer has a documented starting point for `config.toml` without
  needing the source tree.
- **`matplotlib` and `tkinter` excluded** — neither is an actual dependency
  (confirmed by `grep -rl matplotlib src/claudetrade` finding only a
  defensive log-noise-suppression string in `logging_setup.py`, not an
  import); excluding them is a size optimisation, not a behaviour change.

## Known caveats

### `claudetrade.exe ui` is not expected to work without a code change

This is the one issue in this document found by reading the code, not by
guessing at generic PyInstaller pitfalls, and it needs to be checked first on
Windows before relying on anything else here.

`src/claudetrade/cli.py`'s `ui` command does this (lightly abridged):
```python
command = [
    sys.executable, "-m", "streamlit", "run", str(app_path),
    "--server.port", str(port or cfg.ui.port),
]
raise typer.Exit(subprocess.call(command))
```
When running from a normal Python install, `sys.executable` is the real
`python.exe`, so `python.exe -m streamlit run ...` works exactly like typing
it yourself. **Inside a PyInstaller-frozen executable, `sys.executable` is
the frozen `claudetrade.exe` itself, not a Python interpreter** (this is
documented PyInstaller behaviour, not a bug in the build). The command above
becomes, effectively:
```
dist\claudetrade\claudetrade.exe -m streamlit run <path> --server.port 8501
```
which re-invokes the **same frozen `claudetrade` CLI**, with `sys.argv` set
to `["-m", "streamlit", "run", ...]`. The `claudetrade` Typer app has no `-m`
option and no `streamlit` subcommand, so this is expected to fail with a
Click/Typer usage error and a non-zero exit code — not launch Streamlit.

Contrast this with `src/claudetrade/version.py`, which already anticipates
running frozen (`_git_revision()` checks for a `.git` directory and falls
back cleanly when PyInstaller-bundled) — the `ui` command has no equivalent
handling, which is why this is flagged as a real, specific gap rather than a
generic "PyInstaller is fragile" disclaimer.

**This means, until fixed in source (out of scope for this documentation
pass — `src/claudetrade/cli.py` is intentionally not modified here):**
- Every other command (`version`, `init`, `status`, `probe`, `refresh`,
  `scan`, `backtest`, `secrets`, `paper`, `db`, `verify`) is expected to work
  from `claudetrade.exe` once the hidden-import list above is correct, because
  none of them shells back out to `sys.executable`.
- `claudetrade.exe ui` is expected to **fail**. If you need the Streamlit
  dashboard, run it from the Python install
  ([docs/windows-install.md](windows-install.md)) instead of the frozen build,
  until this is fixed.
- A real fix (for a future change, not this doc) would have `ui` detect
  `getattr(sys, "frozen", False)` and, when frozen, either invoke Streamlit's
  own in-process bootstrap API (`streamlit.web.bootstrap.run`) directly
  instead of shelling out, or locate a real embedded Python interpreter to
  invoke `-m streamlit` against.

### Everything else here needs first-build validation

Beyond the `ui` issue above, the standard risk with a from-scratch PyInstaller
spec for a Streamlit + SQLAlchemy + keyring app is a `ModuleNotFoundError` at
runtime for something the hidden-imports list above didn't anticipate. If
`scripts\build-windows.bat` reports a build failure, or `claudetrade.exe`
raises `ModuleNotFoundError` when run, add the missing module to
`hiddenimports` in `claudetrade.spec` and rebuild — the comments in the spec
explain the reasoning behind each existing entry so the pattern is easy to
extend.

Suggested first validation pass, in order (see
[docs/windows-smoke-test.md](windows-smoke-test.md) for the equivalent
Python-install checklist to compare against):
1. `dist\claudetrade\claudetrade.exe version` — confirms the executable
   starts at all and the version/disclaimer print correctly.
2. `dist\claudetrade\claudetrade.exe init` — confirms database creation and
   `%LOCALAPPDATA%\ClaudeTrade` path resolution work from a frozen build
   (this doesn't touch streamlit/plotly at all, so it isolates whether the
   *baseline* freeze — SQLAlchemy, pydantic, typer — is sound).
3. `dist\claudetrade\claudetrade.exe secrets set anthropic_api_key` then
   `secrets list` — the specific check for whether the `keyring`/
   `win32ctypes` hidden-import chain actually resolved on Windows.
4. `dist\claudetrade\claudetrade.exe refresh` and `scan` — exercises pandas/
   numpy/SQLAlchemy under load.
5. `dist\claudetrade\claudetrade.exe backtest --export .\out` — exercises
   `openpyxl`/CSV export.
6. `dist\claudetrade\claudetrade.exe ui` — expected to fail per the caveat
   above; confirm it fails the way predicted (a Click/Typer usage error, not
   a `ModuleNotFoundError`) as a sanity check on the reasoning, then fall back
   to the Python install for the UI.

Please update this document with the actual results once run on Windows —
in particular, replace "expected to work" / "expected to fail" language
with what was actually observed.
