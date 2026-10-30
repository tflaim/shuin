"""Deadpan lines shared by the CLI and the board.

These are Shuin's words, not agent claims.
"""


def seal_readout(
    *,
    blocked: bool,
    seal_broken: bool,
    stale_verification: bool,
    verification_state: str | None,
    seal_refusal: str | None,
) -> dict:
    """seal_refusal is the Seal's refusal when it does not hold, or None.

    It has no default: a readout cannot stamp without asking the Seal.
    """
    stale = seal_broken or stale_verification
    if blocked:
        seal, voice = "blocked", "Blocked."
    elif seal_broken:
        seal, voice = "stale", "Seal broken: head moved."
    elif stale_verification:
        seal, voice = "stale", "Stale verification."
    elif seal_refusal and verification_state == "passed":
        seal, voice = "pending", seal_refusal
    elif verification_state == "passed":
        seal, voice = "stamped", "Stamped."
    else:
        seal, voice = "pending", "Awaiting the verifier."
    return {
        "seal": seal,
        "voice": voice,
        "stale": stale,
        "seal_broken": seal_broken,
        "stale_verification": stale_verification,
        "blocked": blocked,
        "verification_state": verification_state,
    }


def page_voice(readouts: list[dict], task_count: int) -> str:
    if task_count == 0:
        return "No cards. The night is quiet."
    if any(item.get("seal_broken") for item in readouts):
        return "Seal broken: head moved."
    stale_verification = sum(1 for item in readouts if item.get("stale_verification"))
    if stale_verification:
        return f"Stale verification: {stale_verification}."
    if any(
        item["verification_state"] in ("queued", "running") or item["seal"] == "pending"
        for item in readouts
    ):
        return "Awaiting the verifier."
    if any(item["seal"] == "blocked" for item in readouts):
        return "Blocked."
    if any(item["seal"] == "stamped" for item in readouts):
        return "Stamped."
    return "The board is still."


def format_left(seconds: int) -> str:
    if seconds <= 0:
        return "0s"
    minutes, secs = divmod(int(seconds), 60)
    if minutes >= 60:
        hours, minutes = divmod(minutes, 60)
        return f"{hours}h {minutes}m"
    if minutes and secs:
        return f"{minutes}m {secs:02d}s"
    if minutes:
        return f"{minutes}m"
    return f"{secs}s"
