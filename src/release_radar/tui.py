"""Textual TUI: shows verdicts in a table with a detail panel, lets you queue
board updates. The panel under the table shows the selected row's finding and
its concrete action item.

Keys:
  r  rescan            u  toggle 'write to board' for the selected NEEDS_BOARD row
  d  dismiss: on a "review PR" row drops just that candidate PR (KEP stays
     tracked); on any other row hides the whole KEP. Remembered in state.
  a  apply queued board writes
  o  open the KEP issue          O  open its PR (accepted or flagged candidate)
  q  quit (state is saved on exit)
"""
from __future__ import annotations

import webbrowser

from loguru import logger
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import DataTable, Footer, Header, Static

from .config import Config, State
from .github import GitHub
from .logic import Status, Verdict, evaluate
from .scan import run_scan
from .writeback import apply_writes

_STATUS_STYLE = {
    Status.MEETS: ("✓ meets", "green"),
    Status.NEEDS_BOARD: ("● needs board", "yellow"),
    Status.MISMATCH: ("✗ mismatch", "red"),
    Status.BAD_PR: ("⚠ review PR", "yellow"),
    Status.NO_PR: ("… no PR", "grey62"),
    Status.NO_DOCS: ("∅ no docs needed", "blue"),
}


def _shorten(url: str, width: int = 40) -> str:
    if not url:
        return ""
    return url if len(url) <= width else "…" + url[-(width - 1):]


