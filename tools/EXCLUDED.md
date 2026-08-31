# Deliberately not open sourced

These scripts existed in the private toolkit and were left out on purpose.

| File | Why |
|---|---|
| `classify_israeli_names.py` | Classifies people by name to infer nationality/ethnicity. Ethically wrong to publish, and superseded internally by location-based routing, which is both more accurate and less fraught. |
| `alon_post_engagement_xlsx.py`, `alon_post_report.py` | Built around one named individual's LinkedIn presence. |
| `adaptive_icp_webhooks.py`, `validate_adaptive_icp.py` | One customer's ICP webhook contract. |
| `knock_ai_thought_leader_audience_qualifier.py` | One customer's audience definition. |
| `migrate_swan_snooze_to_deal_hold.py` | One-shot data migration for a single instance. |
| `demand_gen_prompts.py` | 51KB of customer-tuned LLM prompts. |
| `sample_keyword_stats.py`, `load_tenant_scan.py` | Ad-hoc one-offs with no general use. |
| `.blitz_cache/`, `logs/`, `configs/buying_committee/` | Real scraped personal data and named customer configs. Never publishable. |

If a generic version of any of these is worth having, write it fresh rather than
sanitizing the original.
