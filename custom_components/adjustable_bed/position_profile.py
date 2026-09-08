"""Explicit per-frame calibration options for the recorded EDFE16 layout."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

CONF_CALIBRATED_POSITION = "keeson_calibrated_position"
CONF_BACK_RAW_MAX = "keeson_back_raw_max"
CONF_LEGS_RAW_MAX = "keeson_legs_raw_max"
CONF_ALLOW_MOTION_PROBE = "keeson_allow_motion_probe"
CONF_NATIVE_633_POSITION = "keeson_native_633_position"
PROFILE_KEYS = (
    CONF_CALIBRATED_POSITION,
    CONF_BACK_RAW_MAX,
    CONF_LEGS_RAW_MAX,
    CONF_ALLOW_MOTION_PROBE,
    CONF_NATIVE_633_POSITION,
)


@dataclass(frozen=True)
class PositionProfile:
    """Measured raw maxima; neither values nor activation are inferred from identity."""

    back_raw_max: int
    legs_raw_max: int
    allow_motion_probe: bool = False
    native_633_position: bool = False

    def __post_init__(self) -> None:
        for value in (self.back_raw_max, self.legs_raw_max):
            if type(value) is not int or not 1 <= value <= 65534:
                raise ValueError("Raw calibration maxima must be integers from 1 to 65534")
        if type(self.allow_motion_probe) is not bool:
            raise ValueError("Motion probe opt-in must be a boolean")

        if type(self.native_633_position) is not bool:
            raise ValueError("Native 633 opt-in must be a boolean")

    @property
    def maxima(self) -> dict[str, int]:
        return {"back": self.back_raw_max, "legs": self.legs_raw_max}

    @classmethod
    def from_data(cls, data: Mapping[str, Any]) -> PositionProfile | None:
        if data.get(CONF_CALIBRATED_POSITION) is not True:
            return None
        if (
            data.get("bed_type") != "keeson"
            or data.get("protocol_variant") != "ergomotion"
            or data.get("motor_count", 2) != 2
            or data.get("disable_angle_sensing", True)
        ):
            raise ValueError("Calibration requires Keeson/Ergomotion, two motors and angle sensing")
        return cls(
            data.get(CONF_BACK_RAW_MAX, 0),
            data.get(CONF_LEGS_RAW_MAX, 0),
            data.get(CONF_ALLOW_MOTION_PROBE, False),
            data.get(CONF_NATIVE_633_POSITION, False),
        )
