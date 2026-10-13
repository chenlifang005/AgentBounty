# -*- coding: utf-8 -*-
"""AgentBounty direct-mode tests (run: pytest tests/direct/ -v)"""
import json


def _deploy(direct_deploy):
    """Deploy for local direct-mode tests.

    The contract header uses the concrete SDK hash for on-chain deployment;
    direct mode resolves dependencies from a local cache, so we keep the same
    hash here. (See README for the header rule.)
    """
    import pathlib
    import tempfile

    src = pathlib.Path(__file__).resolve().parents[2] / "contracts" / "agent_bounty.py"
    code = src.read_text(encoding="utf-8")
    tmp = pathlib.Path(tempfile.gettempdir()) / "agent_bounty_local.py"
    tmp.write_text(code, encoding="utf-8")
    return direct_deploy(str(tmp))


def _addr(v):
    """Wrap raw bytes into a genlayer Address (available at test runtime)."""
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


def test_create_bounty(direct_vm, direct_deploy, direct_alice):
    contract = _deploy(direct_deploy)
    direct_vm.sender = direct_alice

    bid = contract.create_bounty(
        "Build a GenLayer price oracle contract",
        "Must fetch a live price from a public API and expose a view method.",
        500,
    )
    assert int(bid) == 1

    b = contract.get_bounty(bid)
    assert b["title"] == "Build a GenLayer price oracle contract"
    assert int(b["reward"]) == 500
    assert int(b["state"]) == 0
    assert int(b["submission_count"]) == 0
    assert int(contract.get_bounty_count()) == 1


def test_create_bounty_validation(direct_vm, direct_deploy, direct_alice):
    contract = _deploy(direct_deploy)
    direct_vm.sender = direct_alice

    with direct_vm.expect_revert("Title is required"):
        contract.create_bounty("", "criteria", 100)
    with direct_vm.expect_revert("Acceptance criteria are required"):
        contract.create_bounty("title", "   ", 100)
    with direct_vm.expect_revert("Reward must be positive"):
        contract.create_bounty("title", "criteria", 0)


def test_submit_work(direct_vm, direct_deploy, direct_alice, direct_bob, direct_charlie):
    contract = _deploy(direct_deploy)
    direct_vm.sender = direct_alice
    bid = contract.create_bounty("Write a tutorial", "At least 800 words with code samples", 100)

    direct_vm.sender = direct_bob
    contract.submit_work(bid, "https://example.com/bob-tutorial", "Full tutorial with 5 code samples")
    direct_vm.sender = direct_charlie
    contract.submit_work(bid, "https://example.com/charlie-tutorial", "Short tutorial")

    b = contract.get_bounty(bid)
    assert int(b["submission_count"]) == 2
    subs = b["submissions"]
    assert len(subs) == 2
    assert subs[0]["url"] == "https://example.com/bob-tutorial"
    assert subs[1]["url"] == "https://example.com/charlie-tutorial"

    # invalid URL rejected
    with direct_vm.expect_revert("A valid deliverable URL is required"):
        contract.submit_work(bid, "not-a-url", "bad")


def test_judge_picks_winner(direct_vm, direct_deploy, direct_alice, direct_bob, direct_charlie):
    contract = _deploy(direct_deploy)
    direct_vm.sender = direct_alice
    bid = contract.create_bounty(
        "Port the demo to GenLayer",
        "Deliverable must contain actual GenLayer contract code.",
        250,
    )

    direct_vm.sender = direct_bob
    contract.submit_work(bid, "https://example.com/bob", "Ported contract")
    direct_vm.sender = direct_charlie
    contract.submit_work(bid, "https://example.com/charlie", "Ported contract")

    direct_vm.mock_web(r".*example\.com/bob.*",
                       {"status": 200, "body": "<html>import genlayer: real contract code</html>"})
    direct_vm.mock_web(r".*example\.com/charlie.*",
                       {"status": 200, "body": "<html>coming soon - placeholder page</html>"})
    direct_vm.mock_llm(r".*", {
        "winner_index": 1,
        "reason": "Submission #1 contains actual GenLayer contract code while submission #2 is only a placeholder page.",
        "confidence": "high",
    })

    contract.judge_bounty(bid)

    b = contract.get_bounty(bid)
    assert int(b["state"]) == 1
    assert int(b["winner_index"]) == 1
    assert "GenLayer" in b["verdict_reason"]
    assert b["verdict_confidence"] == "high"
    assert _hex(b["winner"]) == direct_bob.hex()

    contract.claim_reward(bid)
    assert int(contract.get_bounty(bid)["state"]) == 2


def test_judge_nobody_qualifies(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = _deploy(direct_deploy)
    direct_vm.sender = direct_alice
    bid = contract.create_bounty("Deliver X", "Must include a deployed contract address", 100)
    direct_vm.sender = direct_bob
    contract.submit_work(bid, "https://example.com/empty", "TODO later")

    direct_vm.mock_web(r".*", {"status": 200, "body": "<html>nothing here</html>"})
    direct_vm.mock_llm(r".*", {"winner_index": 0, "reason": "No submission meets the criteria.", "confidence": "high"})

    contract.judge_bounty(bid)
    b = contract.get_bounty(bid)
    assert int(b["winner_index"]) == 0
    assert b["winner"] == ""
    with direct_vm.expect_revert("No submission qualified for this bounty"):
        contract.claim_reward(bid)


def test_cannot_judge_twice_or_without_subs(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = _deploy(direct_deploy)
    direct_vm.sender = direct_alice
    bid = contract.create_bounty("Task", "criteria", 10)

    with direct_vm.expect_revert("No submissions to judge"):
        contract.judge_bounty(bid)

    direct_vm.sender = direct_bob
    contract.submit_work(bid, "https://example.com/x", "done")

    direct_vm.mock_web(r".*", {"status": 200, "body": "<html>done</html>"})
    direct_vm.mock_llm(r".*", {"winner_index": 1, "reason": "Meets criteria.", "confidence": "medium"})
    contract.judge_bounty(bid)

    with direct_vm.expect_revert("Bounty is already judged"):
        contract.judge_bounty(bid)
    with direct_vm.expect_revert("Bounty is closed for submissions"):
        contract.submit_work(bid, "https://example.com/late", "too late")


def test_only_creator_can_cancel(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = _deploy(direct_deploy)
    direct_vm.sender = direct_alice
    bid = contract.create_bounty("Task", "criteria", 10)

    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("Only the creator can cancel"):
        contract.cancel_bounty(bid)

    direct_vm.sender = direct_alice
    contract.cancel_bounty(bid)
    assert int(contract.get_bounty(bid)["state"]) == 3
