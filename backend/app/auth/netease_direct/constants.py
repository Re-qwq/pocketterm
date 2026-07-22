"""网易直连认证常量 - 加密密钥、服务器URL、设备指纹。

所有数据来源于 Login.Core.dll 反编译分析,用于直连网易服务器完成认证。
"""

# ===========================================================================
# 版本管理 (网易升级时只需修改这里)
# ===========================================================================
# 升级指南:
#   1. 修改 ENGINE_VERSION 为新版本号 (如 "3.9.x.xxxxxx")
#   2. 修改 GAME_VERSION 为对应的 Bedrock 版本 (如 "1.21.xx.0")
#   3. 如果协议版本号变化,同步修改 constants/minecraft.py 的 ProtocolVersion
#   4. 如果 SDK 版本变化,修改 SDK_VERSION_PC / SDK_VERSION_PE
#   5. 运行测试验证认证流程
#
# 版本来源:
#   - ENGINE_VERSION: TapTap/官网最新版本号 (如 3.8.25.293531)
#   - GAME_VERSION: Minecraft Bedrock 对应版本 (3.8→1.21.90, 3.9→待确认)
#   - SDK_VERSION: 网易 SDK 版本号 (x19=PC, g79=PE)
# ===========================================================================

# ---------------------------------------------------------------------------
# 服务器 URL
# ---------------------------------------------------------------------------
COREOBT_PC = "https://x19obtcore.nie.netease.com:8443"
APIGATEWAYOBT_PC = "https://x19apigatewayobt.nie.netease.com"
APIGATEWAYOBT_PE = "https://g79apigatewayobt.minecraft.cn"
COREOBT_PE = "https://g79obtcore.minecraft.cn:8443"

# 认证服务器 (用于获取 chainInfo)
AUTH_SERVER = "https://g79authobt.nie.netease.com"

# ---------------------------------------------------------------------------
# 加密密钥 (3套,对应不同协议版本)
# ---------------------------------------------------------------------------

# _keys: PC 版 AES-128-CBC 密钥 (16字节)
KEYS = [
    "MK6mipwmOUedplb6",  # 0
    "OtEylfId6dyhrfdn",  # 1
    "VNbhn5mvUaQaeOo9",  # 2
    "bIEoQGQYjKd02U0J",  # 3
    "fuaJrPwaH2cfXXLP",  # 4
    "LEkdyiroouKQ4XN1",  # 5
    "jM1h27H4UROu427W",  # 6
    "DhReQada7gZybTDk",  # 7
    "ZGXfpSTYUvcdKqdY",  # 8
    "AZwKf7MWZrJpGR5W",  # 9
    "amuvbcHw38TcSyPU",  # 10
    "SI4QotspbjhyFdT0",  # 11
    "VP4dhjKnDGlSJtbB",  # 12
    "UXDZx4KhZywQ2tcn",  # 13
    "NIK73ZNvNqzva4kd",  # 14
    "WeiW7qU766Q1YQZI",  # 15
]

# _keys_g79v3: g79 v3 版密钥
KEYS_G79V3 = [
    "75yWE1DMlhP6JZre",  # 0
    "NtDdtr7zaCO7MGqK",  # 1
    "5P3gbvwC2x2qVsXK",  # 2
    "Qgg0y2foklzV8W2P",  # 3
    "ItCyfnGMte15pFXe",  # 4
    "bp8UGVtOcS4Cc0VS",  # 5
    "ZRoxt2LItMBL2Rko",  # 6
    "EyVV2FUOWSU3pfEE",  # 7
    "L9molWm6kVuE6c6m",  # 8
    "oPDdpwvjN2YgZzE8",  # 9
    "K5rvy5Jb2S1J4SpX",  # 10
    "IYDhVUqFPlVjA7to",  # 11
    "LCR32BrjIVqkaYbS",  # 12
    "RWAss9Mri8bThLgF",  # 13
    "cdxDfuavFR1Frds5",  # 14
    "euKUQqtpUkUKF5aY",  # 15
]

# _keys_g79v12: g79 v12 版密钥 (32字符hex字符串, HexToBytes解码为16字节, AES-128)
KEYS_G79V12 = [
    "60F1E0D1FD635362430747215CF1C2FF",
    "EA5B62D27D0338374852C4B9469D7AC6",
    "17238D55501C5F020B155FB3303591E6",
    "8C5CEAE0F443E006A050266F73ADD5B0",
    "1C02CE22FB22F0E72060217418F351F3",
    "9A01773FEBB0CFE0EBDBF37F4D23C27F",
    "43F32300BF252CC320E2572ACE766367",
    "07F161011B3101F1ED0301735631E734",
    "0454E7707A5F37565601E100406060AF",
    "647554BAD3100C43C16660F002CC10F3",
    "E157213170F842382032564265B0B043",
    "914FC59311B04151393EF6896A847636",
    "0710C0205D224237025323265C145FA1",
    "054E6F01165267025C3111F562A921E9",
    "722D1789E792E2CA0D5322211FD0F5AE",
    "91F7C751FCF671F34943430772341799",
]

