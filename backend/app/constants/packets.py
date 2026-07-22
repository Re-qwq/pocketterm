"""Minecraft Bedrock Edition (MCBE) packet id constants.

The numeric ids below are the values used by the Bedrock RakNet protocol for
protocol version ~685 (game 1.21.9x). They are exposed both as a flat
:class:`PacketId` namespace and via a reverse-lookup dictionary
``PACKET_ID_TO_NAME`` for convenient logging.
"""


class PacketId:
    """Numeric identifiers for the most common Bedrock packets."""

    # ---- Handshake / login --------------------------------------------
    Login = 0x01
    ServerToClientHandshake = 0x03
    ClientToServerHandshake = 0x04
    Disconnect = 0x05
    ResourcePacksInfo = 0x06
    ResourcePackStack = 0x07
    ResourcePackClientResponse = 0x08

    # ---- Chat / text --------------------------------------------------
    Text = 0x09

    # ---- World / time -------------------------------------------------
    SetTime = 0x0A
    StartPlay = 0x0B

    # ---- Actors / entities -------------------------------------------
    AddPlayer = 0x0C
    AddActor = 0x0D
    RemoveActor = 0x0E
    AddItemActor = 0x0F
    TakeItemActor = 0x11
    MoveActorAbsolute = 0x12
    MovePlayer = 0x13
    RiderJump = 0x14

    # ---- World / blocks ----------------------------------------------
    UpdateBlock = 0x15
    AddPainting = 0x16
    TickSync = 0x17
    LevelSoundEventV1 = 0x18
    LevelEvent = 0x19
    BlockEvent = 0x1A

    # ---- Actor events -------------------------------------------------
    ActorEvent = 0x1B
    MobEffect = 0x1C
    UpdateAttributes = 0x1D

    # ---- Inventory ----------------------------------------------------
    InventoryContent = 0x1E
    InventoryTransaction = 0x1F
    MobArmorEquipment = 0x20
    MobEquipment = 0x21
    Interact = 0x22
    BlockPickRequest = 0x23
    ActorPickRequest = 0x24
    PlayerAction = 0x25
    HurtArmor = 0x26

    # ---- Actor metadata ----------------------------------------------
    SetActorData = 0x27
    SetActorMotion = 0x28
    SetActorLink = 0x29

    # ---- Player state -------------------------------------------------
    SetHealth = 0x2A
    SetSpawnPosition = 0x2B
    Animate = 0x2C
    Respawn = 0x2D

    # ---- Containers ---------------------------------------------------
    ContainerOpen = 0x2E
    ContainerClose = 0x2F
    PlayerHotbar = 0x30
    InventoryAction = 0x31

    # ---- Settings -----------------------------------------------------
    RequestPermissions = 0x32
    AdventureSettings = 0x33

    # ---- Blocks / world interaction ----------------------------------
    BlockEntityData = 0x34
    SetPlayerInventoryOptions = 0x35
    PlayerInput = 0x36
    LevelChunkData = 0x37
    SetCommandsEnabled = 0x38
    SetDifficulty = 0x39
    ChangeDimension = 0x3A
    SetPlayerGameType = 0x3B

    # ---- Player list --------------------------------------------------
    PlayerList = 0x3C
    SimpleEvent = 0x3D
    Event = 0x3E

    # ---- World data ---------------------------------------------------
    MobSpawnPosition = 0x3F
    RequestChunkRadius = 0x40
    ChunkRadiusUpdated = 0x41

    # ---- Items / recipes ---------------------------------------------
    ItemFrameDropItem = 0x42
    GameRulesChanged = 0x43
    Camera = 0x44
    BossEvent = 0x45
    ShowCredits = 0x46
    AvailableCommands = 0x47
    CommandRequest = 0x48
    CommandBlockUpdate = 0x49
    UpdateTrade = 0x4A
    UpdateEquip = 0x4B
    ResourcePackDataInfo = 0x4C
    ResourcePackChunkData = 0x4D
    ResourcePackChunkRequest = 0x4E

    # ---- Transfer / misc --------------------------------------------
    Transfer = 0x4F
    PlaySound = 0x50
    StopSound = 0x51
    SetTitle = 0x52
    AddBehaviorTree = 0x53
    StructureBlockUpdate = 0x54
    ShowStoreOffer = 0x55
    PurchaseReceipt = 0x56
    PlayerSkin = 0x57
    SubClientLogin = 0x58
    InitiateWebSocketConnection = 0x59
    SetLastHurtBy = 0x5A
    BookEdit = 0x5B
    NpcRequest = 0x5C
    PhotoTransfer = 0x5D
    ModalFormRequest = 0x5E
    ModalFormResponse = 0x5F
    ServerSettingsRequest = 0x60
    ServerSettingsResponse = 0x61
    ShowProfile = 0x62
    SetDefaultGameType = 0x63
    RemoveObjective = 0x64
    SetDisplayObjective = 0x65
    SetScore = 0x66
    LabTable = 0x67
    UpdateBlockSynced = 0x68
    MoveActorDelta = 0x69
    SetScoreboardIdentity = 0x6A
    SetLocalPlayerAsInitialized = 0x6B
    UpdateSoftEnum = 0x6C
    NetworkStackLatency = 0x6D

    # ---- Scripting / extras -----------------------------------------
    SpawnParticleEffect = 0x6E
    AvailableActorIdentifiers = 0x6F
    LevelSoundEventV2 = 0x70
    NetworkChunkPublisherUpdate = 0x71
    BiomeDefinitionList = 0x72
    LevelSoundEvent = 0x73
    LevelEventGeneric = 0x74
    LecternUpdate = 0x75
    VideoStreamConnect_DEPRECATED = 0x76
    AddEntity = 0x77
    RemoveEntity = 0x78
    ClientCacheStatus = 0x79
    OnScreenTextureAnimation = 0x7A
    MapCreateLockedCopy = 0x7B
    StructureTemplateDataExportRequest = 0x7C
    StructureTemplateDataExportResponse = 0x7D
    UpdateBlockProperties = 0x7E
    ClientCacheBlobStatus = 0x7F
    ClientCacheMissResponse = 0x80
    EducationSettings = 0x81
    Emote = 0x82
    MultiplayerSettings = 0x83
    SettingsCommand = 0x84
    AnimateEntity = 0x85
    CameraInstruction = 0x86
    CameraPresets = 0x87
    UnlockedRecipes = 0x88
    CameraShake = 0x89
    CodeBuilder = 0x8A

    # ---- More recent additions --------------------------------------
    ResourcePackChunkDataV2 = 0x8B
    ResourcePackDataInfoV2 = 0x8C
    DebugRenderer = 0x8D
    BiomeDefinitionData = 0x8E


