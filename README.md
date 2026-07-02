# release-radar

Tracks a Kubernetes release project-board view against that view's deadline
rules, and flags what needs attention. The first rule (`placeholder_pr`) checks
whether the KEPs on the board have a docs **placeholder PR** open against
`dev-<release>` in `kubernetes/website` before the docs deadline; more views and
deadline rules can be added (see [Adding another deadline](#adding-another-deadline)).

For each KEP row assigned to a configured docs shadow it:

1. discovers a `kubernetes/website` PR — via the KEP issue's timeline
   (cross-referenced / connected PRs), then the description, then **new**
   comments since the last run (resume cursor persisted in `state.json`);
2. checks the PR is acceptable (base branch `dev-1.37`, not closed);
3. compares against the board's `Docs PR` field and classifies the row:

   | status | meaning |
   | --- | --- |
   | `meets` | acceptable PR, already on the board |
   | `needs_board` | acceptable PR found, board `Docs PR` empty → offer to write it |
   | `mismatch` | board points at a *different* PR → warn, probe |
   | `bad_pr` | an in-cycle PR exists but on the wrong base branch / closed → review candidate |
   | `no_pr` | authors haven't opened a website PR yet |
   | `no_docs` | board `Doc Status` = `No docs needed` → not tracked, no PR expected |

`needs_board` rows can be written back to the board from the TUI.

A PR is only surfaced as a `bad_pr` review candidate if it was opened inside the
release cycle window (`cycle_start`/`cycle_end` in the config); a stale PR from a
previous cycle is ignored and the row falls back to `no_pr`. On a `bad_pr` row,
`d` dismisses just that candidate PR (remembered in `state.json`) while the KEP
stays tracked for a real one.

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

TUI keys: `r` rescan · `u` queue/unqueue a board write · `a` apply queued writes ·
`d` dismiss (the candidate PR on a `bad_pr` row, else the whole KEP; remembered) ·
`o` open the KEP · `O` open its PR (accepted or flagged candidate) · `q` quit.

> On a corporate network with TLS interception, prefix `uv` commands with
> `--system-certs` (e.g. `uv run --system-certs …`).

## Config

See `config.example.yaml`. Field names (`Docs Assignee`, `Docs PR`, `Doc Status`)
are case-sensitive — confirm against the live board with
`gh project field-list <n> --owner <org>`. `cycle_start`/`cycle_end` bound the
release window used to filter stale PRs; `no_docs_status` is the `Doc Status`
value that marks a KEP as needing no docs.

## Adding another deadline

`deadline: placeholder_pr` selects a rule in `logic.py::DEADLINE_RULES`. Add a
`evaluate_<name>(row, dest_branch) -> Verdict` function, register it in that
dict, and set `deadline:` in the config. Everything else (board read, PR
discovery, TUI, write-back) is deadline-agnostic.

## Layout

```
logic.py      pure decisions (rules, PR matching) — unit-testable, `--check`
github.py     GraphQL client (schema-verified queries + writes)
scan.py       orchestration: board → filter by assignee → discover PR (threaded) → evaluate
writeback.py  populate the 'Docs PR' field for queued rows
tui.py        Textual UI
cli.py        entry point (+ --no-tui rich report)
config.py     config + state file
```
