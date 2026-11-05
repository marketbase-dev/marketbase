-- 085_lead_name_guard_validate.sql
--
-- 084 added leads_name_not_formatted_none NOT VALID: it bound every new write at
-- once, while the 97 existing "None None" names were repaired by
-- tools/repair_lead_names.py (89 to the person's real name from a cached vendor
-- response, Comp Intel, or a cached saleleads lookup; 8 to NULL; each with an
-- assertions row). With none left, the constraint now holds for every row.
--
-- Refuses rather than half-applies: if a formatted-None name is still present,
-- it raises with the count; run the repair tool, then re-run.

DO $$
DECLARE
  v_left bigint;
BEGIN
  SELECT count(*) INTO v_left FROM leads WHERE name ~ '(^|\s)None(\s|$)';
  IF v_left > 0 THEN
    RAISE EXCEPTION '085: % lead name(s) still carry a formatted None; run tools/repair_lead_names.py, then re-run',
      v_left USING ERRCODE = '23514';
  END IF;
END $$;

ALTER TABLE leads VALIDATE CONSTRAINT leads_name_not_formatted_none;
