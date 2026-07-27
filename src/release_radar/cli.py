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
        Status.MEETS: "green",
        Status.NEEDS_BOARD: "yellow",
        Status.MISMATCH: "red",
        Status.BAD_PR: "yellow",
        Status.NO_PR: "grey62",
        Status.NO_DOCS: "blue",
        Status.MERGED: "green",
        Status.CLOSED: "red",
    }
    table = Table(title=f"{cfg.deadline} · {cfg.project_url}")
    table.add_column("KEP", overflow="fold")
    table.add_column("Status")
    if cfg.deadline == "pr_ready_for_review":
        table.add_column("Labels")
        table.add_column("Docs Freeze")
    table.add_column("Assignee")
    table.add_column("Finding", overflow="fold")
    table.add_column("Action item", overflow="fold")
    for v in verdicts:
        if cfg.deadline == "pr_ready_for_review":
            pr = v.row.discovered_pr
            labels_str = pr.label_display if pr else "—"
            freeze_str = v.row.docs_freeze_status
            freeze_styled = f"[green]{freeze_str}[/]" if freeze_str == "Tracked for Docs Freeze" else f"[yellow]{freeze_str}[/]"
            table.add_row(
                v.row.kep,
                f"[{styles.get(v.status, 'white')}]{v.status.value}[/]",
                labels_str,
                freeze_styled,
                v.row.assignee,
                v.detail,
                v.action or "[green]—[/]",
            )
        else:
            table.add_row(
                v.row.kep,
                f"[{styles.get(v.status, 'white')}]{v.status.value}[/]",
                v.row.assignee,
                v.detail,
                v.action or "[green]—[/]",
            )
    console.print(table)
    actionable = sum(
        1
        for v in verdicts
        if v.status
        in (Status.NEEDS_BOARD, Status.MISMATCH, Status.BAD_PR, Status.NO_PR, Status.CLOSED)
    )
    console.print(f"[bold]{actionable}[/] rows need attention.")
    return 1 if actionable else 0


def select_template(
    template_arg: str | None, cfg: Config | None = None
) -> tuple[Path, str]:
    templates_dir = Path("templates")
    templates_dir.mkdir(exist_ok=True)

    templates = list(templates_dir.rglob("*.md"))

    if not templates:
        default_dir = templates_dir / "docs-pr"
        default_dir.mkdir(exist_ok=True)
        default_template = default_dir / "docs_freeze_reminder.md"
        default_template.write_text(
            "Hello {doc/KEP owners} 👋! v{release_version} Docs team here,\n\n"
            "As we approach:\n"
            "- Ready to Review deadline: {ready_review_deadline}\n"
            "- Docs Freeze deadline: {docs_freeze_deadline}\n\n"
            "Here's where this enhancement currently stands:\n"
            "- [{crit1}] The docs PR(s) to the `k/website` repo that are related to your enhancement are linked in the above issue description (for tracking purposes).\n"
            "- [{crit2}] The docs PR(s) is created against the dev-{release_version} branch.\n"
            "- [{crit3}] The docs PR(s) are in Ready to Review state wherein they are updated with all the changes required and marked ready to review.\n"
            "- [{crit4}] The docs PR(s) are ready to be merged (they have `approved` and `lgtm` labels applied) by the Docs Freeze deadline.\n\n"
            "The status of this enhancement is marked as {docs_freeze_status}.\n\n"
            "If you anticipate missing docs freeze, you can file an [exception request](https://github.com/kubernetes/sig-release/blob/master/releases/EXCEPTIONS.md) in advance.\n"
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
        rel_name = str(t.relative_to(templates_dir))
        console.print(f"  [bold green]{i + 1}[/]: {rel_name}")

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

    release_ver = (
        cfg.dest_branch.removeprefix("dev-")
        if cfg and cfg.dest_branch.startswith("dev-")
        else (cfg.dest_branch if cfg else "1.37")
    )
    ready_deadline = cfg.ready_review_deadline if cfg else "Tuesday 28th July 2026"
    freeze_deadline = cfg.docs_freeze_deadline if cfg else "Wednesday 5th August 2026"

    dummy_vars = {
        "pr_author": "@pr-author-username",
        "pr_assignees": "@shadow1 @shadow2",
        "pr_url": "https://github.com/kubernetes/website/pull/100",
        "pr_number": "100",
        "pr_num": "100",
        "pr_status": "Draft",
        "kep_author": "@kep-author-username",
        "kep_assignees": "@shadow1 @shadow2",
        "kep_title": "Sample KEP Title",
        "kep_url": "https://github.com/kubernetes/enhancements/issues/123",
        "doc/KEP owners": "@pr-author-username",
        "release_version": release_ver,
        "ready_review_deadline": ready_deadline,
        "ready_for_review_deadline": ready_deadline,
        "ready_for_review": ready_deadline,
        "ready_to_review_deadline": ready_deadline,
        "ready_to_review": ready_deadline,
        "ready_deadline": ready_deadline,
        "Ready to review deadline": ready_deadline,
        "docs_freeze_deadline": freeze_deadline,
        "docs_freeze": freeze_deadline,
        "freeze_deadline": freeze_deadline,
        "Docs Freeze deadline": freeze_deadline,
        "crit1": "x",
        "crit2": "x",
        "crit3": " ",
        "crit4": " ",
        "docs_freeze_status": "At Risk for Docs Freeze",
        "future-release": f"v{release_ver}",
    }

    class SafeFormatter(dict):
        def __missing__(self, key):
            return f"{{{key}}}"

    try:
        preview_content = raw_content.format_map(SafeFormatter(**dummy_vars))
    except Exception as e:
        preview_content = (
            f"Error rendering preview variables: {e}\n\nRaw Content:\n{raw_content}"
        )

    console.print("\n[bold cyan]Template Preview (Rendered GFM):[/]")
    console.print(
        Panel(
            Markdown(preview_content),
            title=f"Preview: {selected.name}",
            subtitle="Press Enter to accept or Ctrl+C to abort",
        )
    )
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
    p.add_argument(
        "--no-tui", action="store_true", help="print a report instead of the TUI"
    )
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    p.add_argument(
        "--check", action="store_true", help="run internal logic self-test and exit"
    )
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
        logger.error(
            "No config at {}. Copy config.example.yaml -> {}.", cfg_path, cfg_path
        )
        sys.exit(2)

    cfg = Config.load(cfg_path)
    state = State.load(args.state)

    from .github import GitHub, MissingScopeError

    template_content = ""
    if not args.no_tui and cfg.deadline == "pr_ready_for_review":
        _, template_content = select_template(args.template, cfg)

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
