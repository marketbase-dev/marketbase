#!/usr/bin/env python3
"""MarketBase pending-decisions report

List every lead currently carrying `flag:awaiting_dana_decision` — the leads
whose next strategic move (pursue / route / pass, edge-case persona, etc.) is
Dana's to decide, not the outreach skill's. For each one it prints a clean,
Dana-ready brief: who they are, the decision needed, the full conversation,
and the proposed reply parked in `campaign_members.notes`.

This is the read side of the `flag:awaiting_dana_decision` convention (see
CONVENTIONS.md). The write side lives in `acme-propose-reply` (it tags the
flag + parks the draft when it hits a Dana-decision lead).

Usage:
  set -a; source ~/.env.Acme; set +a
  python3 pending_decisions_report.py --client Acme
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import connect

FLAG = "flag:awaiting_dana_decision"


def fetch(conn):
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT l.id, l.name, l.current_title, l.current_company,
                   l.city, l.country, l.linkedin_url, lt.notes
            FROM lead_tags lt
            JOIN leads l ON l.id = lt.lead_id
            WHERE lt.tag = %s
            ORDER BY l.name
        """, (FLAG,))
        leads = cur.fetchall()

        out = []
        for lid, name, title, company, city, country, url, flag_note in leads:
            cur.execute("""
                SELECT c.name, cm.status, cm.notes
                FROM campaign_members cm JOIN campaigns c ON c.id = cm.campaign_id
                WHERE cm.lead_id = %s
            """, (lid,))
            campaigns = cur.fetchall()

            cur.execute("""
                SELECT tag FROM lead_tags WHERE lead_id = %s ORDER BY tag
            """, (lid,))
            tags = [t[0] for t in cur.fetchall()]

            cur.execute("""
                SELECT lm.direction, lm.sent_at, lm.body,
                       COALESCE(o.display_name, '') AS operator
                FROM lead_messages lm
                JOIN lead_conversations lc ON lc.id = lm.conversation_id
                LEFT JOIN outbound_operators o ON o.id = lc.operator_id
                WHERE lc.lead_id = %s
                ORDER BY lm.sent_at
            """, (lid,))
            msgs = cur.fetchall()

            out.append(dict(name=name, title=title, company=company, city=city,
                            country=country, url=url, flag_note=flag_note,
                            campaigns=campaigns, tags=tags, msgs=msgs))
        return out


def render(rows) -> str:
    if not rows:
        return "No leads currently carry `flag:awaiting_dana_decision`. Nothing pending for Dana."

    lines = [f"# Pending decisions for Dana — {len(rows)} lead(s)\n"]
    for i, r in enumerate(rows, 1):
        loc = ", ".join(x for x in (r["city"], r["country"]) if x) or "—"
        lines.append(f"\n## {i}. {r['name']} — {r['title'] or '?'} @ {r['company'] or '?'}")
        lines.append(f"- **Location:** {loc}")
        lines.append(f"- **LinkedIn:** {r['url']}")
        for cname, cstatus, _ in r["campaigns"]:
            lines.append(f"- **Campaign:** {cname} (status={cstatus})")
        lines.append(f"- **Tags:** {', '.join(r['tags'])}")

        # The decision is stored in the flag's note.
        lines.append(f"\n**🔑 Decision needed:** {r['flag_note'] or '(no note — fix: add the decision to the flag note)'}")

        # Conversation
        lines.append("\n**Conversation:**")
        if not r["msgs"]:
            lines.append("> (no synced messages)")
        for direction, sent_at, body, operator in r["msgs"]:
            who = operator if direction == "outbound" else r["name"]
            label = "SENT" if direction == "outbound" else "REPLY"
            ts = sent_at.strftime("%Y-%m-%d %H:%M") if sent_at else "?"
            body = (body or "").strip()
            lines.append(f"\n*[{label}] {who} — {ts}*")
            for ln in body.splitlines() or [""]:
                lines.append(f"> {ln}")

        # Parked proposed reply (lives in the campaign_members.notes)
        parked = next((n for _, _, n in r["campaigns"] if n and "DRAFT PENDING" in n), None)
        if not parked:
            parked = next((n for _, _, n in r["campaigns"] if n), None)
        lines.append("\n**Proposed reply parked for review:**")
        if parked:
            for ln in parked.splitlines():
                lines.append(f"> {ln}")
        else:
            lines.append("> (none parked — compose one in campaign_members.notes)")
        lines.append("\n---")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="List leads awaiting a Dana decision.")
    ap.add_argument("--client", required=True)
    args = ap.parse_args()
    with connect(args.client) as conn:
        rows = fetch(conn)
    print(render(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
