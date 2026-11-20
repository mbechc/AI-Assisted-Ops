# Bug hunt

Small bugs and polish items, collected here and fixed together in a later release. Adding an item does not
trigger a fix or a new version.

Status: **open**, **fixed in X.Y.Z** or **won't fix**.

| # | Area | Problem | Status |
|---|---|---|---|
| 1 | Login | The Apple Passwords app (and Safari/iCloud Keychain) does not recognise the login form, so it neither fills in nor offers to save the username and password | open |

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
