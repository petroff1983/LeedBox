def member_status(status: str, restricted_member: bool = False) -> bool:
    return status in {"creator", "administrator", "member"} or (
        status == "restricted" and restricted_member
    )
