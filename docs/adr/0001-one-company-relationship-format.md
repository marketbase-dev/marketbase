# 1. One company-relationship format: company links plus claims, in every database

Date: 2026-11-25. Status: accepted.

## Context

What a company is to a client (a competitor, a possible competitor, the client itself, something else) was stored in three ways:

1. A per-client row (`company_relationships`): one row per company and relationship type, with how close a competitor is, whether we monitor it, whether the client chose it, and conflict vetting as columns. It cannot name the counterparty: "competitor" means "competitor of whoever owns this database". It has no history: a change overwrites the row.
2. Decisions as assertions (072): a choice, who made it, how, when, why; closed, never deleted.
3. Company links (`company_edges`, 040) with assertions on them, naming both companies. Used by the house instance, which holds many prospects at once, and for customer links.

The connection-tracking app holds every client in one database and is planned to merge with MarketBase. A format that cannot name the client cannot survive that merge, and the operator asked that every MarketBase instance use the same format.

The first design for the app mixed "what the company is" and "whether we monitor it" into one field. MarketBase had already learned, over migrations 035, 059 and 072, that these are separate facts (instances hold competitors that are deliberately not monitored). That design was withdrawn.

## Decision

Every database (each MarketBase instance and the app) stores company relationships as **form 3**:

- A company link names both companies and holds identity only. It is never deleted.
- Every fact about the link is its own assertion with history: the live marker (`relationship/<type>`; competitor with confidence `suspected` = a possible competitor), how close (`relationship/scope`), conflict vetting (`relationship/status`), monitor or not (`monitoring/monitor`, a decision), the client's choice (`client/choice`, a decision citing its artifact), leave out of scans (`scan/leave_out`, a decision), and how sellers are found (`discovery/*`).
- The instance's own company is a company marked `client/own_company = primary`.
- One write door (database functions, so Python and TypeScript share it). An identical write is a no-op; a change closes the old claim ("superseded by <id>") and opens a new one. Two ways to remove: stop monitoring (the company stays left out of scans) and withdraw as added by mistake (every live claim closed, the link kept).
- The per-client table becomes a read-only view with the same name and columns, so its readers do not change. Until then the door keeps its rows current.

## Consequences

- Every claim keeps who wrote it, who decided, how, when and why; nothing is overwritten or deleted.
- The two databases can merge by copying rows: same tables, columns and keys.
- Reclassifying a competitor as possible keeps its link and decisions; changing a link's type (competitor to other) is a withdrawal plus a new link.
- During the transition a link mirrored by the old table cannot be withdrawn, because the old table cannot say "withdrawn" without a delete.
- Writers move to the door one by one; the old table's rows are frozen, not dropped, at the swap.

## Alternatives rejected

- **Keep one "kind" field per company and client.** No history, and it mixes two facts.
- **Copy the per-client row into the app with a client column.** Still no history, and a second shape to convert at merge time.
