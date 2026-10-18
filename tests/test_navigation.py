from leedbox.telegram.navigation import can_return_to_expert


def test_only_known_expert_sessions_can_return_from_recipient_mode():
    assert not can_return_to_expert({"mode": "recipient"})
    assert not can_return_to_expert({})
    assert can_return_to_expert({"mode": "expert"})
    assert can_return_to_expert({"mode": "recipient", "expert_return_allowed": True})
    assert can_return_to_expert({"mode": "recipient", "library_item_messages": {"1": 123}})
