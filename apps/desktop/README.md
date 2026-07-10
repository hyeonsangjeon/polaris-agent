# Polaris Agent desktop

Tauri 2 + React 19 operator console for the independent Polaris daemon.

## Development

```sh
npm install
npm run dev
npm test
npm run typecheck
npm run build
```

Use `VITE_POLARIS_DEMO=1 npm run dev` for labeled, in-memory demo data. Browser preview never falls back to direct daemon HTTP; production requests go through the Rust `daemon_request` command.

## Connection security

The console accepts a daemon URL (default `http://127.0.0.1:8765`) and bearer token **file path**. Rust reads the credential for each request. The token value is never returned to React or stored in browser storage.

The proxy accepts only:

- loopback/private HTTP(S) targets;
- `GET` or `POST`;
- `/health` or `/v1/*`.
