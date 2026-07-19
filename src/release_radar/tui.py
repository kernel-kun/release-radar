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
from textual.widgets import DataTable, Header, Static, Label, Button
from textual.containers import Vertical, Horizontal, ScrollableContainer
from textual.screen import ModalScreen, Screen

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
    Status.MERGED: ("✓ merged", "green"),
    Status.CLOSED: ("⚠ attention needed", "red"),
}


def _shorten(url: str, width: int = 40) -> str:
    if not url:
        return ""
    return url if len(url) <= width else "…" + url[-(width - 1):]


class MultilineFooter(Static):
    DEFAULT_CSS = """
    MultilineFooter {
        dock: bottom;
        height: auto;
        background: $footer-background;
        color: $footer-foreground;
        padding: 0 1;
        text-wrap: wrap;
    }
    """

    def on_mount(self) -> None:
        self.screen.bindings_updated_signal.subscribe(self, self.update_bindings)
        self.update_bindings()

    def on_unmount(self) -> None:
        self.screen.bindings_updated_signal.unsubscribe(self)

    def update_bindings(self, screen: Screen | None = None) -> None:
        if not self.is_attached:
            return
        active_bindings = self.screen.active_bindings
        text = Text()
        first = True
        for (_, binding, enabled, _) in active_bindings.values():
            if not binding.show:
                continue
            if not first:
                text.append("  ")
            first = False
            
            key_str = f" {self.app.get_key_display(binding)} "
            text.append(key_str, style="bold reverse" if enabled else "dim")
            text.append(f" {binding.description}", style="default" if enabled else "dim")
            
        self.update(text)


