# Runtime secrets

Polaris keeps provider and channel values in an owner-only
`runtime-secrets.env`; JSON configuration contains environment-variable names
only. The default path is `<data_dir>/runtime-secrets.env`. Override it with
`daemon.secrets_file` or `POLARIS_SECRETS_FILE`.

## File contract

The file must be a regular, non-symlink file owned by the current user with mode
`0600`, valid UTF-8/LF, and no more than 64 KiB. Its parser is deliberately not
a shell:

- each data line is exactly `NAME=value`;
- blank lines and comments are allowed;
- `export`, duplicate/invalid names, NUL/CR/newline values, backticks, and `$(` are
  rejected;
- there is no quoting, interpolation, variable expansion, or command execution.

Writes are atomic and preserve mode `0600`. Values are never printed by the CLI.

## CLI

```bash
# Hidden interactive prompt
uv run polaris secrets set TELEGRAM_BOT_TOKEN

# Or copy a value from the current process without putting it on argv
uv run polaris secrets set AZURE_FOUNDRY_API_KEY \
  --from-env AZURE_FOUNDRY_API_KEY

uv run polaris secrets list
uv run polaris secrets check
uv run polaris secrets check TELEGRAM_BOT_TOKEN SLACK_BOT_TOKEN
uv run polaris secrets remove TELEGRAM_BOT_TOKEN
```

`check` without arguments derives required names from enabled providers and
channels.

## Precedence and launchd

Polaris reads the runtime file, then overlays the daemon process environment;
the process environment wins for duplicate names. `POLARIS_SECRETS_FILE`
selects the file itself.

`polaris daemon install` writes only the secrets-file **path** into the
LaunchAgent configuration. It does not copy values into the plist. The daemon
loads the owner-only file at runtime.

## Backup and rotation

Polaris backups include configuration, the SQLite snapshot, and artifacts. They
exclude the API bearer-token file, `runtime-secrets.env`, and
environment-provided credentials. Re-establish secrets after restore.

To rotate:

1. create/revoke the credential at its provider;
2. run `polaris secrets set NAME` with the replacement;
3. restart the daemon so adapters/providers reconnect with the new value;
4. run `polaris secrets check`; and
5. remove/revoke the old credential and inspect redacted status.

Never store secret values in memory, config JSON, Compose files, screenshots,
logs, issue reports, or scheduled payloads.
