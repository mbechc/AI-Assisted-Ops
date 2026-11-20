# Bug hunt

Small bugs and polish items, collected here and fixed together in a later release. Adding an item does not
trigger a fix or a new version.

Status: **open**, **fixed in X.Y.Z** or **won't fix**.

| # | Area | Problem | Status |
|---|---|---|---|
| 1 | Login | The Apple Passwords app (and Safari/iCloud Keychain) does not recognise the login form, so it neither fills in nor offers to save the username and password | open |
| 2 | Dashboard | "Since last shift" shows a raw Python traceback for a failed run instead of a short, readable message | open |
| 3 | Wrong-site detection | Run 27 failed with a database error (SQLAlchemy `do_execute`); cause not yet known | open |

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

Seen on the lab appliance (0.2.5-dev.2, with mocks) via the shift summary. The visible part of the
traceback only shows that a database statement failed. Needed: the end of the error from **Runs → run 27**
(or `docker compose logs worker-inventory`) to find the cause.
