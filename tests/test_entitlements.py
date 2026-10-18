from datetime import UTC, datetime, timedelta

import pytest

from leedbox.domain.entitlements import Grant, decide_access

NOW = datetime(2026, 10, 9, tzinfo=UTC)


@pytest.mark.parametrize(
    "mode,kind,offset,grace,expected",
    [
        ("free", "trial", -100, 0, "free"),
        ("paid", "trial", 1, 0, "trial"),
        ("paid", "paid", 1, 0, "active"),
        ("paid", "gift", 1, 0, "active"),
        ("paid", "trial", 0, 0, "expired"),
        ("paid", "paid", -1, 2, "grace"),
        ("paid", "paid", -2, 2, "expired"),
    ],
)
def test_access_boundaries(mode, kind, offset, grace, expected):
    grant = Grant(kind, NOW - timedelta(days=110), NOW + timedelta(days=offset))
    result = decide_access(NOW, mode, (grant,), grace)
    assert result.state == expected
    assert result.allowed == (expected != "expired")


def test_activation_grace_and_future_grants():
    future = Grant("paid", NOW + timedelta(days=5), NOW + timedelta(days=35))
    assert not decide_access(NOW, "paid", (future,)).allowed
    assert (
        decide_access(NOW, "paid", (), activation_deadline=NOW + timedelta(days=1)).state == "grace"
    )
    assert not decide_access(NOW, "paid", (), activation_deadline=NOW).allowed


def test_recommendations_hidden_only_for_paid_not_trial_or_gift():
    for kind in ("trial", "gift", "paid"):
        grant = Grant(kind, NOW - timedelta(days=1), NOW + timedelta(days=1))
        result = decide_access(NOW, "paid", (grant,), hide_for_paid=True)
        assert result.hide_recommendations == (kind == "paid")
