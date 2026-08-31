#!/usr/bin/env python3
"""Expand from an eligible seed lead to the rest of that org's buying committee.

Premise (Acme comp-intel call, 2026-08-04): when a competitor is being
evaluated at an organisation, the whole finance buying committee is involved in
that evaluation, not just the one person who tripped the signal. So once ONE
contact at an org is eligible, add every other Director+ person in the same
function at the same company.

    seed lead (tagged, e.g. comp_intel:webhooked >= 2026-08-03)
      -> seed's company name
        -> Blitz: resolve name to the exact company LinkedIn URL
          -> Blitz: people at that company whose title carries a function token
            -> LOCAL rank + function filter (Director and above, finance/accounting)
              -> MarketBase: leads + lead_sources + lead_tags

Everything is driven by configs/buying_committee/<Client>.json — the seed
selector, the title universe, the rank floor, the per-company cap, the Blitz key.
Nothing about the criteria is hardcoded per client.

WHY THE FILTERING IS LOCAL
--------------------------
Blitz /v2/search/people silently IGNORES `people.seniority`, `people.department`,
`company.domain` and `company.headcount` — a garbage seniority value returns the
same rows as no filter at all, and a domain filter returns the entire 394M-person
universe. Only `company.name` (fuzzy substring), `company.linkedin_url` (exact,
array) and `people.job_title` (substring) actually filter. So every seniority and
function decision is made here, on the returned rows. Do not "optimise" this by
pushing it back into the API body: it will not error, it will just ingest the
whole company.

Cost + safety:
  * the Blitz key is PINNED from config (`blitz.key_env`) and rotation is off by
    default, so a run for one client can never be billed to another client's key;
  * every Blitz page is cached in `enrichment_calls` BEFORE it is processed, so a
    crash or re-run replays instead of re-paying.

Usage:
  python3 expand_buying_committee.py --client Acme [--config <path>]
      [--limit N] [--dry-run] [--refresh]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import date
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib import (connect, load_client_env, normalize_linkedin_url,  # noqa: E402
                 linkedin_urn, resolve_canonical_url)
# Reuse the LeadMagic domain resolver rather than re-implementing it.
from research_competitor import leadmagic_company_by_domain  # noqa: E402

from psycopg.types.json import Jsonb  # noqa: E402

SEARCH_URL = "https://api.blitz-api.ai/v2/search/people"
PAGE_SIZE = 50
CONFIG_DIR = Path(__file__).resolve().parent / "configs" / "buying_committee"

# ── rank ladder, highest first ────────────────────────────────────────────────
RANKS = ["c_suite", "vp", "director", "head", "controller_family"]

RE_C_SUITE = re.compile(r"\b(cfo|cao)\b|\bchief\s+(financial|finance|accounting)\s+officer\b", re.I)
RE_VP = re.compile(r"\b(evp|svp|vp|vice[\s\-]?president)\b", re.I)
RE_DIRECTOR = re.compile(r"\bdirector\b", re.I)
RE_HEAD = re.compile(r"\bhead\s+(of|,)\b", re.I)
RE_CONTROLLER = re.compile(r"\b(controller|comptroller|treasurer)\b", re.I)

# Dropped even when a rank word matches. Three families:
#   1. sub-Director qualifiers ("Assistant Controller", "Associate Director")
#   2. titles that orbit a finance exec without being one ("EA to the CFO")
#   3. people who SELL to finance rather than run it. "Office of the CFO" is a
#      product segment at Workday/Oracle/Deloitte, so an Account Executive there
#      matches /\bcfo\b/ and would otherwise rank as C-suite. Verified on a live
#      dry run: 9 of Workday's 12 "C-suite" hits were Office-of-the-CFO sellers.
RE_DISQUALIFY = re.compile(
    r"\b(assistant|asst\.?|deputy|associate|junior|jr\.?|intern|apprentice|trainee|"
    r"student|analyst|clerk|bookkeeper|coordinator|administrator)\b"
    r"|\b(ea|executive\s+assistant|assistant)\s+to\s+(the\s+)?(cfo|ceo|coo|cro|chief)\b"
    r"|\bexecutive\s+assistant\b"
    r"|\b(former|ex|retired)\b"
    r"|\boffice\s+of\s+the\s+(cfo|coo|ceo)\b"
    r"|\b(account\s+executive|sales|customer\s+success|solutions?\s+(consultant|engineer|"
    r"architect|advisor)|pre[\s\-]?sales|marketing|business\s+development|partner\s+manager)\b", re.I)

# Function words that are not the finance function. "Financial Advisor", "Financial
# Services", wealth management, and HR/talent titles all carry a finance token
# without being anywhere near the FP&A buying committee.
RE_WRONG_FUNCTION = re.compile(
    r"\b(financial\s+(advisor|advisers?|advisors?|services|representative)|wealth|"
    r"talent|recruit\w*|human\s+resources|\bhr\b|compensation|benefits|"
    r"investor\s+relations|\bir\b|procurement|payroll)\b"
    # Revenue-cycle / AR roles. They own cash collection, not planning, and a large
    # health system floats a dozen of them ("Director Financial Clearance",
    # "Director of Patient Accounting").
    r"|\b(revenue\s+cycle|patient|clinical|billing|collections?|claims|"
    r"financial\s+clearance|financial\s+counseling|registration)\b"
    # Finance as the PRODUCT being sold, not the department running the company.
    # "Director, Equipment Finance" at a lender is a banker, not a buyer.
    r"|\b(equipment|structured|sponsor|project|trade|asset|consumer|commercial|"
    r"corporate\s+banking|leasing)\s+finance\b"
    # Finance as a SERVICE delivered to clients. At an accounting firm (RSM, BDO,
    # Grant Thornton, the Big Four) every title carries a finance word, but these
    # people staff other companies' books. Catching it on the title generalises
    # better than maintaining a list of firm names.
    # ("tax" is deliberately NOT here: "Director of Tax" already fails the function
    # gate, while "VP Tax & Treasury" is a genuine finance exec and should survive.)
    r"|\b(consult\w*|advisory|assurance|audit\w*|outsourcing|"
    r"client\s+accounting|practice\s+leader)\b"
    # CFO-as-a-service: "CFO Technology", "Solution Lead, FP&A", "Interim CFO
    # for a CPG client" are delivery roles at an advisory firm, not a buyer.
    r"|\bcfo\s+(technology|tech|services|advisory|solutions)\b"
    r"|\b(solution|solutions)\s+lead\b|\b(fractional|outsourced)\s+(global\s+)?cfo\b"
    # NOTE: plain "Interim CFO" is deliberately KEPT — at a real company that is
    # a genuine buyer (often hired to fix exactly the reporting this replaces).
    # The advisory-firm variants are caught by the company blocklist instead.
    , re.I)

# "Controller" that has nothing to do with finance.
RE_FALSE_CONTROLLER = re.compile(
    r"\b(quality|document|documentation|air\s?traffic|traffic|production|inventory|"
    r"materials|stock|credit|process|systems?)\s+controller\b", re.I)


_COMPANY_ID_RE = re.compile(r"/company/(\d+)")

_LEGAL_SUFFIX = (r"\b(inc|llc|l\.l\.c|lp|l\.p|ltd|limited|corp|corporation|co|company|"
                 r"plc|pa|p\.a|group|holdings)\b")


def slug_candidates(name: str) -> list[str]:
    """Plausible LinkedIn company slugs for a company name, best guess first.

    LinkedIn slugs are derived from the company's own name often enough that
    guessing and VERIFYING beats asking Blitz to fuzzy-match the name string —
    which is what silently drops "David", "Citi" and anything idiosyncratic.
    Hyphens are treated as word separators ("Borland-Groover" -> borlandgroover).
    """
    n = (name or "").lower().strip()
    n = re.sub(r"[’']", "", n)
    n = n.replace("-", " ")
    variants = []
    for amp in (n.replace("&", " and "), n.replace("&", " ")):
        v = re.sub(_LEGAL_SUFFIX, " ", re.sub(r"[^a-z0-9&\s]", " ", amp))
        v = re.sub(r"\s+", " ", v).strip()
        if v:
            variants.append(v)
    variants.append(re.sub(r"\s+", " ", re.sub(r"[^a-z0-9&\s]", " ", n)).strip())

    out: list[str] = []
    for v in variants:
        w = v.split()
        if not w:
            continue
        out += ["-".join(w), "".join(w)]
        if len(w) > 2:
            out += ["-".join(w[:2]), "".join(w[:2])]
        if len(w) > 1:
            out.append(w[0])
    # LinkedIn sometimes keeps the ampersand verbatim ("shambaugh-&-son-lp")
    raw = re.sub(r"\s+", "-", re.sub(r"[^a-z0-9&\s]", " ", (name or "").lower()).strip())
    out.append(raw)
    seen: set[str] = set()
    return [s for s in out if s and not (s in seen or seen.add(s))][:10]


def resolve_by_slug_probe(name: str, blitz, norm_company_fn, company_matches_fn):
    """Guess a slug, then VERIFY it with Blitz before trusting it.

    A candidate counts only if Blitz returns employees whose own company_name
    matches the name we started from — so a wrong guess that happens to be a
    real company (e.g. "hurricane" -> Hurricane Inc) is rejected rather than
    silently expanded. Costs one unfiltered page per candidate on an unlimited key.
    """
    key = norm_company_fn(name)
    for slug in slug_candidates(name):
        url = f"https://www.linkedin.com/company/{slug}"
        try:
            people, total, _ = blitz.search({"linkedin_url": [url]}, [], max_pages=1,
                                            log=lambda *_: None)
        except Exception:
            continue
        if not people:
            continue
        names = [(current_exp(p).get("company_name") or "") for p in people]
        agree = sum(1 for cn in names
                    if cn and company_matches_fn(norm_company_fn(cn), key))
        if agree and agree >= max(1, len(names) // 3):
            return url, total
    return None, 0


def same_company(ce: dict, company_url: str) -> bool:
    """Is this experience at the company we searched for?

    The two sources spell the same company differently: Blitz reports the SLUG
    form (`/company/lightedge-solutions`) while Saleleads `/user/experience` —
    which is where `leads.current_company_url` comes from — reports the NUMERIC
    form (`/company/33863/`). A plain string compare therefore rejects every
    person whenever the search URL came from the seed's own profile, silently
    emptying the company. Prefer the numeric LinkedIn id, which both sources
    agree on, and fall back to a normalised URL compare.
    """
    want_id = _COMPANY_ID_RE.search(company_url or "")
    got_id = str(ce.get("company_linkedin_id") or "").strip()
    if want_id and got_id:
        return got_id == want_id.group(1)

    def norm(u: str) -> str:
        return (u or "").strip().rstrip("/").lower()

    return norm(ce.get("company_linkedin_url")) == norm(company_url)


def rank_of(title: str) -> str | None:
    """Highest rank the title carries, or None. VP is tested before C-suite is
    *not* needed here (the C-suite regex is anchored on CFO/CAO, which a VP title
    cannot contain), but the disqualify pass runs first either way."""
    t = (title or "").strip()
    if (not t or RE_DISQUALIFY.search(t) or RE_FALSE_CONTROLLER.search(t)
            or RE_WRONG_FUNCTION.search(t)):
        return None
    if RE_C_SUITE.search(t):
        return "c_suite"
    if RE_VP.search(t):
        return "vp"
    if RE_DIRECTOR.search(t):
        return "director"
    if RE_HEAD.search(t):
        return "head"
    if RE_CONTROLLER.search(t):
        return "controller_family"
    return None


def function_ok(title: str, keep_tokens: list[str], rank: str) -> bool:
    """C-suite here is only CFO/CAO/Chief Financial Officer, which is finance by
    construction. Everything else has to carry a function token."""
    if rank == "c_suite":
        return True
    t = (title or "").lower()
    return any(tok in t for tok in keep_tokens)


def norm_company(name: str) -> str:
    """Loose company-name key: lowercase, drop punctuation and legal suffixes."""
    n = re.sub(r"[^a-z0-9& ]+", " ", (name or "").lower())
    n = re.sub(r"\b(inc|llc|ltd|limited|corp|corporation|co|plc|gmbh|sa|nv|bv|ag|"
               r"group|holdings|holding|the)\b", " ", n)
    return re.sub(r"\s+", " ", n).strip()


def company_matches(name_key: str, blocked_key: str) -> bool:
    """True when two normalised company names refer to the same company.

    Token-prefix, NOT substring. A raw substring test makes "EY" match "attorney",
    "Berkeley" and "beyond", which silently drops real prospects. Prefix matching
    still catches "Workday" vs "Workday Adaptive Planning"."""
    if not name_key or not blocked_key:
        return False
    a, b = name_key.split(), blocked_key.split()
    if a == b:
        return True
    short, long_ = (a, b) if len(a) < len(b) else (b, a)
    return long_[:len(short)] == short


def company_slug(url: str) -> str:
    m = re.search(r"linkedin\.com/company/([^/?#]+)", url or "", re.I)
    return m.group(1).lower() if m else ""


# ── Blitz, pinned key + enrichment_calls cache ────────────────────────────────
class Blitz:
    def __init__(self, cfg: dict, conn, *, refresh: bool = False):
        self.key_env = cfg["key_env"]
        self.key = os.environ.get(self.key_env, "")
        if not self.key:
            sys.exit(f"{self.key_env} is not set. Refusing to fall back to another "
                     f"client's Blitz key.")
        self.allow_rotation = bool(cfg.get("allow_key_rotation", False))
        self.conn = conn
        self.refresh = refresh
        self.live_calls = 0
        self.cache_hits = 0
        self.retries = 0

    def post(self, body: dict) -> dict:
        with self.conn.cursor() as cur:
            if not self.refresh:
                cur.execute("""SELECT response FROM enrichment_calls
                               WHERE api = 'blitz' AND endpoint = 'search-people'
                                 AND params = %s AND success LIMIT 1""", (Jsonb(body),))
                row = cur.fetchone()
                if row:
                    self.cache_hits += 1
                    return row[0]

            req = urllib.request.Request(
                SEARCH_URL, data=json.dumps(body).encode(),
                headers={"x-api-key": self.key, "Content-Type": "application/json"},
                method="POST")
            # Transport failures are transient and WILL happen across a several-
            # hundred-company run (a dropped socket killed one at company 25/759).
            # Retry those with backoff. An HTTP status is a real answer, so it is
            # handled once, not retried.
            resp, success, err = {}, False, None
            for attempt in range(4):
                try:
                    with urllib.request.urlopen(req, timeout=90) as r:
                        resp = json.loads(r.read())
                    success, err = True, None
                    break
                except urllib.error.HTTPError as e:
                    payload = e.read().decode()
                    err = f"HTTP {e.code}: {payload[:300]}"
                    if ("credit" in payload.lower() or e.code in (402, 429)) \
                            and not self.allow_rotation:
                        # Never silently spend another customer's credits.
                        cur.execute("""INSERT INTO enrichment_calls
                                         (api, endpoint, params, success, response, error_message)
                                       VALUES ('blitz','search-people',%s,false,%s,%s)""",
                                    (Jsonb(body), Jsonb({}), err))
                        self.conn.commit()
                        sys.exit(f"\n{self.key_env} is exhausted or rate-limited ({err}).\n"
                                 f"Key rotation is off for this client. Stopping so the run "
                                 f"cannot spill onto another customer's key.")
                    break
                except (urllib.error.URLError, ConnectionResetError, TimeoutError,
                        OSError, json.JSONDecodeError) as e:
                    err = f"{type(e).__name__}: {e}"
                    if attempt == 3:
                        break
                    self.retries += 1
                    time.sleep(2 ** attempt)
            self.live_calls += 1
            cur.execute("""INSERT INTO enrichment_calls
                             (api, endpoint, params, success, response, error_message)
                           VALUES ('blitz','search-people',%s,%s,%s,%s)""",
                        (Jsonb(body), success, Jsonb(resp), err))
        self.conn.commit()          # cache the page BEFORE it is processed
        time.sleep(0.2)             # ~5 req/s
        return resp

    def search(self, company_filter: dict, titles: list[str], max_pages: int,
               *, skip_if_total_over: int = 0, log=print) -> tuple[list[dict], int, bool]:
        """Paginate one search. Returns (people, total_results, truncated).

        `skip_if_total_over` aborts after the first page when the company is too
        big to be a buying committee (Workday reports ~19.7k people carrying a
        finance title). Callers log the skip; nothing is silently dropped."""
        base = {"company": company_filter,
                "people": {"job_title": {"include": titles}},
                "max_results": PAGE_SIZE}
        out: dict[str, dict] = {}
        cursor, total = None, 0
        for page in range(max_pages):
            body = dict(base)
            if cursor is not None:
                body["cursor"] = cursor
            resp = self.post(body)
            results = resp.get("results") or []
            if page == 0:
                total = resp.get("total_results") or 0
                if skip_if_total_over and total > skip_if_total_over:
                    return list(out.values()), total, True
            for p in results:
                url = (p.get("linkedin_url") or "").strip()
                if url:
                    out[url] = p
            cursor = resp.get("cursor")
            if not cursor or not results:
                return list(out.values()), total, False
        log(f"    ! hit max_pages={max_pages} with a cursor still open "
            f"(total_results={total}, collected={len(out)}) — results are TRUNCATED")
        return list(out.values()), total, True


def current_exp(p: dict) -> dict:
    return (p.get("experiences") or [{}])[0] or {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--client", required=True)
    ap.add_argument("--config", default="")
    ap.add_argument("--limit", type=int, default=0, help="cap # seed companies (0=all)")
    ap.add_argument("--dry-run", action="store_true", help="search + filter, write nothing")
    ap.add_argument("--refresh", action="store_true", help="ignore the Blitz cache")
    args = ap.parse_args()

    cfg_path = Path(args.config) if args.config else CONFIG_DIR / f"{args.client}.json"
    if not cfg_path.exists():
        sys.exit(f"No config at {cfg_path}")
    cfg = json.loads(cfg_path.read_text())
    sel, exp, out_cfg = cfg["seed_selector"], cfg["expansion"], cfg["output"]
    keep_tokens = [t.lower() for t in exp["function_keep"]]
    rank_floor = RANKS.index(exp.get("min_rank", "controller_family"))
    cap = int(exp.get("per_company_cap") or 0)

    load_client_env(args.client)
    print(f"=== expand-buying-committee: {args.client} ===")
    print(f"config: {cfg_path.name} | rank floor: {exp.get('min_rank')} | "
          f"cap/company: {cap or 'none'} | geo: {exp.get('geo')}")

    with connect(args.client) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM source_types WHERE name = %s", (out_cfg["source_type"],))
            if not cur.fetchone():
                sys.exit(f"source_type '{out_cfg['source_type']}' is not registered. "
                         f"Run marketbase-migrate-all-clients first.")

            # ── seeds ────────────────────────────────────────────────────────
            # The source-type / date window is OPTIONAL. Selecting on a tag alone is
            # the normal case once the client returns real eligibility verdicts — a
            # date proxy for "fresh" is only needed while that feedback is missing.
            where, params = ["TRUE"], {"tags": sel["tags"],
                                       "excl": sel.get("exclude_tags") or [""]}
            join = ""
            if sel.get("source_types"):
                join = ("JOIN lead_sources s ON s.lead_id = l.id "
                        "AND s.source_type = ANY(%(stypes)s)")
                params["stypes"] = sel["source_types"]
                if sel.get("since"):
                    where.append("s.source_date >= %(since)s")
                    params["since"] = sel["since"]
            cur.execute(f"""
                SELECT DISTINCT l.id, l.linkedin_url, l.name, l.current_company, l.headline,
                       l.current_company_url
                FROM leads l
                JOIN lead_tags t ON t.lead_id = l.id AND t.tag = ANY(%(tags)s)
                {join}
                WHERE {' AND '.join(where)}
                  AND NOT EXISTS (SELECT 1 FROM lead_tags x
                                  WHERE x.lead_id = l.id AND x.tag = ANY(%(excl)s))
            """, params)
            seeds = cur.fetchall()

            # Never expand into a competitor's or our own org: a person who works
            # AT Vena and connected to a Vena rep is a colleague, not a buyer.
            cur.execute("""SELECT c.name FROM company_relationships r
                           JOIN companies c ON c.id = r.company_id
                           WHERE r.relationship = ANY(%s)""",
                        (exp.get("block_relationships") or ["competitor", "self"],))
            blocked = {norm_company(r[0]) for r in cur.fetchall() if r[0]}
        blocked |= {norm_company(n) for n in (exp.get("company_blocklist") or [])}
        print(f"seeds: {len(seeds)} leads tagged {sel['tags']}"
              + (f" since {sel['since']}" if sel.get("since") else ""))
        print(f"blocked companies (competitor/self + config): {len(blocked)}")

        # group seeds by company name
        by_company: dict[str, list] = defaultdict(list)
        no_company, blocked_seeds = [], []
        known_company_url: dict[str, str] = {}   # company name -> its LinkedIn URL
        for lead_id, url, name, company, headline, company_url in seeds:
            company = (company or "").strip()
            if not company:
                no_company.append((lead_id, url, name))
                continue
            nk = norm_company(company)
            if any(company_matches(nk, b) for b in blocked):
                blocked_seeds.append((company, name))
                continue
            by_company[company].append({"lead_id": lead_id, "url": url, "name": name})
            # The seed's own profile already told us the company's LinkedIn URL.
            # That is the exact identifier Blitz's people-search wants, so there is
            # no reason to ask Blitz to re-derive it by fuzzy-matching the NAME —
            # which is what silently drops ambiguous ones ("David", "Citi").
            cu = (company_url or "").strip()
            if cu and company not in known_company_url:
                known_company_url[company] = cu
        print(f"companies: {len(by_company)} distinct | seeds with no company: "
              f"{len(no_company)} | seeds at blocked companies: {len(blocked_seeds)}")
        if blocked_seeds:
            print("  blocked: " + ", ".join(sorted({c for c, _ in blocked_seeds})[:10]))

        companies = sorted(by_company.items(), key=lambda kv: -len(kv[1]))
        if args.limit:
            companies = companies[: args.limit]
            print(f"--limit {args.limit}: processing {len(companies)} companies")

        blitz = Blitz(cfg["blitz"], conn, refresh=args.refresh)
        titles = exp["function_tokens"]
        max_pages = int(cfg["blitz"].get("max_pages_per_batch", 40))
        seed_urls = {s["url"] for members in by_company.values() for s in members}

        # ── 1. resolve each company name to its exact Blitz LinkedIn URL ─────
        resolved: dict[str, dict] = {}
        unresolved: list[str] = []
        from_known = 0
        for i, (name, members) in enumerate(companies, 1):
            # ── 1a. free + exact: the URL the seed's own profile gave us ──────
            if name in known_company_url:
                resolved[name] = {"url": known_company_url[name], "name": name,
                                  "exact": True, "members": members}
                from_known += 1
                continue

            # one page is deliberate here — we only need the modal company URL
            people, _, _ = blitz.search({"name": {"include": [name]}}, titles,
                                        max_pages=1, log=lambda *_: None)
            key = norm_company(name)
            tally: dict[str, dict] = {}
            for p in people:
                ce = current_exp(p)
                cname, curl = ce.get("company_name") or "", ce.get("company_linkedin_url") or ""
                if not curl:
                    continue
                nk = norm_company(cname)
                # exact normalised match first; containment is the fallback
                # ("Prophix" seed vs "Prophix Software" on LinkedIn)
                score = 2 if nk == key else (1 if key and (key in nk or nk in key) else 0)
                if not score:
                    continue
                slot = tally.setdefault(curl, {"score": score, "n": 0, "name": cname})
                slot["n"] += 1
                slot["score"] = max(slot["score"], score)
            if not tally:
                unresolved.append(name)
                continue
            best_url, best = max(tally.items(), key=lambda kv: (kv[1]["score"], kv[1]["n"]))
            resolved[name] = {"url": best_url, "name": best["name"],
                              "exact": best["score"] == 2, "members": members}
            if i % 25 == 0:
                print(f"  resolved {i}/{len(companies)} "
                      f"(live={blitz.live_calls} cached={blitz.cache_hits})", flush=True)
        # ── 1a-bis. slug probe: guess the LinkedIn slug, verify against Blitz ─
        # Runs before LeadMagic because it needs no extra credential, is exact
        # when it hits, and self-verifies (a wrong guess is rejected rather than
        # silently expanded into the wrong company).
        if unresolved:
            still, by_slug = [], 0
            for name in unresolved:
                url, total = resolve_by_slug_probe(name, blitz, norm_company, company_matches)
                if url:
                    resolved[name] = {"url": url, "name": name, "exact": False,
                                      "members": by_company[name]}
                    by_slug += 1
                    print(f"  slug-probe resolved {name[:44]} -> {url} ({total} staff)")
                else:
                    still.append(name)
            if by_slug:
                print(f"  recovered {by_slug} companies by verified slug probe")
            unresolved = still

        # ── 1b. last resort: LeadMagic /company-search by domain ─────────────
        # Blitz only matches on NAME here, so anything ambiguous or idiosyncratic
        # dies at step 1. When we know the company's website, LeadMagic resolves
        # it by domain — a far stronger key than a name string.
        if unresolved:
            still: list[str] = []
            recovered = 0
            with conn.cursor() as cur:
                for name in unresolved:
                    cur.execute("""SELECT website FROM companies
                                    WHERE lower(btrim(name)) = lower(btrim(%s))
                                      AND coalesce(website,'') <> '' LIMIT 1""", (name,))
                    row = cur.fetchone()
                    domain = (row[0] if row else "") or ""
                    domain = domain.replace("https://", "").replace("http://", "").strip("/")
                    if not domain:
                        still.append(name)
                        continue
                    lm = leadmagic_company_by_domain(domain)
                    curl = (lm or {}).get("company_linkedin_url") or (lm or {}).get("linkedin_url") or ""
                    if curl:
                        resolved[name] = {"url": curl, "name": (lm.get("company_name") or name),
                                          "exact": False, "members": by_company[name]}
                        recovered += 1
                    else:
                        still.append(name)
            if recovered:
                print(f"  recovered {recovered} via LeadMagic /company-search by domain")
            unresolved = still

        print(f"\nresolved {len(resolved)} companies "
              f"({from_known} from the seed's own profile, no Blitz call), "
              f"{len(unresolved)} unresolved")
        if unresolved:
            print("  unresolved: " + ", ".join(unresolved[:15])
                  + (f" … +{len(unresolved)-15} more" if len(unresolved) > 15 else ""))

        # persist the resolution so it is a one-time cost per company
        if not args.dry_run:
            with conn.cursor() as cur:
                for seed_name, r in resolved.items():
                    slug = company_slug(r["url"])
                    if not slug:
                        continue
                    cur.execute("""
                        INSERT INTO companies (linkedin_slug, linkedin_url, name, created_at, updated_at)
                        VALUES (%s, %s, %s, NOW(), NOW())
                        ON CONFLICT (linkedin_slug) DO UPDATE SET
                            linkedin_url = COALESCE(NULLIF(companies.linkedin_url,''), EXCLUDED.linkedin_url),
                            name         = COALESCE(NULLIF(companies.name,''),         EXCLUDED.name),
                            updated_at   = NOW()
                    """, (slug, r["url"], r["name"]))
            conn.commit()

        # ── 2. search each resolved company ──────────────────────────────────
        # ONE COMPANY PER SEARCH, deliberately. `company.linkedin_url` accepts an
        # array, but pagination is shared across the batch: a single 19k-person
        # org consumes every page and starves the other companies in the chunk.
        # Blitz is unlimited on the pinned key, so fairness beats call count.
        skip_over = int(exp.get("skip_company_if_total_over") or 0)
        url_to_seed = {r["url"]: (seed_name, r) for seed_name, r in resolved.items()}
        found: dict[str, list[dict]] = defaultdict(list)     # company url -> people
        too_big: list[tuple[str, int]] = []
        truncated: list[tuple[str, int]] = []
        for i, (curl, (seed_name, r)) in enumerate(url_to_seed.items(), 1):
            people, total, cut = blitz.search({"linkedin_url": [curl]}, titles, max_pages,
                                              skip_if_total_over=skip_over)
            if cut and skip_over and total > skip_over:
                too_big.append((seed_name, total))
                continue
            if cut:
                truncated.append((seed_name, total))
            for p in people:
                if same_company(current_exp(p), curl):
                    found[curl].append(p)
            if i % 20 == 0 or i == len(url_to_seed):
                print(f"  searched {i}/{len(url_to_seed)} companies, "
                      f"{sum(len(v) for v in found.values())} raw people "
                      f"(live={blitz.live_calls} cached={blitz.cache_hits})", flush=True)
        if too_big:
            print(f"\n  skipped {len(too_big)} companies over the "
                  f"{skip_over}-finance-title threshold (not a buying committee):")
            for n, t in sorted(too_big, key=lambda x: -x[1])[:15]:
                print(f"      {n}  ({t:,} matching people)")
        if truncated:
            print(f"  TRUNCATED at max_pages for {len(truncated)} companies: "
                  + ", ".join(f"{n} ({t:,})" for n, t in truncated[:10]))

        # ── 3. local rank + function filter ──────────────────────────────────
        RANK_ORDER = {r: i for i, r in enumerate(RANKS)}
        kept_by_company: dict[str, list[dict]] = {}
        dropped = 0
        for curl, people in found.items():
            seed_name, r = url_to_seed[curl]
            keep = []
            for p in people:
                ce = current_exp(p)
                title = ce.get("job_title") or p.get("headline") or ""
                rank = rank_of(title)
                if not rank or RANK_ORDER[rank] > rank_floor or not function_ok(title, keep_tokens, rank):
                    dropped += 1
                    continue
                url = normalize_linkedin_url(p.get("linkedin_url") or "")
                if not url or url in seed_urls:      # never re-add the seed itself
                    dropped += 1
                    continue
                keep.append({"url": url, "rank": rank, "title": title, "blitz": p,
                             "name": p.get("full_name") or "",
                             "company": ce.get("company_name") or r["name"]})
            keep.sort(key=lambda k: RANK_ORDER[k["rank"]])
            if cap:
                if len(keep) > cap:
                    print(f"    cap: {seed_name} had {len(keep)} matches, keeping top {cap}")
                keep = keep[:cap]
            if keep:
                kept_by_company[curl] = keep
            if len(keep) > 50:
                print(f"    ! {seed_name} yielded {len(keep)} Director+ finance people "
                      f"(large org — no cap is configured)")

        total_kept = sum(len(v) for v in kept_by_company.values())
        print(f"\nfiltered: {total_kept} kept, {dropped} dropped "
              f"across {len(kept_by_company)} companies")

        # Distribution matters more than the total: a handful of enterprises can
        # carry most of the volume, which is a company-size problem, not a cap problem.
        sizes = sorted((len(v) for v in kept_by_company.values()), reverse=True)
        if sizes:
            print("\n  people per company:")
            for lo, hi in [(1, 3), (4, 6), (7, 10), (11, 25), (26, 50), (51, 10 ** 9)]:
                n = sum(1 for s in sizes if lo <= s <= hi)
                tot = sum(s for s in sizes if lo <= s <= hi)
                if n:
                    label = f"{lo}-{hi}" if hi < 10 ** 9 else f"{lo}+"
                    print(f"    {label:>7}: {n:>4} companies, {tot:>5} people "
                          f"({tot * 100 // max(total_kept, 1)}%)")
            print(f"    median {sizes[len(sizes) // 2]}/company, "
                  f"mean {total_kept / len(sizes):.1f}")
            top = sorted(((len(v), url_to_seed[c][0]) for c, v in kept_by_company.items()),
                         reverse=True)[:12]
            print("  biggest: " + ", ".join(f"{n} {nm}" for n, nm in top))
            head = sum(n for n, _ in top)
            print(f"  the 12 biggest companies alone account for {head} people "
                  f"({head * 100 // max(total_kept, 1)}% of the total)")
            print("  what-if (this run is uncapped; these are alternatives):")
            for c in (3, 5, 10):
                print(f"    per_company_cap={c:<3}: {sum(min(s, c) for s in sizes):>5} people")
            for ceiling in (25, 50, 100):
                n = sum(1 for s in sizes if s > ceiling)
                print(f"    drop companies yielding >{ceiling:<4}: "
                      f"{sum(s for s in sizes if s <= ceiling):>5} people "
                      f"({n} companies dropped)")

        with conn.cursor() as cur:
            all_urls = [k["url"] for v in kept_by_company.values() for k in v]
            cur.execute("SELECT count(*) FROM leads WHERE linkedin_url = ANY(%s)", (all_urls,))
            known = cur.fetchone()[0]
        print(f"  already in MarketBase: {known} | net-new: {total_kept - known}")

        # ── 4. write to MarketBase ────────────────────────────────────────────────
        stats = {"leads_new": 0, "leads_updated": 0, "sources": 0,
                 "already_sourced": 0, "seeds_done": 0}
        if args.dry_run:
            print("\n--dry-run: nothing written. Sample of what would be ingested:")
            for curl, keep in list(kept_by_company.items())[:10]:
                seed_name, r = url_to_seed[curl]
                print(f"\n  {seed_name}  (seeds: {', '.join(m['name'] for m in r['members'])})")
                for k in keep[:12]:
                    print(f"      [{k['rank']:<17}] {k['name']:<28} {k['title'][:60]}")
        else:
            # Bulk per company, not per person. Neon round-trip latency dominates:
            # 4 statements x 5.7k people is over an hour, the same work batched into
            # ~6 executemany calls per company is minutes.
            with conn.cursor() as cur:
                for n, (curl, keep) in enumerate(kept_by_company.items(), 1):
                    seed_name, r = url_to_seed[curl]
                    seed_ref = [m["url"] for m in r["members"]]

                    # canonical-URL resolution, batched: map any vanity slug we have
                    # already seen back onto the existing lead row for that person
                    slugs = {s: u for u in (k["url"] for k in keep)
                             if (s := re.search(r"/in/([^/?#]+)", u).group(1)
                                 if re.search(r"/in/([^/?#]+)", u) else None)}
                    canon = {}
                    if slugs:
                        cur.execute("SELECT public_id, linkedin_url FROM leads "
                                    "WHERE public_id = ANY(%s)", (list(slugs),))
                        for pid, existing_url in cur.fetchall():
                            canon[slugs[pid]] = existing_url
                    for k in keep:
                        k["url"] = canon.get(k["url"], k["url"])

                    urls = [k["url"] for k in keep]
                    cur.execute("SELECT linkedin_url FROM leads WHERE linkedin_url = ANY(%s)",
                                (urls,))
                    pre_existing = {row[0] for row in cur.fetchall()}
                    stats["leads_new"] += len(set(urls)) - len(pre_existing)
                    stats["leads_updated"] += len(pre_existing)

                    cur.executemany("""
                        INSERT INTO leads (linkedin_url, linkedin_urn, name, headline,
                                           current_title, current_company, current_company_url,
                                           city, country, created_at, updated_at)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(),NOW())
                        ON CONFLICT (linkedin_url) DO UPDATE SET
                            name                = COALESCE(NULLIF(leads.name,''),                EXCLUDED.name),
                            headline            = COALESCE(NULLIF(leads.headline,''),            EXCLUDED.headline),
                            current_title       = COALESCE(NULLIF(leads.current_title,''),       EXCLUDED.current_title),
                            current_company     = COALESCE(NULLIF(leads.current_company,''),     EXCLUDED.current_company),
                            current_company_url = COALESCE(NULLIF(leads.current_company_url,''), EXCLUDED.current_company_url),
                            city                = COALESCE(NULLIF(leads.city,''),                EXCLUDED.city),
                            country             = COALESCE(NULLIF(leads.country,''),             EXCLUDED.country),
                            updated_at          = NOW()
                    """, [(k["url"], linkedin_urn(k["url"]), k["name"],
                           (k["blitz"].get("headline") or "").replace("\n", " "),
                           k["title"], k["company"], curl,
                           (k["blitz"].get("location") or {}).get("city") or "",
                           (k["blitz"].get("location") or {}).get("country")
                           or (k["blitz"].get("location") or {}).get("country_code") or "")
                          for k in keep])

                    cur.execute("SELECT linkedin_url, id FROM leads WHERE linkedin_url = ANY(%s)",
                                (urls,))
                    ids = dict(cur.fetchall())
                    cur.execute("""SELECT lead_id FROM lead_sources
                                   WHERE source_type = %s AND source_label = %s
                                     AND lead_id = ANY(%s)""",
                                (out_cfg["source_type"], out_cfg["source_label"],
                                 list(ids.values())))
                    sourced = {row[0] for row in cur.fetchall()}

                    fresh = [k for k in keep
                             if ids.get(k["url"]) and ids[k["url"]] not in sourced]
                    stats["already_sourced"] += len(keep) - len(fresh)
                    if fresh:
                        cur.executemany("""INSERT INTO lead_sources
                                             (lead_id, source_type, source_label, source_date, raw_data)
                                           VALUES (%s,%s,%s,%s,%s)""",
                                        [(ids[k["url"]], out_cfg["source_type"],
                                          out_cfg["source_label"], date.today(),
                                          Jsonb({"seed_linkedin_urls": seed_ref,
                                                 "seed_company": seed_name,
                                                 "company_linkedin_url": curl,
                                                 "rank": k["rank"],
                                                 "matched_title": k["title"],
                                                 "blitz": k["blitz"]}))
                                         for k in fresh])
                        stats["sources"] += len(fresh)
                        cur.executemany("""INSERT INTO lead_tags (lead_id, tag, notes, tagged_by)
                                           VALUES (%s,%s,%s,'expand_buying_committee')
                                           ON CONFLICT (lead_id, tag) DO NOTHING""",
                                        [(ids[k["url"]], out_cfg["tag"], f"seed: {seed_ref[0]}")
                                         for k in fresh])

                    # mark the seeds handled only after their peers are written
                    cur.executemany("""INSERT INTO lead_tags (lead_id, tag, notes, tagged_by)
                                       VALUES (%s,%s,%s,'expand_buying_committee')
                                       ON CONFLICT (lead_id, tag) DO NOTHING""",
                                    [(m["lead_id"], out_cfg["seed_done_tag"],
                                      f"{len(keep)} peers added") for m in r["members"]])
                    stats["seeds_done"] += len(r["members"])

                    if n % 20 == 0:
                        conn.commit()
                        print(f"  wrote {n}/{len(kept_by_company)} companies "
                              f"({stats['sources']} people)…", flush=True)
            conn.commit()

    # ── report ───────────────────────────────────────────────────────────────
    seeds_expanded = sum(len(url_to_seed[c][1]["members"]) for c in kept_by_company)
    print("\n=== summary ===")
    print(f"  seeds selected      : {len(seeds)}")
    print(f"  companies distinct  : {len(by_company)}  (processed {len(companies)})")
    print(f"  companies resolved  : {len(resolved)}  unresolved: {len(unresolved)}")
    print(f"  companies with hits : {len(kept_by_company)}")
    print(f"  people kept         : {total_kept}   (dropped by filter: {dropped})")
    if seeds_expanded:
        print(f"  expansion ratio     : {(seeds_expanded + total_kept) / seeds_expanded:.1f}x "
              f"({seeds_expanded} seeds -> {seeds_expanded + total_kept} people)")
    print(f"  blitz calls         : {blitz.live_calls} live, {blitz.cache_hits} cached, "
          f"{blitz.retries} retried (key: {blitz.key_env})")
    for k, v in stats.items():
        print(f"  {k:<20}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
