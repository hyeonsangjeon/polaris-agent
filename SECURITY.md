# Security policy

## Supported versions

Polaris is alpha software. Security fixes are applied to the latest `0.1.x`
release and the default branch only.

| Version | Supported |
|---|---|
| Latest `0.1.x` | Yes |
| Older versions | No |

## Report a vulnerability

Please use a
[private GitHub security advisory](https://github.com/hyeonsangjeon/polaris-agent/security/advisories/new)
or email **wingnut0310@gmail.com**. Do not open a public issue for a suspected
vulnerability.

Include the affected version, impact, reproduction steps, and any proposed
mitigation. Remove API keys, bearer tokens, prompts, file contents, and personal
data. Response and remediation timing depends on severity and maintainer
availability during the alpha.

## Security model

- **Local daemon:** `polarisd` binds to `127.0.0.1:8765` by default and requires a
  bearer token. Any local process running as the same user may still be able to
  read user-owned state or invoke local capabilities; the token is not a boundary
  against a compromised account.
- **Remote binding:** non-loopback listening requires `--allow-remote` and a
  configured token. Polaris does not terminate TLS. Put it behind a trusted
  authenticated TLS proxy/firewall and never expose port 8765 directly to the
  public internet.
- **Shell and tools:** filesystem roots limit path access, not the authority of
  commands approved for the shell. Shell execution is an opaque side effect and
  requires approval by default. Review every command and working directory.
- **Desktop:** the Tauri app is an independent authenticated client of the daemon,
  not a sandbox or privilege boundary.
- **Tokens and provider secrets:** setup writes the daemon token with private file
  permissions. Provider JSON stores environment-variable names only. Backups
  exclude credentials and the API token.
- **Recovery:** an ambiguous opaque effect stops for a decision. Approval to retry
  can duplicate an effect; Polaris does not promise arbitrary exactly-once
  execution.

## Redaction checklist

Before sharing `doctor`, logs, timelines, configuration, or artifacts:

1. Replace bearer tokens and every provider/API credential.
2. Remove environment values; leave only variable names.
3. Review prompts, model outputs, tool arguments, file paths, URLs, and evidence.
4. Remove hostnames, IP addresses, usernames, and workspace content if sensitive.
5. Prefer the minimum event range needed to reproduce the problem.

See [docs/security.md](docs/security.md) for the full threat model.
