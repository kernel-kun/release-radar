"""Entry point. TUI by default; --no-tui prints a rich table (good for CI/logs)."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from loguru import logger

from .config import Config, State

_DEFAULT_CONFIG = "config.yaml"
_DEFAULT_STATE = "state.json"


def _report(cfg: Config, state: State) -> int:
    """Headless scan -> rich table. Returns exit code (non-zero if actionable rows)."""
    from rich.console import Console
    from rich.table import Table

    from .github import GitHub
    from .logic import Status
    from .scan import run_scan

    console = Console()
    with GitHub() as gh:
        with console.status("Scanning board…"):
            verdicts = run_scan(cfg, state, gh)
        state.save()

    verdicts = [v for v in verdicts if v.row.kep not in state.dismissed]
    styles = {
        Status.MEETS: "green", Status.NEEDS_BOARD: "yellow",
        Status.MISMATCH: "red", Status.BAD_PR: "yellow", Status.NO_PR: "grey62",
        Status.NO_DOCS: "blue",
    }
    table = Table(title=f"{cfg.deadline} · {cfg.project_url}")
    table.add_column("KEP", overflow="fold")
    table.add_column("Status")
    table.add_column("Assignee")
    table.add_column("Finding", overflow="fold")
    table.add_column("Action item", overflow="fold")
    for v in verdicts:
        table.add_row(
            v.row.kep,
            f"[{styles[v.status]}]{v.status.value}[/]",
            v.row.assignee,
            v.detail,
            v.action or "[green]—[/]",
        )
    console.print(table)
    actionable = sum(
        1 for v in verdicts
        if v.status in (Status.NEEDS_BOARD, Status.MISMATCH, Status.BAD_PR, Status.NO_PR)
    )
    console.print(f"[bold]{actionable}[/] rows need attention.")
    return 1 if actionable else 0


def main() -> None:
    p = argparse.ArgumentParser(prog="release-radar", description=__doc__)
    p.add_argument("-c", "--config", default=_DEFAULT_CONFIG, help="config YAML path")
    p.add_argument("-s", "--state", default=_DEFAULT_STATE, help="state JSON path")
    p.add_argument("--no-tui", action="store_true", help="print a report instead of the TUI")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    p.add_argument("--check", action="store_true", help="run internal logic self-test and exit")
    args = p.parse_args()

    logger.remove()
    logger.add(sys.stderr, level="DEBUG" if args.verbose else "INFO")

    if args.check:
        from .logic import _demo as logic_demo
        from .scan import _demo as scan_demo
        logic_demo()
        scan_demo()
        return

    cfg_path = Path(args.config)
    if not cfg_path.exists():
        logger.error("No config at {}. Copy config.example.yaml -> {}.", cfg_path, cfg_path)
        sys.exit(2)

    cfg = Config.load(cfg_path)
    state = State.load(args.state)

    from .github import GitHub, MissingScopeError

    try:
        if args.no_tui:
            sys.exit(_report(cfg, state))

        from .tui import TrackerApp

        gh = GitHub()
        try:
            TrackerApp(cfg, state, gh).run()
        finally:
            gh.close()
            state.save()
    except MissingScopeError as e:
        logger.error(str(e))
        sys.exit(3)
