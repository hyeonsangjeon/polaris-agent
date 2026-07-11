# Design rationale

Polaris is designed around observable failure behavior rather than breadth of
integrations. The following public Hermes Agent issues are **external reports**:
they provide examples of operational failure modes and feature pressure. Their
text and implementation are not copied here, and a report does not imply that
Hermes is uniquely affected or that Polaris eliminates every related risk.

## Resume only what is provably eligible

[Hermes #58933](https://github.com/NousResearch/hermes-agent/issues/58933)
reports a pending resume expiring into a silently replaced session. Polaris keeps
run identity and state transitions in the journal, exposes pause/failure state,
and recovers only created/running runs whose leases expired and whose providers
remain available. It does not silently create a replacement run.

[Hermes #51329](https://github.com/NousResearch/hermes-agent/issues/51329)
reports a cron double-fire race between dispatch and in-flight registration.
Polaris records deterministic step intent before ownership, then uses leases and
unique `(run_id, deterministic_key)` constraints. This narrows duplicate local
scheduling; it is not a distributed exactly-once claim.

[Hermes #59549](https://github.com/NousResearch/hermes-agent/issues/59549)
reports timeout handling that can leave child processes and misattribute the
failure as a provider timeout. Polaris classifies shell execution as an opaque
side effect and preserves typed step/event failures. Process-tree containment is
still a host-level concern and remains an alpha limitation.

## Probe providers explicitly, then preserve their contract

[Hermes #26489](https://github.com/NousResearch/hermes-agent/issues/26489)
reports a custom LiteLLM/Ollama path hanging during endpoint probing. Polaris has
a native Ollama contract with explicit tags, show, and chat endpoints; `doctor`
reports model availability, context length, and tool capability. It does not probe
through unrelated API families.

[Hermes #61265](https://github.com/NousResearch/hermes-agent/issues/61265)
reports oversized prompts stalling local OpenAI-compatible models. Polaris
surfaces model context in the Ollama doctor, exposes token/call/wall budgets, and
documents the Foundry router's smallest-subset context limit. Prompt compaction
and hard preflight token fitting are not claimed complete.

[Hermes #62055](https://github.com/NousResearch/hermes-agent/issues/62055)
reports a desktop picker unexpectedly overriding a configured model and affecting
billing. Polaris persists the requested provider/model with the run. For Foundry
Router it separately records each actual `response.model`; the desktop is an API
client rather than an independent routing authority.

## Make authority and policy visible

[Hermes #33905](https://github.com/NousResearch/hermes-agent/issues/33905)
requests per-tool/per-toolset approval policies. Polaris already assigns every
tool a safety class and defaults non-read operations to approval. The current
public configuration does not yet expose a complete per-tool policy language, so
the documentation does not claim one.

[Hermes #55039](https://github.com/NousResearch/hermes-agent/issues/55039)
requests depth-based subagent model routing for cost tapering, while
[Hermes #61622](https://github.com/NousResearch/hermes-agent/issues/61622)
adds per-subagent model selection. Polaris chooses an explicit provider per
fan-out worker and persists that selection. Foundry mode intentionally does the
opposite: every stage targets the router deployment and Foundry chooses the
underlying model. The two strategies are kept distinct to avoid hidden authority.

The same principle shapes the v0.2 harness. Memory is curated rather than
silently inferred, its profile/subject scope is bound outside model control, and
every run freezes what it saw. Schedules persist local occurrence identity before
dispatch and stop stale ambiguity instead of treating process restart as retry
permission. Telegram and Slack are narrow private transports with
two-dimensional allowlists, not new public control planes.

[Hermes #34273](https://github.com/NousResearch/hermes-agent/issues/34273)
requests customizable swarm verifier/synthesizer behavior. Polaris makes verifier
and synthesizer providers explicit and enforces evidence/disagreement output
contracts. Arbitrary custom prompt bodies are not currently presented as a
stable user configuration surface.

## Resulting principles

1. **Persist identity before work.** A resumed run is the same journaled run.
2. **Separate ownership from completion.** Leases expire; committed outputs do
   not.
3. **Classify effects before execution.** Recovery depends on read-only,
   idempotent, reconcilable, or opaque semantics.
4. **Stop at ambiguity.** A local journal cannot prove an arbitrary remote or
   shell effect.
5. **Name the routing authority.** Polaris owns explicit K-worker fan-out;
   Foundry owns selection inside a model-router deployment.
6. **Record requested and actual models.** This makes routing and billing
   surprises inspectable.
7. **Replay records; rerun work.** The verbs have different cost and side-effect
   semantics.
8. **Prefer behavioral comparisons.** Polaris is a narrow runtime, not a claim
   that other agent frameworks are categorically unsafe.
9. **Remember only by an explicit act.** Trust/provenance labels describe a
   memory claim; they do not grant it instruction authority.
10. **Separate trigger, execution, and delivery.** A due occurrence, a Polaris
    run, and a channel send have different identities and failure states.
11. **Deny remote input by two identities.** A recognized user in an unrecognized
    conversation—and the reverse—does not pass channel policy.

These choices use established concepts—curated memory, cron, transactional
inboxes/outboxes, idempotency keys, and least-privilege allowlists—without
copying another project's prose or presenting those concepts as novel. For the
implemented contract, see [durability](durability.md),
[architecture](architecture.md), [memory](memory.md), and
[scheduler](scheduler.md).
