# Goal Tracker: Build-Ready Spec

A web app for setting goals with deadlines, breaking them into weekly targets, logging daily progress, and seeing how far ahead or behind plan you are. Examples: marathon training, weight loss, learning a language, completing a syllabus.

## 1. Tech constraints

- Latest stable Flask, Python 3.11+
- No third-party packages other than Flask
- SQLite via `sqlite3` (a single local file)
- Passwords hashed with `hashlib.scrypt`; CSRF tokens from `secrets`
- Secret key read from an environment variable, with a clear error if it is missing
- Charts rendered server-side as inline SVG (no JavaScript chart library)
- Runs locally only; lives in the `lowcode-project` repo
- Mobile-first, responsive, accessible (labels, contrast, keyboard navigation)

## 2. Decisions made

- A user can have multiple goals, plus a combined dashboard
- Friends are mutual: both people must accept the connection
- Each goal can have several metrics, with exactly one marked primary
- On track means hitting the exact target; each goal has a user-set acceptance threshold
- Daily entries roll up into weekly totals
- Email and password login

## 3. Data model

| Table | Key fields |
| --- | --- |
| `users` | id, name, email (unique), password\_hash, created\_at |
| `goals` | id, user\_id, title, description (max 50 words), start\_date, end\_date, acceptance\_pct (default 0 = exact target), share\_level (private / summary / full), status |
| `metrics` | id, goal\_id, name, unit, direction (increase / decrease), aggregation (sum / latest), start\_value, final\_target, is\_primary |
| `plan_versions` | id, metric\_id, version\_no, end\_date, final\_target, reason, created\_at. Version 1 is the original and is never overwritten. |
| `weekly_targets` | id, plan\_version\_id, week\_start, target\_value, description (max 50 words) |
| `entries` | id, metric\_id, entry\_date, value, note. One per metric per day. |
| `friendships` | id, requester\_id, addressee\_id, status (pending / accepted / declined / revoked) |
| `comments` | id, goal\_id, author\_id, body, created\_at |

## 4. Behaviour rules

- **Weekly status per metric:** a week is *met* if actual is at or past target (direction respected), or within the goal's acceptance threshold of target. Otherwise it is *behind*. Variance is shown per week and cumulatively.
- **Goal score:** driven by the primary metric. Secondary metrics show their own status chips but do not change the goal's headline status.
- **Roll-up:** cumulative metrics (e.g. miles) sum the week's daily entries; point-in-time metrics (e.g. weight) use the latest entry that week.
- **Combined dashboard:** one card per goal showing the primary metric's percent of plan, a status chip (ahead / on track / behind), weeks ahead or behind, and current streak. Units are never added together.
- **Plan generation:** weekly targets are generated from start to end date, distributed evenly, ramped linearly, or edited manually.
- **Revisions:** changing the final target or end date creates a new plan version with a required reason. The graph shows the original as a dashed line and the current plan as solid, and the history page lists every version.
- **Friends:** mutual connection only. A friend sees a goal only at the level the owner set (private, summary, or full detail). Revoking removes access immediately. Friends can comment on goals shared with them.

## 5. Form validation (server-side)

On any failure: re-render the same form with HTTP 400, show an error summary at the top and an inline message under each field, preserve everything the user typed, and set `aria-invalid` and `aria-describedby`. Jinja autoescape stays on.

| Field | Rule |
| --- | --- |
| Name | Required, trimmed, max 100 characters |
| Email | Required, well-formed, max 254 characters, unique at registration |
| Password | Required, at least 10 characters |
| Goal title and description | Required, max 50 words |
| Metric name and unit | Required |
| Start date | Valid ISO date, not in the past |
| End date | Valid date, after start, at least 1 week away |
| Final target | Numeric, greater than 0 |
| Acceptance threshold | Numeric, 0 to 50 (%) |
| Weekly target value | Numeric, greater than 0; week start within the goal's date range |
| Weekly target description | Required, max 50 words |
| Daily entry | Numeric; date within goal range and not in the future |

## 6. Routes

| Route | Purpose |
| --- | --- |
| `/` | Combined dashboard |
| `/register`, `/login`, `/logout` | Accounts |
| `/goals/new` | Create goal and metrics |
| `/goals/<id>` | Goal detail and graph |
| `/goals/<id>/log` | Log daily entries |
| `/goals/<id>/plan` | Edit weekly targets |
| `/goals/<id>/revise` | Change final target or end date |
| `/goals/<id>/history` | Version timeline |
| `/goals/<id>/share` | Set share level |
| `/friends`, `/friends/invite` | Connections |
| `/contact` | Optional feedback form |

## 7. Design

Clean card layout, one accent colour, generous white space, a progress ring per goal on the dashboard, and green / amber / red chips for ahead / on track / behind. Empty states include a clear call to action. Dates display in the user's locale format.

## 8. Acceptance criteria

1. Any empty required field shows a visible error and keeps the other values.
2. A malformed email such as `a@b` is rejected server-side even if browser validation is bypassed.
3. Each goal has exactly one primary metric.
4. With a threshold of 0, a week counts as met only when the target is hit exactly.
5. Daily entries update the weekly roll-up and the graph immediately.
6. Revising a goal keeps the original version visible.
7. A friend never sees more than the owner's share level allows.
8. The app runs with only Flask installed.
