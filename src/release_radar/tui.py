"""Textual TUI: shows verdicts in a table with a detail panel, lets you queue
board updates. The panel under the table shows the selected row's finding and
its concrete action item.

Keys:
  r  rescan            u  toggle 'write to board' for the selected NEEDS_BOARD row
  d  dismiss: on a "review PR" row drops just that candidate PR (KEP stays
     tracked); on any other row hides the whole KEP. Remembered in state.
  a  apply queued board writes
  o  open the KEP issue          O  open its PR (accepted or flagged candidate)
  s  sort table (multi-column)
  q  quit (state is saved on exit)
"""

from __future__ import annotations

import json
import re
import webbrowser
from datetime import datetime, timezone

from loguru import logger
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, DataTable, Header, Label, Select, Static

from .config import Config, State
from .github import GitHub
from .logic import (
    CommentInfo,
    PRInfo,
    Status,
    Verdict,
    evaluate,
    find_pr_numbers,
    format_mentions,
)
from .scan import _hydrate, run_scan
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
    return url if len(url) <= width else "…" + url[-(width - 1) :]


def _get_col_val(v: Verdict, col: str):
    pr = v.row.discovered_pr
    if col == "Docs Freeze":
        return 0 if v.row.docs_freeze_status == "Tracked for Docs Freeze" else 1
    if col == "KEP":
        return v.row.kep
    if col == "PR State":
        if not pr:
            return 4
        if pr.state == "MERGED":
            return 0
        if pr.state == "OPEN" and not pr.is_draft:
            return 1
        if pr.is_draft:
            return 2
        return 3
    if col == "Labels":
        if not pr:
            return 3
        if pr.has_lgtm and pr.has_approved:
            return 0
        if pr.has_lgtm:
            return 1
        if pr.has_approved:
            return 2
        return 3
    if col == "Status":
        from .logic import STATUS_ORDER

        return STATUS_ORDER.get(v.status, 99)
    if col == "Reminders":
        return pr.reminder_count() if pr else 0
    if col == "Assignee":
        return v.row.assignee
    return v.row.kep


