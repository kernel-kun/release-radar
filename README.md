# release-radar

Tracks a Kubernetes release project-board view against that view's deadline
rules, and flags what needs attention. It supports two main deadline rules configured via `deadline` in `config.yaml`:

1. `placeholder_pr`: Checks whether KEPs on the board have a docs **placeholder PR** open against `dev-<release>` in `kubernetes/website` before the docs deadline.
2. `pr_ready_for_review`: Checks and tracks whether placeholder PRs are ready for review by posting messages, tracking reminders, and syncing state with the board's `Docs Notes` field.

For each KEP row assigned to a configured docs shadow it:

1. discovers a `kubernetes/website` PR — via the KEP issue's timeline (cross-referenced / connected PRs), then the description, then comments;
2. checks the PR is acceptable (base branch `dev-1.37`, not closed);
3. evaluates it against the active deadline's rule and classifies the row.

## Deadline Modes

### 1. Placeholder PR (`placeholder_pr`)
Compares discovered PR against the board's `Docs PR` field:

| status | meaning |
| --- | --- |
| `meets` | acceptable PR, already on the board |
| `needs_board` | acceptable PR found, board `Docs PR` empty → offer to write it |
| `mismatch` | board points at a *different* PR → warn, probe |
| `bad_pr` | an in-cycle PR exists but on the wrong base branch / closed → review candidate |
| `no_pr` | authors haven't opened a website PR yet |
| `no_docs` | board `Doc Status` = `No docs needed` → not tracked, no PR expected |

### 2. PR Ready for Review (`pr_ready_for_review`)
Uses GitHub comments to offload tracking state invisibly using tracking comments containing metadata:
`<!-- release-radar: {"reminder_number": N, "ready_for_review": bool} -->`.

Compares the state against the board's `Docs Notes` field and suggests writebacks:
- For Merged PRs: `✅ (Merged)`
- For Draft, Closed, or Open PRs: `{color_dot} ({pr_state}) {count} Reminder Sent`

Where:
- **`{color_dot}`** is:
  - `🟢` for Open and marked ready
  - `🟠` for Open and not marked ready
  - `🔴` for Draft or Closed
- **`{pr_state}`** is: `Closed`, `Draft`, or `Open`
- **`{count}`** is the number of reminder comments sent to the author.

---

## Setup

```bash
uv sync                              # install deps
cp config.example.yaml config.yaml   # then edit config.yaml
```

Reading a project board needs the Projects scope, which the default `gh`
token does **not** have. One-time:

```bash
gh auth refresh -s project -h github.com
```

(`read:project` is enough for read-only; `project` also allows the board write-back.)
The tool reuses your `gh` token via `gh auth token` — no separate secret.

## Run

```bash
uv run release-radar            # TUI
uv run release-radar --no-tui   # plain report (CI/logs), exit 1 if rows need attention
uv run release-radar --check    # logic self-test, no network
```

TUI keys:
- `R` ➡️ rescan
- `u` ➡️ queue/unqueue a board write
- `a` ➡️ apply queued board writes
- `d` ➡️ dismiss (the candidate PR on a `bad_pr` row, else the KEP; remembered)
- `o` ➡️ open the KEP issue
- `O` ➡️ open its PR (accepted or flagged candidate)
- `m` ➡️ send reminder message to the PR (only in `pr_ready_for_review` mode; checks WIP/TODO title confidence)
- `h` ➡️ view chronological reminder comment history popup (only in `pr_ready_for_review` mode)
- `r` ➡️ mark PR as ready for review (only in `pr_ready_for_review` mode; edits the last tracking comment on GitHub)
- `q` ➡️ quit

> On a corporate network with TLS interception, prefix `uv` commands with
> `--system-certs` (e.g. `uv run --system-certs …`).

## Config

See `config.example.yaml`. Field names (`Docs Assignee`, `Docs PR`, `Doc Status`, `Docs Notes`)
are case-sensitive — confirm against the live board with
`gh project field-list <n> --owner <org>`. `cycle_start`/`cycle_end` bound the
release window used to filter stale PRs.

## Adding another deadline

`deadline: <name>` selects a rule in `logic.py::DEADLINE_RULES`. Add a
`evaluate_<name>(row, dest_branch) -> Verdict` function, register it in that
dict, and set `deadline:` in the config.

## Layout

```
logic.py      pure decisions (rules, PR matching) — unit-testable, `--check`
github.py     GraphQL client (schema-verified queries + writes)
scan.py       orchestration: board → filter by assignee → discover PR (threaded) → evaluate
writeback.py  populate board fields for queued rows
tui.py        Textual UI + modal history and confirmation popups
cli.py        entry point (+ template selection & rich GFM preview rendering)
config.py     config + state file
```
