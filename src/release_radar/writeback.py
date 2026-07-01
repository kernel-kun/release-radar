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
    """Write suggested PR URLs into the 'Docs PR' field. Returns (kep, ok, msg) per target."""
    project_id, field_id, data_type = gh.project_id_and_field(
        cfg.owner, cfg.owner_is_org, cfg.project_number, cfg.field_docs_pr
    )
    results: list[tuple[str, bool, str]] = []
    if not field_id:
        return [(v.row.kep, False, f"field {cfg.field_docs_pr!r} not found") for v in targets]
    if data_type != "TEXT":
        return [
            (v.row.kep, False, f"field is {data_type}, only TEXT writes supported")
            for v in targets
        ]

    for v in targets:
        url = v.suggested_pr_url
        if not url:
            results.append((v.row.kep, False, "no suggested PR"))
            continue
        try:
            gh.set_text_field(project_id, v.row.item_id, field_id, url)
            state.updated_board[v.row.kep] = url
            results.append((v.row.kep, True, url))
        except Exception as e:
            logger.error("write failed {}: {}", v.row.kep, e)
            results.append((v.row.kep, False, str(e)))
    return results
