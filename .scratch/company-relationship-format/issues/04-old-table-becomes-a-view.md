# 04: The old table becomes a read-only view

**What to build:** `company_relationships` stops being a table anyone can write. Its rows are kept, frozen under a legacy name with writes refused; a view with the same name and the same columns shows the current state from the new format. The 36 readers keep working with no change. The door stops writing the old row.

**Blocked by:** 02, 03

**Status:** ready-for-agent

- [ ] Per instance, before the swap: the view's rows equal the table's rows, column by column; the swap refuses to run otherwise
- [ ] After the swap, a direct INSERT or UPDATE on `company_relationships` fails with a message naming the door
- [ ] The frozen legacy table keeps every row; nothing deleted
- [ ] A smoke run of the readers that run on a schedule (employment check, tip scoring, the competitor sales-force report) on one instance
