import pytest

from leedbox.domain.invites import member_status


@pytest.mark.parametrize(
    "status,restricted,expected",
    [
        ("member", False, True),
        ("administrator", False, True),
        ("creator", False, True),
        ("left", False, False),
        ("kicked", False, False),
        ("restricted", False, False),
        ("restricted", True, True),
    ],
)
def test_membership_transitions(status, restricted, expected):
    assert member_status(status, restricted) is expected
