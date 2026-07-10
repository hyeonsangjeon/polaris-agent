# Microsoft Foundry Model Router provider

Polaris treats Foundry Model Router as a **thin provider strategy**. It sends
Responses API requests to one `model-router` deployment. Microsoft Foundry owns
underlying model selection and failover; Polaris does not reimplement the router.

Official references:

- [Auto and direct model routing with the Responses API](https://learn.microsoft.com/azure/foundry/openai/how-to/responses-model-routing)
- [Model Router concepts](https://learn.microsoft.com/azure/foundry/openai/concepts/model-router)

## Contract

- `kind` must be `foundry_router`.
- `api_mode` must be `responses`; chat completions are rejected.
- `model` is the deployment name, normally `model-router`.
- `base_url` is the resource's OpenAI-compatible `/openai/v1` endpoint.
- Authentication is API key through a named environment variable or Entra
  through `DefaultAzureCredential`.
- The actual model is returned by Foundry as `response.model`. Polaris records it
  in completed provider calls and ensemble outputs for audit and cost analysis.

The model-router deployment can select a different underlying model for each
worker, verification, or synthesis request.

## API-key configuration

[`examples/config.foundry-router-key.json`](../../examples/config.foundry-router-key.json)
contains the full application configuration. Its provider block is:

```json
{
  "kind": "foundry_router",
  "model": "model-router",
  "base_url": "https://YOUR-RESOURCE.services.ai.azure.com/openai/v1",
  "api_key_env": "AZURE_FOUNDRY_API_KEY",
  "api_mode": "responses",
  "azure_auth": "api_key"
}
```

```bash
export POLARIS_HOME='/absolute/path/used/as/data_dir'
install -d -m 700 "$POLARIS_HOME"
POLARIS_TOKEN_FILE="$POLARIS_HOME/api-token" uv run python - <<'PY'
import os
import secrets

path = os.environ["POLARIS_TOKEN_FILE"]
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as token_file:
    token_file.write(secrets.token_urlsafe(48) + "\n")
os.chmod(path, 0o600)
PY
export AZURE_FOUNDRY_API_KEY='retrieve-from-your-secret-manager'
uv run polarisd --config examples/config.foundry-router-key.json
```

Keep that terminal open. From another terminal:

```bash
uv run polaris --config examples/config.foundry-router-key.json doctor
```

These commands assume all `/ABSOLUTE/PATH/...` and resource placeholders were
replaced first; see the [examples guide](../../examples/README.md).

The JSON contains only the environment-variable name. Do not put the key in
`headers`, the URL, `.env.example`, logs, or issue reports.

Run API-key configurations in the foreground. `polaris daemon install` rejects
providers that use `api_key_env` because launchd does not inherit shell API-key
variables, and Polaris does not put secrets in a plist. Credential-free Ollama and
Foundry Entra/Managed Identity configurations remain eligible for launchd.

## Entra configuration

Install the project's existing optional Azure identity extra:

```bash
uv sync --extra azure
```

Provider block:

```json
{
  "kind": "foundry_router",
  "model": "model-router",
  "base_url": "https://YOUR-RESOURCE.services.ai.azure.com/openai/v1",
  "api_mode": "responses",
  "azure_auth": "entra",
  "entra_scope": "https://ai.azure.com/.default"
}
```

Polaris uses `DefaultAzureCredential` with interactive browser credentials
excluded by default. Establish a supported credential before starting the daemon,
for example with Azure CLI login or workload identity appropriate to the host.
The credential must be authorized to invoke the deployment.

Some classic Azure OpenAI resource endpoints and authorization setups expect
`https://cognitiveservices.azure.com/.default` instead. New Foundry and classic
resource scopes are not interchangeable in every tenant. Use the endpoint and
scope shown for your resource in the Foundry portal and official documentation;
do not guess by changing only the hostname.

## Deployment-owned routing settings

Configure these in Microsoft Foundry when deploying the router:

- **Balanced** (default): considers a narrow quality range and selects a
  cost-effective eligible model.
- **Cost:** considers a wider quality range to favor cost.
- **Quality:** selects the highest-rated eligible model for the prompt.
- **Model subset:** limits eligible models.

The smallest context window in the selected subset determines the effective
context limit. A larger model in the subset does not make an oversized prompt
safe if a smaller eligible model cannot accept it.

These controls do not belong in Polaris provider JSON. Polaris persists the
requested deployment, budget, and actual model observations; it does not infer
or override the deployment's routing mode.

## Run

```bash
uv run polaris --config examples/config.foundry-router-key.json run \
  "Verify the supplied claims and preserve unresolved disagreement." \
  --mode foundry-router \
  --provider foundry-router \
  --call-limit 8 \
  --token-limit 24000 \
  --wait
```

This strategy creates one research worker plus verifier and synthesizer stages,
all against the same router deployment. It is not a K-model fan-out chosen by
Polaris.

## Troubleshooting

- **401/403:** verify key/credential, resource endpoint, scope, role assignment,
  and deployment access.
- **404:** verify the `/openai/v1` base path and deployment name.
- **Responses error:** router mode does not support chat-completions fallback;
  keep `api_mode` set to `responses`.
- **Context error or long stall:** reduce prompt/tool context or remove
  small-context models from the deployment subset.
- **Unexpected model/cost:** inspect the recorded `response.model`, then review
  Foundry routing mode and subset. The configured `model-router` name is the
  requested deployment, not the underlying responder.
