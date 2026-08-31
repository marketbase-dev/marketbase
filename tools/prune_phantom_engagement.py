#!/usr/bin/env python3
"""
prune_phantom_engagement.py — integrity guard for conversation/engagement tags.

Enforces two rules across a client's MarketBase and repairs existing violations:

  RULE A — a `they:replied` tag (and the campaign_members `replied` status) must be
           backed by a REAL inbound message in lead_messages. Smartlead's reply
           reconciliation used to stamp `replied` from its own signal (which counts
           connection-accepts and other non-replies) without syncing a message.

  RULE B — conversation/engagement tags (they:* and we:*) may only exist on leads
           who are STAGED in a campaign (a row in campaign_members). We never tag a
           prospect we didn't put into a campaign, even if a platform claims a reply.

For campaign_members whose status is `replied` with no backing inbound message, the
status is reset to the most recent prior non-`replied` status in status_history
(fallback: `uploaded`), and a correction entry is appended to status_history.

Idempotent. Default is --dry-run; pass --apply to write. Run periodically.

Usage:
  python3 prune_phantom_engagement.py --client Acme [--apply]
"""
import argparse, json, os, sys, datetime

CONV_TAG_PREFIXES = ('they:', 'we:')          # RULE B scope: pure conversation tags
BACKED_TAGS = ('they:replied',)               # RULE A scope

def connstring(client):
    path = os.path.expanduser(f'~/.env.{client}')
    for line in open(path):
        if line.startswith('GTM_DB_CONNSTRING'):
            return line.split('=', 1)[1].strip().strip('"').strip("'")
    raise SystemExit(f'GTM_DB_CONNSTRING not found in {path}')

INBOUND = "('in','inbound','received')"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--client', required=True)
    ap.add_argument('--apply', action='store_true', help='write changes (default: dry-run)')
    args = ap.parse_args()
    import psycopg2, psycopg2.extras
    c = psycopg2.connect(connstring(args.client))
    cur = c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    has_inbound = (f"exists(select 1 from lead_conversations lc "
                   f"join lead_messages lm on lm.conversation_id=lc.id "
                   f"where lc.lead_id=%s and lm.direction in {INBOUND})")
    in_campaign = "exists(select 1 from campaign_members cm where cm.lead_id=%s)"

    # ---- RULE A: they:replied without a backing inbound message ----
    cur.execute(f"""select t.lead_id, l.name from lead_tags t join leads l on l.id=t.lead_id
      where t.tag='they:replied' and not {has_inbound.replace('%s','t.lead_id')}""")
    a_viol = cur.fetchall()

    # ---- RULE B: conversation tags on non-campaign-members ----
    like = " or ".join(f"t.tag like '{p}%'" for p in CONV_TAG_PREFIXES)
    cur.execute(f"""select t.id, t.lead_id, t.tag, l.name from lead_tags t join leads l on l.id=t.lead_id
      where ({like}) and not {in_campaign.replace('%s','t.lead_id')}""")
    b_viol = cur.fetchall()

    # ---- STATUS: campaign_members.status='replied' with no inbound message ----
    cur.execute(f"""select cm.id, cm.lead_id, cm.status_history from campaign_members cm
      where cm.status='replied' and not {has_inbound.replace('%s','cm.lead_id')}""")
    s_viol = cur.fetchall()

    def prior_status(hist):
        if not hist: return 'uploaded'
        for entry in reversed(hist):
            st = entry.get('status'); src = (entry.get('source') or '')
            if st and st != 'replied' and 'reconciliation' not in src:
                return st
        return 'uploaded'

    print(f"[{args.client}] Rule A (they:replied, no message): {len(a_viol)}")
    print(f"[{args.client}] Rule B (they:*/we:* on non-campaign-members): {len(b_viol)}")
    print(f"[{args.client}] Status resets (replied, no message): {len(s_viol)}")
    if b_viol:
        from collections import Counter
        print("   Rule B by tag:", dict(Counter(r['tag'] for r in b_viol)))

    if not args.apply:
        print("\nDRY RUN — pass --apply to write these changes.")
        return

    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    # Rule A + (they:replied subset of) B are handled by the tag deletes below.
    a_ids = tuple({str(r['lead_id']) for r in a_viol})
    if a_ids:
        cur.execute("delete from lead_tags where tag='they:replied' and lead_id = any(%s::uuid[])", (list(a_ids),))
    b_tag_ids = tuple({str(r['id']) for r in b_viol})
    if b_tag_ids:
        cur.execute("delete from lead_tags where id = any(%s::uuid[])", (list(b_tag_ids),))
    for r in s_viol:
        new = prior_status(r['status_history'])
        hist = (r['status_history'] or []) + [{
            'at': now, 'status': new, 'source': 'guard:prune_phantom_engagement',
            'notes': 'reset from phantom "replied" (no backing inbound message)'}]
        cur.execute("update campaign_members set status=%s, status_history=%s where id=%s",
                    (new, json.dumps(hist), r['id']))
    c.commit()
    print(f"\nAPPLIED: removed {len(a_ids)} phantom they:replied, "
          f"{len(b_tag_ids)} non-campaign conversation tags, reset {len(s_viol)} statuses.")

if __name__ == '__main__':
    main()
