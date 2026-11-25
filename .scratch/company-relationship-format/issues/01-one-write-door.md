# 01: One write door for company relationships

**What to build:** a writer records "Acme Rival is a competitor of Acme" and every judgment about it (how close, monitor or not, the client's choice, leave out of scans, conflict vetting) through one door, and can read back both the current state and the full history of each fact. The door writes the new format (a company link plus assertions on it, see the README). Until ticket 04 it also keeps the old per-client row current, so every existing reader keeps working unchanged.

**Blocked by:** None (can start immediately).

**Status:** done 2026-11-25 (#64). Applied to all seven instances; the check, run inside each instance and rolled back, passes 46/46 and no client row changed.

**As built:**

- The door is database functions, so the connection-tracking app's TypeScript and this repo's Python share one door: `set_company_relationship()`, `set_link_decision()` (monitor, client_choice, leave_out, status), `stop_monitoring()` (do_not_monitor and keeps leave_out), `withdraw_company_link()` (added by mistake). `link_assert()` is the core: identical write = no-op; a change closes the active claim "superseded by <id>".
- A possible competitor is the competitor link with confidence `suspected`, not a second link type, so reclassifying keeps the link and its decisions; the old row's relationship flips in place, as the old tool did.
- Mirroring into the old table copies only the fact a call wrote, so a door write never blanks a value the old row holds before the conversion.
- Withdrawing a link the old table mirrors is refused until 04 (the old table cannot say "withdrawn" without a delete).
- A door write is refused on an instance with no own company recorded when the old table holds that company. Recording own companies therefore moved to 02, and switching `set_company_relationship.py` to the door moved to 03 (until 02 runs the door would refuse it on every client instance).
- Views: `company_link_state` (current, live links only), `company_link_history` (every claim), `v_company_relationships_from_links` (the new format in the old table's shape, for the equality checks in 02 and 04).

- [x] Migration applies cleanly and a second run is a no-op
- [x] A check script on a fresh database: record, change scope, decisions, stop monitoring, mark possible, withdraw; current state and history read back as expected at each step; every refusal fires; no row is deleted
- [x] The old per-client row stays equal to the new current state after every step of that check (a deliberately broken mirror fails it)
- [x] ADR written
- [x] CI green on the PR
- [x] Applied to every instance by the sanctioned path; nothing written to client data (registry rows and functions only)