class TrackerApp(App):
    CSS = """
    #summary { height: 1; content-align: center middle; background: $boost; }
    DataTable { height: 1fr; }
    #detail { height: auto; min-height: 4; padding: 0 1; border-top: solid $accent; }
    """
    BINDINGS = [
        Binding("R", "rescan", "Rescan"),
        Binding("u", "toggle_write", "Queue/unqueue write"),
        Binding("d", "dismiss", "Dismiss (PR on review rows, else KEP)"),
        Binding("a", "apply", "Apply writes"),
        Binding("o", "open_kep", "Open KEP"),
        Binding("O", "open_pr", "Open PR"),
        Binding("m", "send_message", "Send message/reminder"),
        Binding("h", "view_history", "View message history"),
        Binding("r", "mark_ready", "Mark PR as ready for review"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, cfg: Config, state: State, gh: GitHub, template_content: str = ""):
        super().__init__()
        self.cfg = cfg
        self.state = state
        self.gh = gh
        self.verdicts: list[Verdict] = []
        self.queued: set[str] = set()  # kep keys queued for board write
        self.template_content = template_content

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="summary")
        yield DataTable(id="table", cursor_type="row", zebra_stripes=True)
        yield Static("", id="detail")
        yield MultilineFooter()

    def on_mount(self) -> None:
        t = self.query_one(DataTable)
        if self.cfg.deadline == "pr_ready_for_review":
            t.add_columns("KEP", "Status", "Q", "Reminders", "Assignee", "Board 'Docs Notes'", "Finding")
        else:
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
            if self.cfg.deadline == "pr_ready_for_review":
                pr = v.row.discovered_pr
                reminders = str(pr.reminder_count()) if pr and pr.reminder_count() > 0 else "—"
                board = v.row.board_docs_notes or Text("—", style="grey62")
                t.add_row(
                    v.row.kep,
                    Text(label, style=style),
                    Text(queued, style="cyan"),
                    Text(reminders, style="magenta" if reminders != "—" else "grey62"),
                    v.row.assignee,
                    board,
                    v.detail,
                    key=v.row.kep,
                )
            else:
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

    def action_send_message(self) -> None:
        if self.cfg.deadline != "pr_ready_for_review":
            self.notify("Sending messages is only supported for the 'pr_ready_for_review' deadline.", severity="warning")
            return
        v = self._selected()
        if not v:
            return
        pr = v.row.discovered_pr
        if not pr:
            self.notify(f"{v.row.kep}: no discovered PR to comment on", severity="warning")
            return
        if not self.template_content:
            self.notify("No template selected for this session.", severity="warning")
            return

        title_upper = pr.title.upper()
        is_wip = "WIP" in title_upper or "TODO" in title_upper

        if pr.is_draft:
            msg = f"Send reminder to Draft PR #{pr.number}? (Confidence: 100%)"
        elif is_wip:
            msg = f"Send reminder to WIP/TODO PR #{pr.number}? (Low Confidence - Manual Check Required!)"
        else:
            msg = f"Send reminder to PR #{pr.number}? (Confidence: Standard)"

        def check_choice(confirmed: bool) -> None:
            if confirmed:
                self.run_worker(lambda: self._send_message(v, pr), thread=True, exclusive=True)

        self.push_screen(ConfirmationModal(msg), check_choice)

    def _send_message(self, v: Verdict, pr: PRInfo) -> None:
        try:
            variables = {
                "pr_author": pr.author,
                "pr_assignees": pr.assignees or "none",
                "pr_status": "Draft" if pr.is_draft else "Ready for review",
                "kep_author": v.row.kep_author or "none",
                "kep_assignees": v.row.kep_assignees or "none",
                "kep_title": v.row.title,
                "kep_url": v.row.url,
            }
            body = self.template_content.format(**variables)

            import json
            import re
            from datetime import datetime, timezone

            tc = pr.tracking_comments()
            if not tc:
                meta = {
                    "sent_at": datetime.now(timezone.utc).isoformat(),
                    "reminder_number": 1,
                    "ready_for_review": False
                }
                comment_body = f"{body}\n\n<!-- release-radar: {json.dumps(meta)} -->"
                self.gh.add_comment(pr.id, comment_body)
                self.call_from_thread(self.notify, f"Posted first reminder to PR #{pr.number}")
            else:
                comment_body = f"{body}\n\n<!-- release-radar: subsequent -->"
                self.gh.add_comment(pr.id, comment_body)

                first_comment = tc[0]
                orig_body = first_comment.body
                m = re.search(r"<!--\s*release-radar:\s*({.*?})\s*-->", orig_body)
                if m:
                    try:
                        meta = json.loads(m.group(1))
                    except Exception:
                        meta = {}
                else:
                    meta = {}

                meta["reminder_number"] = meta.get("reminder_number", 1) + 1
                new_meta_str = f"<!-- release-radar: {json.dumps(meta)} -->"
                if m:
                    new_body = orig_body[:m.start()] + new_meta_str + orig_body[m.end():]
                else:
                    new_body = f"{orig_body}\n\n{new_meta_str}"

                self.gh.update_comment(first_comment.id, new_body)
                self.call_from_thread(self.notify, f"Posted reminder #{meta['reminder_number']} and updated first comment metadata.")

            self.call_from_thread(self.action_rescan)
        except Exception as e:
            logger.error("Failed to send message: {}", e)
            self.call_from_thread(self.notify, f"Failed to send message: {e}", severity="error")

    def action_view_history(self) -> None:
        if self.cfg.deadline != "pr_ready_for_review":
            self.notify("Viewing message history is only supported for the 'pr_ready_for_review' deadline.", severity="warning")
            return
        v = self._selected()
        if not v:
            return
        pr = v.row.discovered_pr
        if not pr:
            self.notify(f"{v.row.kep}: no discovered PR", severity="warning")
            return

        self.push_screen(HistoryModal(pr.url, pr.tracking_comments()))

    def action_mark_ready(self) -> None:
        if self.cfg.deadline != "pr_ready_for_review":
            self.notify("Marking as ready is only supported for the 'pr_ready_for_review' deadline.", severity="warning")
            return
        v = self._selected()
        if not v:
            return
        pr = v.row.discovered_pr
        if not pr:
            self.notify(f"{v.row.kep}: no discovered PR", severity="warning")
            return

        tc = pr.tracking_comments()
        if tc:
            msg = f"Mark PR #{pr.number} as Ready for Review? This will update the first tracking comment."
        else:
            msg = f"No past tracking comments found for PR #{pr.number}. Post a new ready-for-review comment?"

        def check_choice(confirmed: bool) -> None:
            if confirmed:
                self.run_worker(lambda: self._mark_ready(v, pr, tc), thread=True, exclusive=True)

        self.push_screen(ConfirmationModal(msg), check_choice)

    def _mark_ready(self, v: Verdict, pr: PRInfo, tc: list[CommentInfo]) -> None:
        try:
            import json
            import re
            from datetime import datetime, timezone

            if tc:
                first_comment = tc[0]
                body = first_comment.body

                m = re.search(r"<!--\s*release-radar:\s*({.*?})\s*-->", body)
                if m:
                    try:
                        meta = json.loads(m.group(1))
                    except Exception:
                        meta = {}
                else:
                    meta = {}

                meta["ready_for_review"] = True
                new_meta_str = f"<!-- release-radar: {json.dumps(meta)} -->"
                if m:
                    new_body = body[:m.start()] + new_meta_str + body[m.end():]
                else:
                    new_body = f"{body}\n\n{new_meta_str}"

                self.gh.update_comment(first_comment.id, new_body)
                self.call_from_thread(self.notify, f"Updated first comment to mark PR #{pr.number} as Ready for Review.")
            else:
                meta = {
                    "sent_at": datetime.now(timezone.utc).isoformat(),
                    "reminder_number": 1,
                    "ready_for_review": True
                }
                comment_body = f"This PR is marked as Ready for Review from our side.\n\n<!-- release-radar: {json.dumps(meta)} -->"
                self.gh.add_comment(pr.id, comment_body)
                self.call_from_thread(self.notify, f"Posted ready-for-review comment to PR #{pr.number}.")

            self.call_from_thread(self.action_rescan)
        except Exception as e:
            logger.error("Failed to mark ready: {}", e)
            self.call_from_thread(self.notify, f"Failed to mark ready: {e}", severity="error")

    def action_quit(self) -> None:
        self.state.save()
        self.exit()


