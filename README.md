# MCP Context Hub

Shared contract context for AI agents. Frontend, Flutter, backend, and DevOps agents read one hub over MCP instead of stuffing a whole OpenAPI file into the prompt. A publisher can anchor the SHA-256 of a contract on Solana devnet. The hub stores the spec. The chain stores the hash, the signer, and the slot.

This repository already contained the local indexer, hybrid search, role packs, and the search UI before the Solana proof was added. That earlier work is disclosed in [COLOSSEUM.md](COLOSSEUM.md).

## What works

- Index a project into Chroma, incrementally, with `.gitignore`.
- Hybrid search (vector + BM25), filtered by role. Frontend and Flutter receive API chunks, not backend source.
- MCP tools for one endpoint, its schema, its error codes, and a token-size comparison.
- Wallet sign-in (Phantom, ed25519 over a one-time nonce). No seed phrase. No JWT.
- Anchor the current contract hash with the Solana Memo program on devnet, then verify that transaction through RPC before calling it confirmed.

## Local run

```bash
python -m venv venv
venv\Scripts\pip install -r requirements.txt
venv\Scripts\python indexer.py
venv\Scripts\python -m uvicorn server:app --host 127.0.0.1 --port 8000
```

On macOS or Linux, use `venv/bin/` instead of `venv\Scripts\`.

Open http://127.0.0.1:8000. Search `login JWT` with the Frontend role. The hits should come from `test_sample/openapi.json`.

MCP endpoint: `http://127.0.0.1:8000/mcp/sse`.

Optional file watcher:

```bash
venv\Scripts\python watcher.py
```

## Tests

```bash
venv\Scripts\python -m pytest tests -q
```

The tests cover the hash, the wallet signature, rejected transactions, nonce replay, and anchor replay. They do not broadcast a devnet transaction.

## Try the Solana proof

1. Install [Phantom](https://phantom.com/) and switch it to Devnet.
2. Airdrop devnet SOL from a faucet. The app never asks for a secret key.
3. Open the site, choose Connect, and sign the login message.
4. Choose Anchor on devnet and approve the memo transaction.
5. The status changes to a slot number only after this server has fetched the transaction from RPC and checked the signer, the memo, and `meta.err`.

The memo looks like `hub:v1:auth:<sha256>`. The OpenAPI file stays off-chain.

If the file changes later, the proof endpoint reports that the anchored hash does not match. That mismatch is the point.

## Plans

`GET /api/plans` describes a free tier and a per-seat team tier. The dollar amounts are hypotheses (`revenue = paying seats × 29`). Checkout is not connected. A wallet can publish five anchors per UTC month until `HUB_PLAN=team` or `HUB_ANCHOR_MONTHLY_LIMIT` is set by the operator.

## Public mode

`HUB_PUBLIC=1` disables the MCP tool that reads files from disk. Search still uses the index. See `.env.example`.

Copy `.env.example` to `.env` before `docker compose up`. Compose loads that file.

## Deployment

See [DEPLOY.md](DEPLOY.md). This repository does not claim that a public URL is already live.

## Colosseum

See [COLOSSEUM.md](COLOSSEUM.md). Submission materials must be in English. Registration and the video are manual.