class TrackerApp(App):
    CSS = """
    #summary { height: 1; content-align: center middle; background: $boost; }
    DataTable { height: 1fr; }
    #detail { height: auto; min-height: 4; padding: 0 1; border-top: solid $accent; }
    """
    BINDINGS = [
        Binding("r", "rescan", "Rescan"),
        Binding("u", "toggle_write", "Queue/unqueue write"),
        Binding("d", "dismiss", "Dismiss (PR on review rows, else KEP)"),
        Binding("a", "apply", "Apply writes"),
        Binding("o", "open_kep", "Open KEP"),
        Binding("O", "open_pr", "Open PR"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, cfg: Config, state: State, gh: GitHub):
        super().__init__()
        self.cfg = cfg
        self.state = state
        self.gh = gh
        self.verdicts: list[Verdict] = []
        self.queued: set[str] = set()  # kep keys queued for board write

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="summary")
        yield DataTable(id="table", cursor_type="row", zebra_stripes=True)
        yield Static("", id="detail")
        yield Footer()

    def on_mount(self) -> None:
        t = self.query_one(DataTable)
        t.add_columns("KEP", "Status", "Q", "Assignee", "Board 'Docs PR'", "Finding")
        self.action_rescan()

    # --- scanning ----------------------------------------------------------

    def action_rescan(self) -> None:
        self.sub_title = f"{self.cfg.deadline} · {self.cfg.project_url}"
        self.query_one("#summary", Static).update("Scanning…")
        self.run_worker(self._scan, thread=True, exclusive=True)

    def _scan(self) -> None:
        verdicts = run_scan(self.cfg, self.state, self.gh)
        self.state.save()
        self.call_from_thread(self._populate, verdicts)

    def _populate(self, verdicts: list[Verdict]) -> None:
        # hide dismissed rows
        self.verdicts = [v for v in verdicts if v.row.kep not in self.state.dismissed]
        t = self.query_one(DataTable)
        saved_row = t.cursor_row  # preserve cursor across rebuilds
        t.clear()
        counts: dict[Status, int] = {}
        for v in self.verdicts:
            counts[v.status] = counts.get(v.status, 0) + 1
            label, style = _STATUS_STYLE[v.status]
            queued = "→ write" if v.row.kep in self.queued else ""
            board = _shorten(v.row.board_docs_pr) or Text("—", style="grey62")
            t.add_row(
                v.row.kep,
                Text(label, style=style),
                Text(queued, style="cyan"),
                v.row.assignee,
                board,
                v.detail,
                key=v.row.kep,
            )
        if t.row_count:
            t.move_cursor(row=max(0, min(saved_row, t.row_count - 1)))
        summary = "  ".join(
            f"[{_STATUS_STYLE[s][1]}]{_STATUS_STYLE[s][0]}: {counts.get(s,0)}[/]"
            for s in Status
        )
        self.query_one("#summary", Static).update(
            Text.from_markup(f"{len(self.verdicts)} KEPs   {summary}   ({len(self.queued)} queued)")
        )
        self._show_detail(self._selected())

    # --- row actions -------------------------------------------------------

    def _selected(self) -> Verdict | None:
        t = self.query_one(DataTable)
        if not self.verdicts:
            return None
        try:
            key = t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value
        except Exception:
            return None
        return next((v for v in self.verdicts if v.row.kep == key), None)

    def _show_detail(self, v: Verdict | None) -> None:
        panel = self.query_one("#detail", Static)
        if v is None:
            panel.update("")
            return
        label, style = _STATUS_STYLE[v.status]
        lines = [
            Text.assemble((label, f"bold {style}"), "  ", (v.row.kep, "bold"), f"  {v.row.title}"),
            Text.assemble(("Finding: ", "bold"), v.detail),
        ]
        if v.status in (Status.MEETS, Status.NO_DOCS):
            lines.append(Text("Action:  nothing to do", style="green"))
        else:
            lines.append(Text.assemble(("Action:  ", "bold"), (v.action, "yellow")))
        panel.update(Text("\n").join(lines))

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        key = event.row_key.value if event.row_key else None
        self._show_detail(next((v for v in self.verdicts if v.row.kep == key), None))

    def action_toggle_write(self) -> None:
        v = self._selected()
        if not v:
            return
        if v.status is not Status.NEEDS_BOARD:
            self.notify(f"{v.row.kep}: only NEEDS_BOARD rows can be written", severity="warning")
            return
        self.queued.symmetric_difference_update({v.row.kep})
        self._populate(self.verdicts)

    def action_dismiss(self) -> None:
        v = self._selected()
        if not v:
            return
        # BAD_PR rows carry candidate PR(s) that aren't the placeholder: dismiss
        # those specific PRs so the KEP keeps being tracked for a real one.
        if v.status is Status.BAD_PR and v.row.rejected_prs:
            n = len(v.row.rejected_prs)
            for pr in v.row.rejected_prs:
                self.state.dismissed_prs[pr.url] = v.row.kep
            v.row.rejected_prs = []
            # re-evaluate this one row locally (no network) so it drops to NO_PR now
            fresh = evaluate(self.cfg.deadline, v.row, self.cfg.dest_branch)
            self.verdicts = [fresh if x.row.kep == v.row.kep else x for x in self.verdicts]
            self.notify(f"{v.row.kep}: dismissed {n} candidate PR(s)")
        else:
            self.state.dismissed[v.row.kep] = v.detail
            self.queued.discard(v.row.kep)
        self.state.save()
        self._populate(self.verdicts)

    def action_open_kep(self) -> None:
        v = self._selected()
        if v and v.row.url:
            webbrowser.open(v.row.url)

    def action_open_pr(self) -> None:
        v = self._selected()
        if not v:
            return
        # the accepted PR, else a flagged review candidate
        pr = v.row.discovered_pr or (v.row.rejected_prs[0] if v.row.rejected_prs else None)
        if pr:
            webbrowser.open(pr.url)
        else:
            self.notify(f"{v.row.kep}: no PR to open", severity="warning")

    def action_apply(self) -> None:
        if not self.queued:
            self.notify("Nothing queued. Press 'u' on NEEDS_BOARD rows first.", severity="warning")
            return
        targets = [v for v in self.verdicts if v.row.kep in self.queued]
        self.query_one("#summary", Static).update(f"Writing {len(targets)} board fields…")
        self.run_worker(lambda: self._apply(targets), thread=True, exclusive=True)

    def _apply(self, targets: list[Verdict]) -> None:
        results = apply_writes(self.cfg, self.state, self.gh, targets)
        self.state.save()
        for kep, ok, msg in results:
            if ok:
                self.queued.discard(kep)
            logger.info("write {}: {} {}", kep, "ok" if ok else "FAIL", msg)
        self.call_from_thread(self.action_rescan)
        self.call_from_thread(
            self.notify,
            f"Wrote {sum(1 for _,ok,_ in results if ok)}/{len(results)} rows",
        )

    def action_quit(self) -> None:
        self.state.save()
        self.exit()