class ConfirmationModal(ModalScreen[bool]):
    CSS = """
    ConfirmationModal {
        align: center middle;
        background: rgba(0, 0, 0, 0.6);
    }
    #confirm_container {
        width: 60%;
        height: auto;
        background: $panel;
        border: thick $primary;
        padding: 1 2;
    }
    #confirm_message {
        margin-bottom: 1;
        text-align: center;
    }
    #confirm_buttons {
        align: center middle;
    }
    #confirm_buttons Button {
        margin: 0 1;
    }
    """

    def __init__(self, message: str):
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm_container"):
            yield Label(self.message, id="confirm_message")
            with Horizontal(id="confirm_buttons"):
                yield Button("Yes", variant="primary", id="yes_btn")
                yield Button("No", variant="error", id="no_btn")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "yes_btn":
            self.dismiss(True)
        else:
            self.dismiss(False)


class HistoryModal(ModalScreen[None]):
    CSS = """
    HistoryModal {
        align: center middle;
        background: rgba(0, 0, 0, 0.6);
    }
    #modal_container {
        width: 80%;
        height: 80%;
        background: $panel;
        border: thick $primary;
        padding: 1 2;
    }
    #modal_title {
        font-weight: bold;
        text-align: center;
        margin-bottom: 1;
        background: $accent;
        color: $text;
        padding: 0 1;
    }
    #comments_list {
        height: 1fr;
        border: solid $accent;
        padding: 1;
        background: $boost;
    }
    #close_btn {
        margin-top: 1;
        align: center middle;
    }
    """

    def __init__(self, pr_url: str, comments: list[CommentInfo]):
        super().__init__()
        self.pr_url = pr_url
        self.comments = comments

    def compose(self) -> ComposeResult:
        with Vertical(id="modal_container"):
            yield Label(f"Chronological Message History: {self.pr_url}", id="modal_title")
            with ScrollableContainer(id="comments_list"):
                if not self.comments:
                    yield Label("No release-radar messages sent using this utility yet.")
                for idx, c in enumerate(self.comments):
                    yield Label(f"[bold green]Message #{idx+1} ({c.created_at})[/]")
                    yield Label(c.body)
                    yield Label("-" * 40)
            yield Button("Close", id="close_btn")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "close_btn":
            self.dismiss()
