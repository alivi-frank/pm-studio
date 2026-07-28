"""Entry point: `python -m pm_studio` (or the `pm-studio` script) from the target
repo's root.

    python -m pm_studio                 run the PM Studio server for the repo at cwd
    python -m pm_studio --port 8005      ... on port 8005 (overrides [server] port)
    python -m pm_studio --host 0.0.0.0   ... bound to a specific host interface
    python -m pm_studio init            scaffold pm_studio_local/ + seed docs into the repo
    python -m pm_studio version          print the installed package version

The server run path accepts `--host`/`--port` flags that override the corresponding
`[server]` values in `pm_studio_local/config.toml` for this process only, without
editing the file. Precedence for the port is: `--port` flag > `[server] port` in
config.toml > the built-in default (8000). An invalid or missing flag value is a hard
error (clear message on stderr, non-zero exit) - there is no silent fallback.
"""

import argparse
import dataclasses
import sys
import threading
import time
import webbrowser

from . import __version__
from . import config


def _open_browser() -> None:
    time.sleep(1.0)
    # Read the live module attribute so a `--port`/`--host` override is reflected.
    webbrowser.open(config.CONFIG.base_url)


def _serve() -> None:
    import uvicorn

    cfg = config.CONFIG
    print(f"PM Studio v{__version__} - project '{cfg.project_name}' at {cfg.repo_root}")
    threading.Thread(target=_open_browser, daemon=True).start()
    uvicorn.run("pm_studio.server:app", host=cfg.host, port=cfg.port, reload=False)


def _apply_overrides(host: str | None, port: int | None) -> None:
    """Rebuild the frozen config with any CLI overrides and reassign the module-level
    `config.CONFIG`. This must run BEFORE `pm_studio.server` is imported, since server
    binds `from .config import CONFIG` at import time (`_serve` triggers that import)."""
    updates: dict[str, object] = {}
    if host is not None:
        updates["host"] = host
    if port is not None:
        updates["port"] = port
    if updates:
        config.CONFIG = dataclasses.replace(config.CONFIG, **updates)


def _run(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        prog="pm-studio",
        description="Run the PM Studio server for the repo at the current directory.",
    )
    parser.add_argument(
        "--host",
        default=None,
        help="host interface to bind (overrides [server] host in config.toml)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="port to bind (overrides [server] port in config.toml)",
    )
    args = parser.parse_args(argv)
    _apply_overrides(args.host, args.port)
    _serve()


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else None
    if command in ("version", "--version"):
        print(f"pm-studio {__version__}")
    elif command == "init":
        from .scaffold import run_init

        run_init(config.CONFIG.repo_root)
    else:
        # No subcommand (or leading flags): the plain server run path, which parses
        # `--host`/`--port`. argparse reports bad/missing values on stderr and exits
        # non-zero; an unknown positional/flag is likewise a non-zero error.
        _run(sys.argv[1:])


if __name__ == "__main__":
    main()
