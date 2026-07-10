# D0–D30: the 100-star experiment

This is a learning protocol, not a promise or a growth hack. The goal is to test
whether Polaris's durability behavior is useful enough that informed users choose
to follow the project.

## Hypothesis

If operators can reproduce a crash, inspect the exact recovery decision, and
replay a result locally in under 15 minutes, then the project can earn 100
authentic GitHub stars without obscuring alpha risk.

## Guardrails

- Never buy, trade, automate, or pressure for stars.
- Never claim live web research completes in 30 seconds.
- Never publish a benchmark without scripts, configuration, hardware, and raw
  results.
- Never upload user prompts, artifacts, telemetry, or contact information.
- Never weaken approval, security, or uncertainty wording to improve conversion.
- Disclose Ollama model/version, Foundry deployment settings, and whether a demo
  is a deterministic fixture.
- Stop outreach if issue response or security triage cannot keep pace.
- Count a star as interest, not validation, safety evidence, or production use.

## Activation definition

An activated evaluator has:

1. completed setup and `doctor`;
2. submitted one bounded run;
3. inspected its run ID/timeline;
4. restarted or killed the daemon in a disposable recovery drill; and
5. replayed a completed result or observed an honest uncertainty stop.

No analytics are added to the product. Measure only voluntary, aggregate GitHub
signals and opt-in issue/discussion responses.

## Protocol

## D0 baseline

Recorded at `2026-07-10T19:47:00Z`, immediately after publishing `v0.1.0`:

| Signal | Baseline |
|---|---:|
| Stars | 0 |
| Forks | 0 |
| Subscribers | 0 |
| Open user issues | 0 |
| Open pull requests | 13 |
| CI | Passed: Python 3.11–3.13, frontend, Rust, Docker, docs |
| CodeQL | Passed: Python and JavaScript/TypeScript |
| Release assets | 6 |

All 13 pull requests were automated dependency updates created after the first
push; they are recorded separately from user reports and contributions.

### D0–D2: establish the baseline

- Tag the documentation state and record repository stars, unique contributors,
  open issues, median first-response time, and CI status.
- Run the offline fixture and the Ollama quickstart on a clean machine.
- Ask two reviewers to flag every unsupported claim.
- Publish one architecture image and one recovery transcript with environment
  details and redacted data.

**Exit:** quickstart commands work as written and all severity-high documentation
errors are fixed.

### D3–D7: five observed evaluations

- Invite five relevant local-agent/durable-execution practitioners individually.
- Observe setup only with consent; record friction categories, not personal data.
- Ask: “What did you expect after the kill?” and “Could you explain replay versus
  rerun?”
- Convert repeated friction into setup-help or bug issues.

**Target:** 4/5 complete `doctor`; 3/5 reach activation without maintainer shell
access.

### D8–D14: reproducible public demonstration

- Publish a short demo using the deterministic fixture, clearly labeled offline.
- Publish a separate live Ollama recovery drill with elapsed time reported as
  observed, not guaranteed.
- Share in at most three technically relevant communities where project posts
  are allowed.
- Answer every substantive question and add corrections to the changelog/docs.

**Target:** at least 10 activated users or 25 stars, with fewer than 20% reporting
  a misleading durability expectation.

### D15–D21: compare messages, not safety

Test two README entry points over equal windows:

- outcome-first: “recoverable, inspectable work after process death”;
- mechanism-first: “SQLite journal, leases, receipts, and replay.”

Use GitHub referral/traffic aggregates only. Do not fingerprint visitors. Keep
technical and limitation sections identical.

**Decision:** retain the entry point that produces more successful quickstarts,
not merely more views.

### D22–D27: contribution readiness

- Label good first documentation/reproduction tasks.
- Time a clean contributor setup using the exact commands in `CONTRIBUTING.md`.
- Review issue forms for redaction and recovery context.
- Triage any security reports before additional promotion.

**Target:** two external issue reports with complete reproduction context or one
reviewed external contribution.

### D28–D30: decide

Record:

- stars gained (interest);
- activated evaluators (behavior);
- quickstart completion rate;
- first-response and fix time;
- misleading-claim reports;
- unresolved security/high-severity bugs; and
- maintainer hours.

Outcomes:

- **Continue:** activation ≥50% among observed evaluators, no unresolved critical
  security issue, and support load is sustainable.
- **Revise:** interest exists but activation <50%; spend the next cycle on setup
  and recovery observability, not promotion.
- **Pause:** critical security concern, repeated data-loss expectation, or support
  backlog exceeds the stated response capacity.

Reaching 100 stars does not override a pause condition.
