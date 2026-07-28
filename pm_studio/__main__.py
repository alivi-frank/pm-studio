"""Entry point: `python -m pm_studio` (or the `pm-studio` script) from the target
repo's root.

    python -m pm_studio          run the PM Studio server for the repo at cwd
    python -m pm_studio init     scaffold pm_studio_local/ + seed docs into the repo
    python -m pm_studio version  print the installed package version
"""

import sys
import threading
import time
import webbrowser

from . import __version__
from .config import CONFIG


def _open_browser() -> None:
    time.sleep(1.0)
    webbrowser.open(CONFIG.base_url)


def _serve() -> None:
    import uvicorn

    print(f"PM Studio v{__version__} - project '{CONFIG.project_name}' at {CONFIG.repo_root}")
    threading.Thread(target=_open_browser, daemon=True).start()
    uvicorn.run("pm_studio.server:app", host=CONFIG.host, port=CONFIG.port, reload=False)


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else None
    if command in ("version", "--version"):
        print(f"pm-studio {__version__}")
    elif command == "init":
        from .scaffold import run_init

        run_init(CONFIG.repo_root)
    elif command is None:
        _serve()
    else:
        print(__doc__.strip(), file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
