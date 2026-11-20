# Roles and permissions

Every AIOps user has exactly one role. Roles are ranked: **viewer < operator < admin**. A higher role can
do everything a lower one can. Admins assign roles under **Configure → Users**.

| Role | In one sentence |
|---|---|
| **Viewer** | Sees the daily work and what the workers are doing, but changes nothing. |
| **Operator** | Viewer, plus decides what the Expert Tasks propose: approve, override or reject in the Inbox. |
| **Admin** | Everything: configures the Expert Tasks, data sources, users and settings, and sees the Container logs and the Audit log. |

The installer creates the first admin, `admin`, with a generated password it prints once (or the one given
with `--admin-password`). Change it after the first login.

## Menu

The sidebar has three groups. A group or page the user's role cannot use is not shown, and opening its
address directly goes back to the Dashboard.

| Group | Page | Viewer | Operator | Admin |
|---|---|:-:|:-:|:-:|
| **Work** | Dashboard | yes | yes | yes |
| | Inbox: what needs a human now (awaiting review, open L0 findings) | yes | yes | yes |
| | History: what was approved, executed, failed, rejected or superseded, by people and by automation | yes | yes | yes |
| **Configure** | Expert Tasks (and each task's page) | - | - | yes |
| | Data sources (and Manage GeoIP) | - | - | yes |
| | Users | - | - | yes |
| | Settings (autonomy cap, kill switch, AI assistance, updates) | - | - | yes |
| **Observe** | Expert Task history | yes | yes | yes |
| | Runners | yes | yes | yes |
| | Container logs | - | - | yes |
| | Audit log | - | - | yes |

The whole **Configure** group is visible to admins only.

## Actions

| Action | Where | Viewer | Operator | Admin |
|---|---|:-:|:-:|:-:|
| See items, evidence, rationale and outcome | Inbox, History | yes | yes | yes |
| Save personal filters; share a filter with everyone | Inbox, History | yes | yes | yes |
| Delete someone else's shared filter | Inbox, History | - | - | yes |
| Approve, approve with override, reject | Inbox | - | yes | yes |
| Bulk approve (selected, group above threshold, all matching) | Inbox | - | yes | yes |
| Write the AI shift summary (when AI is on) | Dashboard | - | yes | yes |
| See run details and the run log | Expert Task history | yes | yes | yes |
| Follow the live container log of a run | Expert Task history | - | - | yes |
| See workers, queues and the schedule | Runners | yes | yes | yes |
| Run a task now | Expert Tasks | - | - | yes |
| Change schedule, source, parameters, autonomy level and threshold | Expert Tasks | - | - | yes |
| Approve the site hierarchy baseline | Expert Tasks | - | - | yes |
| AI reviewer insights and AI owner reports | Expert Tasks | - | - | yes |
| Add, edit, delete and test data sources | Data sources | - | - | yes |
| Upload GeoIP databases, choose fields, edit GeoIP mappings | Data sources → Manage GeoIP | - | - | yes |
| Create users, change roles, disable users, reset passwords | Users | - | - | yes |
| Global autonomy cap, kill switch | Settings | - | - | yes |
| AI mode (No AI, Local LLM, Azure OpenAI) | Settings | - | - | yes |
| Update track, check and install updates | Settings | - | - | yes |
| Follow container logs | Container logs | - | - | yes |
| Read the audit trail | Audit log | - | - | yes |
| Change own password and theme | User menu | yes | yes | yes |

## Notes

- **Autonomy.** At L2 and above a task approves items itself when the confidence is at or above its
  threshold. Those decisions appear in **History** as decided by `auto`. Only admins can raise a task's
  level, and the effective level is always the lowest of the task level, the task cap and the global cap.
  The kill switch stops all execution against the network, including approved items.
- **Auditing.** Logins (and failed logins), every decision (manual and automatic), executions, task and
  setting changes, data source changes and tests, GeoIP changes, user changes, update actions, AI summaries
  and every container log view are written to the audit log.
- **Secrets.** Catalyst Center passwords, CA bundles and Azure OpenAI keys are stored encrypted and are
  never returned by the API, not even to admins.
- **Read access behind the menu.** The Inbox and Dashboard need task names, data source names and the
  kill switch state, so these remain readable by every signed-in user through the API. Changes, tests,
  runs, user data, container logs and the audit trail need the admin role on the server, not only in the
  menu.
- **API tokens.** The API accepts the session cookie or `Authorization: Bearer <token>` with the same
  token; the same role rules apply.
