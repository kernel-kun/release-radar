"""GitHub GraphQL client. Queries verified against the live schema (2026).

Auth: reuses the `gh` CLI token (`gh auth token`) so we don't manage secrets.
The token needs the `read:project` scope to read the board and `project` to
write the 'Docs PR' field. If missing, run:  gh auth refresh -s project
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

import httpx
from loguru import logger

from .logic import PRInfo

GRAPHQL_URL = "https://api.github.com/graphql"


class MissingScopeError(RuntimeError):
    """Token lacks the Projects scope needed to read/write the board."""


class RateLimitError(RuntimeError):
    """GitHub API rate limit exceeded."""


# --- queries (paste-ready, schema-verified) --------------------------------

_FIELDS_Q = """
query ($owner: String!, $number: Int!, $after: String) {
  %ROOT% {
    projectV2(number: $number) {
      id
      title
      fields(first: 50, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          __typename
          ... on ProjectV2FieldCommon { id name dataType }
          ... on ProjectV2SingleSelectField { id name options { id name } }
        }
      }
    }
  }
}
"""

_ITEMS_Q = """
query ($owner: String!, $number: Int!, $after: String, $q: String!) {
  %ROOT% {
    projectV2(number: $number) {
      items(first: 50, after: $after, query: $q) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          isArchived
          fieldValues(first: 100) {
            totalCount
            nodes {
              __typename
              ... on ProjectV2ItemFieldTextValue { text field { ... on ProjectV2FieldCommon { name } } }
              ... on ProjectV2ItemFieldSingleSelectValue { name field { ... on ProjectV2FieldCommon { name } } }
              ... on ProjectV2ItemFieldUserValue { users(first: 10) { nodes { login } } field { ... on ProjectV2FieldCommon { name } } }
              ... on ProjectV2ItemFieldNumberValue { number field { ... on ProjectV2FieldCommon { name } } }
              ... on ProjectV2ItemFieldPullRequestValue { pullRequests(first: 5) { nodes { url number } } field { ... on ProjectV2FieldCommon { name } } }
            }
          }
          content {
            __typename
            ... on Issue { number title url body repository { nameWithOwner } author { login } assignees(first: 10) { nodes { login } } }
            ... on PullRequest { number title url repository { nameWithOwner } }
            ... on DraftIssue { title }
          }
        }
      }
    }
  }
}
"""

_TIMELINE_Q = """
query ($owner: String!, $repo: String!, $num: Int!, $after: String) {
  repository(owner: $owner, name: $repo) {
    issue(number: $num) {
      timelineItems(first: 100, after: $after,
                    itemTypes: [CROSS_REFERENCED_EVENT, CONNECTED_EVENT]) {
        pageInfo { hasNextPage endCursor }
        nodes {
          __typename
          ... on CrossReferencedEvent { source { %PR_FRAG% } }
          ... on ConnectedEvent { subject { %PR_FRAG% } }
        }
      }
    }
  }
}
"""

_PR_FRAG = """__typename
  ... on PullRequest {
    id
    number
    url
    title
    baseRefName
    state
    isDraft
    createdAt
    author { login }
    assignees(first: 10) { nodes { login } }
    repository { nameWithOwner }
    labels(first: 100) { nodes { name } }
    comments(first: 100) {
      nodes {
        id
        body
        createdAt
        updatedAt
        author { login }
      }
    }
  }"""

_COMMENTS_Q = """
query ($owner: String!, $repo: String!, $num: Int!, $after: String) {
  repository(owner: $owner, name: $repo) {
    issue(number: $num) {
      comments(first: 100, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes { createdAt updatedAt body }
      }
    }
  }
}
"""

_ADD_COMMENT_M = """
mutation ($subjectId: ID!, $body: String!) {
  addComment(input: { subjectId: $subjectId, body: $body }) {
    subject { id }
  }
}
"""

_UPDATE_COMMENT_M = """
mutation ($id: ID!, $body: String!) {
  updateIssueComment(input: { id: $id, body: $body }) {
    issueComment { id }
  }
}
"""

_VIEW_FILTER_Q = """
query ($owner: String!, $number: Int!, $view: Int!) {
  %ROOT% {
    projectV2(number: $number) { view(number: $view) { name filter } }
  }
}
"""

_DISCOVER_IDS_Q = """
query ($owner: String!, $number: Int!) {
  %ROOT% {
    projectV2(number: $number) {
      id
      field(name: $fieldName) {
        __typename
        ... on ProjectV2FieldCommon { id name dataType }
      }
    }
  }
}
"""

_PR_NODE_Q = """
query ($owner: String!, $repo: String!, $num: Int!) {
  repository(owner: $owner, name: $repo) { pullRequest(number: $num) { id } }
}
"""

_ISSUE_NODE_Q = """
query ($owner: String!, $repo: String!, $num: Int!) {
  repository(owner: $owner, name: $repo) { issue(number: $num) { id } }
}
"""

_ADD_ITEM_M = """
mutation ($projectId: ID!, $contentId: ID!) {
  addProjectV2ItemById(input: { projectId: $projectId, contentId: $contentId }) {
    item { id }
  }
}
"""

_SET_TEXT_M = """
mutation ($projectId: ID!, $itemId: ID!, $fieldId: ID!, $text: String!) {
  updateProjectV2ItemFieldValue(input: {
    projectId: $projectId, itemId: $itemId, fieldId: $fieldId, value: { text: $text }
  }) { projectV2Item { id } }
}
"""


@dataclass
class BoardItem:
    item_id: str
    content_type: str
    kep: str  # owner/repo#num (issues only; "" otherwise)
    number: int
    title: str
    url: str
    body: str
    assignee: str  # resolved Docs Assignee value
    docs_pr: str  # resolved Docs PR value
    doc_status: str  # resolved Doc Status value
    repo: str
    docs_notes: str = ""
    kep_author: str = ""
    kep_assignees: str = ""


def gh_token() -> str:
    out = subprocess.run(
        ["gh", "auth", "token"], capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


class GitHub:
    def __init__(self, token: str | None = None, timeout: float = 30.0):
        self.client = httpx.Client(
            headers={
                "Authorization": f"bearer {token or gh_token()}",
                "Content-Type": "application/json",
            },
            timeout=timeout,
            verify=True,
        )

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> "GitHub":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    def query(self, document: str, **variables) -> dict:
        r = self.client.post(
            GRAPHQL_URL, json={"query": document, "variables": variables}
        )
        r.raise_for_status()
        data = r.json()
        if "errors" in data:
            if any(e.get("type") == "INSUFFICIENT_SCOPES" for e in data["errors"]):
                raise MissingScopeError(
                    "GitHub token is missing the Projects scope. Run:\n"
                    "    gh auth refresh -s project -h github.com\n"
                    "('read:project' is enough for read-only; 'project' also allows board writes.)"
                )
            if any(
                e.get("type") == "RATE_LIMITED"
                or "rate limit" in e.get("message", "").lower()
                for e in data["errors"]
            ):
                msgs = "; ".join(e.get("message", str(e)) for e in data["errors"])
                raise RateLimitError(
                    f"GitHub API rate limit exceeded: {msgs}\n"
                    "Please wait for your GitHub rate limit quota to reset, or check status via `gh api rate_limit`."
                )
            msgs = "; ".join(e.get("message", str(e)) for e in data["errors"])
            raise RuntimeError(f"GraphQL error: {msgs}")
        return data["data"]

    # --- board reads -------------------------------------------------------

    def _root(self, q: str, is_org: bool) -> str:
        root = "organization(login: $owner)" if is_org else "user(login: $owner)"
        return q.replace("%ROOT%", root)

    def project_id_and_field(
        self, owner: str, is_org: bool, number: int, field_name: str
    ) -> tuple[str, str | None, str | None]:
        """Return (projectId, fieldId, dataType) for `field_name`."""
        q = self._root(_DISCOVER_IDS_Q, is_org).replace(
            "$fieldName", '"' + field_name + '"'
        )
        data = self.query(q, owner=owner, number=number)
        proj = data[("organization" if is_org else "user")]["projectV2"]
        f = proj.get("field")
        return proj["id"], (f or {}).get("id"), (f or {}).get("dataType")

    def view_filter(self, owner: str, is_org: bool, number: int, view: int) -> str:
        """The saved view's filter string, replayable via items(query:). '' if none."""
        q = self._root(_VIEW_FILTER_Q, is_org)
        data = self.query(q, owner=owner, number=number, view=view)
        v = data[("organization" if is_org else "user")]["projectV2"].get("view") or {}
        return v.get("filter") or ""

    def board_items(
        self,
        owner: str,
        is_org: bool,
        number: int,
        assignee_field: str,
        docs_pr_field: str,
        doc_status_field: str = "",
        docs_notes_field: str = "",
        query: str = "",
    ) -> list[BoardItem]:
        q = self._root(_ITEMS_Q, is_org)
        root_key = "organization" if is_org else "user"
        after = None
        out: list[BoardItem] = []
        while True:
            data = self.query(q, owner=owner, number=number, after=after, q=query)
            conn = data[root_key]["projectV2"]["items"]
            for node in conn["nodes"]:
                if node.get("isArchived"):
                    continue
                item = _parse_item(
                    node,
                    assignee_field,
                    docs_pr_field,
                    doc_status_field,
                    docs_notes_field,
                )
                if item:
                    out.append(item)
            page = conn["pageInfo"]
            if not page["hasNextPage"]:
                break
            after = page["endCursor"]
        logger.debug("fetched {} board items", len(out))
        return out

    # --- PR discovery on a KEP issue --------------------------------------

    def linked_prs(
        self, owner: str, repo: str, num: int, website_repo: str
    ) -> list[PRInfo]:
        """PRs in `website_repo` cross-referenced/connected to the issue."""
        q = _TIMELINE_Q.replace("%PR_FRAG%", _PR_FRAG)
        after = None
        prs: list[PRInfo] = []
        while True:
            data = self.query(q, owner=owner, repo=repo, num=num, after=after)
            tl = data["repository"]["issue"]["timelineItems"]
            for node in tl["nodes"]:
                pr = node.get("source") or node.get("subject")
                if not pr or pr.get("__typename") != "PullRequest":
                    continue
                if pr["repository"]["nameWithOwner"] != website_repo:
                    continue
                prs.append(_parse_pr(pr))
            page = tl["pageInfo"]
            if not page["hasNextPage"]:
                break
            after = page["endCursor"]
        return prs

    def new_comment_bodies(
        self, owner: str, repo: str, num: int, after: str | None
    ) -> tuple[list[tuple[str, str]], str | None]:
        """(comments, new_cursor). Each comment is (body, updatedAt). Resumes from `after`."""
        q = _COMMENTS_Q
        bodies: list[tuple[str, str]] = []
        cursor = after
        while True:
            data = self.query(q, owner=owner, repo=repo, num=num, after=cursor)
            conn = data["repository"]["issue"]["comments"]
            for n in conn["nodes"]:
                bodies.append((n["body"], n["updatedAt"]))
            page = conn["pageInfo"]
            if page["endCursor"]:
                cursor = page["endCursor"]
            if not page["hasNextPage"]:
                break
        return bodies, cursor

    # --- board write -------------------------------------------------------

    def pr_node_id(self, owner: str, repo: str, num: int) -> str:
        data = self.query(_PR_NODE_Q, owner=owner, repo=repo, num=num)
        return data["repository"]["pullRequest"]["id"]

    def issue_node_id(self, owner: str, repo: str, num: int) -> str:
        data = self.query(_ISSUE_NODE_Q, owner=owner, repo=repo, num=num)
        return data["repository"]["issue"]["id"]

    def add_item(self, project_id: str, content_id: str) -> str:
        data = self.query(_ADD_ITEM_M, projectId=project_id, contentId=content_id)
        return data["addProjectV2ItemById"]["item"]["id"]

    def set_text_field(
        self, project_id: str, item_id: str, field_id: str, text: str
    ) -> None:
        self.query(
            _SET_TEXT_M,
            projectId=project_id,
            itemId=item_id,
            fieldId=field_id,
            text=text,
        )

    def add_comment(self, subject_id: str, body: str) -> None:
        self.query(_ADD_COMMENT_M, subjectId=subject_id, body=body)

    def update_comment(self, comment_id: str, body: str) -> None:
        self.query(_UPDATE_COMMENT_M, id=comment_id, body=body)