class TextType:
    """Text packet ``type`` field values."""

    Raw = 0
    Chat = 1
    Translation = 2
    Popup = 3
    JukeboxPopup = 4
    Tip = 5
    SystemMessage = 6
    Whisper = 7
    Announcement = 8
    ObjectText = 9
    ObjectWhisper = 10


# ---------------------------------------------------------------------------
# Reverse lookup helpers
# ---------------------------------------------------------------------------
#: Human-readable name for every :class:`PacketId` member.
PacketName = {
    "Login": PacketId.Login,
    "ServerToClientHandshake": PacketId.ServerToClientHandshake,
    "ClientToServerHandshake": PacketId.ClientToServerHandshake,
    "Disconnect": PacketId.Disconnect,
    "ResourcePacksInfo": PacketId.ResourcePacksInfo,
    "ResourcePackStack": PacketId.ResourcePackStack,
    "ResourcePackClientResponse": PacketId.ResourcePackClientResponse,
    "Text": PacketId.Text,
    "SetTime": PacketId.SetTime,
    "StartPlay": PacketId.StartPlay,
    "AddPlayer": PacketId.AddPlayer,
    "AddActor": PacketId.AddActor,
    "RemoveActor": PacketId.RemoveActor,
    "MoveActorAbsolute": PacketId.MoveActorAbsolute,
    "MovePlayer": PacketId.MovePlayer,
    "UpdateBlock": PacketId.UpdateBlock,
    "LevelSoundEventV1": PacketId.LevelSoundEventV1,
    "LevelEvent": PacketId.LevelEvent,
    "BlockEvent": PacketId.BlockEvent,
    "ActorEvent": PacketId.ActorEvent,
    "MobEffect": PacketId.MobEffect,
    "UpdateAttributes": PacketId.UpdateAttributes,
    "InventoryContent": PacketId.InventoryContent,
    "InventoryTransaction": PacketId.InventoryTransaction,
    "MobArmorEquipment": PacketId.MobArmorEquipment,
    "MobEquipment": PacketId.MobEquipment,
    "Interact": PacketId.Interact,
    "PlayerAction": PacketId.PlayerAction,
    "SetActorData": PacketId.SetActorData,
    "SetActorMotion": PacketId.SetActorMotion,
    "SetActorLink": PacketId.SetActorLink,
    "SetHealth": PacketId.SetHealth,
    "SetSpawnPosition": PacketId.SetSpawnPosition,
    "Animate": PacketId.Animate,
    "Respawn": PacketId.Respawn,
    "ContainerOpen": PacketId.ContainerOpen,
    "ContainerClose": PacketId.ContainerClose,
    "PlayerHotbar": PacketId.PlayerHotbar,
    "InventoryAction": PacketId.InventoryAction,
    "AdventureSettings": PacketId.AdventureSettings,
    "BlockEntityData": PacketId.BlockEntityData,
    "PlayerInput": PacketId.PlayerInput,
    "LevelChunkData": PacketId.LevelChunkData,
    "SetDifficulty": PacketId.SetDifficulty,
    "ChangeDimension": PacketId.ChangeDimension,
    "SetPlayerGameType": PacketId.SetPlayerGameType,
    "PlayerList": PacketId.PlayerList,
    "Event": PacketId.Event,
    "RequestChunkRadius": PacketId.RequestChunkRadius,
    "ChunkRadiusUpdated": PacketId.ChunkRadiusUpdated,
    "GameRulesChanged": PacketId.GameRulesChanged,
    "BossEvent": PacketId.BossEvent,
    "AvailableCommands": PacketId.AvailableCommands,
    "CommandRequest": PacketId.CommandRequest,
    "UpdateTrade": PacketId.UpdateTrade,
    "UpdateEquip": PacketId.UpdateEquip,
    "Transfer": PacketId.Transfer,
    "PlaySound": PacketId.PlaySound,
    "SetTitle": PacketId.SetTitle,
    "PlayerSkin": PacketId.PlayerSkin,
    "ModalFormRequest": PacketId.ModalFormRequest,
    "ModalFormResponse": PacketId.ModalFormResponse,
    "ServerSettingsRequest": PacketId.ServerSettingsRequest,
    "ServerSettingsResponse": PacketId.ServerSettingsResponse,
    "SetLocalPlayerAsInitialized": PacketId.SetLocalPlayerAsInitialized,
    "NetworkStackLatency": PacketId.NetworkStackLatency,
    "SpawnParticleEffect": PacketId.SpawnParticleEffect,
    "LevelSoundEventV2": PacketId.LevelSoundEventV2,
    "NetworkChunkPublisherUpdate": PacketId.NetworkChunkPublisherUpdate,
    "BiomeDefinitionList": PacketId.BiomeDefinitionList,
    "LevelSoundEvent": PacketId.LevelSoundEvent,
    "LecternUpdate": PacketId.LecternUpdate,
    "ClientCacheStatus": PacketId.ClientCacheStatus,
    "MoveActorDelta": PacketId.MoveActorDelta,
    "SetScoreboardIdentity": PacketId.SetScoreboardIdentity,
    "Emote": PacketId.Emote,
    "AnimateEntity": PacketId.AnimateEntity,
    "CameraShake": PacketId.CameraShake,
    "DebugRenderer": PacketId.DebugRenderer,
}


def _build_reverse_lookup() -> dict:
    """Build an ``{id: name}`` mapping from :data:`PacketName`."""
    lookup = {}
    seen = {}
    for name, pid in PacketName.items():
        # Keep the first registered name for any given id.
        if pid not in seen:
            seen[pid] = name
            lookup[pid] = name
    return lookup


#: ``{packet_id: name}`` dictionary, handy for debug logging.
PACKET_ID_TO_NAME: dict = _build_reverse_lookup()
