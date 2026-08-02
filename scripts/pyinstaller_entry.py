"""PyInstaller entry point for the ``claudetrade`` executable.

PyInstaller's ``Analysis`` needs a concrete script on disk to start tracing
imports from; it cannot be pointed directly at a console-script name the way
``pip install`` can. This file is that script -- it does nothing but call the
exact same function the real ``claudetrade`` console command calls
(``claudetrade.cli:main``, see ``[project.scripts]`` in ``pyproject.toml``),
so the frozen executable's behaviour is identical to running ``claudetrade``
from an activated virtual environment.

See ``claudetrade.spec`` (repo root) and ``docs/windows-build.md`` for the
full build process, and in particular the "Known caveats" section of that
document for why ``claudetrade.exe ui`` specifically is not expected to work
out of the box even though every other command should.
"""

from __future__ import annotations

from claudetrade.cli import main

if __name__ == "__main__":
    main()
