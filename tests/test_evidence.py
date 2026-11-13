from app.evidence import normalize, quote_is_supported, validate_evidence
from app.x.base import XPost


def test_exact_quote_is_supported():
    source = "Building autonomous robot manipulation systems for logistics in Singapore."
    quote = "autonomous robot manipulation systems"
    assert quote_is_supported(quote, source)


def test_case_and_whitespace_differences_are_tolerated():
    source = "Working on “Small Language Models”\n\nand evaluation benchmarks in Hanoi."
    quote = "... \"small language models\" and evaluation benchmarks …"
    assert quote_is_supported(quote, source)


def test_invented_quote_is_rejected():
    source = "I love hiking and reading history books."
    quote = "working on robotics and artificial intelligence"
    assert not quote_is_supported(quote, source)


def test_short_quote_is_rejected():
    source = "Building artificial intelligence tools."
    quote = "tools"
    # min_len is 12 characters by default
    assert not quote_is_supported(quote, source)


def test_post_evidence_must_reference_the_users_own_post():
    posts_by_id = {
        "p101": XPost(id="p101", author_id="user_1", text="Developing quadruped robot controllers.", created_at="2026-11-13T00:00:00Z"),
        "p102": XPost(id="p102", author_id="other_user", text="Autonomous drones for inspection.", created_at="2026-11-13T00:00:00Z"),
    }
    bio = "Robotics researcher based in Kuala Lumpur."

    items = [
        {"source": "bio", "quote": "Robotics researcher based in Kuala Lumpur"},
        {"source": "post", "post_id": "p101", "quote": "quadruped robot controllers"},
        {"source": "post", "post_id": "p102", "quote": "Autonomous drones for inspection"},  # authored by other_user!
        {"source": "post", "post_id": "p999", "quote": "non existent post"},
    ]

    valid = validate_evidence(items, bio=bio, posts_by_id=posts_by_id, author_id="user_1", username="demo_user")
    assert len(valid) == 2

    assert valid[0]["source"] == "bio"
    assert valid[0]["url"] == "https://x.com/demo_user"

    assert valid[1]["source"] == "post"
    assert valid[1]["post_id"] == "p101"
    assert valid[1]["url"] == "https://x.com/demo_user/status/p101"
