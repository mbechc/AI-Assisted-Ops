# Bug hunt

Small bugs and polish items, collected here and fixed together in a later release. Adding an item does not
trigger a fix or a new version.

Status: **open**, **fixed in X.Y.Z** or **won't fix**.

| # | Area | Problem | Status |
|---|---|---|---|
| 1 | Login | The Apple Passwords app (and Safari/iCloud Keychain) does not recognise the login form, so it neither fills in nor offers to save the username and password | fixed in 0.2.5-dev.4 |
| 2 | Dashboard | "Since last shift" shows a raw Python traceback for a failed run instead of a short, readable message | fixed in 0.2.5-dev.4 |
| 3 | Wrong-site detection | Run 27 failed with a database error: two tasks wrote the same cache entry at the same time (`duplicate key ... cache_entries_pkey`) | fixed in 0.2.5-dev.4 |
| 4 | Installer | Redis warns at every start that memory overcommit is disabled (`vm.overcommit_memory`) | fixed in 0.2.5-dev.4 |
| 5 | Workers | worker-inventory restarted on 19 Nov 16:22 while a run was active (run marked failed as orphaned); cause unknown | open, needs info |
| 6 | UI | Sidebar subtitle "Catalyst Center operations assistant" should read "IT Operations Assistant": AIOps will cover more products than Catalyst Center | fixed in 0.2.5-dev.7 |
| 7 | Inbox | The "By group" table looks cluttered: remove the ">= Threshold" and "Need choice" columns and the per-group Approve button | fixed in 0.2.5-dev.7 |

## Notes

### 1. Login not recognised by password managers

`frontend/src/pages/Login.tsx`: the username and password inputs have no `name`, `id` or `autocomplete`
attributes, and the form signs in through a JavaScript API call without navigating. Password managers rely
on these hints to recognise a sign-in form.

Likely fix: `name="username"` with `autoComplete="username"` on the username field, `name="password"` with
`autoComplete="current-password"` on the password field, `method="post"` and an `action` on the form. Check
in Safari and Chrome that saving and filling work.

Related: the Catalyst Center password on **Data sources** and the user password on **Users** should get
`autoComplete="new-password"`, so the browser does not fill in the AIOps login there.

### 2. Tracebacks shown to users

When a run fails, `backend/app/framework/runner.py` stores the full Python traceback in `TaskRun.error`.
The shift summary (`backend/app/framework/digest.py`) shows the first 200 characters of it, which is the
start of the traceback, so the user sees file paths instead of what went wrong.

Likely fix: store a short, readable error (for example "Database error while saving results" or the last
line of the exception) for display, and keep the full traceback in the run log for admins. Use the short
text everywhere a run error is shown to users: the shift summary, Runs and the task page. Check other
places where exception text reaches the UI as well.

### 3. Wrong-site detection run 27 failed

Seen on the lab appliance (0.2.5-dev.2, with mocks). The Postgres log shows
`duplicate key value violates unique constraint "cache_entries_pkey"` at 06:54:08.112, and run 27
(`site_reassignment`) failed at 06:54:08.220.

Cause: `cache_set` in `backend/app/store.py` reads the row and inserts it when missing. AP site assignment
(worker-wireless) and Wrong-site detection (worker-inventory) share cache keys (the site list per source and
the per-AP RF neighbour and AP configuration caches) and run at nearly the same time in the morning. When
both insert the same new key, the second insert fails and the whole run fails with it.

Likely fix: make `cache_set` an upsert (`INSERT ... ON CONFLICT (key) DO UPDATE` on PostgreSQL, the SQLite
equivalent in tests), so concurrent writers never fail. A cache write should also never fail a run. Add a
test with two sessions writing the same key.

### 4. Redis memory overcommit warning

Redis logs `WARNING Memory overcommit must be enabled!` at every start. Without it a background save can
fail under memory pressure.

Likely fix: the installer writes `vm.overcommit_memory = 1` to `/etc/sysctl.d/90-aiops.conf` and applies it
with `sysctl --system` (it is a host kernel setting; a container cannot set it). Mention it in the README
and in `uninstall.sh --purge`.

### 5. worker-inventory restarted during a run

On 19 Nov at 16:22 worker-inventory started, found a run that was still marked running and marked it as
failed (`marked 1 orphaned run(s)`). That is the intended recovery; the open question is why the worker
restarted. If nobody restarted or updated the appliance at that time, check for an out-of-memory kill:
`docker inspect aiops-worker-inventory-1 --format '{{.State.OOMKilled}} {{.RestartCount}}'` and
`journalctl -k | grep -i oom`.

### 6. Sidebar subtitle

The subtitle under the AIOps logo (`frontend/src/App.tsx`, the `brand` block) says "Catalyst Center operations
assistant". Rename it to "IT Operations Assistant", since later releases will support more products than
Catalyst Center. It is the only place the text appears.

### 7. Inbox "By group" table is cluttered

The group table in the Inbox (`GroupsView` in `frontend/src/pages/Review.tsx`) has too much on each row. Remove:

- the **>= Threshold** column,
- the **Need choice** column,
- the per-group **Approve >= N** button.

Without the button, the "Bulk approve threshold" box above the table has no purpose and goes as well. The
table keeps Group, Task, Catalyst Center, Items and Avg conf.; clicking a row still opens the group's items.
Bulk decisions stay available in the **Items** tab (Approve selected, Reject selected, Approve all matching).

### Not bugs (checked)

- `Catalyst Center GET ... -> 429, retry in 1s`: the mocks return HTTP 429 on about every 250th request on
  purpose, to test retries. The request succeeds on the retry.
- `FATAL: terminating connection due to administrator command` (Postgres, 19 Nov 15:36): Postgres was
  stopped or restarted (installation, update or `docker compose` restart) and closed its open connections.
