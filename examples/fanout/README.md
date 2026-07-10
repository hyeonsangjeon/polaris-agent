# Local fan-out example

[`workers.json`](workers.json) documents the three roles and budget in a
machine-readable storyboard. The current CLI accepts those values as explicit
flags; it does not import this storyboard file.

Prerequisites:

```bash
ollama pull llama3.2
uv run polaris setup --root "$PWD"
uv run polarisd
```

Keep the daemon running, then use another terminal:

```bash
sh examples/fanout/run.sh
```

Equivalent command:

```bash
uv run polaris run "Compare the durability risks in this repository." \
  --mode fan-out \
  --worker ollama:recovery \
  --worker ollama:security \
  --worker ollama:operations \
  --verifier ollama \
  --synthesizer ollama \
  --call-limit 24 \
  --token-limit 32000 \
  --wall-seconds-limit 900 \
  --wait
```

Each `--worker` value is `provider:role`. Polaris generates stable worker IDs,
runs at most eight workers, allocates fixed budget slots, then invokes the
verifier and synthesizer. In this example every stage uses the same configured
Ollama model; diversity comes from role instructions, not hidden model routing.

Model output is not guaranteed to contain valid evidence. A failed evidence or
budget contract fails the run rather than presenting fabricated success.
