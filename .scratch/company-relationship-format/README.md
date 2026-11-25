# One company-relationship format

Settled with the operator 2026-11-24/25 in a grilling round. Tipline (the competitor-intel database) and every MarketBase instance store what a company is to a client in ONE format, so the two databases can merge by copying. MarketBase goes first; Tipline copies it (Tipline's tickets live in its own repo, `.scratch/left-out-list/`).

The format is the one `ASSERTIONS_MIGRATION_PLAN.md` §3.4-3.5 already chose and never carried out for `company_relationships`:

- **A company link** (`company_edges`) names both companies: "<company> competitor <the client's own company>". It holds identity only and is never deleted.
- **Every judgment about the link is an assertion** whose subject is the link, with history (`removed_at`, never deleted). Separate facts, never one "kind" field:

| Fact | Values | Kind of record |
| --- | --- | --- |
| what it is | the link's relationship (competitor, suspected_competitor, other, ...) | the link itself |
| how close | direct / adjacent / aspirational | observation or decision |
| monitor its sellers | monitor / do_not_monitor | decision (decided_by, via, note) |
| client's choice | selected / deselected / not_offered | decision, decided by the client, citing the artifact |
| leave out of scans | leave_out | decision: automatic for the client's own company and every monitored competitor; any other company only by the operator's tick |
| conflict vetting | clear / conflicting / checking | decision |

- **The client's own company is a company row**, recorded once per instance.
- **Two removals**: "stop monitoring, keep leaving out" closes the monitor decision and records do_not_monitor; "added by mistake" closes the link's live assertions with that reason (the link row stays).

Why not the per-client row (`company_relationships`): it cannot name the counterparty (fine while one database held one client, wrong once Tipline and MarketBase merge), and it has no history (a change overwrites the row; the old state was pasted into `notes`).

Readers: 36 tools read `company_relationships`. The table becomes a read-only VIEW with the same name and columns (§4 of the plan), so none of them changes.

| # | Ticket | Blocked by |
| --- | --- | --- |
| 01 | One write door for company relationships | none |
| 02 | Convert the existing rows in every instance | 01 |
| 03 | Every writer goes through the door | 01, 02 |
| 04 | The old table becomes a read-only view | 02, 03 |
