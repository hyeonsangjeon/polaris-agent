# Docker and NAS deployment

Polaris runs as an unprivileged user with a read-only root filesystem. The API is published on
`127.0.0.1:8765` by default. Set `POLARIS_BIND_ADDRESS` only when another trusted host must reach
it; remote binds still require the bearer token.

## Required local paths

Create the state, workspace, and export directories before starting:

```sh
mkdir -p .polaris-data workspace exports
cp deploy/docker/config.example.json .polaris-data/config.json
umask 077
python -c 'import secrets; print(secrets.token_urlsafe(48))' > .polaris-data/api-token
docker compose up -d
```

On Linux, set `POLARIS_UID=$(id -u)` and `POLARIS_GID=$(id -g)` before the first build so the
unprivileged process can write the bind-mounted directories.

The container reads configuration and the API token from `/data`; secret values are not placed in
Compose environment variables. The available settings are:

- `POLARIS_DATA_PATH` (default `./.polaris-data`): local persistent state mounted at `/data`.
- `POLARIS_WORKSPACE_PATH` (default `./workspace`): tool workspace mounted at `/workspace`.
- `POLARIS_EXPORT_PATH` (default `./exports`): backup/export destination mounted at `/exports`.
- `POLARIS_BIND_ADDRESS` and `POLARIS_PORT`: host API listener (defaults `127.0.0.1:8765`).
- `POLARIS_UID` and `POLARIS_GID`: container ownership IDs (defaults `10001`).

**Keep `/data`, especially `journal.sqlite3`, on a local/container filesystem. Never put the
SQLite database on SMB, NFS, or another NAS network filesystem.** Encrypted `.polaris-backup`
files and other exported artifacts may be written through `/exports` to a mounted NAS share.

Backups exclude the API token and environment-provided model credentials. Re-establish those
credentials after an import.

## External Ollama (optional)

No model or Ollama server is bundled. The example configuration reaches an Ollama server on the
Docker host. On Linux, add the compatibility override:

```sh
docker compose -f compose.yaml \
  -f deploy/docker/compose.external-ollama.yaml up -d
```

On Docker Desktop for macOS, `host.docker.internal` is available without the override. To use an
Ollama server elsewhere on the LAN, change only the secret-free `base_url` in
`.polaris-data/config.json`.
