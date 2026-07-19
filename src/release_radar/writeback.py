"""Board write-back: populate the 'Docs PR' field for queued rows.

Only writes TEXT fields (the common 'Docs PR' shape). If your board uses a
different field type, it errors loudly rather than guessing.
"""

from __future__ import annotations

from loguru import logger

from .config import Config, State
from .github import GitHub
from .logic import Verdict


def apply_writes(
    cfg: Config, state: State, gh: GitHub, targets: list[Verdict]
) -> list[tuple[str, bool, str]]:
    """Write suggested PR URLs or Docs Notes status into the board fields. Returns (kep, ok, msg) per target."""
    field_name = (
        cfg.field_docs_notes
        if cfg.deadline == "pr_ready_for_review"
        else cfg.field_docs_pr
    )
    project_id, field_id, data_type = gh.project_id_and_field(
        cfg.owner, cfg.owner_is_org, cfg.project_number, field_name
    )
    results: list[tuple[str, bool, str]] = []
    if not field_id:
        return [(v.row.kep, False, f"field {field_name!r} not found") for v in targets]
    if data_type != "TEXT":
        return [
            (v.row.kep, False, f"field is {data_type}, only TEXT writes supported")
            for v in targets
        ]

    for v in targets:
        if cfg.deadline == "pr_ready_for_review":
            val = v.suggested_docs_notes
        else:
            val = v.suggested_pr_url

        if not val:
            results.append((v.row.kep, False, "no suggested value"))
            continue
        try:
            gh.set_text_field(project_id, v.row.item_id, field_id, val)
            if cfg.deadline == "pr_ready_for_review":
                v.row.board_docs_notes = val
            else:
                state.updated_board[v.row.kep] = val
            results.append((v.row.kep, True, val))
        except Exception as e:
            logger.error("write failed {}: {}", v.row.kep, e)
            results.append((v.row.kep, False, str(e)))
    return results
