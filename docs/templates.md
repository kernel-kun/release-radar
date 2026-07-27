# Release Radar Message Templates

Message templates allow you to define standardized reminders to send to Pull Request authors and KEP issue owners when tracking deadlines such as "PR Ready for Review" and "Docs Freeze".

## Directory Structure
Place your `.md` template files inside the `templates/` directory (or subdirectories like `templates/docs-pr/`). During tool startup, you will be prompted to select one of these templates to use for the session (or specify one via `--template <path>`).

## Template Anchors & Variables

User mentions (`{pr_author}`, `{pr_assignees}`, `{kep_author}`, `{kep_assignees}`, `{doc/KEP owners}`) are **automatically formatted into space-separated `@` mentions** (e.g. `@user1 @user2`). Do not add a manual `@` in front of these variables in template files.

| Variable | Description |
| :--- | :--- |
| `{pr_author}` | Formatted `@` mention(s) of the Pull Request author(s). |
| `{pr_assignees}` | Formatted `@` mention(s) of GitHub usernames assigned to the PR (or `"none"`). |
| `{pr_url}` | Direct GitHub URL of the Docs Pull Request. |
| `{pr_number}` / `{pr_num}` | Pull Request number (e.g. `100`). |
| `{pr_status}` | Current status of the PR (`Draft` or `Ready for review`). |
| `{kep_author}` | Formatted `@` mention(s) of the KEP issue author(s). |
| `{kep_assignees}` | Formatted `@` mention(s) of GitHub usernames assigned to the KEP issue (or `"none"`). |
| `{kep_title}` | Title of the KEP issue. |
| `{kep_url}` | Direct GitHub URL of the KEP issue. |
| `{doc/KEP owners}` | Formatted `@` mention(s) addressing PR author(s) or KEP author(s). |
| `{release_version}` | Target release version string (e.g. `1.37`). |
| `{future-release}` | Target release version string with `v` prefix (e.g. `v1.37`). |
| `{ready_review_deadline}` / `{ready_for_review_deadline}` / `{ready_to_review}` | Configured Ready for Review deadline date string (from `config.yaml`). |
| `{docs_freeze_deadline}` / `{docs_freeze}` | Configured Docs Freeze deadline date string (from `config.yaml`). |
| `{crit1}` ... `{crit4}` | Docs Freeze checklist item status (`x` or ` `). |
| `{docs_freeze_status}` | Enhancement Docs Freeze status (`Tracked for Docs Freeze` or `At Risk for Docs Freeze`). |

## Example Template

```markdown
Hello {doc/KEP owners} 👋! v{release_version} Docs team here,

As we approach:
- Ready to Review deadline: {ready_review_deadline}
- Docs Freeze deadline: {docs_freeze_deadline}

Here's where this enhancement currently stands:
- [{crit1}] The docs PR(s) to the `k/website` repo that are related to your enhancement are linked in the above issue description (for tracking purposes).
- [{crit2}] The docs PR(s) is created against the dev-{release_version} branch.
- [{crit3}] The docs PR(s) are in Ready to Review state wherein they are updated with all the changes required and marked ready to review.
- [{crit4}] The docs PR(s) are ready to be merged (they have `approved` and `lgtm` labels applied) by the Docs Freeze deadline.

Docs PR: {pr_url}

The status of this enhancement is marked as {docs_freeze_status}.

If you anticipate missing docs freeze, you can file an [exception request](https://github.com/kubernetes/sig-release/blob/master/releases/EXCEPTIONS.md) in advance.
```
