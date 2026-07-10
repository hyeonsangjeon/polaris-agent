# Ollama provider

Ollama is the default local provider. `polaris setup` configures `llama3.2` at
`http://127.0.0.1:11434`; no cloud provider is required.

## Setup

```bash
ollama serve
ollama pull llama3.2
uv run polaris setup --root "$PWD"
uv run polarisd
```

Keep `polarisd` running in that terminal unless the macOS launchd service is
installed. From another terminal:

```bash
uv run polaris doctor
uv run polaris run "Summarize README.md." --provider ollama --wait
```

The complete secret-free configuration shape is in
[`examples/config.ollama.json`](../../examples/config.ollama.json). Replace every
`/ABSOLUTE/PATH/...` placeholder with an existing private path before use.

## Context and tool probe

`polaris doctor` calls Ollama's tags and show endpoints. Check:

- `model_available` is `true`;
- `context_length` is large enough for the prompt, tool definitions, and expected
  output; and
- `tools` is `true` when the run needs tool calls.

Example output fields:

```text
ok: True
model: llama3.2
model_available: True
context_length: 131072
tools: True
```

Exact values come from the installed model. A model without tool capability can
still answer a simple prompt but cannot reliably drive a tool loop. Polaris does
not silently increase the model's context window.

## Local fan-out

```bash
uv run polaris run "Review this workspace from three perspectives." \
  --mode fan-out \
  --worker ollama:correctness \
  --worker ollama:security \
  --worker ollama:operations \
  --verifier ollama \
  --synthesizer ollama \
  --call-limit 24 --token-limit 32000 --wait
```

Polaris starts bounded K-worker tasks and calls the configured Ollama provider for
each role. The verifier and synthesizer are additional budgeted calls. The model
server may serialize work depending on its own memory and concurrency settings.

## Offline, no-cloud profile

Set the configuration policy:

```json
{
  "offline": {
    "enabled": true,
    "allowed_hosts": [],
    "allow_private_ips": false
  }
}
```

With the native Ollama provider, offline mode permits loopback endpoints, private
or link-local IPs when `allow_private_ips` is enabled, and exact hostnames listed
in `allowed_hosts`. Public endpoints are rejected. Do not configure Foundry or
another public provider in this profile. Polaris also omits the `http_fetch` and
SearXNG search tools entirely; local filesystem and terminal tools remain.

This is configuration enforcement, **not** an OS network sandbox. For a stronger
boundary, combine it with host firewall/container network policy and verify the
result in your environment.

## NAS and LAN endpoints

For Docker Desktop, the supplied container example uses:

```json
"base_url": "http://host.docker.internal:11434"
```

For Linux Compose, include
`deploy/docker/compose.external-ollama.yaml`. For a trusted LAN/NAS Ollama server,
use its private address and keep `offline.allow_private_ips` enabled or list the
exact hostname in `offline.allowed_hosts`. Ollama and the daemon clients ignore
HTTP proxy environment variables so local/LAN traffic is sent directly.

Security considerations:

- Ollama commonly serves plain HTTP; restrict it to a trusted network.
- Do not publish Ollama or Polaris directly to the public internet.
- Set `tools.roots` to container-visible absolute paths.
- Keep `journal.sqlite3` on local storage even when exports live on a NAS share.

See [Docker and NAS deployment](../../deploy/docker/README.md).
