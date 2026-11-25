# 03: Every writer goes through the door

**What to build:** every tool that creates or changes a company relationship does it through ticket 01's door, so the new format is complete by construction and the old per-client row is written by nothing but the door.

- The operator's relationship command (`set_company_relationship.py` in the private toolkit) becomes the door's command line: record, decide (monitor, client choice, leave out, vetting), stop monitoring, withdraw, show (current state and history). Dry run by default. Its old flags keep working (`--relationship suspected_competitor` = record with possible).
- The other known writers (re-derive the list from the code before starting): the competitor research tool, the client-choice importer, the confirmed-users ingester, the competitor-targeting list builder and fit scorer. Several live in the private toolkit, outside this repo; they move in the same change set.

- The two readers of company-level monitor decisions (the voice selector, the coverage-plan page) read the link-level copy instead; then the company-level decisions are closed "moved to company link <id>".

**Blocked by:** 01, 02

**Status:** ready-for-agent

- [ ] No INSERT/UPDATE of the old table remains outside the door (a check in CI greps for it)
- [ ] Each moved writer run once in dry-run against a test instance; its output equals the pre-change output
- [ ] The equality check from 02 still passes on every instance afterwards
