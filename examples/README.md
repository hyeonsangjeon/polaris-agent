# Configuration examples

These files are secret-free starting points:

- [`config.ollama.json`](config.ollama.json): loopback Ollama and strict offline
  policy.
- [`config.foundry-router-key.json`](config.foundry-router-key.json): Foundry
  Model Router with `AZURE_FOUNDRY_API_KEY`.
- [`config.foundry-router-entra.json`](config.foundry-router-entra.json): Foundry
  Model Router with `DefaultAzureCredential`.
- [`fanout/`](fanout/): an actual CLI command for three local worker roles.

## Absolute path placeholders

Polaris requires `data_dir`, `daemon.token_file`, and every `tools.roots` entry to
be absolute. It also requires tool roots to exist when configuration is loaded.
The examples use visibly invalid-for-your-machine placeholders such as
`/ABSOLUTE/PATH/TO/POLARIS_HOME`; replace them before use:

```bash
mkdir -p "$HOME/.local/share/polaris" "$PWD/workspace"
mkdir -p "$HOME/.config/polaris"
cp examples/config.ollama.json "$HOME/.config/polaris/config.json"
# Edit all /ABSOLUTE/PATH placeholders to the paths above.
```

Using the same absolute directory for `POLARIS_HOME`, `data_dir`, and the parent
of `daemon.token_file` keeps path resolution predictable. `polaris setup` is the
safer way to generate a first Ollama config and private token:

```bash
POLARIS_HOME="$HOME/.local/share/polaris" \
  uv run polaris setup --root "$PWD/workspace"
```

Never replace `api_key_env` with a secret value. Export the named variable in the
daemon environment instead.
