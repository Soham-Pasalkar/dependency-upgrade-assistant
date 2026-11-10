# Threat model: {{project_name}}

<!-- Gate G2. Tier 2 needs >= 3 threats, tier 3 needs >= 5. Rate by likelihood x impact.
     See references/threat-modeling.md. -->

## Assets
- {{asset_and_why_it_matters}}

## Trust boundaries
- {{boundary, e.g. browser to API, API to database, agent to tool, host to sandbox}}

## Threats
| ID | Boundary | Threat | Category | Mitigation | Residual risk |
|----|----------|--------|----------|------------|---------------|
| T-1 | {{boundary}} | {{what_can_go_wrong}} | {{STRIDE_letter}} | {{control_and_where_enforced}} | {{low_med_high}} |

## Abuse cases
- {{misuse_story}}

## Out-of-scope threats
- {{threat_and_reason}}
