# Architecture: {{project_name}}

<!-- Gate G2 (together with threat-model.md). Prefer the simplest design that meets the
     requirements. Every non-obvious choice gets an ADR. -->

## Overview
{{two_to_five_sentences_and_optionally_a_diagram}}

## Components
| Component | Responsibility | Talks to | Tech |
|-----------|----------------|----------|------|
| {{name}} | {{one_responsibility}} | {{neighbours}} | {{tech}} |

## Data
{{entities, stores, schema summary, retention, migration approach}}

## Interfaces
{{public APIs/CLI/events with request and error shapes}}

## Failure modes
| Dependency or step | Failure | Behaviour |
|--------------------|---------|-----------|
| {{dependency}} | {{timeout_or_error}} | {{retry_degrade_or_fail_fast}} |

## Quality attributes
{{how performance, availability, observability and testability are achieved}}

## Decision records
- ADR-001: {{decision}}: see docs/aidlc/adr/
