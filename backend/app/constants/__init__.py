"""PocketTerm constants package.

Re-exports the most commonly used constants from the sub-modules so that
callers can simply do ``from app.constants import GameVersion`` instead of
having to know the exact sub-module layout.
"""

from app.constants.minecraft import (
    GameVersion,
    ProtocolVersion,
    DeviceOS,
    DefaultDeviceModel,
    DefaultNamePrefix,
)
from app.constants.packets import (
    PacketId,
    TextType,
    PacketName,
    PACKET_ID_TO_NAME,
)

__all__ = [
    # minecraft
    "GameVersion",
    "ProtocolVersion",
    "DeviceOS",
    "DefaultDeviceModel",
    "DefaultNamePrefix",
    # packets
    "PacketId",
    "TextType",
    "PacketName",
    "PACKET_ID_TO_NAME",
]
