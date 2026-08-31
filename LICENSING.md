# How MarketBase is licensed

A plain-language map of what covers what. The authoritative texts are `LICENSE`,
`datasets/LICENSE`, and `CLA.md`.

## What covers what

| Part of the repo | License | Commercial use |
|---|---|---|
| Code, schema, tools, plugin | [BUSL 1.1](LICENSE) → Apache 2.0 after 4 years | Free, except competing hosted/embedded products |
| `datasets/` | [CC BY 4.0](datasets/LICENSE) | Free with attribution |
| Contributions | [CLA](CLA.md) grants Impact 11, LLC the right to license them under any terms | n/a |

Anything not in this repository is not covered by this repository's license.

## The Change Date is automatic

Each released version converts to Apache 2.0 **four years after that version is
published**. The Change Date is expressed as a formula, not a fixed date, so no
per-release maintenance is needed and no version can accidentally be left
without a conversion date.

BUSL also caps this independently: its own terms convert a version on the Change
Date or the fourth anniversary of that version's first public distribution,
whichever comes first.

## Commercial licenses

If your intended use falls outside the Additional Use Grant, for example
offering a hosted MarketBase service, Impact 11, LLC sells commercial licenses.
Contact us rather than assuming.

## Relicensing

Impact 11, LLC can release **future** versions under different terms, because
the CLA gives us the necessary rights to every contribution.

Two things we cannot do, by design:

1. **Retroactively change an already-published version.** If you received a
   version under BUSL 1.1, you keep those rights permanently. We cannot revoke
   them.
2. **Cancel a pending conversion.** Every published version will become Apache
   2.0 four years after its release, regardless of what we do later. That
   promise is not reversible.

## Ownership and successors

Copyright in MarketBase is held by Impact 11, LLC and may be assigned to a
successor entity through reorganization, a change of corporate form, merger,
acquisition, or an asset sale. The `CLA.md` rights granted by contributors
expressly run to Impact 11, LLC and its successors, so a change of owner does not
disturb them.

Future releases will name the then-current Licensor. Already-published versions
continue to name the Licensor at the time of their release, and remain valid.

## Going source-closed

Impact 11, LLC is under no obligation to publish source for future versions.

Three things would remain true regardless:

1. Every already-published version stays licensed to everyone who received it,
   permanently and irrevocably.
2. Every already-published version still converts to Apache 2.0 four years after
   its own release. Ceasing to publish does not cancel a pending conversion.
3. Anyone may fork the last published version and continue it independently,
   subject to the Additional Use Grant until that version converts.

## Proprietary modules

BUSL is not a copyleft license. It does not require that everything built
alongside or on top of MarketBase carry the same license.

Impact 11, LLC may offer separately licensed commercial modules, hosted
services, or extensions. Those are **not** part of this repository and are not
covered by `LICENSE`. Third parties may likewise build proprietary extensions,
subject to the Additional Use Grant.

Where a directory carries its own `LICENSE` file, that file governs that
directory and takes precedence over the root license. `datasets/` is the current
example.

## Trademark

The BUSL grant covers copyright, not trademarks. "MarketBase" and the MarketBase
name and logo are not licensed for use in a way that suggests endorsement by or
affiliation with Impact 11, LLC. Forks must be renamed.

---

*This document is a summary for convenience and is not legal advice. Where it
and the license texts disagree, the license texts control.*
