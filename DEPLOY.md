# Deployment

Nothing in this file has been executed against a paid host. There is no live domain until you create one.

## What you run

One process serves the UI, the HTTP API, and MCP. ChromaDB is a file under `chroma_db/`. Sessions and anchors are SQLite at `HUB_DB` (default `hub_data/hub.db`). Do not replace either store casually: the index and the proof history live there.

## Docker

```bash
copy .env.example .env
docker compose up --build -d
```

Health: `GET /api/health`.

The image sets `HUB_PUBLIC=1`, so disk reads through MCP are off. The demo contract is the file copied into the image at build time. Rebuild after a contract change, then anchor again. The old chain proof remains and will show as a mismatch until a new memo is confirmed. That is intended.

## A VM without Docker

```bash
python -m venv venv
venv/bin/pip install -r requirements.txt
venv/bin/python indexer.py
HUB_PUBLIC=1 venv/bin/python -m uvicorn server:app --host 127.0.0.1 --port 8000
```

Put Caddy or nginx in front for HTTPS. Proxy `/` to `127.0.0.1:8000`. WebSockets are not required. SSE for MCP is a long-lived GET `/mcp/sse`; disable proxy buffering on that path.

Example Caddy site:

```
your.domain {
    reverse_proxy 127.0.0.1:8000
}
```

## DNS

Create an A or CNAME record to the machine. Issue a certificate with Caddy or certbot. Set `HUB_CORS_ORIGINS` to `https://your.domain` if a browser on another origin calls the API. The UI is same-origin and does not need a wildcard.

## Secrets

- Do not commit `.env`.
- The server has no Solana private key. Publishers sign in Phantom.
- `HUB_ALLOW_MAINNET` stays `0`. A mainnet RPC URL is ignored unless that flag is set on purpose.
- Session tokens are random and only their SHA-256 is stored.

## Logs

Docker writes container logs (`docker compose logs -f hub`). On a VM, run uvicorn under systemd and use `journalctl -u hub`. The process does not ship logs to a third party.

## Backup and restore

Stop the process. Copy `hub_data/` and `chroma_db/`. Restore those two directories and start again. There is no separate migration tool: `hub_db.init_db` creates missing tables on startup and does not drop existing rows.

## Update and rollback

```bash
git pull
docker compose up --build -d
```

Rollback is the previous git revision plus the same volumes. Do not delete the volumes to "fix" a bad release; that deletes anchors and the index.

## Health and limits

`/api/health` returns `status: ok`. `/api/*` except health is limited to 60 requests per minute per IP, in memory. A restart clears the counter. This is a basic limit, not a full edge firewall.

## What you still do by hand

- Buy or point a domain.
- Choose a host (a small VPS is enough; Chroma in this demo is local files, not a managed database).
- Open port 443.
- Put devnet SOL in the publisher wallet. The server cannot airdrop for you.
- Do not point `HUB_SOLANA_RPC` at mainnet for a demo.
