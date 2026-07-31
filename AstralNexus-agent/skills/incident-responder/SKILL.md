---
name: incident-responder
description: Investigate production incidents using logs, metrics, changes, dependencies, low-risk verification, mitigation, recovery, and postmortem evidence.
platforms: [windows, linux, macos]
metadata:
  astralnexus:
    source: https://www.skills.sh/rmyndharis/antigravity-skills/incident-responder
    install_command: npx skills add https://github.com/rmyndharis/antigravity-skills --skill incident-responder
    adapter: curated
---

# Incident Response

First establish impact, severity, affected users and a time-bounded evidence
window. Build and rank hypotheses from logs, metrics, deployments, database,
network and dependency evidence. Prefer reversible, low-risk checks; record
approval before rate limiting, rollback, traffic switching or data repair.
Verify recovery with metrics and consistency checks, then produce a timeline,
root cause, actions, residual risk and prevention plan.
