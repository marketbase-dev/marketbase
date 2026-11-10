# 02: `merge_lead`'s "nothing lost" check counts only the pair's rows

**What to build:** step 6 of `merge_lead()` (080; latest body in 087) proves nothing
was lost by comparing `count(*)` of every touched table, **whole**, before and after
the merge. Live writers insert into those tables all the time (post and engagement
harvests, people sweeps, enrichment), so the check has two faults:

- **False alarms.** A recent run stopped at its last pair with
  `merge_lead: posts.count changed <before> -> <after>; rolled back`: a post harvest
  landed rows in `posts` while the merge ran. Nothing was wrong with the pair. It
  re-planned byte-identical and merged on retry. Harvests run for hours, so on any
  instance with a live harvest, a merge run can stop partway at any pair.
- **A blind spot.** A lost row of the pair and a stranger's new row in the same
  table cancel out, so the whole-table count passes a real loss.

Count only the rows that reference the pair: in each touched table, the rows where
any of its lead-id columns equals the dup or the keeper. Those are exactly the rows
the merge can touch, and it re-points them between the two ids, so their number
cannot change unless one was lost or added.

**Reference implementation:** the person-side twin of this function, `merge_person()`
(same registry shape, `person_*` ↔ `lead_*`), already made exactly this change; its
ticket and migration paths are in the platform TODO item.
Adapted to `merge_lead()`, the pattern is:

```sql
-- declare: v_where jsonb;
SELECT jsonb_object_agg(t, w) INTO v_where
  FROM (SELECT split_part(e, '.', 1) AS t,
               string_agg(format('%I = any($1)', split_part(e, '.', 2)), ' OR ' ORDER BY e) AS w
          FROM unnest(c_moves || ARRAY['lead_identities.lead_id', 'leads.id']) e
         GROUP BY 1) s;
v_rows_before := '{}';
FOR v_tbl IN SELECT jsonb_object_keys(v_where) LOOP
  EXECUTE format('SELECT count(*) FROM %I WHERE %s', v_tbl, v_where ->> v_tbl)
    USING ARRAY[_dup, _keeper] INTO v_n;
  v_rows_before := v_rows_before || jsonb_build_object(v_tbl, v_n);
END LOOP;
-- step 6: the same EXECUTE ... USING ARRAY[_dup, _keeper] INTO v_after per table,
-- compared with v_rows_before plus the 083/086 adjustments below.
```

**Details to keep:**

- **Start from the live body** (`pg_get_functiondef('merge_lead(uuid,uuid,text)')`),
  never from a migration file. When this was written every instance carried the same body,
  087's; record its md5 before applying (the platform TODO item holds the baseline).
- **Build the per-table predicate from `c_moves`** plus `lead_identities.lead_id`
  (step 3 moves identities) and `leads.id` (always the pair's two rows). Do not hand
  list tables: a column added to `c_moves` later must be counted automatically.
- **The two step-6 adjustments stay valid**, because both attach to the keeper
  (read from the live bodies):
  - 083: `retire_merge_flags()` deletes keeper `lead_tags` rows
    (`WHERE t.lead_id = v_m.keeper_id`), each audited by one `assertions` row with
    `subject_lead_id = keeper`, so `-v_retired` / `+v_retired` still balance.
  - 086: `merge_lead_move_email()`'s `identity` outcome files one `lead_identities`
    row for the keeper (`lead_identity_claim(v_m.keeper_id, 'email', …)`), so `+1`
    still applies.
  - Re-check both against the live bodies when you build; if either ever writes a
    row that does not reference the pair, its adjustment must go.
- **Keep the error text** `merge_lead: %.count changed % -> %; rolled back`.
- **No other behaviour changes.** Refusals, moves, collisions, fills and clears are
  untouched; the fixture's existing checks prove it.

**Part 2, the tool (`tools/merge_leads.py`).** On `--execute` the first refusal
escapes as a traceback (`psycopg.errors.RaiseException`) and the run stops, with
earlier pairs committed and later ones not attempted. Give it the retry the
person-side merge tool has: a `count_race(e)` predicate (the error text contains
`.count changed`) and `with_retries(attempt, undo, tries=3)`, where `undo` is
`conn.rollback()`. A pair whose `merge_lead` raised the count-changed error is retried
up to 3 times, each attempt fully rolled back first. Any other error stops the run
cleanly, naming the pair, with no traceback. With pair-scoped counts the retry fires
only when a live writer touched the pair's own rows mid-merge; a real loss trips it
every time and still stops the run.

The installed copy of the tools (the directory named in the comment near the top of
`tools/merge_leads.py`) holds `merge_leads.py` as a **regular file, not a link**; only
`schema/` there links into this repo. It was identical to `tools/merge_leads.py` on
the time of writing. After the merge, copy the merged file over it and `diff` the two.

**Tests: `scripts/check_merge_lead.py`** (one transaction, always rolled back)
gains the two checks the person-side harness gained, each using a trigger that
fires inside the merge, between its two counts:

1. The trigger inserts an **unrelated** lead's row into a counted table (`posts` is
   the realistic one). The merge succeeds.
2. The trigger **deletes one of the pair's moved rows**. The merge refuses with the
   count-changed error.

All existing checks still pass, and CI `validate` stays green: migrations apply
cleanly and a second run is a no-op.

**Number:** the next free one after merging `origin/main` (090 was the newest when
this was written). A stamp is picked after the merge, never before.

**Apply (merging deploys nothing):** the repo's own migration tool, from the
installed copy: first `--dry-run` (expect exactly this migration pending on every
instance), then for real. Then on every instance the live `merge_lead` body differs
from the recorded baseline and matches the migration's, and
`python3 scripts/check_merge_lead.py --instance <one>` passes (rolled back).

**Blocked by:** None

**Status:** ready-for-agent

- [ ] Migration from the live body: the step-1 and step-6 counts scoped to the pair's rows through a predicate built from `c_moves`; error text unchanged; 083/086 adjustments re-checked
- [ ] `check_merge_lead.py`: a stranger's row landing mid-merge passes; a lost pair row mid-merge refuses; every existing check passes; CI green
- [ ] `merge_leads.py`: count-changed retried up to 3 times, each fully rolled back; any other error stops cleanly naming the pair; installed copy updated and diffed
- [ ] Applied to every instance by the repo's migration tool; the live body checked on each
- [ ] Memory `identity-merge-is-non-destructive` and the platform TODO updated
