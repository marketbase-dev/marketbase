# MarketBase conventions

Cross-cutting conventions that all MarketBase skills and client orchestrators
must follow. Schema-level docs live in `schema/README.md`; this file is for
behavioral conventions on top of the schema.

## Schema and migration discipline (read first)

The MarketBase skills are **generic across clients**. Every client gets the same
schema and the same skill set. Two rules follow:

1. **Don't change the database structure unless you have to.** When
   information can be persisted via columns/tables that already exist —
   even if doing so requires a small piece of application logic (a runner
   helper, a Python UPSERT, etc.) — prefer the application-side fix over a
   migration that adds columns, tables, indexes, constraints, or triggers.
   Migrations are forever; they propagate to every client DB via
   `marketbase-migrate-all-clients` and become load-bearing for downstream
   skills. Each new migration raises the cost of every future change.

2. **Never use migrations for client-specific data.** Examples of things
   that are NOT migrations:
   - Flagging a particular company as a competitor / security vendor /
     customer for one client → INSERT INTO company_relationships via a
     one-shot SQL command or `marketbase-research-competitor`.
   - Backfilling a specific client's leads.* columns from a known good
     source → one-shot UPDATE.
   - Re-qualifying a specific cohort under a new classifier version →
     run a script.

   Migrations are for schema and trigger logic that EVERY client benefits
   from equally — they are not the vehicle for fixing or seeding any
   single client's data.

When in doubt, ask: "Would a brand-new client onboarded tomorrow need this
change applied?" If yes, it's a migration. If no, it's a one-shot or an
application-layer concern.

## Durability and batch progress (don't hold work in memory)

Any MarketBase skill that processes more than a handful of records — qualifying a
cohort, scraping engagers, enriching companies, running a classifier over a
list — **must persist progress as it goes**, not collect everything in
Python memory and write at the end. A network blip, OOM, kill -9, or
mistaken Ctrl-C hours in must NOT erase the work.

Rules of thumb:

1. **Flush per batch.** Inside any loop over N items, write to the DB
   every K items (typical: K = 10-50) and `conn.commit()`. The exact K is
   a tradeoff between transaction overhead and how much work you're
   willing to lose to a crash — when in doubt, K = 25.

2. **Idempotency is mandatory for batch runners.** A re-run after a crash
   must skip records that already have their target row, so progress
   accumulates. Use a uniqueness key (typically `lead_id + qualifier_name
   + qualifier_version`) and check before insert.

3. **External async work (OpenAI Batch API, queued jobs, third-party
   pipelines) requires a tracking ID persisted to disk BEFORE the
   submitting call returns.** When a script submits a batch job and then
   exits or dies, the next invocation must be able to find the submitted
   batch and continue (poll, fetch results, ingest) — typically by
   reading a small JSON state file under the skill's working directory.

4. **No "all-or-nothing" accumulators.** A common antipattern: a Python
   dict filling up across thousands of iterations, then a single write at
   the bottom of the loop. If the loop body has any I/O (even an OpenAI
   call that could hang or 502), the dict is gone the moment the process
   dies. Convert these into per-batch flushes.

5. **Save the runtime decisions, not just the final verdicts.** The
   ingest stage of a pipeline should be replayable from persisted
   intermediate state, not from the in-flight memory of an earlier stage.

This rule applies to all MarketBase classifiers, runners, enrichers, and
orchestrators — anything that processes records in a loop. Learned the
hard way (May 2026) when a Northwind pre-filter run died 18 minutes into a
98k-lead Stage 1 with all 2,800 in-flight verdicts held in a Python dict
— total memory loss, ~$0.28 of OpenAI cost wasted.

## Tag conventions

Tags (`lead_tags` table) are mutable, multi-valued categorization that
captures **state** and **classification** decisions about a lead. They are
separate from `lead_sources` (provenance) and `lead_qualifications`
(algorithmic decisions with full_result JSONB).

### Naming rules

**All new tags use `<category>:<value>` snake_case lowercase.** No spaces,
no hyphens. Pick from the closed set of categories below; values inside a
category are open. Status / state tags use the present tense
(`state:engagers_researched`, not `state:engagers_done`). Client-scoped
values prefix the client slug into the value
(`campaign:knock_ai_round_2_invite`, never a third dotted segment).

### Tag categories (closed set)

