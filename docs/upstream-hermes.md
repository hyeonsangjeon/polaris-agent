# Hermes Agent provenance

Polaris Agent is an independent project. It does not use Hermes Agent names,
branding, documentation prose, prompts, UI assets, or interface trade dress.

Selected core implementation patterns may be ported from Hermes Agent under
its MIT License. Each copied or substantially adapted file is recorded in
`provenance/hermes-port.json` with:

- The exact upstream revision and source path.
- The destination path.
- Whether the code was copied, adapted, or independently reimplemented.
- A summary of meaningful modifications.
- The upstream tests that were also ported or replaced.

The pinned upstream revision for the initial audit is:

```text
b9b463f3bd6517b76687d9b3c9dea1e62f01f9e1
```

The full upstream MIT notice is preserved in `THIRD_PARTY_NOTICES.md`.

## Initial selective ports

| Hermes source | Polaris destination | Treatment |
|---|---|---|
| `providers/base.py` | `src/polaris/providers/base.py` | Reduced and adapted |
| `plugins/model-providers/azure-foundry/__init__.py` | `src/polaris/providers/azure_foundry.py` | Adapted into an async provider |
| `agent/azure_identity_adapter.py` | `src/polaris/providers/azure_identity.py` | Reduced and adapted |
| `tools/registry.py` | `src/polaris/tools/registry.py` | Reduced and adapted |

Each destination file carries a source revision header. Detailed modifications
and test mappings are recorded in `provenance/hermes-port.json`.
