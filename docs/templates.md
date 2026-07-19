# Release Radar Message Templates

Message templates allow you to define standardized reminders to send to Pull Request authors when tracking deadlines such as "PR Ready for Review".

## Directory Structure
Place your `.md` template files inside the `templates/` directory in the root of the project. During tool startup, you will be prompted to select one of these templates to use for the session.

## Template Anchors
You can use the following string formatting variables in your templates. They will be dynamically interpolated with the details of the PR and KEP issue when sending:

| Variable | Description |
| --- | --- |
| `{pr_author}` | The GitHub username of the Pull Request author. |
| `{pr_assignees}` | Comma-separated list of GitHub usernames assigned to the PR. |
| `{pr_status}` | The current status of the PR (`Draft` or `Ready for review`). |
| `{kep_author}` | The GitHub username of the KEP issue author. |
| `{kep_assignees}` | Comma-separated list of GitHub usernames assigned to the KEP issue. |
| `{kep_title}` | The title of the KEP. |
| `{kep_url}` | The GitHub URL of the KEP issue. |

## Example Template

```markdown
Hi @{pr_author},

This is a reminder that the Docs PR ready for review deadline is approaching. Please mark this PR as ready for review when it's ready.

- **KEP Assignees**: @{kep_assignees}
- **KEP URL**: {kep_url}

Thank you!
```
