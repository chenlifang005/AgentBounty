# -*- coding: utf-8 -*-
"""AgentBounty v2 direct-mode tests — real reward custody, winner authorization, real payout.

Run: pytest tests/direct/ -v
"""
import json


def _deploy(direct_deploy):
    import pathlib
    import tempfile

    src = pathlib.Path(__file__).resolve().parents[2] / "contracts" / "agent_bounty.py"
    code = src.read_text(encoding="utf-8")
    tmp = pathlib.Path(tempfile.gettempdir()) / "agent_bounty_local.py"
    tmp.write_text(code, encoding="utf-8")
    return direct_deploy(str(tmp))


def _addr(v):
    from genlayer import Address
    return Address(v)


def _hex(v) -> str:
    if isinstance(v, str):
        return v.lower().replace("0x", "")
    for attr in ("as_bytes", "_as_bytes"):
        if hasattr(v, attr):
            b = getattr(v, attr)
            if callable(b):
                b = b()
            return b.hex()
    return bytes(v).hex()


def _funded_bounty(direct_vm, contract, creator, reward=1000,
                   title="Build a price oracle", reqs="Must include real contract code"):
    direct_vm.sender = creator
    direct_vm.value = reward
    bid = contract.create_bounty(title, reqs)
    direct_vm.value = 0
    return bid


# ---------------------------------------------------- 1. reward custody ----

def test_create_requires_funding(direct_vm, direct_deploy, direct_alice):
    """The reviewer's point: the contract must ACCEPT reward funds."""
    contract = _deploy(direct_deploy)
    direct_vm.sender = direct_alice
    direct_vm.value = 0
    with direct_vm.expect_revert("Reward must be funded"):
        contract.create_bounty("Unfunded bounty", "criteria")


def test_reward_is_real_custody(direct_vm, direct_deploy, direct_alice):
    contract = _deploy(direct_deploy)
    bid = _funded_bounty(direct_vm, contract, direct_alice, reward=5000)
    b = contract.get_bounty(bid)
    assert int(b["reward"]) == 5000          # recorded from gl.message.value
    assert b["paid"] is False
    assert int(b["state"]) == 0


def test_create_validation(direct_vm, direct_deploy, direct_alice):
    contract = _deploy(direct_deploy)
    direct_vm.sender = direct_alice
    direct_vm.value = 100
    with direct_vm.expect_revert("Title is required"):
        contract.create_bounty("", "criteria")
    with direct_vm.expect_revert("Acceptance criteria are required"):
        contract.create_bounty("title", "  ")
    direct_vm.value = 0


# --------------------------------------------- 2. winner authorization ----

def test_only_winner_can_claim(direct_vm, direct_deploy, direct_alice, direct_bob, direct_charlie):
    """The reviewer's point: any caller could previously mark a bounty awarded."""
    contract = _deploy(direct_deploy)
    bid = _funded_bounty(direct_vm, contract, direct_alice)

    # bob wins, charlie submitted too
    direct_vm.sender = direct_bob
    contract.submit_work(bid, "https://example.com/bob", "real deliverable")
    direct_vm.sender = direct_charlie
    contract.submit_work(bid, "https://example.com/charlie", "placeholder")

    direct_vm.mock_web(r".*", {"status": 200, "body": "<html>content</html>"})
    direct_vm.mock_llm(r".*", {"winner_index": 1, "reason": "Submission #1 is the real one.",
                               "confidence": "high"})
    contract.judge_bounty(bid)

    b = contract.get_bounty(bid)
    assert int(b["winner_index"]) == 1
    assert _hex(b["winner"]) == direct_bob.hex()

    # the creator cannot claim, and neither can another submitter
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("Only the winning submitter can claim"):
        contract.claim_reward(bid)
    direct_vm.sender = direct_charlie
    with direct_vm.expect_revert("Only the winning submitter can claim"):
        contract.claim_reward(bid)

    # the winner can
    direct_vm.sender = direct_bob
    contract.claim_reward(bid)
    b = contract.get_bounty(bid)
    assert b["paid"] is True
    assert int(b["reward"]) == 0        # escrow released, not left as metadata
    assert int(b["state"]) == 2         # AWARDED


