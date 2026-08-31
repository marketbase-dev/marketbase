#!/usr/bin/env python3
"""fill_follower_count_fresh — backfill leads.follower_count via the Fresh
LinkedIn Profile Data API (`fresh-linkedin-profile-data.p.rapidapi.com`
/enrich-lead), which returns follower_count by URL for BOTH vanity and URN
profiles — unlike Saleleads /search/people (name-match only, ~13% hit) and
/user/profile (no follower field). Also captures company_employee_count.

Every raw response is cached in enrichment_calls (api='fresh-linkedin-profile-data')
so re-runs never re-pay (global "never re-pay for scraping" rule).

Selection composes with AND: --where-tag, --where-persona, --where-blank-follower.
SCOPE IT (e.g. --where-tag engager:alon_rosenberg_posts --where-persona '...')
— a bare --where-persona hits every matching lead in the DB.

Usage:
  python3 fill_follower_count_fresh.py --client Acme-AI \\
    --where-tag engager:alon_rosenberg_posts \\
    --where-persona 'demand gen service provider' --where-blank-follower
"""
from __future__ import annoacmens
import argparse, os, sys, urllib.request, urllib.parse, urllib.error, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import api_cache

HOST = "fresh-linkedin-profile-data.p.rapidapi.com"

def le(p):
    p=os.path.expanduser(p); o={}
    if not os.path.exists(p): return o
    for l in open(p):
        l=l.strip()
        if l and '=' in l and not l.startswith('#'):
            k,v=l.split('=',1); o[k.strip()]=v.strip().strip('"').strip("'")
    return o

def enrich_lead(key, url):
    u="https://"+HOST+"/enrich-lead?"+urllib.parse.urlencode({"linkedin_url":url,"include_skills":"false"})
    req=urllib.request.Request(u, headers={"x-rapidapi-key":key,"x-rapidapi-host":HOST})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=45).read())
    except urllib.error.HTTPError as e:
        if e.code in (429,502,503): raise
        return None

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--client", required=True)
    ap.add_argument("--where-tag")
    ap.add_argument("--where-persona")
    ap.add_argument("--where-blank-follower", action="store_true")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    a=ap.parse_args()
    g=le("~/.env"); cl=le(f"~/.env.{a.client}")
    key=g["FRESH_LINKEDIN_DATA_API_KEY"]; db=cl["GTM_DB_CONNSTRING"]
    import psycopg2
    con=psycopg2.connect(db); cache=psycopg2.connect(db)

    where=["TRUE"]; params={}
    if a.where_tag:
        where.append("exists(select 1 from lead_tags t where t.lead_id=l.id and t.tag=%(tag)s)"); params["tag"]=a.where_tag
    if a.where_persona:
        where.append("""exists(select 1 from lead_qualifications q where q.lead_id=l.id
                        and q.qualifier_name='demand_gen_headline_persona_classifier'
                        and q.persona=%(persona)s)"""); params["persona"]=a.where_persona
    if a.where_blank_follower:
        where.append("l.follower_count is null")
    sql=f"select l.id, l.name, l.linkedin_url from leads l where {' and '.join(where)}"
    if a.limit: sql+=f" limit {a.limit}"
    with con.cursor() as c:
        c.execute(sql, params); targets=c.fetchall()
    print(f"[fresh-followers] {len(targets)} leads to enrich", flush=True)

    upd=miss=err=0
    for i,(lid,name,url) in enumerate(targets,1):
        data,_=api_cache.cached_call(cache, "fresh-linkedin-profile-data", "/enrich-lead",
                                     {"linkedin_url":url}, lambda: enrich_lead(key,url),
                                     use_cache=not a.refresh)
        d=(data or {}).get("data") or data or {}
        fol=d.get("follower_count") if isinstance(d,dict) else None
        hc=d.get("company_employee_count") if isinstance(d,dict) else None
        if fol is None:
            miss+=1
        else:
            with con.cursor() as c:
                c.execute("update leads set follower_count=%s, follower_count_updated_at=now() where id=%s",(fol,lid))
            con.commit(); upd+=1
        if i%10==0 or i==len(targets):
            print(f"  [{i}/{len(targets)}] updated={upd} no-data={miss}", flush=True)
    print(f"[fresh-followers] DONE updated={upd} no-data={miss}", flush=True)

if __name__=="__main__":
    main()