def _field_value(node: dict) -> str:
    t = node["__typename"]
    if t == "ProjectV2ItemFieldTextValue":
        return node.get("text") or ""
    if t == "ProjectV2ItemFieldSingleSelectValue":
        return node.get("name") or ""
    if t == "ProjectV2ItemFieldNumberValue":
        n = node.get("number")
        return "" if n is None else str(n)
    if t == "ProjectV2ItemFieldUserValue":
        return ",".join(u["login"] for u in node.get("users", {}).get("nodes", []))
    if t == "ProjectV2ItemFieldPullRequestValue":
        return ",".join(p["url"] for p in node.get("pullRequests", {}).get("nodes", []))
    return ""


def _parse_item(
    node: dict,
    assignee_field: str,
    docs_pr_field: str,
    doc_status_field: str = "",
    docs_notes_field: str = "",
) -> BoardItem | None:
    content = node.get("content") or {}
    ctype = content.get("__typename", "")
    assignee = docs_pr = doc_status = docs_notes = ""
    fvals = node.get("fieldValues", {})
    total = fvals.get("totalCount", 0)
    nodes = fvals.get("nodes", [])
    # first:100 covers boards with <=100 populated field values. Warn (never
    # silently truncate) if a board ever grows past that — the fix is to
    # paginate fieldValues here.
    if total > len(nodes):
        logger.warning(
            "item {} has {} field values but only {} fetched; a field may be missed",
            node.get("id"),
            total,
            len(nodes),
        )
    for fv in nodes:
        fname = (fv.get("field") or {}).get("name")
        if fname == assignee_field:
            assignee = _field_value(fv)
        elif fname == docs_pr_field:
            docs_pr = _field_value(fv)
        elif doc_status_field and fname == doc_status_field:
            doc_status = _field_value(fv)
        elif docs_notes_field and fname == docs_notes_field:
            docs_notes = _field_value(fv)
    repo = (content.get("repository") or {}).get("nameWithOwner", "")
    num = content.get("number", 0)
    kep = f"{repo}#{num}" if ctype == "Issue" and repo else ""
    kep_author = (content.get("author") or {}).get("login", "")
    kep_assignees = (
        ",".join(u["login"] for u in content.get("assignees", {}).get("nodes", []))
        if ctype == "Issue"
        else ""
    )
    return BoardItem(
        item_id=node["id"],
        content_type=ctype,
        kep=kep,
        number=num,
        title=content.get("title", "") or "",
        url=content.get("url", "") or "",
        body=content.get("body", "") or "",
        assignee=assignee,
        docs_pr=docs_pr,
        doc_status=doc_status,
        docs_notes=docs_notes,
        kep_author=kep_author,
        kep_assignees=kep_assignees,
        repo=repo,
    )


def _parse_pr(pr: dict) -> PRInfo:
    from .logic import CommentInfo

    author = (pr.get("author") or {}).get("login", "")
    assignees = ",".join(u["login"] for u in pr.get("assignees", {}).get("nodes", []))
    labels = [n["name"] for n in pr.get("labels", {}).get("nodes", []) if "name" in n]
    comments = []
    for c in pr.get("comments", {}).get("nodes", []):
        comments.append(
            CommentInfo(
                id=c["id"],
                body=c["body"],
                created_at=c["createdAt"],
                updated_at=c["updatedAt"],
                author=(c.get("author") or {}).get("login", ""),
            )
        )
    return PRInfo(
        id=pr["id"],
        number=pr["number"],
        url=pr["url"],
        base_ref=pr["baseRefName"],
        state=pr["state"],
        is_draft=pr["isDraft"],
        repo=pr["repository"]["nameWithOwner"],
        title=pr.get("title", ""),
        created_at=pr.get("createdAt", "") or "",
        author=author,
        assignees=assignees,
        comments=comments,
        labels=labels,
    )
