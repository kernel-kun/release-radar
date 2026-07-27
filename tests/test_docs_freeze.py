"""Unit tests for Docs Freeze extension integrated into Docs PR Ready for Review deadline mode."""

import unittest
from pathlib import Path
from src.release_radar.logic import (
    CommentInfo,
    KepRow,
    PRInfo,
    Status,
    Verdict,
    evaluate,
    evaluate_pr_ready_for_review,
    format_mentions,
    render_docs_freeze_checklist,
)
from src.release_radar.tui import VerdictSortKey


class TestDocsFreeze(unittest.TestCase):
    def setUp(self):
        self.dest_branch = "dev-1.37"
        self.base_pr_args = dict(
            id="pr_1",
            number=100,
            url="https://github.com/kubernetes/website/pull/100",
            base_ref="dev-1.37",
            repo="kubernetes/website",
            author="author1",
        )

    def test_draft_pr(self):
        pr = PRInfo(state="OPEN", is_draft=True, labels=["lgtm", "approved"], **self.base_pr_args)
        row = KepRow(
            kep="kubernetes/enhancements#1",
            title="Title",
            url="url",
            assignee="shadow",
            board_docs_pr=pr.url,
            discovered_pr=pr,
            discovered_prs=[pr],
        )
        self.assertFalse(pr.is_docs_freeze_ready)
        self.assertEqual(row.docs_freeze_status, "At Risk for Docs Freeze")
        v = evaluate_pr_ready_for_review(row, self.dest_branch)
        self.assertIn("At Risk for Docs Freeze", v.detail)

    def test_open_pr_no_labels(self):
        pr = PRInfo(state="OPEN", is_draft=False, labels=[], **self.base_pr_args)
        row = KepRow(
            kep="kubernetes/enhancements#1",
            title="Title",
            url="url",
            assignee="shadow",
            board_docs_pr=pr.url,
            discovered_pr=pr,
            discovered_prs=[pr],
        )
        self.assertTrue(pr.acceptable(self.dest_branch))
        self.assertFalse(pr.has_lgtm)
        self.assertFalse(pr.has_approved)
        self.assertFalse(pr.is_docs_freeze_ready)
        self.assertEqual(row.docs_freeze_status, "At Risk for Docs Freeze")
        self.assertEqual(pr.label_display, "none")

    def test_open_pr_lgtm_only(self):
        pr = PRInfo(state="OPEN", is_draft=False, labels=["lgtm"], **self.base_pr_args)
        row = KepRow(
            kep="kubernetes/enhancements#1",
            title="Title",
            url="url",
            assignee="shadow",
            board_docs_pr=pr.url,
            discovered_pr=pr,
            discovered_prs=[pr],
        )
        self.assertTrue(pr.has_lgtm)
        self.assertFalse(pr.has_approved)
        self.assertFalse(pr.is_docs_freeze_ready)
        self.assertEqual(row.docs_freeze_status, "At Risk for Docs Freeze")
        self.assertEqual(pr.label_display, "LGTM")

    def test_open_pr_approved_only(self):
        pr = PRInfo(state="OPEN", is_draft=False, labels=["approved"], **self.base_pr_args)
        row = KepRow(
            kep="kubernetes/enhancements#1",
            title="Title",
            url="url",
            assignee="shadow",
            board_docs_pr=pr.url,
            discovered_pr=pr,
            discovered_prs=[pr],
        )
        self.assertFalse(pr.has_lgtm)
        self.assertTrue(pr.has_approved)
        self.assertFalse(pr.is_docs_freeze_ready)
        self.assertEqual(row.docs_freeze_status, "At Risk for Docs Freeze")
        self.assertEqual(pr.label_display, "APPROVED")

    def test_open_pr_lgtm_and_approved(self):
        pr = PRInfo(state="OPEN", is_draft=False, labels=["lgtm", "approved"], **self.base_pr_args)
        row = KepRow(
            kep="kubernetes/enhancements#1",
            title="Title",
            url="url",
            assignee="shadow",
            board_docs_pr=pr.url,
            discovered_pr=pr,
            discovered_prs=[pr],
        )
        self.assertTrue(pr.has_lgtm)
        self.assertTrue(pr.has_approved)
        self.assertTrue(pr.is_docs_freeze_ready)
        self.assertEqual(row.docs_freeze_status, "Tracked for Docs Freeze")
        self.assertEqual(pr.label_display, "LGTM | APPROVED")

    def test_merged_pr(self):
        pr = PRInfo(state="MERGED", is_draft=False, labels=[], **self.base_pr_args)
        row = KepRow(
            kep="kubernetes/enhancements#1",
            title="Title",
            url="url",
            assignee="shadow",
            board_docs_pr=pr.url,
            board_docs_notes="✅ (Merged)",
            discovered_pr=pr,
            discovered_prs=[pr],
        )
        self.assertTrue(pr.is_docs_freeze_ready)
        self.assertEqual(row.docs_freeze_status, "Tracked for Docs Freeze")
        self.assertEqual(pr.label_display, "—")
        v = evaluate_pr_ready_for_review(row, self.dest_branch)
        self.assertEqual(v.status, Status.MERGED)
        self.assertIn("Tracked for Docs Freeze", v.detail)

    def test_merged_pr_unlinked_in_description_is_tracked(self):
        pr = PRInfo(state="MERGED", is_draft=False, labels=[], **self.base_pr_args)
        row = KepRow(
            kep="kubernetes/enhancements#1",
            title="Title",
            url="url",
            assignee="shadow",
            kep_body="",  # Not linked in KEP description body
            board_docs_pr=pr.url,
            board_docs_notes="✅ (Merged)",
            discovered_pr=pr,
            discovered_prs=[pr],
        )
        self.assertEqual(row.docs_freeze_status, "Tracked for Docs Freeze")
        rendered = render_docs_freeze_checklist(row, self.dest_branch)
        self.assertIn("- [ ] The docs PR(s) to the `k/website` repo", rendered)
        self.assertIn("The status of this enhancement is marked as Tracked for Docs Freeze.", rendered)

    def test_branch_mismatch_and_match(self):
        args = dict(self.base_pr_args)
        args["base_ref"] = "main"
        bad_branch_pr = PRInfo(state="OPEN", is_draft=False, **args)
        self.assertFalse(bad_branch_pr.acceptable("dev-1.37"))

        row = KepRow(
            kep="kubernetes/enhancements#1",
            title="Title",
            url="url",
            assignee="shadow",
            board_docs_pr="",
            rejected_prs=[bad_branch_pr],
        )
        v = evaluate_pr_ready_for_review(row, self.dest_branch)
        self.assertEqual(v.status, Status.BAD_PR)
        self.assertEqual(row.docs_freeze_status, "At Risk for Docs Freeze")

    def test_unlinked_pr(self):
        row = KepRow(
            kep="kubernetes/enhancements#1",
            title="Title",
            url="url",
            assignee="shadow",
            board_docs_pr="",
        )
        v = evaluate_pr_ready_for_review(row, self.dest_branch)
        self.assertEqual(v.status, Status.NO_PR)
        self.assertEqual(row.docs_freeze_status, "At Risk for Docs Freeze")

    def test_multi_pr_mixed_states(self):
        pr1 = PRInfo(id="p1", number=1, url="u1", base_ref="dev-1.37", state="OPEN", is_draft=False, repo="kubernetes/website", labels=["lgtm", "approved"])
        pr2 = PRInfo(id="p2", number=2, url="u2", base_ref="dev-1.37", state="OPEN", is_draft=False, repo="kubernetes/website", labels=["lgtm"])
        row = KepRow(
            kep="kubernetes/enhancements#1",
            title="Title",
            url="url",
            assignee="shadow",
            board_docs_pr="u1, u2",
            discovered_pr=pr1,
            discovered_prs=[pr1, pr2],
        )
        # pr1 is ready, but pr2 lacks approved label -> overall At Risk
        self.assertEqual(row.docs_freeze_status, "At Risk for Docs Freeze")

        # Now update pr2 labels to include approved as well
        pr2.labels.append("approved")
        self.assertEqual(row.docs_freeze_status, "Tracked for Docs Freeze")

    def test_ready_for_review_state_consistency(self):
        pr = PRInfo(state="OPEN", is_draft=False, labels=["lgtm", "approved"], **self.base_pr_args)
        row = KepRow(
            kep="kubernetes/enhancements#1",
            title="Title",
            url="url",
            assignee="shadow",
            board_docs_pr=pr.url,
            board_docs_notes="🔴 (Open) 0 Reminder Sent",
            discovered_pr=pr,
            discovered_prs=[pr],
        )
        v = evaluate_pr_ready_for_review(row, self.dest_branch)
        self.assertEqual(v.status, Status.NEEDS_BOARD)
        # expected board notes for un-marked ready open PR with 0 reminders
        self.assertEqual(v.suggested_docs_notes, "🟠 (Open) [WIP] 0 Reminder Sent")

    def test_reminder_detection_and_multiple_reminders(self):
        comment1 = CommentInfo(id="c1", body="Reminder 1\n<!-- release-radar: {\"reminder_number\": 1, \"ready_for_review\": false} -->", created_at="2026-07-20", updated_at="2026-07-20", author="bot")
        comment2 = CommentInfo(id="c2", body="Reminder 2\n<!-- release-radar: subsequent -->", created_at="2026-07-25", updated_at="2026-07-25", author="bot")
        pr = PRInfo(state="OPEN", is_draft=False, labels=[], comments=[comment1, comment2], **self.base_pr_args)
        self.assertEqual(len(pr.tracking_comments()), 2)
        self.assertEqual(pr.reminder_count(), 1)
        self.assertFalse(pr.is_marked_ready())

    def test_checklist_rendering(self):
        ready_comment = CommentInfo(
            id="c1",
            body="Tracking\n<!-- release-radar: {\"reminder_number\": 1, \"ready_for_review\": true} -->",
            created_at="2026-07-20",
            updated_at="2026-07-20",
            author="bot",
        )
        pr = PRInfo(
            state="OPEN",
            is_draft=False,
            labels=["lgtm", "approved"],
            comments=[ready_comment],
            **self.base_pr_args,
        )
        row = KepRow(
            kep="kubernetes/enhancements#123",
            title="Sample KEP",
            url="https://github.com/kubernetes/enhancements/issues/123",
            assignee="shadow",
            kep_author="kep_author_1",
            kep_body="Docs PR: https://github.com/kubernetes/website/pull/100",
            board_docs_pr=pr.url,
            discovered_pr=pr,
            discovered_prs=[pr],
        )
        rendered = render_docs_freeze_checklist(row, self.dest_branch, "July 28", "August 5")
        self.assertIn("Hello @kep_author_1 👋!", rendered)
        self.assertIn("- [x] The docs PR(s) to the `k/website` repo", rendered)
        self.assertIn("- [x] The docs PR(s) is created against the dev-1.37 branch.", rendered)
        self.assertIn("- [x] The docs PR(s) are in Ready to Review state", rendered)
        self.assertIn("- [x] The docs PR(s) are ready to be merged", rendered)
        self.assertIn("Tracked for Docs Freeze", rendered)

    def test_multi_column_sorting(self):
        pr_tracked = PRInfo(id="p1", number=1, url="u1", base_ref="dev-1.37", state="OPEN", is_draft=False, repo="kubernetes/website", labels=["lgtm", "approved"])
        pr_at_risk = PRInfo(id="p2", number=2, url="u2", base_ref="dev-1.37", state="OPEN", is_draft=False, repo="kubernetes/website", labels=[])

        row_tracked = KepRow(kep="kubernetes/enhancements#10", title="Tracked", url="u", assignee="a", board_docs_pr="u1", discovered_pr=pr_tracked, discovered_prs=[pr_tracked])
        row_at_risk = KepRow(kep="kubernetes/enhancements#20", title="At Risk", url="u", assignee="b", board_docs_pr="u2", discovered_pr=pr_at_risk, discovered_prs=[pr_at_risk])

        v_tracked = Verdict(row_tracked, Status.MEETS, "detail")
        v_at_risk = Verdict(row_at_risk, Status.MEETS, "detail")

        # Sort by Docs Freeze ASC (Tracked=0 comes before At Risk=1)
        sort_specs = [("Docs Freeze", True), ("KEP", True)]
        key_tracked = VerdictSortKey(v_tracked, sort_specs)
        key_at_risk = VerdictSortKey(v_at_risk, sort_specs)
        self.assertTrue(key_tracked < key_at_risk)

        # Sort by Docs Freeze DESC (At Risk=1 comes before Tracked=0)
        sort_specs_desc = [("Docs Freeze", False), ("KEP", True)]
        key_tracked_desc = VerdictSortKey(v_tracked, sort_specs_desc)
        key_at_risk_desc = VerdictSortKey(v_at_risk, sort_specs_desc)
        self.assertTrue(key_at_risk_desc < key_tracked_desc)

    def test_format_mentions(self):
        self.assertEqual(format_mentions("user1"), "@user1")
        self.assertEqual(format_mentions("user1, user2"), "@user1 @user2")
        self.assertEqual(format_mentions("@user1 @user2"), "@user1 @user2")
        self.assertEqual(format_mentions(""), "none")


if __name__ == "__main__":
    unittest.main()