class VerdictSortKey:
    def __init__(self, v: Verdict, specs: list[tuple[str, bool]]):
        self.v = v
        self.specs = specs

    def __lt__(self, other: "VerdictSortKey") -> bool:
        for col, asc in self.specs:
            val1 = _get_col_val(self.v, col)
            val2 = _get_col_val(other.v, col)
            if val1 != val2:
                return val1 < val2 if asc else val2 < val1
        return False


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
        for _, binding, enabled, _ in active_bindings.values():
            if not binding.show:
                continue
            if not first:
                text.append("  ")
            first = False

            key_str = f" {self.app.get_key_display(binding)} "
            text.append(key_str, style="bold reverse" if enabled else "dim")
            text.append(
                f" {binding.description}", style="default" if enabled else "dim"
            )

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
        Binding("s", "sort_table", "Multi-column sort"),
        Binding("m", "send_message", "Send message/reminder"),
        Binding("h", "view_history", "View message history"),
        Binding("r", "mark_ready", "Mark PR as ready for review"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(
        self, cfg: Config, state: State, gh: GitHub, template_content: str = ""
    ):
        super().__init__()
        self.cfg = cfg
        self.state = state
        self.gh = gh
        self.verdicts: list[Verdict] = []
        self.queued: set[str] = set()  # kep keys queued for board write
        self.template_content = template_content
        self.sort_specs: list[tuple[str, bool]] = [
            ("Docs Freeze", True),
            ("KEP", True),
        ]

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="summary")
        yield DataTable(id="table", cursor_type="row", zebra_stripes=True)
        yield Static("", id="detail")
        yield MultilineFooter()

    def on_mount(self) -> None:
        t = self.query_one(DataTable)
        if self.cfg.deadline == "pr_ready_for_review":
            t.add_columns(
                "KEP",
                "Status",
                "Q",
                "Docs PR",
                "PR State",
                "Labels",
                "Docs Freeze",
                "Reminders",
                "Assignee",
                "Board 'Docs Notes'",
                "Finding",
            )
        else:
            t.add_columns(
                "KEP", "Status", "Q", "Assignee", "Board 'Docs PR'", "Finding"
            )
        self.action_rescan()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "dismiss" and self.cfg.deadline == "pr_ready_for_review":
            return False
        if (
            action in ("send_message", "view_history", "mark_ready")
            and self.cfg.deadline != "pr_ready_for_review"
        ):
            return False
        return True

    # --- scanning ----------------------------------------------------------

    def action_rescan(self) -> None:
        self.sub_title = f"{self.cfg.deadline} · {self.cfg.project_url}"
        self.query_one("#summary", Static).update("Scanning…")
        self.run_worker(self._scan, thread=True, exclusive=True)

    def _scan(self) -> None:
        try:
            verdicts = run_scan(self.cfg, self.state, self.gh)
            self.state.save()
            self.call_from_thread(self._populate, verdicts)
        except Exception as e:
            self.call_from_thread(self._handle_scan_error, str(e))

    def _handle_scan_error(self, err_msg: str) -> None:
        self.notify(f"Scan Error: {err_msg}", severity="error", timeout=10)
        self.query_one("#summary", Static).update(
            f"[bold red]Scan Failed:[/] {err_msg}"
        )

    def _populate(self, verdicts: list[Verdict]) -> None:
        # hide dismissed rows
        self.verdicts = [v for v in verdicts if v.row.kep not in self.state.dismissed]
        self.verdicts.sort(key=lambda v: VerdictSortKey(v, self.sort_specs))

        t = self.query_one(DataTable)
        saved_row = t.cursor_row  # preserve cursor across rebuilds
        t.clear()
        counts: dict[Status, int] = {}
        for v in self.verdicts:
            counts[v.status] = counts.get(v.status, 0) + 1
            label, style = _STATUS_STYLE.get(v.status, (v.status.value, "white"))
            queued = "→ write" if v.row.kep in self.queued else ""
            if self.cfg.deadline == "pr_ready_for_review":
                pr = v.row.discovered_pr
                reminders = (
                    str(pr.reminder_count()) if pr and pr.reminder_count() > 0 else "—"
                )
                board = v.row.board_docs_notes or Text("—", style="grey62")

                # Docs PR Link
                pr_link = (
                    f"#{pr.number}" if pr else (_shorten(v.row.board_docs_pr) or "—")
                )
                pr_link_styled = Text(pr_link, style="default" if pr else "grey62")

                # PR State
                if not pr:
                    pr_state_styled = Text("—", style="grey62")
                elif pr.state == "MERGED":
                    pr_state_styled = Text("Merged", style="bold green")
                elif pr.state == "CLOSED":
                    pr_state_styled = Text("Closed", style="red")
                elif pr.is_draft:
                    pr_state_styled = Text("Draft", style="yellow")
                else:
                    pr_state_styled = Text("Open", style="green")

                # Labels Badge Display
                if not pr or pr.state == "MERGED":
                    labels_styled = Text("—", style="grey62")
                else:
                    if pr.has_lgtm and pr.has_approved:
                        labels_styled = Text("[LGTM] [APPROVED]", style="bold green")
                    elif pr.has_lgtm:
                        labels_styled = Text("[LGTM]", style="bold green")
                    elif pr.has_approved:
                        labels_styled = Text("[APPROVED]", style="bold cyan")
                    else:
                        labels_styled = Text("none", style="dim grey62")

                # Docs Freeze Status Display
                df_status = v.row.docs_freeze_status
                if df_status == "Tracked for Docs Freeze":
                    df_styled = Text("Tracked", style="bold green")
                else:
                    df_styled = Text("At Risk", style="bold yellow")

                t.add_row(
                    v.row.kep,
                    Text(label, style=style),
                    Text(queued, style="cyan"),
                    pr_link_styled,
                    pr_state_styled,
                    labels_styled,
                    df_styled,
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
            f"[{_STATUS_STYLE[s][1]}]{_STATUS_STYLE[s][0]}: {counts.get(s, 0)}[/]"
            for s in Status
            if s in counts
        )
        sort_str = ", ".join(
            f"{c} {'ASC' if a else 'DESC'}" for c, a in self.sort_specs
        )
        self.query_one("#summary", Static).update(
            Text.from_markup(
                f"{len(self.verdicts)} KEPs   {summary}   ({len(self.queued)} queued)   Sort: [cyan]{sort_str}[/cyan]"
            )
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
        label, style = _STATUS_STYLE.get(v.status, (v.status.value, "white"))
        lines = [
            Text.assemble(
                (label, f"bold {style}"),
                "  ",
                (v.row.kep, "bold"),
                f"  {v.row.title}",
            ),
            Text.assemble(
                ("Docs Freeze Status: ", "bold"),
                (
                    v.row.docs_freeze_status,
                    "green"
                    if v.row.docs_freeze_status == "Tracked for Docs Freeze"
                    else "yellow",
                ),
            ),
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
            self.notify(
                f"{v.row.kep}: only NEEDS_BOARD rows can be written", severity="warning"
            )
            return
        self.queued.symmetric_difference_update({v.row.kep})
        self._populate(self.verdicts)

    def action_dismiss(self) -> None:
        v = self._selected()
        if not v:
            return
        if v.status is Status.BAD_PR and v.row.rejected_prs:
            n = len(v.row.rejected_prs)
            for pr in v.row.rejected_prs:
                self.state.dismissed_prs[pr.url] = v.row.kep
            v.row.rejected_prs = []
            fresh = evaluate(self.cfg.deadline, v.row, self.cfg.dest_branch)
            self.verdicts = [
                fresh if x.row.kep == v.row.kep else x for x in self.verdicts
            ]
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
        pr = v.row.discovered_pr or (
            v.row.rejected_prs[0] if v.row.rejected_prs else None
        )
        if pr:
            webbrowser.open(pr.url)
        else:
            self.notify(f"{v.row.kep}: no PR to open", severity="warning")

    def action_sort_table(self) -> None:
        def on_sort(new_specs: list[tuple[str, bool]] | None) -> None:
            if new_specs:
                self.sort_specs = new_specs
                self._populate(self.verdicts)

        self.push_screen(SortModal(self.sort_specs), on_sort)

    def action_apply(self) -> None:
        if not self.queued:
            self.notify(
                "Nothing queued. Press 'u' on NEEDS_BOARD rows first.",
                severity="warning",
            )
            return
        targets = [v for v in self.verdicts if v.row.kep in self.queued]
        self.query_one("#summary", Static).update(
            f"Writing {len(targets)} board fields…"
        )
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
            f"Wrote {sum(1 for _, ok, _ in results if ok)}/{len(results)} rows",
        )

    def action_send_message(self) -> None:
        if self.cfg.deadline != "pr_ready_for_review":
            self.notify(
                "Sending messages is only supported for the 'pr_ready_for_review' deadline.",
                severity="warning",
            )
            return
        v = self._selected()
        if not v:
            return
        pr = v.row.discovered_pr
        if not pr:
            self.notify(
                f"{v.row.kep}: no discovered PR to comment on", severity="warning"
            )
            return
        if not self.template_content:
            self.notify("No template selected for this session.", severity="warning")
            return

        def on_destination_chosen(target: str | None) -> None:
            if not target:
                return
            self.run_worker(
                lambda: self._send_message(v, pr, target),
                thread=True,
                exclusive=True,
            )

        self.push_screen(PostDestinationModal(v.row.kep, pr.number), on_destination_chosen)

    def _send_message(self, v: Verdict, pr: PRInfo, target: str) -> None:
        try:
            release_ver = (
                self.cfg.dest_branch.removeprefix("dev-")
                if self.cfg.dest_branch.startswith("dev-")
                else self.cfg.dest_branch
            )
            repo = prs[0].repo if prs else "kubernetes/website"
            desc_prs = find_pr_numbers(v.row.kep_body, repo)
            c1 = bool(desc_prs) and (not prs or any(p.number in desc_prs for p in prs))
            c2 = bool(prs) and all(p.base_ref == self.cfg.dest_branch for p in prs)
            c3 = bool(prs) and all(
                p.state == "MERGED"
                or (p.state == "OPEN" and not p.is_draft and p.is_marked_ready())
                for p in prs
            )
            c4 = bool(prs) and all(p.is_docs_freeze_ready for p in prs)

            pr_author_mentions = format_mentions(pr.author if pr else "")
            pr_assignees_mentions = format_mentions(pr.assignees if pr else "")
            kep_author_mentions = format_mentions(v.row.kep_author)
            kep_assignees_mentions = format_mentions(v.row.kep_assignees)

            owners_mentions = (
                pr_author_mentions
                if pr_author_mentions != "none"
                else (
                    kep_author_mentions
                    if kep_author_mentions != "none"
                    else "doc/KEP owners"
                )
            )

            variables = {
                "pr_author": pr_author_mentions,
                "pr_assignees": pr_assignees_mentions,
                "pr_url": pr.url if pr else "",
                "pr_number": pr.number if pr else "",
                "pr_num": pr.number if pr else "",
                "pr_status": "Draft" if pr.is_draft else "Ready for review",
                "kep_author": kep_author_mentions,
                "kep_assignees": kep_assignees_mentions,
                "kep_title": v.row.title,
                "kep_url": v.row.url,
                "release_version": release_ver,
                "ready_review_deadline": self.cfg.ready_review_deadline,
                "ready_for_review_deadline": self.cfg.ready_review_deadline,
                "ready_for_review": self.cfg.ready_review_deadline,
                "ready_to_review_deadline": self.cfg.ready_review_deadline,
                "ready_to_review": self.cfg.ready_review_deadline,
                "ready_deadline": self.cfg.ready_review_deadline,
                "Ready to review deadline": self.cfg.ready_review_deadline,
                "docs_freeze_deadline": self.cfg.docs_freeze_deadline,
                "docs_freeze": self.cfg.docs_freeze_deadline,
                "freeze_deadline": self.cfg.docs_freeze_deadline,
                "Docs Freeze deadline": self.cfg.docs_freeze_deadline,
                "crit1": "x" if c1 else " ",
                "crit2": "x" if c2 else " ",
                "crit3": "x" if c3 else " ",
                "crit4": "x" if c4 else " ",
                "docs_freeze_status": v.row.docs_freeze_status,
                "doc/KEP owners": owners_mentions,
                "future-release": f"v{release_ver}",
            }

            class SafeFormatter(dict):
                def __missing__(self, key):
                    return f"{{{key}}}"

            body = self.template_content.format_map(SafeFormatter(**variables))

            targets_to_post = []
            if target in ("pr", "both"):
                targets_to_post.append(("pr", pr.id, f"PR #{pr.number}"))
            if target in ("kep", "both") and v.row.kep:
                try:
                    repo_part, num_part = v.row.kep.split("#", 1)
                    owner, repo_name = repo_part.split("/", 1)
                    kep_id = self.gh.issue_node_id(owner, repo_name, int(num_part))
                    targets_to_post.append(("kep", kep_id, f"KEP {v.row.kep}"))
                except Exception as ex:
                    logger.error("Could not fetch KEP node ID: {}", ex)

            tc = pr.tracking_comments()
            for t_kind, t_node_id, t_name in targets_to_post:
                if not tc:
                    meta = {
                        "sent_at": datetime.now(timezone.utc).isoformat(),
                        "reminder_number": 1,
                        "ready_for_review": False,
                        "docs_freeze_status": v.row.docs_freeze_status,
                    }
                    comment_body = f"{body}\n\n<!-- release-radar: {json.dumps(meta)} -->"
                    self.gh.add_comment(t_node_id, comment_body)
                else:
                    comment_body = f"{body}\n\n<!-- release-radar: subsequent -->"
                    self.gh.add_comment(t_node_id, comment_body)

                    first_comment = tc[0]
                    orig_body = first_comment.body
                    m = re.search(r"<!--\s*release-radar:\s*({.*?})\s*-->", orig_body)
                    meta = json.loads(m.group(1)) if m else {}
                    meta["reminder_number"] = meta.get("reminder_number", 1) + 1
                    meta["docs_freeze_status"] = v.row.docs_freeze_status
                    new_meta_str = f"<!-- release-radar: {json.dumps(meta)} -->"
                    new_body = (
                        orig_body[: m.start()] + new_meta_str + orig_body[m.end() :]
                        if m
                        else f"{orig_body}\n\n{new_meta_str}"
                    )
                    self.gh.update_comment(first_comment.id, new_body)

            posted_names = " & ".join(t[2] for t in targets_to_post)
            self.call_from_thread(
                self.notify, f"Posted Docs Freeze reminder to {posted_names}"
            )
            self._rescan_row(v)
        except Exception as e:
            logger.error("Failed to send message: {}", e)
            self.call_from_thread(
                self.notify, f"Failed to send message: {e}", severity="error"
            )

    def _rescan_row(self, v: Verdict) -> None:
        """Re-scan and re-evaluate only the single row operated on, preserving API rate limit."""
        try:
            prs = []
            for pr in v.row.all_target_prs:
                prs.extend(_hydrate(self.gh, self.cfg, pr.number))
            if prs:
                v.row.discovered_pr = prs[0]
                v.row.discovered_prs = prs

            new_v = evaluate(self.cfg, v.row)

            for i, item in enumerate(self.verdicts):
                if item.row.kep == v.row.kep:
                    self.verdicts[i] = new_v
                    break

            self.state.save()
            self.call_from_thread(self._populate, self.verdicts)
        except Exception as e:
            logger.error("Failed single-row rescan for {}: {}", v.row.kep, e)
            self.call_from_thread(self.action_rescan)

    def action_view_history(self) -> None:
        if self.cfg.deadline != "pr_ready_for_review":
            self.notify(
                "Viewing message history is only supported for the 'pr_ready_for_review' deadline.",
                severity="warning",
            )
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
            self.notify(
                "Marking as ready is only supported for the 'pr_ready_for_review' deadline.",
                severity="warning",
            )
            return
        v = self._selected()
        if not v:
            return
        pr = v.row.discovered_pr
        if not pr:
            self.notify(f"{v.row.kep}: no discovered PR", severity="warning")
            return

        is_merged = pr.state == "MERGED"
        action_name = "Tracked for Docs Freeze" if is_merged else "Ready for Review"

        tc = pr.tracking_comments()
        if tc:
            msg = f"Mark PR #{pr.number} as {action_name}? This will update the first tracking comment."
        else:
            msg = f"No past tracking comments found for PR #{pr.number}. Post a new {action_name.lower()} comment?"

        def check_choice(confirmed: bool) -> None:
            if confirmed:
                self.run_worker(
                    lambda: self._mark_ready(v, pr, tc, is_merged),
                    thread=True,
                    exclusive=True,
                )

        self.push_screen(ConfirmationModal(msg), check_choice)

    def _mark_ready(
        self, v: Verdict, pr: PRInfo, tc: list[CommentInfo], is_merged: bool
    ) -> None:
        try:
            action_name = "Tracked for Docs Freeze" if is_merged else "Ready for Review"

            if tc:
                first_comment = tc[0]
                body = first_comment.body

                m = re.search(r"<!--\s*release-radar:\s*({.*?})\s*-->", body)
                meta = json.loads(m.group(1)) if m else {}

                meta["ready_for_review"] = True
                meta["docs_freeze_status"] = v.row.docs_freeze_status
                new_meta_str = f"<!-- release-radar: {json.dumps(meta)} -->"
                new_body = (
                    body[: m.start()] + new_meta_str + body[m.end() :]
                    if m
                    else f"{body}\n\n{new_meta_str}"
                )

                self.gh.update_comment(first_comment.id, new_body)
                self.call_from_thread(
                    self.notify,
                    f"Updated first comment to mark PR #{pr.number} as {action_name}.",
                )
            else:
                meta = {
                    "sent_at": datetime.now(timezone.utc).isoformat(),
                    "reminder_number": 1,
                    "ready_for_review": True,
                    "docs_freeze_status": v.row.docs_freeze_status,
                }
                comment_body = f"This PR is marked as {action_name} from our side.\n\n<!-- release-radar: {json.dumps(meta)} -->"
                self.gh.add_comment(pr.id, comment_body)
                self.call_from_thread(
                    self.notify,
                    f"Posted {action_name.lower()} comment to PR #{pr.number}.",
                )

            self._rescan_row(v)
        except Exception as e:
            logger.error("Failed to mark ready: {}", e)
            self.call_from_thread(
                self.notify, f"Failed to mark ready: {e}", severity="error"
            )

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


class PostDestinationModal(ModalScreen[str | None]):
    CSS = """
    PostDestinationModal {
        align: center middle;
        background: rgba(0, 0, 0, 0.6);
    }
    #dest_container {
        width: 60%;
        height: auto;
        background: $panel;
        border: thick $accent;
        padding: 1 2;
    }
    #dest_title {
        text-style: bold;
        text-align: center;
        margin-bottom: 1;
    }
    #dest_buttons {
        align: center middle;
    }
    #dest_buttons Button {
        margin: 0 1;
    }
    """

    def __init__(self, kep: str, pr_num: int):
        super().__init__()
        self.kep = kep
        self.pr_num = pr_num

    def compose(self) -> ComposeResult:
        with Vertical(id="dest_container"):
            yield Label(
                f"Select Post Destination for {self.kep} / PR #{self.pr_num}:",
                id="dest_title",
            )
            with Horizontal(id="dest_buttons"):
                yield Button("PR Only", variant="primary", id="pr_btn")
                yield Button("KEP Issue Only", variant="default", id="kep_btn")
                yield Button("Both PR & KEP", variant="success", id="both_btn")
                yield Button("Cancel", variant="error", id="cancel_btn")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "pr_btn":
            self.dismiss("pr")
        elif event.button.id == "kep_btn":
            self.dismiss("kep")
        elif event.button.id == "both_btn":
            self.dismiss("both")
        else:
            self.dismiss(None)


class SortModal(ModalScreen[list[tuple[str, bool]] | None]):
    CSS = """
    SortModal {
        align: center middle;
        background: rgba(0, 0, 0, 0.6);
    }
    #sort_container {
        width: 70%;
        height: auto;
        background: $panel;
        border: thick $primary;
        padding: 1 2;
    }
    #sort_title {
        text-style: bold;
        text-align: center;
        margin-bottom: 1;
    }
    .sort_row {
        height: auto;
        margin-bottom: 1;
    }
    .sort_row Label {
        width: 15;
        padding: 1 0;
    }
    .sort_row Select {
        width: 25;
    }
    #sort_buttons {
        align: center middle;
        margin-top: 1;
    }
    #sort_buttons Button {
        margin: 0 1;
    }
    """

    COLS = ["Docs Freeze", "KEP", "PR State", "Labels", "Status", "Reminders", "Assignee"]

    def __init__(self, current_specs: list[tuple[str, bool]]):
        super().__init__()
        self.current_specs = current_specs

    def compose(self) -> ComposeResult:
        p_col = self.current_specs[0][0] if self.current_specs else "Docs Freeze"
        p_dir = "ASC" if self.current_specs and self.current_specs[0][1] else "DESC"
        s_col = (
            self.current_specs[1][0]
            if len(self.current_specs) > 1
            else "KEP"
        )
        s_dir = (
            "ASC"
            if len(self.current_specs) > 1 and self.current_specs[1][1]
            else "DESC"
        )

        with Vertical(id="sort_container"):
            yield Label("Multi-Column Sort Settings", id="sort_title")
            with Horizontal(classes="sort_row"):
                yield Label("Primary:")
                yield Select(
                    [(c, c) for c in self.COLS],
                    value=p_col,
                    id="p_col_select",
                )
                yield Select(
                    [("Ascending", "ASC"), ("Descending", "DESC")],
                    value=p_dir,
                    id="p_dir_select",
                )
            with Horizontal(classes="sort_row"):
                yield Label("Secondary:")
                yield Select(
                    [(c, c) for c in self.COLS],
                    value=s_col,
                    id="s_col_select",
                )
                yield Select(
                    [("Ascending", "ASC"), ("Descending", "DESC")],
                    value=s_dir,
                    id="s_dir_select",
                )
            with Horizontal(id="sort_buttons"):
                yield Button("Apply", variant="primary", id="apply_btn")
                yield Button("Cancel", id="cancel_btn")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "apply_btn":
            p_c = self.query_one("#p_col_select", Select).value
            p_d = self.query_one("#p_dir_select", Select).value
            s_c = self.query_one("#s_col_select", Select).value
            s_d = self.query_one("#s_dir_select", Select).value

            p_col_str = str(p_c) if p_c and p_c != Select.BLANK else "Docs Freeze"
            s_col_str = str(s_c) if s_c and s_c != Select.BLANK else "KEP"
            p_dir_bool = p_d == "ASC" if p_d and p_d != Select.BLANK else True
            s_dir_bool = s_d == "ASC" if s_d and s_d != Select.BLANK else True

            specs = [(p_col_str, p_dir_bool)]
            if s_col_str != p_col_str:
                specs.append((s_col_str, s_dir_bool))
            self.dismiss(specs)
        else:
            self.dismiss(None)


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
        text-style: bold;
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
            yield Label(
                f"Chronological Message History: {self.pr_url}", id="modal_title"
            )
            with ScrollableContainer(id="comments_list"):
                if not self.comments:
                    yield Label(
                        "No release-radar messages sent using this utility yet."
                    )
                for idx, c in enumerate(self.comments):
                    yield Label(f"[bold green]Message #{idx + 1} ({c.created_at})[/]")
                    yield Label(c.body)
                    yield Label("-" * 40)
            yield Button("Close", id="close_btn")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "close_btn":
            self.dismiss()