def test_claim_requires_judging_and_no_double_pay(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = _deploy(direct_deploy)
    bid = _funded_bounty(direct_vm, contract, direct_alice)

    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("Bounty has not been judged yet"):
        contract.claim_reward(bid)

    contract.submit_work(bid, "https://example.com/x", "deliverable")
    direct_vm.mock_web(r".*", {"status": 200, "body": "<html>ok</html>"})
    direct_vm.mock_llm(r".*", {"winner_index": 1, "reason": "Meets criteria.", "confidence": "medium"})
    contract.judge_bounty(bid)

    contract.claim_reward(bid)          # bob claims
    with direct_vm.expect_revert("Reward already paid out"):
        contract.claim_reward(bid)


# ------------------------------------------------ 3. payout / refunds ----

def test_judge_flow_and_no_winner_refund(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = _deploy(direct_deploy)
    bid = _funded_bounty(direct_vm, contract, direct_alice, reward=2000)
    direct_vm.sender = direct_bob
    contract.submit_work(bid, "https://example.com/empty", "TODO later")

    direct_vm.mock_web(r".*", {"status": 200, "body": "<html>nothing here</html>"})
    direct_vm.mock_llm(r".*", {"winner_index": 0, "reason": "No submission meets the criteria.",
                               "confidence": "high"})
    contract.judge_bounty(bid)

    b = contract.get_bounty(bid)
    assert int(b["winner_index"]) == 0
    assert b["winner"] == ""

    # winner-only claim is impossible
    with direct_vm.expect_revert("No submission qualified"):
        contract.claim_reward(bid)

    # a non-creator cannot reclaim
    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("Only the creator can reclaim"):
        contract.reclaim_no_winner(bid)

    # the creator gets the reward back
    direct_vm.sender = direct_alice
    contract.reclaim_no_winner(bid)
    b = contract.get_bounty(bid)
    assert b["paid"] is True
    assert int(b["reward"]) == 0
    assert int(b["state"]) == 3


def test_creator_cannot_reclaim_when_winner_exists(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = _deploy(direct_deploy)
    bid = _funded_bounty(direct_vm, contract, direct_alice)
    direct_vm.sender = direct_bob
    contract.submit_work(bid, "https://example.com/good", "real work")
    direct_vm.mock_web(r".*", {"status": 200, "body": "<html>real code</html>"})
    direct_vm.mock_llm(r".*", {"winner_index": 1, "reason": "Qualifies.", "confidence": "high"})
    contract.judge_bounty(bid)

    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("A winner exists"):
        contract.reclaim_no_winner(bid)


def test_cancel_open_bounty_refunds_creator(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = _deploy(direct_deploy)
    bid = _funded_bounty(direct_vm, contract, direct_alice, reward=700)

    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("Only the creator can cancel"):
        contract.cancel_bounty(bid)

    direct_vm.sender = direct_alice
    contract.cancel_bounty(bid)
    b = contract.get_bounty(bid)
    assert b["paid"] is True
    assert int(b["reward"]) == 0
    assert int(b["state"]) == 3


def test_cancel_blocked_once_submissions_exist(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = _deploy(direct_deploy)
    bid = _funded_bounty(direct_vm, contract, direct_alice)
    direct_vm.sender = direct_bob
    contract.submit_work(bid, "https://example.com/x", "work")

    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("already has submissions"):
        contract.cancel_bounty(bid)


def test_judge_guards(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = _deploy(direct_deploy)
    bid = _funded_bounty(direct_vm, contract, direct_alice)

    with direct_vm.expect_revert("No submissions to judge"):
        contract.judge_bounty(bid)

    direct_vm.sender = direct_bob
    contract.submit_work(bid, "https://example.com/x", "work")
    direct_vm.mock_web(r".*", {"status": 200, "body": "<html>ok</html>"})
    direct_vm.mock_llm(r".*", {"winner_index": 1, "reason": "ok", "confidence": "low"})
    contract.judge_bounty(bid)

    with direct_vm.expect_revert("Bounty is already judged"):
        contract.judge_bounty(bid)
    with direct_vm.expect_revert("Bounty is closed for submissions"):
        contract.submit_work(bid, "https://example.com/late", "late")
