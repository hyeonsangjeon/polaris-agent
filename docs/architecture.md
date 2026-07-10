# Architecture

Polaris separates operator surfaces, durable coordination, and model providers.
The Python daemon is the authority for run state. The CLI and macOS Tauri console
are independent authenticated clients.

![Polaris architecture: clients call a loopback daemon backed by a journal and artifacts, which coordinates Ollama or Foundry](assets/architecture.svg)

```mermaid
flowchart TB
    subgraph Clients["Operator clients"]
        CLI["polaris CLI"]
        UI["Tauri macOS console"]
    end

    subgraph Daemon["polarisd · 127.0.0.1:8765"]
        API["Bearer-authenticated FastAPI"]
        Service["Run service<br/>recovery · approvals · budgets"]
        Single["Single-agent runtime"]
        Ensemble["K-worker ensemble<br/>workers · verifier · synthesizer"]
    end

    subgraph Durable["Local durable state"]
        Journal[("SQLite WAL journal<br/>runs · steps · leases · events<br/>calls · receipts · approvals")]
        Artifacts[("Content-addressed artifacts<br/>evidence · reports · manifests")]
    end

    subgraph Providers["Provider boundary"]
        Ollama["Ollama<br/>explicit local models"]
        Router["Foundry Responses API<br/>model-router deployment"]
        Routed["Underlying model<br/>selected by Foundry"]
    end

    CLI --> API
    UI --> API
    API --> Service
    Service --> Single
    Service --> Ensemble
    Single <--> Journal
    Ensemble <--> Journal
    Ensemble <--> Artifacts
    Single --> Ollama
    Ensemble --> Ollama
    Ensemble --> Router
    Router --> Routed
```

## Components

### Operator clients

`polaris` creates and inspects runs, submits approval decisions, resumes eligible
work, and replays committed results. The Tauri desktop application is a separate
macOS operator console. Neither client owns recovery state.

### Daemon and API

`polarisd` listens on `127.0.0.1:8765` by default. Every `/v1` route requires the
setup-generated bearer token; `/health` is unauthenticated and returns only
service health. A non-loopback listener requires `--allow-remote` and a token.

The service schedules in-process tasks and, at startup:

1. reclaims expired leases;
2. ignores work with an active lease;
3. refuses automatic recovery across an uncertain opaque side effect;
4. verifies that the run's persisted provider names are still available; and
5. schedules eligible created/running top-level runs.

### Execution modes

- **single:** one durable model/tool loop.
- **fan-out:** one to eight explicit workers, followed by verifier and synthesizer
  stages. The Polaris K-worker engine owns concurrency and fixed budget slots.
- **foundry-router:** a thin fan-out strategy with one research worker, verifier,
  and synthesizer all calling the same `model-router` deployment. Foundry owns
  underlying model selection/failover.

### Journal and artifacts

The journal uses SQLite WAL with full synchronous commits. Run/step transitions,
leases, append-only events, provider calls, receipts, approvals, and budget
reservations share the durable record. The artifact store writes ensemble
outputs by content hash and records their metadata in the journal.

The journal is a coordination mechanism, not a distributed consensus system.
Keep its SQLite files on local storage, not SMB or NFS.

## Main run sequence

```mermaid
sequenceDiagram
    actor Operator
    participant Client as CLI / desktop
    participant API as polarisd
    participant J as Journal
    participant P as Provider
    participant T as Tool

    Operator->>Client: submit run + budget
    Client->>API: authenticated POST
    API->>J: create run and deterministic steps
    API->>J: claim lease + reserve budget
    API->>P: model request
    P-->>API: completion + response.model
    API->>J: commit provider call and step
    alt read-only or approved tool
        API->>T: execute
        T-->>API: result / receipt
        API->>J: commit result
    else approval required
        API->>J: persist approval + pause
        Operator->>Client: approve or deny
        Client->>API: durable decision
    end
    API->>J: commit terminal state + artifacts
    Client->>API: replay
    API-->>Client: recorded result, no execution
```

## Boundaries and non-goals

- The daemon is local-first but is not a host sandbox.
- The journal prevents accidental duplicate scheduling only where its recorded
  state and the operation's safety contract permit.
- Foundry routing configuration lives in the Foundry deployment.
- The desktop console does not embed or supervise the daemon.
- Docker Compose supports persistent deployment; it does not make SQLite safe on
  a network filesystem.

Continue with [durability](durability.md) and [security](security.md).
