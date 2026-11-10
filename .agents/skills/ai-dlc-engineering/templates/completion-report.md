# Completion report: {{project_name}}

<!-- Gate G5. Honesty section first: what is NOT verified matters more than what is. -->

## Summary
{{what_was_delivered_vs_the_intent}}

## Verification performed
| Check | Command | Result |
|-------|---------|--------|
| Full regression | {{command}} | {{result}} |
| Dependency audit | {{command}} | {{result}} |
| Secrets scan | `scan_secrets.py` | {{result}} |

## Not verified
- {{anything_untested_or_assumed}}

## Security review
{{summary_of_threat_model_mitigations_actually_verified_and_open_items}}

## Performance
{{measured_vs_non_functional_requirements}}

## Documentation
{{README_runbook_and_where_they_live}}

## Residual risks
- {{risk_owner_and_follow_up}}

## Release recommendation
{{ship / ship_with_conditions / do_not_ship, and why}}
