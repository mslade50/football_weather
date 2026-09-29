"""Shared roof policy for both sports, including neutral-site games."""


def resolve_roof_state(roof_type: str | None, roof_state: str | None) -> str | None:
    """Fixed venue construction wins over conflicting schedule metadata.

    A retractable roof's state must come from the game, never a weather guess.
    Generic ``outdoors`` is not confirmation that a retractable roof is open.
    """
    if roof_type == "dome":
        return "dome"
    if roof_type == "open":
        return "outdoors"
    if roof_type == "retractable":
        return roof_state if roof_state in ("open", "closed", "dome") else None
    return roof_state


def weather_exposed(roof_type: str | None, roof_state: str | None) -> bool:
    state = resolve_roof_state(roof_type, roof_state)
    if state in ("dome", "closed"):
        return False
    return roof_type != "retractable" or state == "open"
