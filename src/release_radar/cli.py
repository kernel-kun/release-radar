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


def select_template(template_arg: str | None) -> tuple[Path, str]:
    templates_dir = Path("templates")
    templates_dir.mkdir(exist_ok=True)

    templates = list(templates_dir.glob("*.md"))

    if not templates:
        default_template = templates_dir / "pr_ready_for_review_reminder.md"
        default_template.write_text(
            "Hi @{pr_author} :wave:! v1.37 Docs team here\n\n"
            "We noticed that this Pull Request is currently in the **Draft** state.\n"
            "If you haven't already, please go ahead and add the required documentation changes and move this PR from a `Draft` to `Open` state.\n\n"
            "> [!IMPORTANT]\n"
            "> **Upcoming Docs Deadlines**:\n"
            "> - **PR Ready for Review**: `Tuesday 28th July 2026`\n"
            "> - **Docs Freeze**: `Wednesday 5th August 2026 (AoE) / Thursday 6th August 2026, 12:00 UTC`\n\n"
            "Thanks!\n"
        )
        templates = [default_template]

    if template_arg:
        path = Path(template_arg)
        if not path.exists():
            raise FileNotFoundError(f"Template file not found: {template_arg}")
        return path, path.read_text()

    templates.sort()

    if not sys.stdin.isatty():
        return templates[0], templates[0].read_text()

    from rich.console import Console
    from rich.panel import Panel
    from rich.markdown import Markdown

    console = Console()
    console.print("\n[bold cyan]Available Markdown Templates:[/]")
    for i, t in enumerate(templates):
        console.print(f"  [bold green]{i + 1}[/]: {t.name}")

    while True:
        try:
            choice = input(f"Select a template (1-{len(templates)}) [1]: ").strip()
            if not choice:
                idx = 0
                break
            idx = int(choice) - 1
            if 0 <= idx < len(templates):
                break
        except (ValueError, KeyboardInterrupt, EOFError) as e:
            if isinstance(e, (KeyboardInterrupt, EOFError)):
                sys.exit(0)
            pass
        console.print("[red]Invalid choice. Please try again.[/]")

    selected = templates[idx]
    raw_content = selected.read_text()

    dummy_vars = {
        "pr_author": "pr-author-username",
        "pr_assignees": "shadow1, shadow2",
        "pr_status": "Draft",
        "kep_author": "kep-author-username",
        "kep_assignees": "shadow1, shadow2",
        "kep_title": "Sample KEP Title",
        "kep_url": "https://github.com/kubernetes/enhancements/issues/123",
    }
    class SafeFormatter(dict):
        def __missing__(self, key):
            return f"{{{key}}}"

    try:
        preview_content = raw_content.format_map(SafeFormatter(**dummy_vars))
    except Exception as e:
        preview_content = f"Error rendering preview variables: {e}\n\nRaw Content:\n{raw_content}"

    console.print("\n[bold cyan]Template Preview (Rendered GFM):[/]")
    console.print(Panel(Markdown(preview_content), title=f"Preview: {selected.name}", subtitle="Press Enter to accept or Ctrl+C to abort"))
    try:
        input()
    except (KeyboardInterrupt, EOFError):
        console.print("\n[yellow]Aborted.[/]")
        sys.exit(0)

    return selected, raw_content


def main() -> None:
    p = argparse.ArgumentParser(prog="release-radar", description=__doc__)
    p.add_argument("-c", "--config", default=_DEFAULT_CONFIG, help="config YAML path")
    p.add_argument("-s", "--state", default=_DEFAULT_STATE, help="state JSON path")
    p.add_argument("-t", "--template", help="path to markdown template file")
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

    template_content = ""
    if not args.no_tui and cfg.deadline == "pr_ready_for_review":
        _, template_content = select_template(args.template)

    try:
        if args.no_tui:
            sys.exit(_report(cfg, state))

        from .tui import TrackerApp

        gh = GitHub()
        try:
            TrackerApp(cfg, state, gh, template_content=template_content).run()
        finally:
            gh.close()
            state.save()
    except MissingScopeError as e:
        logger.error(str(e))
        sys.exit(3)
