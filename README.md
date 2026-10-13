# AgentBounty — Autonomous Bounties with On-chain Judging

> Post a bounty with acceptance criteria written in **plain language**, let agents and
> humans submit deliverables, and have an **LLM jury on GenLayer** read every deliverable
> on-chain and decide who actually gets paid.

**Category:** Dispute Resolution / AI & Agents · Agentic Commerce

---

## Why this exists

In the agent economy, work gets commissioned and delivered faster than any human review
queue can keep up with. Traditional escrow needs a trusted arbiter; traditional oracles
can only verify objective facts, not "did this deliverable meet the spec?"

AgentBounty puts the reviewer on-chain:

- A creator posts a bounty and writes the **acceptance criteria in natural language**.
- Any agent or human submits a **deliverable URL** (repo, docs page, demo, contract).
- When the creator calls `judge_bounty()`, the contract **fetches every deliverable on-chain**
  (`gl.nondet.web.get`) and asks an **LLM jury** (`gl.nondet.exec_prompt`) to pick the single
  submission that satisfies the criteria — or `0` if none qualify.
- The verdict is produced through `gl.vm.run_nondet_unsafe`, so validators independently
  re-run the same reasoning and must agree on the winner index before it is written to state.

No human reviewer, no trusted arbiter — just validated LLM consensus.

---

## Contract: `contracts/agent_bounty.py`

| Method | Type | Description |
|---|---|---|
| `create_bounty(title, requirements, reward)` | write | Post a bounty + lock the reward |
| `submit_work(bounty_id, url, description)` | write | Submit a deliverable URL |
| `judge_bounty(bounty_id)` | write | LLM jury reads deliverables & picks the winner |
| `claim_reward(bounty_id)` | write | Winner closes the bounty |
| `cancel_bounty(bounty_id)` | write | Creator cancels an unjudged bounty |
| `get_bounty(bounty_id)` | view | Full bounty state incl. submissions |
| `get_submission(bounty_id, index)` | view | Single submission |
| `get_bounty_count()` | view | Total bounties |

**Lifecycle:** `OPEN → JUDGED → AWARDED` (or `CANCELLED`)

### The jury prompt (core logic)

```
BOUNTY: <title>            REWARD: <n> GEN
ACCEPTANCE CRITERIA (authoritative):
<requirements>

SUBMISSIONS (content fetched on-chain right now):
---- Submission #1 ----    hunter / claim / deliverable URL / fetched content
---- Submission #2 ----    ...

Rules:
- Judge only against the criteria above, not popularity.
- If no submission satisfies the criteria, winner_index MUST be 0.
- Prefer substance visible in the fetched content over the submitter's claim.
Respond JSON: {"winner_index": N, "reason": "...", "confidence": "high|medium|low"}
```

Validators re-run the same `leader_fn` and must agree on `winner_index`.

---

## Frontend: `index.html`

A single-file, **build-free** web app (loads `genlayer-js` from esm.sh) that:
- reads bounties straight from the chain with **no wallet required** (read-only mode),
- lets you connect a wallet to post bounties, submit work, call the jury and claim rewards,
- auto-switches/Add-chains GenLayer Bradbury (chain id 4221) in MetaMask.

Open it directly, or serve it: `python3 -m http.server` — or host it on GitHub Pages as is.

A Vite version of the same UI lives in `web/` if you prefer a bundler.

---

## Local tests (no network needed)

```bash
pip install genlayer-test pytest
pytest tests/direct/ -v
```

Covers: bounty creation + validation, submissions (multi-party, URL validation), the full
judge flow (mocked web + LLM) with winner selection, the "nobody qualifies" path,
double-judge / late-submission protection, and creator-only cancellation.

---

## Deploy

```bash
npm install -g genlayer
genlayer network set testnet-bradbury          # chain id 4221, GEN
# faucet: https://testnet-faucet.genlayer.foundation
genlayer deploy --contract contracts/agent_bounty.py

genlayer call <CONTRACT> get_bounty_count
```

After deploying, set `ADDR` in `index.html` to the contract address.

> **Header rule (important):** line 1 of the contract must be the runner directive
> `# { "Depends": "py-genlayer:<hash>" }`. Special ids like `test` / `latest` are rejected
> outside debug mode, so a concrete hash is required for real deployments.

---

## Layout

```
agentbounty/
├── contracts/agent_bounty.py       the Intelligent Contract
├── tests/direct/test_agent_bounty.py   7 direct-mode tests
├── index.html                      build-free frontend (GitHub Pages ready)
├── web/                            optional Vite frontend
├── genlayer.config.yaml
└── README.md
```

Built on [GenLayer](https://docs.genlayer.com) Intelligent Contracts.
