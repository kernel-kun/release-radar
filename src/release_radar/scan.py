"""Orchestration: board items -> filter by assignee -> discover PR -> evaluate.

Per-KEP PR discovery is network-bound and independent, so it runs on a thread
pool. The GitHub client is thread-safe for our use (httpx.Client + stateless
queries), so we share one.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed

from loguru import logger

from .config import Config, State
from .github import BoardItem, GitHub
from .logic import KepRow, PRInfo, Status, Verdict, evaluate, find_pr_numbers


def _assignee_matches(value: str, assignees: list[str]) -> bool:
    if not value:
        return False
    parts = {p.strip().lstrip("@").lower() for p in value.replace(",", " ").split()}
    return any(a.strip().lstrip("@").lower() in parts for a in assignees)


def _in_cycle(pr: PRInfo, cfg: Config) -> bool:
    """Was this PR opened within the configured cycle window?

    Gates out prior-cycle noise (e.g. a dev-1.27 PR surfacing for the dev-1.37
    cycle). ISO dates sort lexicographically, so a date-prefix compare is enough.
    Empty bounds disable that side of the window.
    """
    day = pr.created_at[:10]
    if not day:
        return True  # unknown date: don't hide it
    if cfg.cycle_start and day < cfg.cycle_start:
        return False
    if cfg.cycle_end and day > cfg.cycle_end:
        return False
    return True


def _candidate_rejects(rejected: list[PRInfo], cfg: Config, state: State) -> list[PRInfo]:
    """Rejected PRs worth surfacing: opened this cycle and not user-dismissed."""
    return [
        p for p in rejected
        if _in_cycle(p, cfg) and p.url not in state.dismissed_prs
    ]


def _pick_pr(prs: list[PRInfo], dest_branch: str) -> tuple[PRInfo | None, list[PRInfo]]:
    """Choose the best acceptable PR; return (chosen, rejected)."""
    acceptable = [p for p in prs if p.acceptable(dest_branch)]
    rejected = [p for p in prs if not p.acceptable(dest_branch)]
    if acceptable:
        # prefer OPEN over MERGED, then highest number (latest)
        acceptable.sort(key=lambda p: (p.state != "OPEN", -p.number))
        return acceptable[0], rejected
    return None, rejected


def _discover_pr_for(
    gh: GitHub, cfg: Config, state: State, item: BoardItem
) -> tuple[PRInfo | None, list[PRInfo]]:
    """Find a website PR for a KEP item: description first, then timeline, then new comments."""
    owner, repo = item.repo.split("/", 1)

    # 1) structured timeline (most reliable — catches cross-repo links + connected PRs)
    prs = gh.linked_prs(owner, repo, item.number, cfg.website_repo)

    # 2) description regex (cheap, catches PRs only written in the body)
    for num in find_pr_numbers(item.body, cfg.website_repo):
        if not any(p.number == num for p in prs):
            prs.extend(_hydrate(gh, cfg, num))

    chosen, rejected = _pick_pr(prs, cfg.dest_branch)
    if chosen:
        return chosen, rejected

    # 3) nothing yet -> scan NEW comments since last run, persist the resume cursor
    after = state.comment_cursor.get(item.kep)
    bodies, new_cursor = gh.new_comment_bodies(owner, repo, item.number, after)
    if new_cursor:
        state.comment_cursor[item.kep] = new_cursor
    if bodies:
        state.last_seen_at[item.kep] = bodies[-1][1]
    comment_nums: list[int] = []
    for body, _ in bodies:
        comment_nums.extend(find_pr_numbers(body, cfg.website_repo))
    for num in dict.fromkeys(comment_nums):
        if not any(p.number == num for p in prs):
            prs.extend(_hydrate(gh, cfg, num))

    return _pick_pr(prs, cfg.dest_branch)


def _hydrate(gh: GitHub, cfg: Config, num: int) -> list[PRInfo]:
    """Fetch full PR state for a bare PR number found in text."""
    owner, repo = cfg.website_repo.split("/", 1)
    q = """
    query ($owner: String!, $repo: String!, $num: Int!) {
      repository(owner: $owner, name: $repo) {
        pullRequest(number: $num) {
          number url baseRefName state isDraft createdAt repository { nameWithOwner }
        }
      }
    }"""
    try:
        data = gh.query(q, owner=owner, repo=repo, num=num)
        pr = data["repository"]["pullRequest"]
        if not pr:
            return []
        return [PRInfo(pr["number"], pr["url"], pr["baseRefName"], pr["state"],
                       pr["isDraft"], pr["repository"]["nameWithOwner"],
                       pr.get("createdAt", "") or "")]
    except Exception as e:  # a referenced number may be an issue, not a PR
        logger.debug("hydrate #{} failed: {}", num, e)
        return []


def run_scan(
    cfg: Config, state: State, gh: GitHub, max_workers: int = 8, progress=None
) -> list[Verdict]:
    """Full scan for the configured deadline. `progress(done, total)` is optional."""
    # Scope to the saved view (e.g. view 3 "Docs"): replay its filter server-side.
    # GraphQL can't fetch "items in a view", but items(query:) takes the same
    # search syntax the view stores in its `filter`.
    view_query = ""
    if cfg.view_number is not None:
        view_query = gh.view_filter(
            cfg.owner, cfg.owner_is_org, cfg.project_number, cfg.view_number
        )
        logger.info("view {} filter: {!r}", cfg.view_number, view_query)

    items = gh.board_items(
        cfg.owner, cfg.owner_is_org, cfg.project_number,
        cfg.field_docs_assignee, cfg.field_docs_pr, cfg.field_doc_status,
        query=view_query,
    )
    keps = [
        it for it in items
        if it.kep and _assignee_matches(it.assignee, cfg.docs_assignees)
    ]
    logger.info("{} items in view, {} KEPs assigned to configured docs shadows",
                len(items), len(keps))

    verdicts: list[Verdict] = []
    done = 0

    def work(item: BoardItem) -> Verdict:
        row = KepRow(
            kep=item.kep, title=item.title, url=item.url,
            assignee=item.assignee, board_docs_pr=item.docs_pr,
            item_id=item.item_id,
        )
        # 'No docs needed' on the board -> no PR is expected; skip discovery.
        if item.doc_status == cfg.no_docs_status:
            return Verdict(
                row, Status.NO_DOCS,
                detail=f"board 'Doc Status' = {cfg.no_docs_status!r}",
                action="",
            )
        chosen, rejected = _discover_pr_for(gh, cfg, state, item)
        row.discovered_pr = chosen
        row.rejected_prs = _candidate_rejects(rejected, cfg, state)
        return evaluate(cfg.deadline, row, cfg.dest_branch)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futs = {pool.submit(work, it): it for it in keps}
        for fut in as_completed(futs):
            it = futs[fut]
            try:
                verdicts.append(fut.result())
            except Exception as e:
                logger.error("scan failed for {}: {}", it.kep, e)
            done += 1
            if progress:
                progress(done, len(keps))

    verdicts.sort(key=lambda v: v.sort_key)
    return verdicts


def _demo() -> None:
    """Self-check for the cycle-window + dismissal gate (pure, no network)."""
    from pathlib import Path

    def pr(url, day):
        return PRInfo(number=int(url.rsplit("/", 1)[1]), url=url, base_ref="main",
                      state="CLOSED", is_draft=False, repo="kubernetes/website",
                      created_at=day)

    class C:  # minimal cfg stand-in
        cycle_start, cycle_end = "2026-05-01", "2026-08-31"

    old = pr("https://github.com/kubernetes/website/pull/40065", "2022-03-10T00:00:00Z")
    cur = pr("https://github.com/kubernetes/website/pull/50000", "2026-06-01T00:00:00Z")
    assert not _in_cycle(old, C)          # prior-cycle PR -> filtered out
    assert _in_cycle(cur, C)              # this-cycle PR -> kept
    assert PRInfo(1, "u", "main", "OPEN", False, "r").created_at == ""  # unknown date
    assert _in_cycle(PRInfo(1, "u", "main", "OPEN", False, "r"), C)     # unknown -> keep

    st = State(path=Path("/dev/null"))
    assert _candidate_rejects([old, cur], C, st) == [cur]   # only in-cycle survives
    st.dismissed_prs[cur.url] = "kep"
    assert _candidate_rejects([old, cur], C, st) == []      # dismissed PR dropped

    # Invariant: the window gates only *rejected* PRs. A PR correctly targeting
    # dev-1.37 is "acceptable" -> becomes `chosen` -> never sees the window,
    # even if it was opened years before cycle_start.
    correct_but_old = PRInfo(7, "https://github.com/kubernetes/website/pull/7",
                             "dev-1.37", "OPEN", False, "kubernetes/website",
                             "2022-01-01T00:00:00Z")
    chosen, rejected = _pick_pr([correct_but_old], "dev-1.37")
    assert chosen is correct_but_old and rejected == []
    print("scan._demo ok")


if __name__ == "__main__":
    _demo()