# ---------------------------------------------------------------------------
# 版本信息 (网易升级时修改这里)
# ---------------------------------------------------------------------------
# 当前版本: 3.8 (BE 1.21.90, 2026年5月)
# 来源: TapTap 最新版本 3.8.25.293531, 网易开发者文档 V3.8
# 网易3.9版本预计 2026年7月24日更新,届时需要修改以下常量

# 网易引擎版本 (用于 auth_entity.version.version 和 SA_DATA_PE.app_ver)
ENGINE_VERSION = "3.8.25.293531"
PATCH_VERSION = "3.8.25.293531"
LIB_MINECRAFT_PE = "de9c85e47c5bb586f689d813d45b12a7"  # 旧版哈希, sign 为空时不影响
PATCH_HASH = "ba0dc911f785bc1026631b906070b6db2b3e7ca013bb30a74d822579860c042b"  # 旧版哈希

# Minecraft Bedrock 游戏版本 (用于 Login 包的 protocol_version 字段)
# 注意: 此值应与 constants/minecraft.py 的 GameVersion 保持一致
GAME_VERSION = "1.21.90.0"

# SDK 版本号 (用于 sauth_json 的 sdk_version 字段)
# PC (x19) 模式使用, 来源: Drug.NetEase x19Auth.cs
SDK_VERSION_PC = "3.4.0"
# PE (g79) 模式使用, 来源: SA_DATA_PE.sdk_ver
SDK_VERSION_PE = "5.2.0"

# PC 启动器版本 (WpfVersion), 用于 sa_data.app_ver 和 auth_entity.version.version
# 来源: Drug.NetEase x19Auth.cs WpfVersion = "1.10.7.22905"
# 服务器接受此版本号, 不要改为 Minecraft 游戏版本
LAUNCHER_VERSION = "1.10.7.22905"
USER_AGENT = "WPFLauncher/0.0.0.0"

# PESignCount 参数
OFFSET = 2
ROUNDS = 9

# ComputeDynamicToken 盐值
DYNAMIC_TOKEN_SALT = "0eGsBkhl"

# ---------------------------------------------------------------------------
# 设备指纹 (PC 版) - 基础模板
# ---------------------------------------------------------------------------
# 注意: 实际使用时应调用 generate_sa_data_pc() 为每个账号生成独立指纹
# 所有账号共用同一设备指纹会被反作弊系统识别为机器人农场

SA_DATA_PC_TEMPLATE = (
    '{"os_name":"windows","os_ver":"Microsoft Windows 10 专业版",'
    '"mac_addr":"{mac_addr}","udid":"{udid}",'
    '"app_ver":"1.10.7.22905","sdk_ver":"","network":"",'
    '"disk":"{disk}","is64bit":"1",'
    '"video_card1":"{video_card}","video_card2":"",'
    '"video_card3":"","video_card4":"",'
    '"launcher_type":"PC_java","pay_channel":"netease",'
    '"dotnet_ver":"4.8.0","cpu_type":"{cpu_type}",'
    '"ram_size":"{ram_size}","device_width":"{width}",'
    '"device_height":"{height}","os_detail":"10"}'
)

# 默认设备指纹 (仅作为后备,不推荐多账号共用)
SA_DATA_PC = (
    '{"os_name":"windows","os_ver":"Microsoft Windows 10 专业版",'
    '"mac_addr":"B8975A4AD6166","udid":"BFEBFBFF0006A9C78C00D8",'
    '"app_ver":"1.10.7.22905","sdk_ver":"","network":"",'
    '"disk":"C78C00D8","is64bit":"1",'
    '"video_card1":"Video_card1","video_card2":"",'
    '"video_card3":"","video_card4":"",'
    '"launcher_type":"PC_java","pay_channel":"netease",'
    '"dotnet_ver":"4.8.0","cpu_type":"Intel(R) Xeon(R) CPU i32100 3.10GHz",'
    '"ram_size":"8555332736","device_width":"1920",'
    '"device_height":"1080","os_detail":"10"}'
)