| Category | Meaning | Example |
| --- | --- | --- |
| **`source:`** | Where this person came from in a way that doesn't fit `lead_sources` (rare — usually use `lead_sources` instead) | `source:manual_referral` |
| **`campaign:`** | Campaign / source bucket — which engagement story they're part of | `campaign:engaged_with_northwind_a` |
| **`they:`** | What THEY did (their behavior toward us / our content / competitor content) | `they:willing_to_give_feedback` |
| **`we:`** | What WE did to them (outreach actions we've taken) | `we:sent_them_demo` |
| **`qual:`** | Qualification result / classification. Index for the latest `lead_qualifications` decision. | `qual:qualified` |
| **`plan:`** | What we plan to do next — current intent | `plan:book_meeting` |
| **`objection:`** | An objection/concern the lead raised that we may attempt to handle before moving on. Coexists with `they:*` — the `they:*` says *what they did*, the `objection:*` says *the substantive concern behind it*. | `objection:asked_about_differences` |
| **`state:`** | Process state in the MarketBase pipeline (queued / in-progress / failed / done) | `state:qualification_queued` |

Adding a new category requires updating this doc — keep the set small.

### Canonical tag values

#### `campaign:` (open — clients add their own)

| Tag | Meaning |
| --- | --- |
| `campaign:engaged_with_maze` | Engaged with content from competitor "Maze" |
| `campaign:likely_to_accept` | In the likely-to-accept-connection-request bucket |
| `campaign:engaged_with_northwind_a` | Engaged with Anthropic Northwind content, segment A |
| `campaign:engaged_with_northwind_b` | Engaged with Anthropic Northwind content, segment B |

#### `they:`

| Tag | Meaning | Notes |
| --- | --- | --- |
| `they:declined_to_give_feedback` | Asked for feedback, said no | — |
| `they:willing_to_give_feedback` | Agreed to give feedback (but has not committed to a meeting) | — |
| `they:willing_to_meet` | Agreed to meet — accepted a call, proposed/accepted times, or gave email/availability to book. Stronger than `they:willing_to_give_feedback`; supersedes it once a meeting is agreed. Pairs with `plan:book_meeting`. | — |
| `they:politely_greeted` | Polite hi but no engagement substance | — |
| `they:engaged_with_competitor_content` | Reacted/commented on a competitor's post | — |
| `they:engaged_with_our_content` | Reacted/commented on the client's (or client team's) post | — |
| `they:redirected_to_colleague` | Prospect confirmed the problem exists in their org but said they're not personally involved. Used instead of `they:declined_to_give_feedback` when they are routing, not declining. | **required**: comma-separated colleague names |
| `they:booked_meeting` | Prospect self-booked a meeting (e.g. clicked a Calendly link themselves). **Triggers** `campaign_members.status='meeting_booked'` and removes `plan:book_meeting` (updated 2026-06-24, alice: a self-booking now counts as booked — see note below). | — |
| `they:accepted_calendar_invite` | Prospect accepted a direct calendar invite (.ics) we sent. **Triggers** `campaign_members.status='meeting_booked'` and removes `plan:book_meeting`. | — |
| `they:replied` | Lead sent an inbound message in any of our `lead_conversations` with them. **Auto-applied** by `marketbase-sync-conversations`. **Triggers** `campaign_members.status='replied'` (terminal-status guarded). | — |
| `they:reacted_to_our_message` | Lead added an emoji reaction to one of our outbound messages without sending new content. **Auto-applied** by `marketbase-sync-conversations`. Does **not** trigger a status change. Coexists with `they:replied`. | — |
| `they:gave_deep_feedback` | Shared real industry perspective, technical opinions, market analysis, or specific pain points. Goes beyond surface acknowledgment ("sure", "sounds interesting"). Coexists with `they:willing_to_give_feedback`. | — |
| `they:skeptical_but_engaged` | Pushed back on claims, questioned differentiation, challenged assumptions — but kept engaging in the conversation. Not a decline. Coexists with `they:willing_to_give_feedback`. | — |
| `they:asked_about_scope` | Asked what the commitment / involvement with **us** looks like before agreeing. About the **scope of their engagement WITH US** ("how much of my time would this take?", "what would you need from me?", "are you looking for advisors / partners?"). **NOT** for technical/product questions about how the product works (that's `they:asked_technical_question`), and **NOT** for competitive-comparison questions (that's `objection:asked_about_differences`). | — |
| `they:asked_technical_question` | Asked a substantive technical / product question — "how do you do this in production?", "is it runtime or build-time?", "what about agentless deployments?". A positive engagement signal; typically coexists with `they:willing_to_give_feedback`. Distinct from `they:asked_about_scope` (about *their* involvement, not the product) and from `objection:asked_about_differences` (competitive framing). | — |
| `they:job_seeker` | Responded but is looking for a role, not a buyer. Typically coexists with `they:politely_greeted`. Should usually pair with `qual:disqualified`. | — |
| `they:confused` | Replied with `?` or expressed confusion about why we contacted them. Didn't understand the context of the outreach. | — |
| `they:not_interested` | Lead is not interested. Distinct from `they:declined_to_give_feedback` in that `they:not_interested` represents the final state — they passed on the offer with no implied objection to handle. Typically applied after `plan:handle_objections` was tried (and didn't change their mind), or directly when the decline is clearly final. Usually paired with `plan:nurture`. | — |

#### `we:`

| Tag | Meaning | Notes |
| --- | --- | --- |
| `we:sent_them_promo_video` | Sent promotional video | — |
| `we:sent_them_demo` | Sent a demo (video or in-person) | — |
| `we:sent_them_possible_times` | Sent calendar / availability | — |
| `we:nudged` | Sent a follow-up nudge | — |
| `we:sent_referral_ask` | Sent a message asking the prospect to name or confirm the right colleague to connect with. | **required**: comma-separated proposed names |
| `we:sent_calendar_invite` | We sent the prospect a direct calendar invite (.ics) — distinct from sending a Calendly link (which is `we:sent_them_possible_times`). | — |
| `we:thanked_for_declining` | Sent a closing thank-you message after the lead declined, to close the loop gracefully and keep the door open. Usually applied together with `they:not_interested` + `plan:nurture`. | — |
| `we:sent_wiz_reframe` | Sent the Wiz Green agent differentiator framing (see Wiz battlecard / `references/wiz-battlecard.md`). Tracks where we've already deployed the canonical Wiz comparison so we don't repeat it in a future touch. | — |
| `we:proposed_future_collaborations` | Sent a closing message that, beyond thanking for the reply, explicitly invited the lead into a non-buying future relationship (podcast guest, event speaker, advisor circle, etc.). Reserved for qualified leads with whom the conversation was substantive — even if they declined the product. Usually applied together with `we:thanked_for_declining`. | — |

#### `qual:` (final classification)

`qual:*` tags are never noted. The reason lives in
`lead_qualifications.full_result.reason` (and the full decision in
`full_result`), and the latest decision is queryable via the
`lead_current_qualification` view.

| Tag | Meaning | Notes |
| --- | --- | --- |
| `qual:qualified` | Latest classifier ran and said yes | — |
| `qual:disqualified` | Latest classifier ran and said no | — |
| `qual:networker` | Worth keeping in network but not a direct buyer prospect | — |
| `qual:potential_thought_leader` | Candidate thought leader awaiting evaluation | — |
| `qual:thought_leader` | Evaluated and accepted as a research seed | — |
| `qual:thought_leader_rejected` | Evaluated and rejected | — |
| `qual:pending` | Persona/buyer-fit is ambiguous and needs human review (ask the team, check ICP fit, decide whether to pursue as buyer / partner / pass). Distinct from `qual:qualified` (decided yes) and `qual:disqualified` (decided no). Should usually pair with `plan:check_fit`. | — |
| `qual:senior_ic` | Senior IC at a target company — **Lead Engineer, Principal Engineer, Staff Engineer**, or equivalent IC roles with deep technical scope but no buying authority. Worth pursuing as a **technical champion** even though they aren't the decision-maker. Sales motion typically multi-threads to their VP/Director/CISO. Distinct from `qual:qualified` (decision-maker or senior buyer) and `plan:ic_do_not_pursue` (junior IC, no influence, not pursued under current policy). | — |

**Important: `qual:*` is about persona/buyer fit, NOT about whether they
said yes.** A lead who declines our offer can still be `qual:qualified` (right
persona, right company, but not interested today) — we don't downgrade their
qualification just because they passed. Use `they:*` and `plan:*` for the
disposition; keep `qual:` honest about whether this is the kind of buyer we'd
ever want to sell to.

#### `objection:` (concerns we may try to handle)

`objection:*` indexes a substantive concern the lead raised — the *reason*
behind a soft decline or a competitive question. Coexists with `they:*`
(which captures behavior) and usually pairs with `plan:handle_objections`
for the immediate next step. If the objection-handling attempt doesn't move
them, the `objection:*` tag remains in place as a record and they shift to
`they:not_interested` + `plan:nurture`.

| Tag | Meaning | Notes |
| --- | --- | --- |
| `objection:too_similar_to_other_pitches` | Lead said the pitch sounds like everyone else's — they can't see what's different. | — |
| `objection:asked_about_differences` | Lead asked how we differ from named competitors / incumbents ("how does this compete with Wiz / Alt / Terra?"). The competitive-differentiation question, not a scope-of-engagement one (see `they:asked_about_scope`). | — |

#### `plan:` (current intent)

| Tag | Meaning | Notes |
| --- | --- | --- |
| `plan:pass` | Skip — don't pursue further | — |
| `plan:network` | Add to networking pool | — |
| `plan:nurture` | Slow-drip content / soft touch | — |
| `plan:book_meeting` | Push for a meeting | — |
| `plan:rebook_meeting` | A meeting that was already booked fell through — invite never delivered, prospect didn't attend, or it was cancelled — and we're actively **re-booking** it. Distinct from `plan:book_meeting` (first-time booking): this signals a recovery, so the next message should acknowledge the miss and make re-scheduling frictionless (lead with Calendly when an `.ics` delivery failure caused it). Pairs with `flag:meeting_not_attended` when a booked event was missed/undelivered, or with `flag:booking_dropped` when the prospect confirmed a time but no invite was ever issued. Clears to `meeting_booked` (re-booked) or `plan:nurture` (gave up). | — |
| `plan:do_not_pursue` | Hard stop — outreach and classifier skip this lead | — |
| `plan:ic_do_not_pursue` | Individual contributor — not pursued under current policy. Outreach skips; enrichment and classifier still run. Revisitable: clear in bulk if the policy changes. | — |
| `plan:snooze` | Pause outreach now, follow up later. Lead signaled "not now" — busy, traveling, on leave, "ping me in Q3," etc. Cleared manually or by a scheduled wake when the resume date arrives. | **optional**: resume date as `YYYY-MM-DD`. Human hint only — for hard enforcement use a `lead_actions(action='resume_outreach', not_before=…)` row. |
| `plan:cooldown` | Outreach pushed without reply through a chase cadence; back off so the lead doesn't feel hounded. Cleared manually or by the sequencer after a defined quiet period (e.g. 60–90 days). | — |
| `plan:deal_hold` | Outreach paused because the lead's employer has an open or won deal (active-deal suppression). **Machine-owned**: applied and cleared by `smartlead:deal_sync` based on `v_leads_at_deal_company` / `company_deals`, never by lead behavior. Distinct from `plan:snooze` (lead-signaled "not now," resumes on a date) and `plan:cooldown` (self-imposed backoff after chasing). Auto-resumes when the employer no longer has an active deal. See the precedence rule under "Outreach-only pause tags". | **required**: `Active deal at employer — <deal_company> (<open_stage>)`. |
| `plan:book_downgraded_to_nurture` | We were at `plan:book_meeting`, sent ONE post-times nudge (the minimal `<Name>?` bump or a circle-back) and they still didn't reply. This tag downgrades the lead from active meeting-push to nurture AND records that **we've already used our single nudge — do not nudge again**. Apply it together with `we:nudged` and remove `plan:book_meeting` at the same time. Distinct from plain `plan:nurture` (never had a meeting in motion) and `plan:cooldown` (multi-touch chase backoff): this is specifically the one-and-done rule after a times-proposal went cold. Cleared if the lead later re-engages on their own. | — |
| `plan:handle_objections` | The lead raised an objection (see the `objection:*` tag). Next action is to send a reframe / clarification before deciding pursue vs nurture. After handling, the plan typically moves to `plan:book_meeting` (objection cleared) or `plan:nurture` (still not interested → also tag `they:not_interested`). | — |
| `plan:check_fit` | Internal action: check whether this lead fits our ICP / engagement model before committing to outreach. Used when the lead is interesting but ambiguous (potential partner vs buyer, unfamiliar segment, edge-case persona). Pairs with `qual:pending`. The reply to the lead, if any, should buy time without committing to a meeting ("I'll come back with a time"). | — |
| `plan:verify_disqualification` | Internal action: a lead was auto-disqualified by a classifier (`qual:disqualified`) BUT showed real engagement (replied, booked, reacted) before the flip — so a human should confirm the disqualification is right before we fully drop them. Catches the false-negative risk where an engaged lead is dropped on a persona/size technicality. While set, hold campaign removal / further outreach. Resolves to `plan:pass` (DQ confirmed) or a re-qualify + re-instate (DQ was wrong, e.g. an ICP-policy change like adding AppSec). Typically pairs with a `they:*` engagement tag. | **optional**: one-phrase reason for the doubt (e.g. `replied before v5 persona flip`). |
| `plan:partnership_play` | Lead is not a direct buyer (`qual:disqualified` for sales) but is being pursued as a **partnership opportunity** — channel partner, ecosystem influencer, MSP/consultancy, or someone with industry reach (e.g. Microsoft / AWS / Google Cloud customer-facing roles, MSSPs, security advisors). Outreach continues but with **partner-framing messaging** ("compare notes", "share what we're seeing", "explore how this could complement what you're doing") rather than a buyer pitch. Distinct from `plan:network` (no specific goal, just keep warm) and `plan:nurture` (still warming a buyer). | — |
| `plan:discuss_webinar` | Lead is being invited to **join / speak in our webinar series** (cloud-security strategies for the AI era) as a **two-way professional exchange**, not pushed a product demo. Used when the person is a credible practitioner / thought-leader whose participation as a speaker is valuable (thought-leadership + relationship + soft top-of-funnel), or when a direct vendor call is declined but they're a serious voice worth engaging as a peer. The next action is a **2-way chat about the webinar** (who's speaking, format, topic), NOT a demo — framed as "instead of a one-way vendor conversation, let's talk about you joining." Distinct from `plan:book_meeting` (product demo/feedback call), `plan:nurture` (passive warm), `plan:network` (keep warm, no specific goal), and `plan:partnership_play` (channel/ecosystem partner). Can convert to `plan:book_meeting` later if genuine buying interest emerges. See `acme-propose-reply` → "Webinar-speaker invite". | **optional**: one-phrase context (e.g. `serious practitioner, posts on topic; declined vendor call but engaged as peer`). |
| `plan:let_go` | **Transient state** — lead has been disqualified internally (per latest classifier or human review) AFTER substantive engagement, and a final goodwill message is OWED. While carrying `plan:let_go`, the lead is the **LOWEST priority in any queue** — we touch them last, after every other action is handled. **Once the goodwill message is sent, immediately flip `plan:let_go` → `plan:pass`** (terminal state). The let-go message acknowledges them warmly, sets up potential future contact ("I'll reach back out when we have something more concrete"), and ends without any CTA. Distinct from `plan:pass` (terminal — no further action ever) and from polite-decline closers (they declined; here WE stepped back). See `acme-propose-reply` → "Let-go close (internal disqualification after engagement)" for the template. | — |
| `plan:name_drop_reservoir` | Lead is queued in a `referralDiscovery_*_nameDropReservoir_*` campaign because we discovered them via someone else (a referrer pointing at their team during routing). Cold outreach is **on hold** until either the referrer confirms the team/relevance, or the `not_before` date passes — at which point the lead graduates and the cold outreach is sent. The reservoir campaign is **sender-scoped** — its `<sequencer>` token names the operator who has the relationship with the referrer (e.g. `romanLinkedin`, `danaLinkedin`, `eyarLinkedin`), so the graduating cold outreach is sent by that same operator (the "had a brief chat with X" line must be truthful). The `campaign_members.notes` for this lead carries the staged context: `referrer`, `referrer_role`, `referrer_company`, `referrer_team`, `referrer_seniority`, `not_before`, `context`, `opener_variant`, plus the verbatim `ice_breaker` text composed at staging time (see `acme-propose-reply` skill → "The ice breaker"). At graduation, the cold outreach skill retrieves the pre-written `ice_breaker` and stitches a pitch + CTA on after — no re-composition. | — |

