# AgentBounty — Escrowed Bounties with On-chain Judging

> Post a bounty and the reward GEN is **locked in the contract**. Anyone submits a
> deliverable URL, an LLM jury reads each deliverable **on-chain**, and **only the winner**
> can claim the payout. If nobody qualifies, the creator reclaims the reward.

**Category:** Marketplaces / Dispute Resolution · Agentic Commerce

---

## What changed after review (v2)

The first version was rejected with a fair criticism: the escrow and payout were not
implemented — the contract accepted no funds, any caller could mark a bounty awarded, and
no transfer ever reached the winner. v2 fixes all three:

| Reviewer requirement | How v2 implements it |
|---|---|
| **Repository-backed custody** (the contract must accept and hold the reward) | `create_bounty` is `@gl.public.write.payable`; the reward is `gl.message.value` — the GEN actually sent. `get_contract_balance()` and `get_bounty().contract_balance` expose the live custody (verified on-chain: balance went 0 → 0.5 GEN). |
| **Winner authorization** (any caller could mark a bounty awarded) | `claim_reward()` rejects everyone except the address the jury selected (`Only the winning submitter can claim this reward`), and rejects a second claim (`Reward already paid out`). |
| **Actual payout path** (no transfer reached the winner) | `claim_reward()` calls `emit_transfer` for the escrowed amount **to the winner's address**; the runtime debits it from the contract balance and the network executes it on finalization. Refund paths (`reclaim_no_winner`, `cancel_bounty`) use the same payout primitive. |

---

## Lifecycle

```
OPEN (reward locked) → submit work → JUDGED (jury picks a winner)
                                        ├─ winner claims  → AWARDED  (GEN paid out)
                                        └─ nobody qualified → creator reclaims → CANCELLED
OPEN with no submissions → creator cancels → CANCELLED (reward refunded)
```

| State | Meaning |
|---|---|
| `0 OPEN` | reward escrowed, accepting submissions |
| `1 JUDGED` | jury decided (`winner_index` = 1-based submission, or 0 for none) |
| `2 AWARDED` | winner claimed; reward transferred out |
| `3 CANCELLED` | reward refunded to the creator |

---

## Contract: `contracts/agent_bounty.py`

| Method | Type | Description |
|---|---|---|
| `create_bounty(title, requirements)` | **payable** write | Posts a bounty and locks the reward in the contract |
| `submit_work(bounty_id, url, description)` | write | Submits a deliverable URL (open bounties only) |
| `judge_bounty(bounty_id)` | write | Fetches every deliverable on-chain, LLM jury picks the winner |
| `claim_reward(bounty_id)` | write | **Winner-only**: transfers the escrowed reward to the winner |
| `reclaim_no_winner(bounty_id)` | write | Creator refund when the jury found nobody qualified |
| `cancel_bounty(bounty_id)` | write | Creator refund for an open bounty with no submissions |
| `get_bounty(bounty_id)` | view | Full state + submissions + live contract balance |
| `get_bounty_count()` / `get_contract_balance()` | view | Totals |

### The payout primitive

```python
@gl.evm.contract_interface
class _Payee:
    class View: pass
    class Write: pass

def _pay(self, to: Address, amount: u256):
    if amount <= u256(0):
        return
    _Payee(to).emit_transfer(value=amount)   # external message → real GEN out
```

`claim_reward` enforces authorization before paying:

```python
if self._addr_bytes(gl.message.sender_address) != self._addr_bytes(b.winner):
    raise gl.vm.UserError("Only the winning submitter can claim this reward")
...
b.reward = u256(0) ; b.paid = True ; b.state = AWARDED
self._pay(b.winner, amount)
```

### The judging path

`judge_bounty()` snapshots the bounty, fetches **every** deliverable with
`gl.nondet.web.get`, and asks the jury (`gl.nondet.exec_prompt`) for
`{winner_index, reason, confidence}`. Validators re-run the same reasoning through
`gl.vm.run_nondet_unsafe` and must agree on the index. Awards only happen through the
payout path above — the verdict itself never moves money.

---

## Frontend: `index.html`

Single-file, build-free (loads `genlayer-js` from esm.sh), GitHub Pages ready:
- live **"rewards held by contract"** readout + board of bounties (read-only, no wallet)
- wallet actions: **lock reward & post**, submit work, ask the jury,
  **claim (winner only)**, reclaim (creator)
- the claim button stays disabled unless the connected wallet *is* the winner

---

## Local tests (10 passing)

```bash
pip install genlayer-test pytest
pytest tests/direct/ -v
```

Covers exactly what the review asked for: funding required to create a bounty, the reward
recorded as custody, winner-only claiming (creator and other submitters are rejected),
double-claim protection, judging guards, no-winner refund to the creator, and creator-only
cancellation blocked once submissions exist.

---

## Deploy

```bash
npm install -g genlayer
genlayer network set testnet-bradbury
genlayer deploy --contract contracts/agent_bounty.py
genlayer call <CONTRACT> get_contract_balance
```

> **Header rule:** line 1 must be the runner directive
> `# { "Depends": "py-genlayer:<hash>" }` — `test`/`latest` are rejected outside debug mode.

---

## Verified end-to-end on Bradbury

Two wallets: the creator locks 0.5 GEN, one submitter delivers a real repo, another submits a
placeholder page.

| Step | Result |
|---|---|
| `create_bounty` payable (0.5 GEN) | contract balance **0 → 0.5 GEN** (real custody) |
| `submit_work` ×2 | one real GitHub repo, one placeholder |
| `judge_bounty` | ACCEPTED · jury picked the real repo |
| `claim_reward` (winner) | ACCEPTED · payout message emitted to the winner |
| final state | `AWARDED`, `paid = true`, `reward = 0`, contract balance back to 0 GEN |

```
agentbounty/
├── contracts/agent_bounty.py         the Intelligent Contract (v2, escrowed)
├── tests/direct/test_agent_bounty.py 10 direct-mode tests
├── index.html                        build-free frontend (GitHub Pages ready)
├── genlayer.config.yaml
└── README.md
```

Built on [GenLayer](https://docs.genlayer.com) Intelligent Contracts.