def generate_sa_data_pc(seed: str = "") -> str:
    """为每个账号生成独立的 PC 设备指纹。

    基于种子(通常是 sdkuid)生成确定性的设备指纹,
    确保同一账号每次生成相同指纹,不同账号生成不同指纹。

    Args:
        seed: 种子字符串 (通常是 sdkuid 或 udid)

    Returns:
        SA_DATA JSON 字符串
    """
    import hashlib
    import random as _random_module

    # P1 修复: 使用独立的 Random 实例, 不修改全局 random 模块状态
    # 之前调用 random.seed() 会影响全局随机数生成器,
    # 在多账号并发生成指纹时会导致指纹碰撞
    rng = _random_module.Random()
    if seed:
        h = hashlib.md5(seed.encode("utf-8")).hexdigest()
        rng.seed(int(h[:8], 16))
    # else: 不显式 seed, Random() 默认用系统熵

    def rand_mac() -> str:
        """生成随机 MAC 地址 (无分隔符)"""
        return "".join(f"{rng.randint(0x00, 0xFF):02X}" for _ in range(6))

    def rand_disk() -> str:
        """生成随机磁盘序列号 (8位hex)"""
        return f"{rng.randint(0x10000000, 0xFFFFFFFF):08X}"

    def rand_udid() -> str:
        """生成随机 UDID (CPUID + 磁盘序列号格式)"""
        cpuid = f"{rng.randint(0x00000000, 0xFFFFFFFF):08X}"
        disk = f"{rng.randint(0x00000000, 0xFFFFFFFF):08X}"
        return f"{cpuid}{disk}"

    # CPU 型号池
    cpu_models = [
        "Intel(R) Core(TM) i5-10400 CPU @ 2.90GHz",
        "Intel(R) Core(TM) i7-10700 CPU @ 2.90GHz",
        "Intel(R) Core(TM) i5-10400F CPU @ 2.90GHz",
        "Intel(R) Core(TM) i7-9700 CPU @ 3.00GHz",
        "Intel(R) Core(TM) i5-9400 CPU @ 2.90GHz",
        "AMD Ryzen 5 3600 6-Core Processor",
        "AMD Ryzen 7 3700X 8-Core Processor",
        "AMD Ryzen 5 5600X 6-Core Processor",
        "AMD Ryzen 7 5800X 8-Core Processor",
        "Intel(R) Core(TM) i5-12400 CPU @ 2.50GHz",
        "Intel(R) Core(TM) i7-11700 CPU @ 2.50GHz",
        "AMD Ryzen 5 7600X 6-Core Processor",
    ]

    # 显卡型号池
    gpu_models = [
        "NVIDIA GeForce GTX 1660",
        "NVIDIA GeForce RTX 2060",
        "NVIDIA GeForce RTX 3060",
        "NVIDIA GeForce GTX 1050 Ti",
        "NVIDIA GeForce RTX 3050",
        "AMD Radeon RX 580",
        "AMD Radeon RX 5600 XT",
        "Intel(R) UHD Graphics 630",
        "NVIDIA GeForce RTX 4060",
        "AMD Radeon RX 6600",
    ]

    # 分辨率池
    resolutions = [
        ("1920", "1080"),
        ("2560", "1440"),
        ("1366", "768"),
        ("1920", "1200"),
        ("1680", "1050"),
    ]

    mac_addr = rand_mac()
    disk = rand_disk()
    udid = rand_udid()
    cpu_type = rng.choice(cpu_models)
    video_card = rng.choice(gpu_models)
    ram_size = str(rng.randint(4000000000, 17000000000))
    width, height = rng.choice(resolutions)

    return (
        '{"os_name":"windows","os_ver":"Microsoft Windows 10 专业版",'
        f'"mac_addr":"{mac_addr}","udid":"{udid}",'
        '"app_ver":"1.10.7.22905","sdk_ver":"","network":"",'
        f'"disk":"{disk}","is64bit":"1",'
        f'"video_card1":"{video_card}","video_card2":"",'
        '"video_card3":"","video_card4":"",'
        '"launcher_type":"PC_java","pay_channel":"netease",'
        f'"dotnet_ver":"4.8.0","cpu_type":"{cpu_type}",'
        f'"ram_size":"{ram_size}","device_width":"{width}",'
        f'"device_height":"{height}","os_detail":"10"}}'
    )


# 设备指纹 (PE/手机版) - app_ver 引用 ENGINE_VERSION,升级时自动同步
SA_DATA_PE = (
    '{"app_channel":"netease","app_ver":"' + ENGINE_VERSION + '",'
    '"core_num":"8","cpu_digit":"64","cpu_hz":"1882000",'
    '"cpu_name":"vendor Kirin810","device_height":"2000",'
    '"device_model":"HUAWEI BAH3-W09","device_width":"1200",'
    '"disk":"","emulator":0,"first_udid":"11ff2c22e0b4b5a6",'
    '"is_guest":0,"launcher_type":"PE_C++",'
    '"mac_addr":"02:00:00:00:00:00","network":"CHANNEL_UNKNOW",'
    '"os_name":"android","os_ver":"7.1.2",'
    '"ram":"6130167808","rom":"114965872640",'
    '"root":false,"sdk_ver":"' + SDK_VERSION_PE + '","start_type":"default",'
    '"udid":"11ff2c22e0b4b5a6"}'
)
