# Colosseum submission notes

Official contest checked on 9 October 2026: [Crypto World's Fair Hackathon](https://colosseum.com/worldsfair). Rules PDF: https://colosseum.com/legal/Crypto%20World's%20Fair%20Hackathon%20Rules.pdf

The Solana Frontier hackathon (April–May 2026) is an earlier contest. These notes follow the World's Fair rules, not Frontier.

## Deadline and eligibility

- Contest window: 6:00am PT on 14 September 2026 through 11:59pm PT on 12 October 2026.
- Every member registers on colosseum.com before that deadline. The team leader submits one project.
- One team per person.
- Content submitted to Colosseum must be in English.
- Judges score functionality, impact, novelty, UX, open source, and business plan.
- Solana track in the same rules: $100,000 across 10 products that integrate with Solana. Prize text can change; re-read the PDF before submitting.
- Eligibility excludes people located or ordinarily resident in sanctioned places listed in section 3, including Russia. Confirm this yourself. This repository does not check nationality.
- Winners are announced by about 5 December 2026. Prizes are paid to the team leader, possibly to a wallet Colosseum specifies. Taxes are the winner's job.

Portal materials the rules and the current hackathon page ask for, which this repo cannot create for you:

- Colosseum account and team registration
- Product name, description, chain, team bios, location
- Logo
- GitHub link (or private access for hackathon@colosseum.com)
- A 2–3 minute pitch video
- A product demo video of at most 3 minutes
- Go-to-market note
- Disclosure of work that existed before the contest

Prior work to disclose: the local indexer, Chroma hybrid search, OpenAPI chunking, role packs, and the search UI existed before this Solana proof. The proof, wallet session, plan endpoint, Docker packaging, and these docs were added for the submission.

## Problem

AI agents on different parts of a product do not share one small source of truth. A frontend agent is handed an entire OpenAPI document, or it guesses fields. The same agent can also be shown a contract that the hub operator changed after the fact.

## Who it is for

Teams of roughly 5–50 people with separate frontend, mobile, and backend repos. The buyer is the team lead. The user of the data is each developer's coding agent.

## What the product does

The hub indexes the contract and serves a role pack over MCP: one route, its schema, its error codes. On the included Auth sample, that slice is about 10% of the OpenAPI file by a characters/4 estimate (about 89 tokens versus about 870). That estimate is not a model tokenizer.

## Why Solana, and why not the other mechanisms

The hub is centralized. It can swap bytes and still claim they are the version the backend published. A database row with a timestamp does not stop the operator from editing that row.

The publisher signs a Solana devnet transaction whose Memo instruction is `hub:v1:<service>:<sha256>`. The signature, slot, and success bit are Solana's. This server accepts the anchor only after `getTransaction` on the configured RPC shows:

- `meta.err` is empty
- the connected wallet is a signer
- the memo is exactly the hash of the current file

Clients must not treat a local flag as confirmation. `matches: false` means the file moved after the anchor.

What is on-chain: service name and SHA-256, inside a memo, plus the wallet signature and the slot. What stays off-chain: source, OpenAPI bodies, prompts, and session tokens.

No token, NFT, or custom program. The Memo program `MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr` already exists. A custom program would only add an account layout we do not need for a single hash.

Proof of History is how Solana orders its own blocks. This app does not reimplement it. Order is the slot of the confirmed memo transaction.

Proof of Work is not used. It would burn energy to slow publishers down, and the wallet signature plus a monthly cap already limits spam. A hash puzzle would not make the contract more truthful.

HS256 is not the hub's login. The sample `auth_service.py` contains a toy JWT so the index has something to find. That secret is demo data, not a production key, and it is not how publishers authenticate. Publishers prove wallet control with an ed25519 signature over a one-time message (domain, wallet, nonce, time). The nonce is stored server-side and cannot be reused. Connecting a wallet is not identity beyond "this key signed this message."

## Business model

Someone has to pay for seats, storage, and RPC. The model that matches the product is a free developer tier and a per-seat team plan.

Hypothesis, not a forecast: team revenue per month = paying seats × $29. Free tier is $0 and includes 5 devnet anchors per wallet per month. There is no Stripe integration and no measured user count. Expenses at MVP are a small VPS, disk for Chroma and SQLite, and devnet fees paid by the publisher (devnet SOL is not real money). Profit needs more seat fees than the VPS and support time. The risk is that editor vendors ship the same contract slice inside the IDE and teams never pay for a separate hub.

Conservative: a few design partners, revenue near zero, cost is one small server.  
Base: a handful of teams on the seat plan. Revenue is seats × price. Still a hypothesis.  
Optimistic: the anchor becomes the audit trail agents check before generating a client. Same formula, more seats. Still not a forecast.

`GET /api/plans` repeats this and sets `checkout` to `not_connected`.

## Demo script

1. Show Frontend search for `login JWT`. Hits are the login route and schemas, not `auth_service.py`.
2. Call `get_client_context` for `POST /api/v1/auth/login`. Before an anchor, the tool returns `REFUSED` and does not include the schema. After a matching devnet memo, the same tool returns `VERIFIED` plus the slice.
3. Change one byte of the spec, call the tool again, and show `REFUSED` while the old explorer link still resolves.

## Architecture

```
Agent --MCP--> Hub (FastAPI, Chroma, SQLite)
Human --browser--> same Hub
Publisher wallet --memo tx--> Solana devnet
Hub --getTransaction--> Solana RPC
```

## Known limits

- One demo service, `auth`, is wired in the proof panel.
- Verification trusts the configured RPC. A lying RPC can lie about confirmation. A second RPC is not implemented.
- The in-memory rate limit resets on restart.
- The site is not deployed from this workspace. Docker files are ready; no domain was purchased.
- Russian-language comments in the index are not found by the Latin-only BM25 tokenizer.
- Search still loads the Chroma collection for hybrid ranking. That is fine for the sample and weak for a large monorepo.
- No payment processor, no production user accounts beyond the wallet session, no backup cron.

## Pitch, about 80 words

Coding agents on a frontend repo invent API fields because the only shared document is a huge OpenAPI file, and a central hub can edit that document later without a trace. MCP Context Hub serves one route, its schema, and its errors to the agent that needs them. The publisher anchors a SHA-256 of that contract in a Solana memo. Agents check the hash against the slot. The source never goes on-chain. Teams pay for seats if the shared proof is worth more than pasting the spec into every chat.
