# 02: Convert the existing rows in every instance

**What to build:** every instance knows its own company, and every existing per-client relationship row, in all seven instances, is also present in the new format, so the new current state answers every question the old rows answer, and nothing claims more than the old row recorded.

- **Own company first.** Each instance's own company is marked `client/own_company`: `primary` for the client itself, `brand` for another own brand (one instance has two `self` rows). Instances with no `self` row get one chosen with the operator. The house instance's own company is the house itself; its prospects are the `to` side of its links, not own companies.
- **Conversion**, per row: a link from the company to the primary own company, with the old row's type (`suspected_competitor` = competitor with confidence `suspected`).

Provenance rules (settled 2026-11-25):

- The row's `source` becomes the marker's writer, its `notes` the marker's notes, `recorded_at` its date. No source recorded stays "no source recorded"; it is never turned into somebody's decision.
- `monitor_desired` becomes a monitor decision only where a decision with a decider already exists (some instances hold `monitoring:monitor` decisions on the company; those move to the link, the company-level one closed "moved to the link <id>"). Otherwise it travels as an unclassified record. A `false` is never turned into a do_not_monitor decision: it has two meanings (rejected / never asked), which 059 documents.
- `client_disposition` becomes a client-choice decision citing `client_disposition_source`; `not_offered` and NULL travel as they are, never as a rejection.
- `status`, `rep_discovery` and `discovery_config` travel unchanged.
- Existing links with no live marker (the house instance's competitor links carry scope and evidence but no marker) get one, so `company_link_state` shows them.
- Leave-out decisions are NOT created here; that is the app's left-out list work.

**Blocked by:** 01

**Status:** ready-for-agent

- [ ] Dry run per instance prints what it would write; execute is per instance, id-keyed, one transaction, rowcounts asserted against the dry run
- [ ] After execute, per instance: old rows and `v_company_relationships_from_links` are equal, column by column
- [ ] Re-running writes nothing
- [ ] Existing company links (the house instance, one client instance) are reused, never duplicated; the house instance's links read through `company_link_state`
