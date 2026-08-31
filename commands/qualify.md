---
description: Run a versioned qualifier over a cohort of leads
argument-hint: "--qualifier <name> [--version <v>] [--where <sql>]"
---

# marketbase:qualify

Apply a qualifier to a cohort and record the verdict.

## A qualification is a decision, not a flag

Every run writes a row to `lead_qualifications` with:

- `qualifier_name` and `qualifier_version`
- `qualified` (boolean)
- `reason` (short human-readable string)
- `full_result` (JSONB, the complete output of the classifier)

Never mutate a previous verdict. Write a new row. `lead_current_qualification`
resolves the current verdict per lead, so history stays intact and last
quarter's targeting can be reproduced exactly.

Bump `qualifier_version` whenever the logic changes. Two runs of different
versions must be distinguishable after the fact.

## Selecting the cohort

Scope on something immutable, such as how the lead was sourced, rather than on
a field that churns (a headline or job title that gets refreshed mid-run will
make the cohort shift underneath you).

## Durability

Idempotency is mandatory. Before inserting, check for an existing row on
`(lead_id, qualifier_name, qualifier_version)` and skip it. A re-run after a
crash must resume, not restart. Commit every 25 leads.

For long runs, wrap the runner in a supervisor that relaunches until the cohort
is empty. Transient database DNS failures are common and should not end the job.

## Always apply suppression policies

The main qualifier does not know about deal state. After qualifying, run the
active-deal suppression policy so no lead at an account with an open
opportunity is left in a qualified state:

```sql
SELECT * FROM v_qualified_held_by_active_deal;
```

Report qualified, disqualified, skipped, and held-by-deal counts separately.
