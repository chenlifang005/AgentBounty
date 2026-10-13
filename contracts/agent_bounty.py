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
    requirements: str      # natural-language acceptance criteria
    reward: u256
    state: u8              # 0=OPEN 1=JUDGED 2=AWARDED 3=CANCELLED
    submission_count: u256
    winner_index: u256     # 0 = nobody qualified
    winner: Address
    verdict_reason: str
    verdict_confidence: str


@allow_storage
@dataclass
class Submission:
    hunter: Address
    deliverable_url: str
    description: str


# ------------------------------------------------------------------ contract --

class AgentBounty(gl.Contract):
    """Autonomous bounty board for the agent economy.

    A creator posts a bounty with acceptance criteria written in plain language
    and locks a reward. Any agent or human submits a deliverable URL. When the
    creator calls judge_bounty(), an LLM jury reads every deliverable on-chain
    and decides which submission actually satisfies the criteria - so agents can
    take on work without a human reviewer in the loop.
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
            "state": b.state,
            "submission_count": b.submission_count,
            "winner_index": b.winner_index,
            "winner": "0x" + self._addr_bytes(b.winner).hex() if int(b.winner_index) > 0 else "",
            "verdict_reason": b.verdict_reason,
            "verdict_confidence": b.verdict_confidence,
            "submissions": self._subs(bounty_id, b.submission_count),
        }

    @gl.public.view
    def get_bounty_count(self) -> u256:
        return self.next_id - u256(1)

    @gl.public.view
    def get_submission(self, bounty_id: u256, index: u256) -> dict:
        key = str(int(bounty_id)) + ":" + str(int(index) - 1)
        s = self.submissions[key]
        return {
            "hunter": "0x" + self._addr_bytes(s.hunter).hex(),
            "deliverable_url": s.deliverable_url,
            "description": s.description,
        }

    # ------------------------------------------------------------ writes ----

    @gl.public.write
    def create_bounty(self, title: str, requirements: str, reward: u256) -> u256:
        """Post a bounty with plain-language acceptance criteria."""
        if len(title.strip()) == 0:
            raise gl.vm.UserError("Title is required")
        if len(requirements.strip()) == 0:
            raise gl.vm.UserError("Acceptance criteria are required")
        if reward <= u256(0):
            raise gl.vm.UserError("Reward must be positive")

        b = Bounty(
            creator=self._to_addr(gl.message.sender_address),
            title=title,
            requirements=requirements,
            reward=reward,
            state=u8(0),
            submission_count=u256(0),
            winner_index=u256(0),
            winner=self._to_addr(gl.message.sender_address),
            verdict_reason="",
            verdict_confidence="",
        )
        bid = self.next_id
        self.bounties[bid] = b
        self.next_id = bid + u256(1)
        return bid

    @gl.public.write
    def submit_work(self, bounty_id: u256, deliverable_url: str, description: str):
        """Submit a deliverable URL for a bounty (one entry per submission)."""
        if bounty_id not in self.bounties:
            raise gl.vm.UserError("Bounty not found")
        if len(deliverable_url.strip()) == 0 or not (
                deliverable_url.startswith("http://") or deliverable_url.startswith("https://")):
            raise gl.vm.UserError("A valid deliverable URL is required")

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
        """LLM jury reads every deliverable and picks the best against the criteria."""
        if bounty_id not in self.bounties:
            raise gl.vm.UserError("Bounty not found")

        b = self.bounties[bounty_id]
        if b.state != u8(0):
            raise gl.vm.UserError("Bounty is already judged")
        if int(b.submission_count) == 0:
            raise gl.vm.UserError("No submissions to judge")

        # Snapshot everything into plain Python values outside the nondet block.
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
                content = _fetch_text(s["url"])
                blocks.append(
                    "---- Submission #" + str(s["index"]) + " ----\n"
                    + "hunter: " + s["hunter"] + "\n"
                    + "claim: " + s["description"] + "\n"
                    + "deliverable URL: " + s["url"] + "\n"
                    + "fetched content: " + content
                )
            joined = "\n\n".join(blocks)

            prompt = (
                "You are an impartial bounty judge on an autonomous agent marketplace.\n\n"
                "BOUNTY: " + title_mem + "\n"
                "REWARD: " + str(reward_mem) + " GEN\n"
                "ACCEPTANCE CRITERIA (written by the creator, authoritative):\n"
                + req_mem + "\n\n"
                "SUBMISSIONS (content was fetched on-chain right now):\n"
                + joined + "\n\n"
                "Decide which single submission best satisfies the acceptance criteria.\n"
                "Rules:\n"
                "- Judge only against the criteria above, not against popularity.\n"
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
            a = mine.get("winner_index")
            b2 = leaders_res.calldata.get("winner_index")
            try:
                return int(a) == int(b2)
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
        """Winner confirms receipt and closes the bounty."""
        if bounty_id not in self.bounties:
            raise gl.vm.UserError("Bounty not found")
        b = self.bounties[bounty_id]
        if b.state != u8(1):
            raise gl.vm.UserError("Bounty has not been judged yet")
        if int(b.winner_index) == 0:
            raise gl.vm.UserError("No submission qualified for this bounty")
        b.state = u8(2)

    @gl.public.write
    def cancel_bounty(self, bounty_id: u256):
        """Creator cancels an open bounty with no qualifying work."""
        if bounty_id not in self.bounties:
            raise gl.vm.UserError("Bounty not found")
        b = self.bounties[bounty_id]
        if self._addr_bytes(gl.message.sender_address) != self._addr_bytes(b.creator):
            raise gl.vm.UserError("Only the creator can cancel")
        if b.state != u8(0):
            raise gl.vm.UserError("Bounty cannot be cancelled now")
        b.state = u8(3)
