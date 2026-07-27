"""Config + state file loading. State is a plain JSON dict persisted next to config."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# github.com/orgs/<org>/projects/<n>/views/<v>  OR  /users/<user>/projects/<n>
_PROJECT_RE = re.compile(
    r"github\.com/(?P<kind>orgs|users)/(?P<owner>[^/]+)/projects/(?P<number>\d+)"
    r"(?:/views/(?P<view>\d+))?"
)


@dataclass
class Config:
    owner: str
    owner_is_org: bool
    project_number: int
    view_number: int | None
    project_url: str
    website_repo: str
    dest_branch: str
    enhancements_repo: str
    field_docs_assignee: str
    field_docs_pr: str
    field_doc_status: str
    field_docs_notes: str
    no_docs_status: str
    docs_assignees: list[str]
    deadline: str
    cycle_start: (
        str  # ISO date "YYYY-MM-DD" or "" — PRs before this are prior-cycle noise
    )
    ready_review_deadline: str = ""
    docs_freeze_deadline: str = ""
    path: Path = field(default_factory=lambda: Path("config.yaml"))

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        path = Path(path)
        raw = yaml.safe_load(path.read_text())
        m = _PROJECT_RE.search(raw["project_url"])
        if not m:
            raise ValueError(f"Could not parse project_url: {raw['project_url']!r}")
        fields = raw.get("fields", {})
        d_dates = raw.get("deadlines", {})
        return cls(
            owner=m["owner"],
            owner_is_org=m["kind"] == "orgs",
            project_number=int(m["number"]),
            view_number=int(m["view"]) if m["view"] else None,
            project_url=raw["project_url"],
            website_repo=raw.get("website_repo", "kubernetes/website"),
            dest_branch=raw.get("dest_branch", "dev-1.37"),
            enhancements_repo=raw.get("enhancements_repo", "kubernetes/enhancements"),
            field_docs_assignee=fields.get("docs_assignee", "Docs Assignee"),
            field_docs_pr=fields.get("docs_pr", "Docs PR"),
            field_doc_status=fields.get("doc_status", "Doc Status"),
            field_docs_notes=fields.get("docs_notes", "Docs Notes"),
            no_docs_status=str(raw.get("no_docs_status", "No docs needed")),
            docs_assignees=[str(x) for x in raw.get("docs_assignees", [])],
            deadline=raw.get("deadline", "placeholder_pr"),
            cycle_start=str(raw.get("cycle_start", "") or ""),
            cycle_end=str(raw.get("cycle_end", "") or ""),
            ready_review_deadline=str(
                d_dates.get("ready_for_review")
                or raw.get("ready_review_deadline")
                or "Tuesday 28th July 2026"
            ),
            docs_freeze_deadline=str(
                d_dates.get("docs_freeze")
                or raw.get("docs_freeze_deadline")
                or "Wednesday 5th August 2026"
            ),
            path=path,
        )


@dataclass
class State:
    """Persisted between runs: per-KEP resume cursors + user decisions."""

    path: Path
    # keyed by KEP issue "owner/repo#num"
    dismissed: dict[str, str] = field(
        default_factory=dict
    )  # kep -> note (hide whole row)
    dismissed_prs: dict[str, str] = field(
        default_factory=dict
    )  # pr url -> kep (not a placeholder)
    updated_board: dict[str, str] = field(
        default_factory=dict
    )  # kep -> pr url we wrote

    @classmethod
    def load(cls, path: str | Path) -> "State":
        path = Path(path)
        if not path.exists():
            return cls(path=path)
        d = json.loads(path.read_text())
        return cls(
            path=path,
            dismissed=d.get("dismissed", {}),
            dismissed_prs=d.get("dismissed_prs", {}),
            updated_board=d.get("updated_board", {}),
        )

    def save(self) -> None:
        self.path.write_text(
            json.dumps(
                {
                    "dismissed": self.dismissed,
                    "dismissed_prs": self.dismissed_prs,
                    "updated_board": self.updated_board,
                },
                indent=2,
                sort_keys=True,
            )
        )
