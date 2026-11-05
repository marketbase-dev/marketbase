# 01: Migrate the identity merge to the comp-intel shape

**What to build:** MarketBase adopts the same non-destructive identity-merge shape that competitor-intel is building (spec: `competitor-intel/.scratch/person-merge/SPEC.md`, decisions Q0 and Q6, 2026-11-03), so the two databases behave identically ahead of their unification. This SUPERSEDES the 2026-10-10 plan (resolve on read, `person_rows` + view rename, denormalised `canonical_id`): that plan needed a trigger-kept canonical column on every child table anyway, which is re-pointing under another name; keeping canonical in the FK column makes every present and future reader correct by default, and the per-row original id keeps the provenance.

- `leads.merged_into uuid null REFERENCES leads(id)`.
- `merged_from_lead_id uuid null` beside every `lead_id` column on every child table (tags, campaign_members, conversations, assertions, lead_sources, lead_identities, … — derive the list from `pg_constraint` and assert it in a test so a new referencing table cannot be silently missed).
- `lead_identity_conflicts (id, lead_id, kind, value, owner_lead_id, found_at, source, resolved_by_merge uuid null)`: `lead_identity_claim()` writes a row on its `duplicate:<owner>` outcome. The existing `flag:duplicate_lead` + `expansion:do_not_push` tag pair stays as a derived convenience; the row is the finding, and unresolved rows ARE the merge queue.
- `lead_merges (id, dup_id, keeper_id, merged_at, merged_by, moved jsonb, stayed jsonb, snapshot_ref)`.
- `merge_lead(_dup, _keeper, _by) returns jsonb`, one transaction: refuses if either is already merged, the same row, or the pair shares no `linkedin_urn` identity; for each `lead_id` column, re-points rows from dup to keeper and stamps `merged_from_lead_id`, EXCEPT rows that would collide on that table's unique key (e.g. `campaign_members (campaign_id, lead_id)`), which stay on the dup untouched — the pre-merge enrolment is true; moves the dup's `lead_identities` to the keeper as non-primary; COALESCE-fills keeper scalars that are NULL, never overwriting a non-NULL value; sets `merged_into`; resolves the pair's conflict rows; writes the `lead_merges` row; DELETEs nothing.
- A trigger on every child table that REFUSES (raises, never redirects) an insert/update aiming a `lead_id` at a merged lead.
- The merge tool (dry run → the owner approves → one id-keyed batch with rowcounts asserted in-transaction; snapshot of affected rows kept IN the database, never on disk) then runs over the open duplicate groups recorded in the platform TODO. The existing `tools/dedupe_leads.py` (moves children, COALESCE-fills, then DELETEs the duplicate) is retired: its body becomes a refusal pointing here.

Shape parity with comp-intel (`person_*` ↔ `lead_*`): `lead_identity_kinds` already exists (058) and `lead_identity_claim()` is already the sole writer (070/071); comp-intel copies those. Related, not blocking: issue #1 (a persons entity).

**Blocked by:** competitor-intel ticket 05 (`merge_person`) merged and proven on its tier-1 pilot — cross-repo, so a note, not a file here.

**Status:** built 2026-11-05 (schema/080, `scripts/check_merge_lead.py` 44/44 on a fresh database, `tools/merge_leads.py`); applying to the instances follows the merge.

**As built (beyond the text above):**
- The dup's own `flag:duplicate_lead` (and its paired `expansion:do_not_push`) stays on the dup: moved, it would mark the keeper a duplicate and stop it being pushed.
- The dup's identity columns (`linkedin_url`, `linkedin_urn`, `member_urn`, `public_id`) are cleared and given to the keeper where the keeper's are NULL, so an upsert keyed on `linkedin_url` lands on the keeper; `lead_merges.cleared` keeps the values, and they are the keeper's identities.
- The dup's other open conflicts are handed to the keeper (with `merged_from_` provenance), so a group of three merges one pair at a time.
- 080 backfills conflict rows from the leads columns (the findings the claim made before it had a table), so existing pairs are mergeable.
- `tools/merge_leads.py`: default keeper = most relationship history (child rows, the dup's own duplicate tags excluded), then the oldest row; `--pair DUP:KEEPER` overrides. Dry run → digest → `--execute --plan <digest>`, a per-pair transaction with an in-database snapshot (`merge_snapshot_rows`).
- Callers that write a lead_id they looked up must resolve `leads.merged_into` (or go through lead_identities) once merges happen, or the refusal trigger fails them loudly. Known writers to check: sf_sync.py, the per-client bookings collectors, research_competitor.py, rehandle_leads.py, the Fresh engager harvesters, list_voice_posts.py / select_voices.py / import_voice_roster.py.
- Open: one instance's ~1,400 rep/engager twins share a vanity URL, not a URN (open `linkedin_url` conflicts), so `merge_lead()` refuses them by the rule above.

- [x] Schema: `merged_into`, `merged_from_lead_id` on every child table, `lead_identity_conflicts`, `lead_merges`; guard test over `pg_constraint`
- [x] `lead_identity_claim()` writes a conflict row on `duplicate:`; the tag pair still lands; existing tests pass
- [x] `merge_lead()` fixture test: non-colliding rows re-pointed and stamped; colliding rows (a `campaign_members` twin) untouched on the dup; every table's row count unchanged; refuses already-merged / same-row / no-shared-URN
- [x] Refuse trigger raises on a write aimed at a merged lead
- [x] `dedupe_leads.py` refuses to run and names this ticket
- [ ] Migration follows the repo's bookkeeping rules (CI pushes; never from an unmerged branch; no back-dated stamps); applied to every client instance by the sanctioned path
- [ ] Memory `identity-merge-is-non-destructive` and the platform TODO item updated to say the mechanism changed and why
