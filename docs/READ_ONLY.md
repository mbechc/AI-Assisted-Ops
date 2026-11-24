# Read-only assessment

You can let Steward assess a Catalyst Center without any possibility of change. The guarantee comes from
**Catalyst Center**, not from Steward: give Steward a Catalyst Center user with the built-in read-only
role, and Catalyst Center refuses every change Steward could ever attempt, whatever Steward's code or
configuration does.

No setting inside Steward replaces this. The kill switch, the global autonomy cap and the per-task caps limit
what Steward tries to do; they are conveniences for running a writable installation, not a security
boundary against a faulty or compromised Steward.

## Set it up

1. In Catalyst Center, open **System > Users & Roles > User Management** and add a user, for example
   `steward-ro`, with the role **OBSERVER-ROLE** (read-only). Use a long random password.
2. In Steward, open **Configure > Data sources**, add (or edit) the Catalyst Center source and enter that
   user and password.
3. Press **Test**. The connection test only reads.

## What you get

- Every Expert Task runs: inventory snapshots, analyses, confidence scores and evidence, reports, the
  Dashboard and the Inbox, and the AI summaries if you enable AI.
- Proposals are listed as usual, so you see exactly what Steward would change.
- If someone approves a change, Steward sends it and Catalyst Center refuses it. The item fails with
  "Catalyst Center refused the change: the account has no write permission", and **Configure > Data
  sources** shows that Catalyst Center refused changes for that source. Nothing in the network changes.

Keep every task at L0 or L1 during an assessment so nothing is sent automatically; with a read-only user it
would be refused anyway.

## Check it on your release

Steward is tested against simulated Catalyst Centers, which cannot prove what a role may read on your
release. After the first runs, check **Runs**: a read that the observer role may not perform fails with
"Catalyst Center request failed (HTTP 403)" in the run of the task that needs it.

## Moving to changes

When the assessment is done and you want Steward to make changes, create a separate Catalyst Center user
with only the permissions the enabled tasks need, replace the read-only user on the data source, and raise
the autonomy per task deliberately. Read the [terms](../DISCLAIMER.md) again: you are responsible for every
change made with Steward.