#### `state:` (pipeline process state)

| Tag | Meaning | Applied by | Removed by |
| --- | --- | --- | --- |
| `state:enrichment_queued` | Lead lacks usable company/title; drainer will pick up | Upload skills; classifier when prerequisites missing | Enrichment drainer on completion |
| `state:enrichment_failed` | Drainer gave up. Prevents re-queue loops. | Enrichment drainer on giving up | Manual when retry-worthy / new data source |
| `state:qualification_queued` | Needs classifier to run / re-run | Upload skills; requalifier on criteria bump; manual | Classifier on completion (any outcome) |
| `state:engager_research_queued` | Research this person's engagers | Manual; evaluator when prerequisites missing | engagers-research skill on completion |
| `state:engagers_researched` | Engagers research has run; `searches`/`posts`/`post_engagements` exist | engagers-research skill on completion | Manual only |
| `state:removed_from_monitoring` | The lead was removed as a **target** from the client's competitor-intel / Buyer Monitor workspace and is no longer scanned. The lead row stays — it's accurate history of who worked at a competitor — but nothing should re-add them without a decision. **required note**: `<reason>; removed from <workspace> YYYY-MM-DD`, where reason is one of `departed_competitor:now_at_<company>`, `excluded_role:<sales_operations\|sales_enablement\|sdr_bdr\|bare_vp>` (see `competitor_targeting.titles.excluded_target_reason`), `no_current_position_on_profile`, or `manual_removal`. | Whoever removes the target (manual or a cleanup pass) | Manual only — re-adding a target is a deliberate decision |

