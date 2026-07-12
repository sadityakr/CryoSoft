# Incident report template

Written when triage cannot conclude. Save to
`cryosoft/logs/incidents/YYYY-MM-DD-<short-slug>.md`. A complete report is a
successful outcome: it lets a stronger model or a human expert start where you
stopped instead of repeating your work.

```markdown
# Incident: <one-line symptom>  (YYYY-MM-DD)

## Symptom
What was expected, what happened instead, when it started, what changed
recently (from the human's words + LOGBOOK).

## Environment
- Config: <path + name>
- Branch/commit: <git describe>
- Main app state: closed / was running until <time>
- Relevant setup.md quirks consulted: <list or "none">

## Evidence collected
One row per diagnostic step, in order:

| # | Command / source | Result (FaultCode or value) | Interpretation |
|---|---|---|---|
| 1 | `check --json` | ... | ... |

Include raw log excerpts (with timestamps) that anchor the timeline.

## Layers ruled out
- L0 driver: <how it was ruled out, or "not ruled out">
- L2 config: ...
- Physical (visible to VISA): ...

## Suspected layer and hypothesis
Best current hypothesis, stated with confidence (high/moderate/low), and the
specific observation that supports it.

## What was NOT tried, and why
Especially: excitation tests skipped under safe-testing rules, hardware swaps
not yet performed, anything needing an approval that was not given.

## Recommended next steps
Ordered, concrete, each with who does it (human / agent / stronger model).
```
