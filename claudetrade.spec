# -*- mode: python -*-
"""PyInstaller build spec for ClaudeTrade (Windows, onedir mode).

STATUS: authored by reading the source tree (``src/claudetrade/**``) on a
Linux development machine, and has **not been executed anywhere** -- PyInstaller
does not cross-compile, so a Windows ``.exe`` cannot be produced from Linux at
all, and this spec was written without ever invoking ``pyinstaller``. Every
hidden-import and ``collect_all()`` call below is a best-effort prediction
based on reading ``src/claudetrade``'s imports and known PyInstaller pitfalls
for these specific packages, not a verified-working configuration. It
**requires validation on a real Windows machine** before anyone should trust
the resulting executable -- see docs/windows-build.md, especially "Known
caveats", before running this.

Build with:  scripts\\build-windows.bat   (Windows only)
or directly: pyinstaller claudetrade.spec

Onedir, not onefile: see docs/windows-build.md "Why onedir, not onefile" for
the full reasoning. In short, onefile self-extracts to a fresh temp directory
on every launch, which is slow and antivirus-flagged even for a plain CLI, and
actively hostile to a Streamlit app (its static frontend assets have to be
re-extracted every run, and Streamlit itself does file-mtime-based caching
that behaves oddly against a temp directory that's wiped and rebuilt each
time). Onedir keeps everything unpacked on disk once, which is what the
"known caveats" investigation below actually depends on being true.
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata

block_cipher = None

# SPECPATH is injected into this file's namespace by PyInstaller itself.
REPO_ROOT = Path(SPECPATH)  # noqa: F821
SRC_DIR = REPO_ROOT / "src"

# --------------------------------------------------------------------------
# Packages that need their data/binaries/submodules bundled explicitly.
#
# Why these three specifically (see docs/windows-build.md for the long
# version):
#   - streamlit: ships a compiled JS/CSS frontend as package *data*, not
#     code, plus does dynamic imports of its own component modules. Static
#     analysis alone reliably misses both.
#   - plotly: dynamically imports renderer/backend submodules based on the
#     environment it detects at runtime (notebook vs. script vs. browser).
#   - keyring: selects its backend (Windows Credential Manager, on a Windows
#     build) via importlib.metadata entry points at runtime
#     (claudetrade.secrets._keyring_backend calls keyring.get_keyring()),
#     which PyInstaller's import scanner cannot follow -- it never sees a
#     literal `import keyring.backends.Windows` anywhere in claudetrade or
#     keyring's own code.
# --------------------------------------------------------------------------
datas = []
binaries = []
hiddenimports = []

for pkg in ("streamlit", "plotly", "keyring"):
    pkg_datas, pkg_binaries, pkg_hidden = collect_all(pkg)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden

# importlib.metadata version/requirement lookups at runtime (streamlit reads
# its own installed version on startup; click/typer/rich do similar checks)
# need each package's dist-info *present* in the frozen build, not just its
# code -- collect_all() above does not copy this by itself.
for pkg in (
    "streamlit",
    "plotly",
    "keyring",
    "typer",
    "click",
    "rich",
    "pydantic",
    "pydantic-settings",
    "httpx",
    "sqlalchemy",
):
    try:
        datas += copy_metadata(pkg)
    except Exception as exc:  # a missing/renamed distribution should not
        # abort the whole build -- surfaced in build output for whoever runs
        # this on Windows to notice and fix the package name if it changed.
        print(f"claudetrade.spec: could not copy metadata for {pkg!r}: {exc}")

# keyring's Windows Credential Manager backend depends on pywin32-ctypes
# (pulled in automatically by `pip install -r requirements.txt` on Windows,
# via keyring's own environment-marker dependency -- but only on Windows,
# so this call will legitimately find nothing when the spec is *read* on
# Linux/macOS for development, which is fine; it must succeed on the actual
# Windows build machine).
try:
    hiddenimports += collect_submodules("win32ctypes")
except Exception as exc:
    print(f"claudetrade.spec: win32ctypes not importable while authoring/checking this "
          f"spec ({exc}); this is expected on non-Windows, but MUST resolve into real "
          f"submodules when this spec is actually built on Windows, or the Windows "
          f"Credential Manager keyring backend will silently fail at runtime.")

# Modules PyInstaller's static analysis is otherwise known to miss, because
# nothing in claudetrade's own source imports them by literal name -- they're
# selected by a config string (SQLAlchemy dialect) or OS-specific dynamic
# dispatch (shell detection) instead.
hiddenimports += [
    # Selected by the "sqlite+pysqlite://" URL string in
    # claudetrade.config.DatabaseConfig.effective_url, not a static import.
    "sqlalchemy.dialects.sqlite",
    "sqlalchemy.dialects.sqlite.pysqlite",
    # typer's shell-completion support probes the OS at runtime.
    "shellingham.posix",
    "shellingham.nt",
]

a = Analysis(  # noqa: F821 -- Analysis/PYZ/EXE/COLLECT are injected by PyInstaller
    [str(REPO_ROOT / "scripts" / "pyinstaller_entry.py")],
    pathex=[str(SRC_DIR)],
    binaries=binaries,
    # Ships next to the executable so a non-developer can find a documented
    # starting point for config.toml without opening the source tree. Copy
    # it to %LOCALAPPDATA%\ClaudeTrade\config.toml (see
    # docs/windows-install.md) rather than editing it in place.
    datas=[*datas, (str(REPO_ROOT / "config.example.toml"), ".")],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Not an actual claudetrade dependency: mentioned defensively in
        # src/claudetrade/logging_setup.py's noisy-logger suppression list
        # (in case something else ever pulls it in), but nothing in
        # src/claudetrade imports it (confirmed by grep across the tree).
        # Excluding it here is a size optimisation, not a functional change.
        "matplotlib",
        "tkinter",
    ],
    noarchive=False,
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="claudetrade",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX compression is a common source of Windows Defender/antivirus false
    # positives on freshly-built executables; left off for a first trial
    # build. Revisit once the build is validated end-to-end on Windows.
    upx=False,
    console=True,  # this is a CLI; `claudetrade ui` opens a browser tab, it
                    # does not need (or get) a native GUI window of its own.
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="claudetrade",
)
