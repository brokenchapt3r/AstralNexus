---
name: fullstack-development
description: 全栈开发 fullstack full-stack workflow: API contracts, database, frontend, live HTTP and browser checks, targeted repair and delivery evidence.
platforms: [windows, linux, macos]
---

# Fullstack Development

Use the existing repository, dependency environment and task contract. This
procedure supplies execution order; it does not change acceptance requirements,
file ownership or the selected technology stack.

## Establish a runnable vertical slice

1. Read the entry point, dependency manifest, API/data contract and existing
   tests. Record the exact working directory, executable and startup/test argv.
   Do not repeatedly enumerate the whole project once these are known.
2. Implement one user flow through database, API and UI. Reuse the agreed API
   names and response shapes. Give producers explicit files and consumers
   explicit dependencies. Parallelize independent features after this contract.
3. Name the owner of the service process and test database. Start the service,
   wait for its actual readiness response and record its address. A dead service
   is an environment prerequisite to repair, not evidence that all business
   assertions failed. Keep it available until dependent consumers finish.

## Execute checks against the current implementation

Run existing unit/integration tests first. Exercise real HTTP and browser
behavior required by the task, including persistence after reload and relevant
permission/business boundaries. Record commands, exit codes, test case outcomes
and tool receipts. A custom test adapter must summarize actual assertions in a
format specified by the runtime test execution contract. Never add a constant
success line or rewrite old failed receipts to satisfy the parser.

## Repair one recorded gap

Use `recovery_handoff.repair_procedures` when present. Carry the original
criterion and command forward. Inspect only the named failure and necessary
implementation paths first:

- Missing summary: fix the test adapter/collector, then rerun its assertions.
- Missing execution: execute the required check after verifying prerequisites.
- Failed assertion: repair the responsible implementation, then rerun.
- Stale evidence: rerun on the current versions; do not refresh report dates.
- Zero tests: repair test discovery/selection before reporting success.

If a prerequisite diagnosis is now false, provide the concrete contradictory
evidence. If the fix belongs to another owner, return the file, required change
and recheck command to the Governor. A report-only successor does not discharge
the test obligation. An implementation repair can be followed by a dependent
tester carrying that same criterion.

## Deliver after re-verification

Bind the final report to current test receipts and artifact versions. Preserve
failed history separately. Assemble and publish only after the required checks
pass. Report assembly, a completed node and a limited publication are not full
acceptance. Do not start a new investigation or rewrite unaffected features
merely because a recovery node has a new identifier.
