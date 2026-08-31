---
description: Stage qualified leads into a campaign
argument-hint: "--campaign <name> [--from-file <path> | --where <sql>]"
---

# marketbase:stage

Assign qualified leads to a campaign.

## Campaign naming

Validate the campaign name against the convention:

```
<source>_<persona>_<sequencer>_<descriptor>_<period>
```

If the name does not parse, show the user what it would become and confirm
before creating it. Campaign names end up in reporting forever.

## Steps

1. Create the `campaigns` row if it does not exist.
2. UPSERT into `campaign_members` so re-running never duplicates membership.
3. Only stage leads where `lead_current_qualification.qualified = true`.
4. Never stage a lead that is already active in another campaign unless the user
   explicitly asks for it.

## Status changes write themselves

`campaign_members.status` has a trigger that appends to `status_history` JSONB
on every change. Always set `last_status_source` to something reconstructable,
for example `sequencer:smartlead:bounced`, so an audit query can answer why.

## The sequencer contract

MarketBase does not send messages. The sequencer reads from MarketBase, acts,
and writes state back:

| To learn | Query |
|---|---|
| Who to add | `lead_current_qualification` left-joined against `campaign_members`, qualified and unmembered |
| Who to remove | `SELECT * FROM v_pending_removals` |
| What to write back | `UPDATE campaign_members SET status = ..., last_status_source = ...` |

## Verify

```sql
SELECT * FROM v_campaign_funnel WHERE campaign_name = '<name>';
```
