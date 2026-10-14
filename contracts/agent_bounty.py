# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }

import json
from dataclasses import dataclass
from genlayer import *

# ---------------------------------------------------------------- storage ---


@allow_storage
@dataclass
class Bounty:
    creator: Address
    title: str
    requirements: str      # natural-language acceptance criteria (the jury judges against this)
    reward: u256           # GEN actually held by this contract (from gl.message.value)
    state: u8              # 0=OPEN 1=JUDGED 2=AWARDED(paid) 3=CANCELLED(refunded)
    submission_count: u256
    winner_index: u256     # 0 = nobody qualified
    winner: Address
    verdict_reason: str
    verdict_confidence: str
    paid: bool


@allow_storage
@dataclass
class Submission:
    hunter: Address
    deliverable_url: str
    description: str


# external message interface for paying an EOA / EVM address on the chain layer
@gl.evm.contract_interface
class _Payee:
    class View:
        pass

    class Write:
        pass


# ------------------------------------------------------------------ contract --

class AgentBounty(gl.Contract):
    """Bounty board with real reward custody and real payouts.

      1. create_bounty() is PAYABLE - the creator locks the reward in the contract.
      2. submit_work() - agents/humans attach deliverable URLs.
      3. judge_bounty() - the LLM jury fetches every deliverable on-chain and picks
         the submission that satisfies the criteria (or 0 if none qualify).
      4. claim_reward() - ONLY the winning submitter may claim, and the contract
         transfers the escrowed GEN to them.
      5. If nobody qualified (or an open bounty has no takers), the creator can
         reclaim / cancel and gets the reward back.

    Reward money is custody, not metadata: it is recorded from gl.message.value and
    leaves the contract through emit_transfer.
    """

    bounties: TreeMap[u256, Bounty]
    submissions: TreeMap[str, Submission]     # key: "<bounty_id>:<index>"
    next_id: u256

    def __init__(self):
        self.next_id = u256(1)

    # -------------------------------------------------------- internals ----

    def _to_addr(self, value) -> Address:
        if isinstance(value, Address):
            return value
        return Address(value)

    def _addr_bytes(self, value) -> bytes:
        b = getattr(value, "as_bytes", None)
        if b is None:
            b = getattr(value, "_as_bytes", None)
        if callable(b):
            b = b()
        if isinstance(b, (bytes, bytearray)):
            return bytes(b)
        return bytes(self._to_addr(value))

    def _subs(self, bounty_id: u256, count: u256) -> list:
        out = []
        i = u256(0)
        while i < count:
            key = str(int(bounty_id)) + ":" + str(int(i))
            if key in self.submissions:
                s = self.submissions[key]
                out.append({
                    "index": int(i) + 1,
                    "hunter": "0x" + self._addr_bytes(s.hunter).hex(),
                    "url": s.deliverable_url,
                    "description": s.description,
                })
            i += u256(1)
        return out

    def _pay(self, to: Address, amount: u256):
        """Send GEN out of this contract (external message, executed on finalization)."""
        if amount <= u256(0):
            return
        _Payee(to).emit_transfer(value=amount)

    # ------------------------------------------------------------- views ----

    @gl.public.view
    def get_bounty(self, bounty_id: u256) -> dict:
        b = self.bounties[bounty_id]
        return {
            "id": bounty_id,
            "creator": "0x" + self._addr_bytes(b.creator).hex(),
            "title": b.title,
            "requirements": b.requirements,
            "reward": b.reward,
            "contract_balance": self.balance,
            "state": b.state,
            "submission_count": b.submission_count,
            "winner_index": b.winner_index,
            "winner": "0x" + self._addr_bytes(b.winner).hex() if int(b.winner_index) > 0 else "",
            "verdict_reason": b.verdict_reason,
            "verdict_confidence": b.verdict_confidence,
            "paid": b.paid,
            "submissions": self._subs(bounty_id, b.submission_count),
        }

    @gl.public.view
    def get_bounty_count(self) -> u256:
        return self.next_id - u256(1)

    @gl.public.view
    def get_contract_balance(self) -> u256:
        return self.balance

    # ------------------------------------------------------------ writes ----

    @gl.public.write.payable
    def create_bounty(self, title: str, requirements: str) -> u256:
        """Post a bounty and LOCK the reward in this contract (payable)."""
        value = gl.message.value
        if value <= u256(0):
            raise gl.vm.UserError("Reward must be funded: send GEN with the call")
        if len(title.strip()) == 0:
            raise gl.vm.UserError("Title is required")
        if len(requirements.strip()) == 0:
            raise gl.vm.UserError("Acceptance criteria are required")

        b = Bounty(
            creator=self._to_addr(gl.message.sender_address),
            title=title,
            requirements=requirements,
            reward=value,
            state=u8(0),
            submission_count=u256(0),
            winner_index=u256(0),
            winner=self._to_addr(gl.message.sender_address),
            verdict_reason="",
            verdict_confidence="",
            paid=False,
        )
        bid = self.next_id
        self.bounties[bid] = b
        self.next_id = bid + u256(1)
        return bid

    @gl.public.write
    def submit_work(self, bounty_id: u256, deliverable_url: str, description: str):
        """Submit a deliverable URL for an open bounty."""
        if bounty_id not in self.bounties:
            raise gl.vm.UserError("Bounty not found")
        if len(deliverable_url.strip()) == 0 or not (
                deliverable_url.startswith("http://") or deliverable_url.startswith("https://")):
            raise gl.vm.UserError("A valid deliverable URL is required")
        if len(description.strip()) == 0:
            raise gl.vm.UserError("Describe what you delivered")

        b = self.bounties[bounty_id]
        if b.state != u8(0):
            raise gl.vm.UserError("Bounty is closed for submissions")

        idx = b.submission_count
        key = str(int(bounty_id)) + ":" + str(int(idx))
        self.submissions[key] = Submission(
            hunter=self._to_addr(gl.message.sender_address),
            deliverable_url=deliverable_url,
            description=description,
        )
        b.submission_count = idx + u256(1)

    @gl.public.write
    def judge_bounty(self, bounty_id: u256):
        """LLM jury reads every deliverable on-chain and picks the qualifying submission."""
        if bounty_id not in self.bounties:
            raise gl.vm.UserError("Bounty not found")

        b = self.bounties[bounty_id]
        if b.state != u8(0):
            raise gl.vm.UserError("Bounty is already judged")
        if int(b.submission_count) == 0:
            raise gl.vm.UserError("No submissions to judge")

        # Snapshot into plain Python values outside the nondet block.
        title_mem = str(b.title)
        req_mem = str(b.requirements)
        reward_mem = int(b.reward)
        subs = self._subs(bounty_id, b.submission_count)

        def _fetch_text(url: str, limit: int = 1000) -> str:
            try:
                resp = gl.nondet.web.get(url)
                if isinstance(resp, bytes):
                    raw = resp.decode("utf-8", errors="ignore")
                elif hasattr(resp, "body"):
                    raw = resp.body.decode("utf-8", errors="ignore")
                else:
                    raw = str(resp)
                return " ".join(raw.split())[:limit]
            except Exception:
                return "[could not fetch this URL]"

        def leader_fn():
            blocks = []
            for s in subs:
                blocks.append(
                    "---- Submission #" + str(s["index"]) + " ----\n"
                    + "hunter: " + s["hunter"] + "\n"
                    + "claim: " + s["description"] + "\n"
                    + "deliverable URL: " + s["url"] + "\n"
                    + "fetched content: " + _fetch_text(s["url"])
                )
            prompt = (
                "You are an impartial bounty judge on an autonomous agent marketplace.\n\n"
                "BOUNTY: " + title_mem + "\n"
                "REWARD: " + str(reward_mem) + " wei of GEN (held in escrow by the contract)\n"
                "ACCEPTANCE CRITERIA (authoritative):\n" + req_mem + "\n\n"
                "SUBMISSIONS (content fetched on-chain right now):\n"
                + "\n\n".join(blocks) + "\n\n"
                "Decide which single submission best satisfies the acceptance criteria.\n"
                "Rules:\n"
                "- Judge only against the criteria above, not popularity.\n"
                "- If no submission satisfies the criteria, winner_index MUST be 0.\n"
                "- Prefer substance visible in the fetched content over the submitter's claim;\n"
                "  a submission whose content does not support its claim must not win.\n\n"
                'Respond as JSON with exactly these keys:\n'
                '- "winner_index": integer (1-based submission number, or 0 if none qualify)\n'
                '- "reason": concise 2-4 sentence justification\n'
                '- "confidence": one of "high", "medium", "low"'
            )
            return gl.nondet.exec_prompt(prompt, response_format='json')

        def validator_fn(leaders_res) -> bool:
            if not isinstance(leaders_res, gl.vm.Return):
                return False
            mine = leader_fn()
            if not isinstance(mine, dict) or not isinstance(leaders_res.calldata, dict):
                return False
            try:
                return int(mine.get("winner_index")) == int(leaders_res.calldata.get("winner_index"))
            except Exception:
                return False

        result = gl.vm.run_nondet_unsafe(leader_fn, validator_fn)

        try:
            winner_index = int(result.get("winner_index", 0))
        except Exception:
            raise gl.vm.UserError("Jury returned an invalid winner index")
        if winner_index < 0 or winner_index > int(b.submission_count):
            raise gl.vm.UserError("Jury returned an out-of-range winner index")

        b.winner_index = u256(winner_index)
        b.verdict_reason = str(result.get("reason", ""))
        b.verdict_confidence = str(result.get("confidence", ""))
        b.state = u8(1)
        if winner_index > 0:
            key = str(int(bounty_id)) + ":" + str(winner_index - 1)
            if key in self.submissions:
                b.winner = self.submissions[key].hunter

    @gl.public.write
    def claim_reward(self, bounty_id: u256):
        """Winner-only payout: the contract transfers the escrowed reward to the winner.

        Winner authorization is enforced here - only the address the jury selected
        may trigger the payout, and the transfer goes to that same address.
        """
        if bounty_id not in self.bounties:
            raise gl.vm.UserError("Bounty not found")
        b = self.bounties[bounty_id]
        if b.paid:
            raise gl.vm.UserError("Reward already paid out")
        if b.state != u8(1):
            raise gl.vm.UserError("Bounty has not been judged yet")
        if int(b.winner_index) == 0:
            raise gl.vm.UserError("No submission qualified for this bounty")
        if self._addr_bytes(gl.message.sender_address) != self._addr_bytes(b.winner):
            raise gl.vm.UserError("Only the winning submitter can claim this reward")

        amount = b.reward
        if amount <= u256(0):
            raise gl.vm.UserError("Nothing left to pay out")

        b.reward = u256(0)
        b.paid = True
        b.state = u8(2)
        self._pay(b.winner, amount)

    @gl.public.write
    def reclaim_no_winner(self, bounty_id: u256):
        """Creator refund when the jury found nobody qualified."""
        if bounty_id not in self.bounties:
            raise gl.vm.UserError("Bounty not found")
        b = self.bounties[bounty_id]
        if self._addr_bytes(gl.message.sender_address) != self._addr_bytes(b.creator):
            raise gl.vm.UserError("Only the creator can reclaim")
        if b.state != u8(1):
            raise gl.vm.UserError("Bounty has not been judged yet")
        if int(b.winner_index) != 0:
            raise gl.vm.UserError("A winner exists: the reward belongs to them")
        if b.paid:
            raise gl.vm.UserError("Reward already paid out")

        amount = b.reward
        b.reward = u256(0)
        b.paid = True
        b.state = u8(3)
        self._pay(b.creator, amount)

    @gl.public.write
    def cancel_bounty(self, bounty_id: u256):
        """Creator cancels an OPEN bounty and gets the reward back."""
        if bounty_id not in self.bounties:
            raise gl.vm.UserError("Bounty not found")
        b = self.bounties[bounty_id]
        if self._addr_bytes(gl.message.sender_address) != self._addr_bytes(b.creator):
            raise gl.vm.UserError("Only the creator can cancel")
        if b.state != u8(0):
            raise gl.vm.UserError("Bounty cannot be cancelled now")
        if int(b.submission_count) > 0:
            raise gl.vm.UserError("Bounty already has submissions: judge it instead")

        amount = b.reward
        b.reward = u256(0)
        b.paid = True
        b.state = u8(3)
        self._pay(b.creator, amount)
