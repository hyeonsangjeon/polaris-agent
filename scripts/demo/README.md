# Deterministic offline demo fixture

`create_fixture.py` creates a **synthetic storyboard**, not a fake provider and
not a production journal. It makes no daemon, model, tool, or network call and
uses fixed IDs/timestamps so CI can compare output deterministically.

```bash
uv run python scripts/demo/create_fixture.py --output demo-output
find demo-output -type f -maxdepth 2 -print
```

The generated files show the intended narrative:

1. a fan-out run exists;
2. two worker records are already committed;
3. a storyboard marker represents daemon death;
4. one expired read-only worker resumes;
5. the verifier records a disagreement with unsafe blanket retry; and
6. replayable report/evidence/disagreement files have SHA-256 entries.

Every generated surface is labeled `fixture` or `synthetic`. It must not be used
as evidence that a live Ollama/Foundry run succeeded or that web research
completed in 30 seconds.

For a real drill, follow the README Ollama quickstart, record the run ID, terminate
only the foreground daemon, restart it after the lease expires, inspect the
timeline, and run `polaris replay RUN_ID`.