#### `flag:` (always-address operational flags about this lead)

`flag:*` tags are situational facts about the lead that **must be considered every time we engage**. They condition outreach but are not classifications (`qual:`), action plans (`plan:`), events (`they:` / `we:`), pipeline state (`state:`), or substantive concerns (`objection:`). Detail lives in `lead_tags.notes` — `flag:*` tags are usually meaningless without their note.

| Tag | Meaning | Notes |
| --- | --- | --- |
| `flag:parallel_threads` | The lead has multiple outbound conversations on our side (e.g. Dana AND Roman both reached out and got replies, or two separate LinkedIn chats with the same person). Acting on the "wrong" thread (the one we've deprioritized) risks a double-touch that looks uncoordinated. Notes encode which is active vs dormant + sender per thread + reason. | **required**: `Active: <chat_id> (<sender>); Dormant: <chat_id> (<sender>); Reason: <why dormant>; Updated: YYYY-MM-DD` |
| `flag:manually_approved` | A human (usually Dana) has explicitly approved pursuing this lead despite an automated signal that would otherwise hold or DQ them — most commonly an `out_of_geo:*` pre-check flag, but also any auto-DQ a person decides to override. Records that the override was deliberate so downstream skills don't re-flag or re-DQ. | **required**: `<who> approved YYYY-MM-DD; overrides <what flag/auto-DQ>` |
| `flag:borderline_qualified` | The lead clears qualification but with real doubt — e.g. a job-seeker signal in their headline, a scope question that reads as angling for a role/ongoing gig rather than buying, or an IC at the edge of persona. Keeps them in play while flagging that the fit is soft, so we invest cautiously (and don't over-commit scarce CEO slots without watching intent). | **required**: one-phrase reason for the doubt |
| `flag:meeting_not_attended` | A scheduled meeting did not take place. Records that a booked call was missed and why, so we re-book deliberately (pairs with `plan:rebook_meeting`) instead of assuming it happened. Most common cause so far: a calendar invite created via the API never reached the prospect (landed in spam / no guest notification sent), so the prospect never knew. Note records which meeting + the cause. **An event DID exist** — contrast `flag:booking_dropped`, where no event was ever created. | **required**: `<meeting date/time> — <cause>` (e.g. `Jun 11 2pm WIB — invite undelivered, landed in spam`) |
| `flag:booking_dropped` | We had a **confirmed meeting time from the prospect** but dropped the ball formalizing it into a booked meeting — most commonly the calendar invite was **never issued** (operator traveling / busy / handoff missed), so **no event ever existed** and there was nothing to attend. This is OUR process failure at the booking step, upstream of a no-show. **Distinct from `flag:meeting_not_attended`** (an event existed but the meeting didn't happen — undelivered invite or no-show) and from `plan:rebook_meeting` (the recovery action; this flag records the cause). Pairs with `plan:rebook_meeting`. The recovery message should acknowledge the miss casually (once) and make re-booking frictionless (lead with Calendly so a fresh invite + Zoom link go out reliably). Note records the dropped slot + cause. | **required**: `<confirmed date/time> — <what dropped> (cause)` (e.g. `Jul 8 9:30 AM ET — invite never issued, operator traveling + slot double-booked`) |
| `flag:possible_consultant` | The lead works at a consulting firm, systems integrator, or MSP (Accenture, Deloitte, Capgemini, etc.) and may be a **consultant/partner rather than a direct buyer** of our product. Holds them for an ICP decision instead of treating them as a normal prospect — they might be a channel/advisor play (`plan:partnership_play`) or out of ICP entirely. Pairs with `qual:pending` + `plan:check_fit` until decided. | **required**: `<firm> — consulting/integrator/MSP; possible consultant/partner not direct buyer; verify ICP fit` |
| `flag:security_vendor` | The lead works at a **security product vendor** (their employer builds/sells security tooling), so they're not a direct-buyer ICP — at best a peer/industry contact. This is the **lead-level** marker; it is **independent of** the company-level `company_relationships.relationship='security_vendor'` row (which drives the triage's `is_security_vendor` auto-DQ for all that company's employees). Use both: the company relationship catches everyone automatically; this flag records the judgment on the individual lead. Typically pairs with `qual:disqualified` + `plan:pass`. | **required**: `<firm> — security product vendor; employee not a direct-buyer ICP` |
| `flag:awaiting_dana_decision` | The lead's **next strategic decision is Dana's to make**, not the outreach skill's — e.g. how to qualify a sharp self-declared non-buyer (networker/nurture vs pass), whether to pursue an edge-case persona/account, or any judgment we deliberately escalate to him. **🚨 CANONICAL PROCEDURE — whenever a lead needs a pursue/route/pass (or any strategic) choice that is Dana's, you MUST: (1) apply `flag:awaiting_dana_decision` with the required note, (2) set `qual:pending` + `plan:check_fit`, (3) park the exact proposed reply in `campaign_members.notes` (prefixed `DRAFT PENDING DANA REVIEW & DECISION (<date>)…`), and (4) NOT send anything.** This is the single tag that the `acme-pending-decisions` report scans, so a lead missing this tag is invisible to that report — never park a Dana-decision lead under `plan:check_fit` alone. The reply we send meanwhile (if any) must be low-commitment (feedback ask / zerodayclock), never a locked meeting. **Clear it once Dana decides**, replacing it with the resulting `qual:`/`plan:`. Distinct from `flag:manually_approved` (Dana already decided *yes*) — this is the *pending* state. | **required**: `<the decision Dana must make> — <one-line context>; clear once decided (replace with the resulting qual:/plan:)` |
| `flag:in_active_hubspot_sequence` | The lead is currently **enrolled in a live HubSpot Sales sequence** (e.g. the BlackHat "party to coffee" invite sequence run from HubSpot, mirrored into MarketBase as a free-text campaign). They are already receiving outbound *from HubSpot*, so staging them into a MarketBase (Smartlead/LinkedIn) outbound campaign would **double-message them across two systems**. `marketbase-stage-to-campaign` **skips leads carrying this flag unless `--force-sequenced`** is passed. Adjacent systems (Smartlead sequencer, any custom stager) should honor it the same way. Clear it when the HubSpot enrollment ends (completed / unenrolled / replied). Added 2026-07-27 (alice). | **optional**: `<sequence name>` (e.g. `Conference Invite 2026`) |
| `flag:remove_from_campaign` | The lead is a member of a MarketBase outbound campaign it **should not be in** — most commonly a **disqualified** lead that got `uploaded`/`staged` into an active Smartlead-LinkedIn campaign anyway. Marks them for removal so they're pulled from the send (set the membership `status='removed_other'`) rather than messaged. A cleanup/reconciliation marker: apply it, then the removal is the follow-through. Added 2026-07-27 (alice). | **optional**: `<why>` (e.g. `disqualified (persona_mismatch) but uploaded in conferenceEvents_tier3`) |

### Lifecycle rules (apply across all lifecycles)

- **Tags index state; detail lives elsewhere.** Decision detail goes into
  `lead_qualifications.full_result` JSONB; action detail goes into
  `lead_actions` rows. **Most tags are note-less.** The exception is a small
  named **note allow-list** — tags whose meaning is incomplete without one
  phrase of context. For those, `lead_tags.notes` holds that single phrase.
  Anything richer than a phrase still belongs in `full_result` or
  `lead_actions`.

  **Note allow-list** (the only tags allowed to write `lead_tags.notes`):
  - `they:redirected_to_colleague` — **required**: comma-separated colleague names.
  - `we:sent_referral_ask` — **required**: comma-separated proposed names.
  - `plan:snooze` — **optional**: resume date `YYYY-MM-DD`. Human hint only — for hard enforcement use a `lead_actions(action='resume_outreach', not_before=…)` row.
  - `plan:deal_hold` — **required**: `Active deal at employer — <deal_company> (<open_stage>)`. Machine-owned (writer: `smartlead:deal_sync`). Added 2026-07-28 (alice).
  - `plan:verify_disqualification` — **optional**: one-phrase reason for the doubt (e.g. `replied before v5 persona flip`).
  - `qual:disqualified` — **optional**: one-phrase DQ reason (e.g. `VAR/reseller`, `wants consulting fee`, `out-of-geo:India`, `met -> irrelevant`, `security vendor`). Added 2026-07-07 (alice) so DQ rationale persists on the tag itself.
  - `flag:parallel_threads` — **required**: structured block `Active: <chat_id> (<sender>); Dormant: <chat_id> (<sender>); Reason: <why dormant>; Updated: YYYY-MM-DD`.
  - `plan:discuss_webinar` — **optional**: one-phrase context (e.g. `serious practitioner, posts on topic; declined vendor call but engaged as peer`). Added 2026-07-08 (alice).
  - `flag:booking_dropped` — **required**: `<confirmed date/time> — <what dropped> (cause)` (e.g. `Jul 8 9:30 AM ET — invite never issued, operator traveling + slot double-booked`). Added 2026-07-08 (alice).
  - `flag:in_active_hubspot_sequence` — **optional**: the HubSpot sequence name (e.g. `Conference Invite 2026`). Added 2026-07-27 (alice). Honored by `marketbase-stage-to-campaign` (skips unless `--force-sequenced`).
  - `flag:remove_from_campaign` — **optional**: one-phrase reason (e.g. `disqualified but uploaded in conferenceEvents_tier3`). Added 2026-07-27 (alice).

  **Adding a new tag that needs notes:** add it to this allow-list in the
  same change that introduces the tag, with its required-vs-optional status
  and the expected note shape. Skills must refuse to write `lead_tags.notes`
  for any tag not on this list (warn now, hard-fail in a later release).

  **Historical noise:** earlier classifier code wrote notes to `qual:*` tags
  (~1,450 rows as of 2026-06-01, mostly Acme-AI `qual:qualified` and
  `qual:disqualified`) — duplicates of `lead_qualifications.full_result.reason`.
  No data migration; the rows stay as documentary residue. New classifier
  code must not add to them.
- **Mutex pairs**: whoever applies one removes the other.
  - `qual:qualified` ↔ `qual:disqualified`
  - `state:engager_research_queued` ↔ `state:engagers_researched`
  - `state:enrichment_queued` ↔ `state:enrichment_failed`
  - `qual:thought_leader` ↔ `qual:thought_leader_rejected`
  - `they:redirected_to_colleague` ↔ `they:declined_to_give_feedback`
- **`qual:networker` can stand alone or coexist** with `qual:disqualified`
  (a person can be a non-buyer but still worth networking).
- **`plan:do_not_pursue` overrides everything.** Both the enrichment drainer
  and the classifier skip leads carrying this tag. Outreach skills also skip.
- **Reply tags bump `campaign_members.status='replied'`.** Any of these
  `they:*` tags, when applied (manually or by the syncer), bumps the lead's
  `campaign_members.status` to `'replied'` — guarded by the same
  terminal-status set used elsewhere (i.e. won't overwrite `'replied'`,
  `'meeting_booked'`, `'disqualified'`, `'completed'`, `'removed_blocked'`,
  `'removed_other'`):
  - `they:replied`
  - `they:willing_to_give_feedback`
  - `they:declined_to_give_feedback`
  - `they:politely_greeted`
  - `they:redirected_to_colleague`
  - `they:gave_deep_feedback`
  - `they:skeptical_but_engaged`
  - `they:asked_about_scope`
  - `they:job_seeker`
  - `they:confused`

  Tags that are **NOT** reply tags (and therefore don't bump status to
  `'replied'`): `they:reacted_to_our_message` (emoji only, no message),
  `they:engaged_with_competitor_content` / `they:engaged_with_our_content`
  (post engagement, not a DM reply). Note `they:booked_meeting` and
  `they:accepted_calendar_invite` skip the `'replied'` bump too — but only
  because they bump straight to the stronger `'meeting_booked'` status (see
  the meeting flow section).

- **Outreach-only pause tags.** `plan:ic_do_not_pursue`, `plan:snooze`, and
  `plan:cooldown` all block outreach but leave enrichment and classifier
  running — qualifications stay current so the lead is ready to re-engage
  the moment the tag is cleared. They differ by *why* outreach is paused:
  - `plan:ic_do_not_pursue` — policy stance against a class of leads (we may
    pursue ICs later; clear in bulk when the policy flips).
  - `plan:snooze` — lead-requested or lead-signaled wait (clear when the
    resume date arrives).
  - `plan:cooldown` — self-imposed quiet period after we chased without
    reply (clear after the cooldown window).

  Tags don't carry timestamps; if a specific resume / clear date matters,
  record it in `lead_actions` (`action='resume_outreach', not_before=<date>`)
  and let the sequencer enforce. Report on each tag independently — they
  signal different lead temperatures.

  A fourth pause tag, `plan:deal_hold`, is **machine-owned and precedence-special**:
  - `plan:deal_hold` is machine-owned; `smartlead:deal_sync` is its **only** writer.
    It coexists with a human `plan:*` and with any `qual:*`. It only suppresses
    sending while active. On resume, the lead's human `plan:*` (if any) is the
    true disposition. Never hand-apply or hand-remove it. Unlike the three tags
    above — which pause outreach for a lead-signal / policy / self-imposed-chase
    reason — `plan:deal_hold` pauses for an **employer** reason (their company
    has an open/won deal) and auto-resumes (smartlead clears it) once the employer
    no longer sits in `v_leads_at_deal_company`.
- **The evaluator never blocks.** If prerequisite data is missing, it tags
  work-to-be-done (`state:*_queued`) and exits. Subsequent invocations after
  the drainer runs advance the lead.
- **`state:*_queued` clears on drainer completion**, regardless of outcome.
  Drainer writes the result row → sets the appropriate `qual:*` or `state:*`
  → drops the `_queued` tag.
- **Pipeline order**: `state:enrichment_queued` → enrichment drains →
  `state:qualification_queued` → classifier drains → `qual:qualified` /
  `qual:disqualified`. The thought-leader lane runs parallel:
  `qual:potential_thought_leader` → `state:engager_research_queued` →
  `state:engagers_researched` → `qual:thought_leader` /
  `qual:thought_leader_rejected`.

### Relationship to `lead_actions`

`lead_actions` is the **one-shot intent queue** — "please do this specific
thing for this lead right now". Tags are **durable state**. They cohabit:

- A `lead_actions.action='requalify'` row triggers the classifier ONCE.
- The resulting state (`qual:qualified` / `qual:disqualified`) is the tag.
- Subsequent invocations of the classifier without a fresh `lead_actions` row
  rely on `state:qualification_queued` to know what to look at.

### `company_relationships.relationship` — canonical values

`company_relationships` flags a company with a typed relationship to the
client we're selling for. The `relationship` column is text (no enum) so
new values can be added without DDL, but skills must use the canonical
set below. `scope` typically holds qualifiers like `direct` / `via_acquisition`
/ `inferred`; `notes` holds plaintext detail (which competitor, source URL,
date observed, etc.).

| `relationship` | Meaning | Effect on lead qualification |
| --- | --- | --- |
| `self` | The client we're selling for. | Employees DQ'd as our own people. |
| `competitor` | Direct competitor — sells our product's category. | Employees DQ'd. |
| `security_vendor` | A cybersecurity / adjacent-security vendor that isn't a direct competitor. Useful when an industry classifier misses a vendor or for adjacent vendors that we still don't sell to. | Employees DQ'd. |
| `bought_competitor_product` | Known customer of a competitor — they already invested in a competing solution. The `notes` field records which competitor and the signal source (e.g. `uses Maze; source: maze.io/customers 2026-05`). | Employees DQ'd. |
| `customer` | A current paying customer of the client. | Employees DQ'd from buyer outreach. |
| `partner` | A formal partner / reseller / channel partner of the client. | Reserved — no automatic DQ today. |
| `vendor` | A vendor *to* the client (e.g. a tool we use). | Reserved — no automatic DQ today. |

**Employer-DQ classifier** — `marketbase-policy@employer_dq_v2.0` reads
`company_relationships` and writes `lead_qualifications` rows with
`qualified=false`, `reason='employed_at_<relationship>'`, and
`full_result.relationship` / `full_result.relationship_notes` capturing the
exact match. The relationship values that DQ today are `competitor`,
`security_vendor`, `bought_competitor_product`, `self`, `customer`.

### Migration from legacy unprefixed tags

Earlier tag names without category prefixes (`thought-leader`,
`potential_thought_leader`, `engagers_researched`, `engager_research_queued`,
`thought-leader-rejected`) are **deprecated as of this convention**. They
remain readable but no new code should write them. The data migration to
prefix them is a one-shot UPDATE per client DB:

```sql
UPDATE lead_tags SET tag = 'qual:thought_leader'          WHERE tag = 'thought-leader';
UPDATE lead_tags SET tag = 'qual:thought_leader_rejected' WHERE tag = 'thought-leader-rejected';
UPDATE lead_tags SET tag = 'qual:potential_thought_leader' WHERE tag = 'potential_thought_leader';
UPDATE lead_tags SET tag = 'state:engagers_researched'    WHERE tag = 'engagers_researched';
UPDATE lead_tags SET tag = 'state:engager_research_queued' WHERE tag = 'engager_research_queued';
```

Run via `migrate_all_clients.py` when ready to cut over. Skills referencing
the old names need to be updated to read both old and new for the
transition window, then to new-only after.

## Client-specific orchestrator skills

When a client needs a workflow that chains generic primitives in client-specific ways (e.g. Acme-AI's "evaluate this candidate" workflow), build a skill named `marketbase-<client-slug>-<verb>-<noun>`:

- `marketbase-acme-ai-evaluate-candidate`
- `marketbase-acme-qualify-lead`

These skills:

- Live alongside the generic MarketBase skills under `~/.claude/tools/MarketBase/`.
- Read **only** the client's MarketBase (passed `--client <Name>`).
- Read per-client classifier configs from `~/.claude/clients/<Client>/classifiers/<name>.yaml` (planned — for now, decision logic lives in the skill itself).
- Compose the generic primitives (`marketbase-tag-lead`, `marketbase-classify`, etc.) rather than re-implementing them.

## Classifier outputs

Every qualification — whether from a generic `marketbase-classify` invocation or a client orchestrator's internal decision — writes a row to `lead_qualifications` with:

- `qualifier_name`: stable identifier for the rule (e.g. `basic-creator-check`, `acme-ai-thought-leader-evaluation`)
- `qualifier_version`: bump when the rule changes (`2026-05-acme-ai`, `1.0`, etc.) so old qualifications stay re-derivable
- `qualified`: bool
- `persona`: free text — typically the "label" the classifier assigned (e.g. `very active demand gen practitioner`, `CISO`)
- `reason`: short human-readable string
- `full_result`: JSONB of the inputs + intermediate scores + final output so the decision is fully audit-able

The view `lead_current_qualification` returns the most recent qualification per lead (across all qualifier_names). Use it for "what's their current state?" queries.

## Campaign member milestone statuses

Some `campaign_members.status` values are **non-terminal milestones** — the
lead is still being worked, but a meaningful checkpoint has been reached.
These are driven by tag-triggered automation, not by sequencer state alone.

### `meeting_booked`

Set when a meeting is on the calendar — either the prospect accepted a direct
calendar invite we sent (`they:accepted_calendar_invite`) **or** they self-booked
a slot themselves (`they:booked_meeting`, e.g. via Calendly). Both flip the
status. (Updated 2026-06-24, alice: a self-booking now counts as booked —
superseding the older carve-out where `they:booked_meeting` alone left the status
unchanged. See the note under the decision flow.)

**Trigger rule (application-side, enforced by `marketbase-tag-lead`):**

```sql
-- ON applying tag `they:accepted_calendar_invite` OR `they:booked_meeting`:
  UPDATE campaign_members
     SET status = 'meeting_booked',
         last_status_at = now(),
         last_status_source = 'marketbase-tag-lead:' || <the tag applied>
   WHERE lead_id = <lead>
     AND status NOT IN ('meeting_booked', 'disqualified', 'completed',
                        'removed_blocked', 'removed_other');
  DELETE FROM lead_tags
   WHERE lead_id = <lead> AND tag = 'plan:book_meeting';
```

The `status NOT IN (...)` guard preserves terminal statuses — a previously
disqualified or completed lead that later accepts an invite is unusual but
should not be silently revived; surface it manually instead.

### `meeting_cancelled_by_us`

Set when **we** cancel a booked meeting (not the prospect). Distinct from a
prospect-initiated cancellation, a no-show, or a reschedule — this records that
**our side** called it off, usually because the meeting is no longer needed
(e.g. we already got the org's feedback through another contact, the lead was
disqualified after booking, or priorities changed). Added to the
`campaign_member_status` enum 2026-06-16.

When applying it, also: cancel the actual calendar event (this is one of the
few times cancelling a prospect-visible event is correct — but **only**
alongside a message to the prospect explaining the cancellation, so the
notification doesn't blindside them), set the `lead_meetings` row to
`cancelled` with a note on why, and set the lead's `plan:*` to whatever follows
(`plan:pass` if we're done, `plan:nurture` if keeping warm, `plan:rebook_meeting`
if we'll re-book later). Verified: Xavier Frederick (Oracle, 2026-06-16) — Dana
had already gathered Oracle's feedback via their VP of Security, so we cancelled
to respect Xavier's time.

### Decision flow — booking a meeting

```text
we:sent_them_possible_times   (we shared a Calendly / availability)
     │
     ├─→ they:booked_meeting              (they self-served)
     │       ↳ status = 'meeting_booked'
     │       ↳ plan:book_meeting removed
     │
     └─→ we:sent_calendar_invite          (we sent a direct .ics)
             │
             └─→ they:accepted_calendar_invite
                     ↳ status = 'meeting_booked'
                     ↳ plan:book_meeting removed
```

Both paths flip the status to `'meeting_booked'` (updated 2026-06-24, alice). A
self-booking now counts as booked, so **we** record it ourselves at the moment we
tag the lead, rather than leaving the status at `'replied'` until Smartlead's
reconciliation catches it. The earlier carve-out (where `they:booked_meeting`
alone left the status unchanged because Calendly self-bookings sometimes ghost) is
superseded: track no-shows / drop-offs via `flag:meeting_not_attended` +
`plan:rebook_meeting` after the fact, not by withholding the booked status up
front.

## Campaign member terminal statuses

`campaign_members.status` is an enum lifecycle. Several of its values are
**terminal** — i.e. the lead is no longer being actively worked in that
campaign. Picking the right terminal value matters for reporting and audit.

| Terminal status | When to use | What goes in `last_status_source` |
| --- | --- | --- |
| `disqualified` | Removed because the lead's current qualification flipped to `qualified=false`. The "why" is queryable from `lead_current_qualification.reason` + `disqualified_reason`. | `sequencer:<name>:<reason>` (e.g. `sequencer:smartlead:employed_at_competitor`) — optional, since the lead-level qualification already holds the why. |
| `removed_blocked` | The upstream platform (Smartlead, Smartlead, LinkedIn, etc.) **rejected** the action — out of our control. Examples: LinkedIn blocked the message, the account got restricted, the inbox bounced. | `sequencer:<name>:<platform_reason>` |
| `removed_other` | Catch-all for everything else — manual deletion, dedupe, list cleanup, user request, "wrong list," etc. Use only when none of the above fits. | `manual:<user>:<reason>` or `sequencer:<name>:<reason>` |
| `completed` | The lead successfully reached the end of the sequence (e.g. all messages sent, no reply expected). | n/a |

**Picking the right one — rule of thumb**: if the removal trigger is a
`qualified=false` row in `lead_qualifications`, use `disqualified`. If the
upstream platform blocked an action, use `removed_blocked`. Otherwise use
`removed_other`. Never use `removed_blocked` to mean "we disqualified them"
— it muddies platform-error reporting.

Flow: a lead at the lead level gets DQ'd by a classifier (e.g.
`marketbase-policy@employer_dq_v2.0`) → `v_pending_removals` surfaces them
(computed branch: active status + `qualified=false`) → sequencer removes
them upstream → sequencer updates `campaign_members.status = 'disqualified'`.
The `status_history` JSONB trigger records each transition.

## Tagging integrity rules (conversation/engagement tags)

Two hard rules govern `they:*` / `we:*` conversation tags and the `replied`
status. They exist because Smartlead's `sequencer:smartlead:reply_reconciliation` was
stamping `they:replied` (and `campaign_members.status='replied'`) from its own
reply signal — which counts connection-accepts and other non-replies — **without
syncing an actual message**. Half of all `they:replied` tags turned out to have
no inbound message behind them (many had no conversation at all).

- **RULE A — message-backed.** `they:replied` and `campaign_members.status='replied'`
  MUST be backed by a real inbound message in `lead_messages`
  (`direction in ('in','inbound','received')`). Never trust an upstream platform's
  "replied" flag on its own. Our own `sync_conversations.py` does this correctly
  (it only tags on a new inbound message); anything sourced from a sequencer
  reconciliation must be verified against a synced message.

- **RULE B — campaign-staged only.** We only ever tag prospects we actually staged
  into a campaign. A `they:*` / `we:*` tag may exist only on a lead who has a row in
  `campaign_members`. If someone appears to have replied but was never in a campaign,
  do not tag them — we did not initiate that outreach.

Qualification (`qual:*`) and sourcing tags are exempt from Rule B — a lead is
qualified/sourced *before* being staged. These rules apply to conversation-outcome
tags only.

**Enforcement.** `prune_phantom_engagement.py --client <C> [--apply]` repairs
existing violations (deletes phantom/non-campaign conversation tags, resets phantom
`replied` statuses to their prior status) and is idempotent — run it periodically as
a guard. Any process that writes these tags must satisfy both rules at write time.
