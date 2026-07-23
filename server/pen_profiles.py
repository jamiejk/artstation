"""Built-in pen motion profiles and job snapshot helpers."""

STANDARD_PROFILE_ID = "standard"

_PROFILES = {
    STANDARD_PROFILE_ID: {
        "id": STANDARD_PROFILE_ID,
        "name": "Standard",
        "description": "Normal AxiDraw pen lowering and raising at stroke endpoints.",
        "gradual_ramp_mm": 0.0,
        "gradual_exit_ramp_mm": 0.0,
        "gradual_tail_mm": 0.0,
        "gradual_segment_mm": 0.0,
    },
    "staedtler_marsmatic": {
        "id": "staedtler_marsmatic",
        "name": "Staedtler Marsmatic",
        "description": (
            "Gradually lowers into and raises out of each stroke to reduce endpoint ink dots."
        ),
        "gradual_ramp_mm": 4.375,
        "gradual_exit_ramp_mm": 4.6875,
        "gradual_tail_mm": 0.4375,
        "gradual_segment_mm": 0.5,
    },
}


def profile_catalog() -> list[dict]:
    """Return independent copies suitable for API responses."""
    return [dict(profile) for profile in _PROFILES.values()]


def resolve_profile(profile_id: object) -> dict:
    key = str(profile_id or STANDARD_PROFILE_ID).strip()
    try:
        return dict(_PROFILES[key])
    except KeyError as exc:
        raise ValueError(f"Unknown pen profile: {key!r}") from exc


def job_snapshot(profile_id: object) -> dict:
    profile = resolve_profile(profile_id)
    return {
        "pen_profile_id": profile["id"],
        "pen_profile_name": profile["name"],
        "gradual_ramp_mm": profile["gradual_ramp_mm"],
        "gradual_exit_ramp_mm": profile["gradual_exit_ramp_mm"],
        "gradual_tail_mm": profile["gradual_tail_mm"],
        "gradual_segment_mm": profile["gradual_segment_mm"],
    }


def gradual_enabled(settings: dict) -> bool:
    return float(settings.get("gradual_ramp_mm", 0) or 0) > 0
