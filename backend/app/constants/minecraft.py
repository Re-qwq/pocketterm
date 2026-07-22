"""Minecraft Bedrock Edition game constants.

These values describe the game version that PocketTerm bots pretend to be when
connecting to a Bedrock server as well as a handful of device / platform
identifiers used by the login sequence.
"""

# ---------------------------------------------------------------------------
# Version information
# ---------------------------------------------------------------------------
#: The Minecraft Bedrock Edition version string reported during login.
GameVersion: str = "1.21.93"

#: The network protocol version that corresponds to ``GameVersion``.
#: 1.21.93 maps to protocol version 685.
ProtocolVersion: int = 685

#: Minimum protocol version the client is willing to speak.
MinProtocolVersion: int = 685

#: Maximum protocol version the client is willing to speak.
MaxProtocolVersion: int = 685

#: The edition reported in the ``ChainData`` JWT payload. 0 == Minecraft (Bedrock).
Edition: int = 0

# ---------------------------------------------------------------------------
# Device OS identifiers
# ---------------------------------------------------------------------------
# These numeric identifiers are sent in the login ``ClientData`` payload to tell
# the server which platform the client is running on.


class DeviceOS:
    """Numeric device-operating-system identifiers used by the Bedrock login."""

    Unknown = -1
    Android = 1
    IOS = 2
    OSX = 3           # macOS
    FireOS = 4        # Amazon Fire
    GearVR = 5
    Hololens = 6
    Windows = 7       # Windows 10/11 x64
    Windows32 = 8     # Dedicated / legacy server
    TVOS = 9          # Apple TV
    PlayStation = 10
    Switch = 11
    Xbox = 12
    WindowsPhone = 13
    Linux = 14

    #: Human-readable labels keyed by the numeric id.
    NAMES = {
        -1: "Unknown",
        1: "Android",
        2: "iOS",
        3: "macOS",
        4: "FireOS",
        5: "GearVR",
        6: "Hololens",
        7: "Windows",
        8: "Windows32",
        9: "tvOS",
        10: "PlayStation",
        11: "Switch",
        12: "Xbox",
        13: "WindowsPhone",
        14: "Linux",
    }

    @classmethod
    def name(cls, os_id: int) -> str:
        """Return the human-readable name for a device os id."""
        return cls.NAMES.get(os_id, "Unknown")


# ---------------------------------------------------------------------------
# Default bot identity values
# ---------------------------------------------------------------------------
#: Default prefix prepended to generated bot gamertags.
DefaultNamePrefix: str = "Bot"

#: A reasonable default device model string for the Android platform.
DefaultDeviceModel: str = "Samsung Galaxy S21"

#: Default ClientRandomId range base used when generating login data.
DefaultClientIdBase: int = 0

# ---------------------------------------------------------------------------
# UI / input mode identifiers sent during login
# ---------------------------------------------------------------------------
class UIProfile:
    """UI profile values reported in ``ClientData``."""

    Classic = 0
    Pocket = 1


class InputMode:
    """Input mode values reported in ``ClientData``."""

    Unknown = 0
    Mouse = 1
    Touch = 2
    GamePad = 3
    MotionController = 4


class GUIProfile:
    """GUI scale profile values reported in ``ClientData``."""

    Classic = 0
    Pocket = 1
