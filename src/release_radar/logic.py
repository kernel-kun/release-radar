"""Pure decision logic. No I/O — everything here is unit-testable.

A KEP row is evaluated against the active deadline's rule and gets a Verdict.
The rule for `placeholder_pr` is the only one implemented; register others in
DEADLINE_RULES to support PR-Ready-for-Review, Docs Freeze, etc.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


# kubernetes/website PR refs in free text: full URL or owner/repo#N shorthand.
# `repo` is templated in at runtime so the same code works if website_repo changes.
def pr_ref_regex(repo: str) -> re.Pattern[str]:
    o = re.escape(repo)
    return re.compile(rf"(?:github\.com/{o}/pull/|\b{o}#)(\d+)")


def find_pr_numbers(text: str, repo: str) -> list[int]:
    """PR numbers referencing `repo` in `text`, in order of first appearance, deduped."""
    if not text:
        return []
    seen: dict[int, None] = {}
    for m in pr_ref_regex(repo).finditer(text):
        seen.setdefault(int(m.group(1)), None)
    return list(seen)


class Status(str, Enum):
    MEETS = "meets"  # PR found, on board, matches -> criteria met
    NEEDS_BOARD = "needs_board"  # PR found but board 'Docs PR' empty -> offer to write
    MISMATCH = "mismatch"  # PR found but board has a *different* PR -> warn, probe
    NO_PR = "no_pr"  # no PR found anywhere -> authors haven't opened one
    BAD_PR = "bad_pr"  # in-cycle PR found but wrong base / closed -> review candidate
    NO_DOCS = "no_docs"  # board 'Doc Status' = 'No docs needed' -> not tracked
    MERGED = "merged"
    CLOSED = "closed"


# ordering for display: worst/most-actionable first
STATUS_ORDER = {
    Status.MISMATCH: 0,
    Status.BAD_PR: 1,
    Status.NO_PR: 2,
    Status.NEEDS_BOARD: 3,
    Status.CLOSED: 4,
    Status.MEETS: 5,
    Status.MERGED: 6,
    Status.NO_DOCS: 7,
}


@dataclass
class CommentInfo:
    id: str
    body: str
    created_at: str
    updated_at: str
    author: str


@dataclass
class PRInfo:
    id: str
    number: int
    url: str
    base_ref: str
    state: str  # OPEN | CLOSED | MERGED
    is_draft: bool
    repo: str  # nameWithOwner
    title: str = ""
    created_at: str = ""  # ISO 8601; used to gate against the cycle window
    author: str = ""
    assignees: str = ""
    comments: list[CommentInfo] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)

    def acceptable(self, dest_branch: str) -> bool:
        # placeholder PR: open (draft or ready) against the dev branch. MERGED also fine.
        return self.base_ref == dest_branch and self.state in ("OPEN", "MERGED")

    @property
    def has_lgtm(self) -> bool:
        return any(l.lower() == "lgtm" for l in self.labels)

    @property
    def has_approved(self) -> bool:
        return any(l.lower() == "approved" for l in self.labels)

    @property
    def is_docs_freeze_ready(self) -> bool:
        if self.state == "MERGED":
            return True
        if self.state == "OPEN" and not self.is_draft:
            return self.has_lgtm and self.has_approved
        return False

    def is_docs_freeze_ready_for(self, dest_branch: str) -> bool:
        if self.base_ref != dest_branch:
            return False
        return self.is_docs_freeze_ready

    @property
    def label_display(self) -> str:
        if self.state == "MERGED":
            return "—"
        parts = []
        if self.has_lgtm:
            parts.append("LGTM")
        if self.has_approved:
            parts.append("APPROVED")
        return " | ".join(parts) if parts else "none"

    def tracking_comments(self) -> list[CommentInfo]:
        return [c for c in self.comments if "<!-- release-radar:" in c.body]

    def reminder_count(self) -> int:
        tc = self.tracking_comments()
        if not tc:
            return 0
        import re
        import json

        m = re.search(r"<!--\s*release-radar:\s*({.*?})\s*-->", tc[0].body)
        if m:
            try:
                return json.loads(m.group(1)).get("reminder_number", 0)
            except Exception:
                pass
        return len(tc)

    def is_marked_ready(self) -> bool:
        tc = self.tracking_comments()
        if not tc:
            return False
        import re
        import json

        m = re.search(r"<!--\s*release-radar:\s*({.*?})\s*-->", tc[0].body)
        if m:
            try:
                data = json.loads(m.group(1))
                return data.get("ready_for_review", False) or data.get("ready", False)
            except Exception:
                pass
        return False


@dataclass
class KepRow:
    kep: str  # "kubernetes/enhancements#1234"
    title: str
    url: str
    assignee: str  # resolved Docs Assignee field value ("" if none)
    board_docs_pr: str  # raw 'Docs PR' field value on the board ("" if empty)
    board_docs_notes: str = ""  # raw 'Docs Notes' field value on the board
    kep_author: str = ""
    kep_assignees: str = ""
    item_id: str = ""  # ProjectV2Item id (needed for board writes)
    discovered_pr: PRInfo | None = None
    discovered_prs: list[PRInfo] = field(default_factory=list)
    # PRs we saw but rejected (closed / wrong branch), for the warning column
    rejected_prs: list[PRInfo] = field(default_factory=list)

    @property
    def all_target_prs(self) -> list[PRInfo]:
        if self.discovered_prs:
            return self.discovered_prs
        if self.discovered_pr:
            return [self.discovered_pr]
        return []

    def get_docs_freeze_status(self, dest_branch: str = "") -> str:
        prs = self.all_target_prs
        if not prs:
            return "At Risk for Docs Freeze"
        for pr in prs:
            if dest_branch:
                if not pr.is_docs_freeze_ready_for(dest_branch):
                    return "At Risk for Docs Freeze"
            else:
                if not pr.is_docs_freeze_ready:
                    return "At Risk for Docs Freeze"
        return "Tracked for Docs Freeze"

    @property
    def docs_freeze_status(self) -> str:
        return self.get_docs_freeze_status()


@dataclass
class Verdict:
    row: KepRow
    status: Status
    detail: str  # what/why: the finding
    action: str = ""  # what to do next (the action item)
    suggested_pr_url: str = ""  # populated when status == NEEDS_BOARD
    suggested_docs_notes: str = ""  # populated for board status writeback

    @property
    def sort_key(self) -> tuple[int, str]:
        return (STATUS_ORDER[self.status], self.row.kep)


def _board_matches(board_value: str, pr: PRInfo) -> bool:
    """Does the board's 'Docs PR' cell already point at this PR?

    The field is free text on most boards, so accept either the full URL or the
    #number shorthand appearing in the cell.
    """
    if not board_value:
        return False
    return pr.number in find_pr_numbers(board_value, pr.repo)


def evaluate_placeholder_pr(row: KepRow, dest_branch: str) -> Verdict:
    """Placeholder-PR deadline rule.

    - acceptable PR, board matches          -> MEETS       (nothing to do)
    - acceptable PR, board empty            -> NEEDS_BOARD  (write the PR to the board)
    - acceptable PR, board has a different  -> MISMATCH     (reconcile)
    - only closed / wrong-branch PR(s)      -> BAD_PR       (chase a proper placeholder)
    - no website PR at all                  -> NO_PR        (author hasn't opened one)
    """
    pr = row.discovered_pr

    # Defensive: the pipeline routes unacceptable PRs into rejected_prs, but if
    # one is passed directly, demote it so the messaging is still correct.
    if pr is not None and not pr.acceptable(dest_branch):
        row.rejected_prs = [pr, *row.rejected_prs]
        pr = None

    if pr is None:
        if row.rejected_prs:
            worst = row.rejected_prs[0]
            reasons = []
            if worst.base_ref != dest_branch:
                reasons.append(f"targets {worst.base_ref} (need {dest_branch})")
            if worst.state == "CLOSED":
                reasons.append("closed")
            why = "; ".join(reasons) or worst.state
            return Verdict(
                row,
                Status.BAD_PR,
                detail=f"possible placeholder: website PR #{worst.number} in this cycle, but it {why}",
                action=(
                    f"Open {worst.url} — if it's the placeholder, ask the author to "
                    f"retarget it to {dest_branch}; if unrelated, press 'd' to dismiss "
                    f"this PR so it won't resurface."
                ),
            )
        return Verdict(
            row,
            Status.NO_PR,
            detail="no website PR linked from this KEP (description, timeline, or comments)",
            action=(
                f"Ping the KEP author in the enhancement issue to open a placeholder "
                f"(draft) docs PR against {dest_branch}."
            ),
        )

    draft = "draft" if pr.is_draft else pr.state.lower()
    if not row.board_docs_pr:
        return Verdict(
            row,
            Status.NEEDS_BOARD,
            detail=f"PR #{pr.number} ({draft}, base {pr.base_ref}) exists; board 'Docs PR' is empty",
            action=f"Add {pr.url} to the board 'Docs PR' field — press 'u' to queue, then 'a' to write.",
            suggested_pr_url=pr.url,
        )

    if _board_matches(row.board_docs_pr, pr):
        return Verdict(
            row,
            Status.MEETS,
            detail=f"PR #{pr.number} ({draft}) is linked and recorded on the board",
            action="",
        )

    return Verdict(
        row,
        Status.MISMATCH,
        detail=f"KEP links PR #{pr.number} but board 'Docs PR' = {row.board_docs_pr}",
        action=(
            f"Confirm which is the real docs PR, then correct the board. "
            f"Discovered: {pr.url}"
        ),
    )


def evaluate_pr_ready_for_review(row: KepRow, dest_branch: str) -> Verdict:
    """PR-Ready-for-Review deadline rule extended with Docs Freeze secondary evaluation.

    Expected board notes format:
    {color_dot} ({pr_state}) {count} Reminder Sent  OR  ✅ (Merged)
    """
    prs = row.all_target_prs
    pr = row.discovered_pr or (prs[0] if prs else None)
    freeze_status = row.get_docs_freeze_status(dest_branch)

    if pr is None:
        if row.rejected_prs:
            worst = row.rejected_prs[0]
            return Verdict(
                row,
                Status.BAD_PR,
                detail=f"PR #{worst.number} exists but targets wrong branch/state ({freeze_status})",
                action="Confirm target branch or retarget it.",
            )
        return Verdict(
            row,
            Status.NO_PR,
            detail=f"no website PR linked from this KEP ({freeze_status})",
            action="Ping author to open a docs PR.",
        )

    # Determine expected board note based on state
    if pr.state == "MERGED":
        expected = "✅ (Merged)"
    else:
        if pr.state == "CLOSED":
            state_str = "Closed"
            color_dot = "🔴"
        elif pr.is_draft:
            state_str = "Draft"
            color_dot = "🔴"
        else:
            state_str = "Open"
            if pr.is_marked_ready():
                color_dot = "🟢"
            else:
                color_dot = "🟠"
        expected = f"{color_dot} ({state_str}) {pr.reminder_count()} Reminder Sent"

    detail_suffix = f"[{freeze_status}]"

    if row.board_docs_notes != expected:
        return Verdict(
            row,
            Status.NEEDS_BOARD,
            detail=f"PR #{pr.number} expected notes: '{expected}'; board Notes: '{row.board_docs_notes}' {detail_suffix}",
            action=f"Update board Docs Notes to '{expected}' (press 'u' then 'a')",
            suggested_docs_notes=expected,
        )

    if pr.state == "MERGED":
        return Verdict(
            row,
            Status.MERGED,
            detail=f"PR #{pr.number} is merged and board matches {detail_suffix}",
            action="",
        )

    if pr.state == "CLOSED":
        return Verdict(
            row,
            Status.CLOSED,
            detail=f"PR #{pr.number} is CLOSED (attention needed!) {detail_suffix}",
            action="Replace with correct open/draft/merged PR on the board.",
        )

    return Verdict(
        row,
        Status.MEETS,
        detail=f"PR #{pr.number} is on board with notes: '{expected}' {detail_suffix}",
        action="",
    )


def render_docs_freeze_checklist(
    row: KepRow,
    dest_branch: str,
    ready_deadline: str = "[Ready to review deadline]",
    freeze_deadline: str = "[Docs Freeze deadline]",
) -> str:
    prs = row.all_target_prs

    # Criterion 1: PR linked
    c1 = bool(prs)
    # Criterion 2: Target branch
    c2 = c1 and all(p.base_ref == dest_branch for p in prs)
    # Criterion 3: Ready for review (Open or Merged, not Draft)
    c3 = c1 and all(p.state in ("OPEN", "MERGED") and not p.is_draft for p in prs)
    # Criterion 4: Merge ready by Docs Freeze (Merged OR (Open + not draft + LGTM + APPROVED))
    c4 = c1 and all(p.is_docs_freeze_ready for p in prs)

    release_ver = dest_branch.removeprefix("dev-") if dest_branch.startswith("dev-") else dest_branch
    status_str = row.docs_freeze_status

    return (
        f"Hello {row.kep_author or '{doc/KEP owners}'} 👋! {release_ver} team here,\n\n"
        f"As we approach:\n"
        f"- Ready to Review deadline: {ready_deadline}\n"
        f"- Docs Freeze deadline: {freeze_deadline}\n\n"
        f"Here's where this enhancement currently stands:\n"
        f"- [{'x' if c1 else ' '}] The docs PR(s) to the `k/website` repo that are related to your enhancement are linked in the above issue description (for tracking purposes).\n"
        f"- [{'x' if c2 else ' '}] The docs PR(s) is created against the dev-{release_ver} branch.\n"
        f"- [{'x' if c3 else ' '}] The docs PR(s) are in Ready to Review state wherein they are updated with all the changes required and marked ready to review.\n"
        f"- [{'x' if c4 else ' '}] The docs PR(s) are ready to be merged (they have `approved` and `lgtm` labels applied) by the Docs Freeze deadline.\n\n"
        f"The status of this enhancement is marked as {status_str}.\n\n"
        f"If you anticipate missing docs freeze, you can file an [exception request](https://github.com/kubernetes/sig-release/blob/master/releases/EXCEPTIONS.md) in advance."
    )


# deadline name -> rule fn(row, dest_branch) -> Verdict
DEADLINE_RULES = {
    "placeholder_pr": evaluate_placeholder_pr,
    "pr_ready_for_review": evaluate_pr_ready_for_review,
}


def evaluate(deadline: str, row: KepRow, dest_branch: str) -> Verdict:
    try:
        rule = DEADLINE_RULES[deadline]
    except KeyError:
        raise ValueError(
            f"Unknown deadline {deadline!r}. Known: {sorted(DEADLINE_RULES)}"
        )
    return rule(row, dest_branch)


def _demo() -> None:
    repo = "kubernetes/website"
    # regex extraction
    assert find_pr_numbers(
        "see https://github.com/kubernetes/website/pull/123 ok", repo
    ) == [123]
    assert find_pr_numbers("ref kubernetes/website#45 and #45 again", repo) == [45]
    assert find_pr_numbers("kubernetes/website/issues/9 not a pr", repo) == []
    dup = "github.com/kubernetes/website/pull/7 and kubernetes/website#7"
    assert find_pr_numbers(dup, repo) == [7]

    base = dict(
        id="node_id",
        url="https://github.com/kubernetes/website/pull/9",
        repo=repo,
        number=9,
    )
    ok = PRInfo(base_ref="dev-1.37", state="OPEN", is_draft=True, **base)
    assert ok.acceptable("dev-1.37")
    assert not PRInfo(base_ref="main", state="OPEN", is_draft=True, **base).acceptable(
        "dev-1.37"
    )
    assert not PRInfo(
        base_ref="dev-1.37", state="CLOSED", is_draft=False, **base
    ).acceptable("dev-1.37")

    def row(**kw):
        d = dict(
            kep="kubernetes/enhancements#1",
            title="t",
            url="u",
            assignee="kernel-kun",
            board_docs_pr="",
        )
        d.update(kw)
        return KepRow(**d)

    assert evaluate("placeholder_pr", row(), "dev-1.37").status is Status.NO_PR
    assert (
        evaluate("placeholder_pr", row(discovered_pr=ok), "dev-1.37").status
        is Status.NEEDS_BOARD
    )
    # unacceptable PR passed directly -> demoted to BAD_PR
    bad = PRInfo(base_ref="main", state="OPEN", is_draft=True, **base)
    assert (
        evaluate("placeholder_pr", row(discovered_pr=bad), "dev-1.37").status
        is Status.BAD_PR
    )
    # unacceptable PR arriving via rejected_prs (the real pipeline path) -> BAD_PR
    assert (
        evaluate("placeholder_pr", row(rejected_prs=[bad]), "dev-1.37").status
        is Status.BAD_PR
    )
    # board already has the exact PR (by number shorthand)
    assert (
        evaluate(
            "placeholder_pr",
            row(discovered_pr=ok, board_docs_pr="kubernetes/website#9"),
            "dev-1.37",
        ).status
        is Status.MEETS
    )
    # board has a different PR
    assert (
        evaluate(
            "placeholder_pr",
            row(
                discovered_pr=ok,
                board_docs_pr="https://github.com/kubernetes/website/pull/999",
            ),
            "dev-1.37",
        ).status
        is Status.MISMATCH
    )

    # every non-MEETS verdict carries an action item
    for v in (
        evaluate("placeholder_pr", row(), "dev-1.37"),
        evaluate("placeholder_pr", row(discovered_pr=ok), "dev-1.37"),
        evaluate("placeholder_pr", row(rejected_prs=[bad]), "dev-1.37"),
    ):
        assert v.action, f"{v.status} should have an action"

    v = evaluate("placeholder_pr", row(discovered_pr=ok), "dev-1.37")
    assert v.suggested_pr_url == ok.url
    print("logic._demo ok")


if __name__ == "__main__":
    _demo()
