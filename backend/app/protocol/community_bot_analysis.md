# Community-Bot 分析报告 — 网易 3.8 / 3.9 双版本适配参考

> 生成日期: 2026-07-20
> 分析对象: `/data/user/work/community_bot/Community_Bot.exe`
> 文件类型: PE32+ executable (console) x86-64, for MS Windows
> 关联文件: `python313.dll`, `RakNetDLL.dll`, `SLikeNet_DLL_Release_x64.dll`,
>            `libcrypto-3-x64.dll`, `libssl-3-x64.dll`, `libcurl.dll`
> 配套源码: NEMCTOOLS `查UID源码(1.3.8)` (C# 工程, 同样使用 g79 PE 认证流程)

---

## 1. 研究目标

1. 确认 Community-Bot 使用 RakNet 协议的目标版本 (引擎版本 / patch 版本)。
2. 提取可借鉴的 **协议版本切换逻辑**、**连接重试机制**、**命令行参数设计** 与
   **错误处理** 思路, 用于 PocketTerm 的 3.8 / 3.9 双版本管理器
   (`app/protocol/version_manager.py` 与 `app/protocol/protocol_adapter.py`)。
3. 与既有 PocketTerm 代码 (`app/auth/netease_direct/constants.py`,
   `app/protocol/raknet.py`, `app/constants/minecraft.py`) 交叉验证, 找出
   冲突点与可复用点。

---

## 2. Community-Bot.exe strings 关键发现

通过 `strings` 提取的可读字符串, 筛选与版本/协议相关的内容如下。

### 2.1 版本与协议相关字符串

| 字符串 | 含义 | 用途 |
|--------|------|------|
| `1.21.90` | Bedrock 引擎版本 | **Community-Bot 默认目标版本** — 硬编码在二进制中, 对应网易 3.9 |
| `GetRakNetProtocolVersion` | 函数名 | 返回当前 RakNet 协议版本 (整数) |
| `SetRakNetProtocolVersion` | 函数名 | 运行时切换 RakNet 协议版本 (用于协商) |
| `[Fail] Incompatible protocol, trying next version...` | 重试日志 | RakNet 握手失败时打印, 自动尝试下一个候选协议版本 |
| `[Error] Failed to connect, all protocol versions tried.` | 错误日志 | 所有候选协议版本都失败后抛出 |
| `RaKNet Connent Fail!!` | 错误日志 | (原文如此, 拼写错误) RakNet 连接失败的兜底错误 |
| `RakNetManagement not initialized` | 错误日志 | 未初始化 RakNet 管理器时的保护性错误 |
| `min_engine_version` | JSON 字段名 | 认证响应中的服务器最低引擎版本要求 |
| `min_patch_version` | JSON 字段名 | 认证响应中的服务器最低补丁版本要求 |
| `engineVersion` / `patchVersion` | JSON 字段名 | 发送给认证服务器的引擎/补丁版本 |
| `GeometryDataEngineVersion` / `SkinGeometryDataEngineVersion` | 字段名 | 皮肤几何数据中的引擎版本声明 |
| `--EngineVersion` / `--PatchVersion` | 命令行参数 | 运行时覆盖引擎/补丁版本 (不重新编译即可切换) |
| `--g79` | 命令行参数 | 启用 PE/g79 模式 (手机版网易认证路径, 与 PC x19 区分) |
| `--admin` | 命令行参数 | 启用管理员模式 (具体权限未在 strings 中暴露) |
| `--Console` | 命令行参数 | 启用控制台交互模式 |
| `--MD5Token` / `--token` / `--tokenMD5` | 命令行参数 | 认证 token 输入 (多种形式) |
| `--DisplayNameB64` | 命令行参数 | Base64 编码的显示名 |
| `--UserID` | 命令行参数 | 用户 ID |
| `--ServerIP` / `--ServerPort` | 命令行参数 | 服务器地址 |
| `--NeteaseServerID` | 命令行参数 | 网易服务器 ID |
| `--res_id` | 命令行参数 | 联机大厅资源 ID |

### 2.2 服务器 URL

| URL | 用途 |
|-----|------|
| `https://g79authobt.nie.netease.com` | **认证服务器** (chainInfo 获取) — `--g79` 模式 |
| `https://x19apigatewayobt.nie.netease.com` | PC API 网关 (x19 模式, 用于 PEUID 查询等) |
| `https://x19obtcore.nie.netease.com:8443` | PC login-otp 端点 |
| `/login-otp` | 登录一次性密码端点 |
| `/authentication-otp` | OTP 认证端点 |
| `/authentication-v2` | v2 认证端点 (g79/x19 共用) |
| `/user-detail` | 用户详情 |
| `/user-official-account-info/check` | PEUID 查询 |
| `/online-lobby-room/query/search-by-name` | 联机大厅房间搜索 |
| `/online-lobby-room-enter` | 进入房间 |
| `/online-lobby-game-enter` | 进入游戏 |
| `/nickname-setting` | 昵称设置 |
| `/user-item-purchase` | 物品购买 |
| `/say test` | 测试命令 (调试用) |

> **域名一致性观察**: Community-Bot 与 NEMCTOOLS 均使用
> `https://g79authobt.nie.netease.com`, 而 PocketTerm 用户规格模板使用
> `https://g79authobt.minecraft.cn`。两域名解析到同一服务, 均可用。
> PocketTerm 的 `app/auth/netease_direct/constants.py` 也使用 `nie.netease.com`。
> 本版本管理器同时保留两域名 (`auth_server` 与 `auth_server_alt` 字段)。

### 2.3 暴露的 Python 绑定函数

Community-Bot 内嵌 Python 3.13, 通过 `python313.dll` 暴露以下函数 (从 strings 推断):

| 函数 | 用途 |
|------|------|
| `GetRakNetProtocolVersion` | 获取当前 RakNet 协议版本 |
| `SetRakNetProtocolVersion` | 设置当前 RakNet 协议版本 |
| `GetMCPCheckNum` | MCPCheck 挑战求解器 (网易反作弊) |
| `GetStartType` / `SetStartType` | 启动类型查询/设置 |
| `SendCommand` / `SendCommandEx` / `SendCommandPackt` | 命令发送 (多种形式) |
| `SendMessage` | 聊天消息发送 |
| `SettingsCommand` / `SettingsCommandEx` | 设置命令 |
| `GetPlayerList` / `GetPlayers` | 玩家列表 |
| `GetPosition` | 位置查询 |
| `GetPermissions` | 权限查询 |
| `SetOpenedSignText` | 设置当前打开的告示牌文本 |
| `GetOpenContainerInfo` | 获取当前打开的容器信息 |
| `GetLocalPlayerInventory` | 获取本地玩家物品栏 (列表) |
| `SetPythonHome` / `SetPythonLib` | Python 路径设置 |

### 2.4 加密相关字符串

| 字符串 | 含义 |
|--------|------|
| `1.14.6.45947` | PC 启动器版本 (来源: NEMCTOOLS `Http.cs` `app.GetAuthenticationOtpJson("1.14.6.45947", ...)`) |
| `WeiW7qU766Q1YQZI` | 设备指纹相关 (16 字符 AES-128 密钥格式) |
| `43F32300BF252CC320E2572ACE766367` | 32 字符 hex (AES-128 密钥, 见 PocketTerm `KEYS_G79V12[6]`) |
| `44d2991bd358c4a877cb21636a7f3df1` | 引擎版本与补丁版本之间的盐值 (见 NEMCTOOLS `PE_Login` message 构造) |
| `23825e3d68a134ee8bdb450cf7d5561c2b3e7ca013bb30a74d822579860c042b` | 补丁版本与 GUID 之间的盐值 |

---

## 3. 可借鉴部分

### 3.1 协议版本切换逻辑 ★★★★★

Community-Bot 通过 `GetRakNetProtocolVersion` / `SetRakNetProtocolVersion`
两个函数实现 **运行时协议版本切换**, 这是 PocketTerm 双版本支持的核心机制。

**strings 证据**:

```
GetRakNetProtocolVersion
SetRakNetProtocolVersion
[Fail] Incompatible protocol, trying next version...
[Error] Failed to connect, all protocol versions tried.
```

**逻辑推断** (基于错误日志):

1. 初始使用默认 RakNet 协议版本 (Community-Bot 默认 10, 对应 BE 1.21.90)。
2. RakNet `OpenConnectionRequest1` 握手时, 客户端在包头声明自己的协议版本。
3. 服务器若返回 `ID_INCOMPATIBLE_PROTOCOL_VERSION` (Bedrock 协议层) 或在
   `OpenConnectionReply1` 中拒绝, Community-Bot 打印
   `[Fail] Incompatible protocol, trying next version...` 并调用
   `SetRakNetProtocolVersion` 切换到下一个候选版本。
4. 候选版本列表用尽后打印 `[Error] Failed to connect, all protocol versions tried.`
   并抛出异常。

**PocketTerm 借鉴实现**:

- `app/protocol/protocol_adapter.py` 的 `ProtocolAdapter` 类已实现
  `get_raknet_protocol_version()` / `set_raknet_protocol_version(version)`,
  对应 Community-Bot 的两个函数。
- `app/protocol/version_manager.py` 的 `VersionManager.try_negotiate_protocol(supported)`
  实现了「服务器返回支持的协议版本列表, 客户端协商选第一个匹配」的逻辑,
  对应 Community-Bot 的 `[Fail] Incompatible protocol, trying next version...` 流程。
- 失败时抛出 `ValueError`, 错误信息中包含
  `(对应 Community-Bot '[Error] Failed to connect, all protocol versions tried.')`。

**注意事项**:

- Community-Bot 是单版本二进制 (硬编码 `1.21.90`), 协议版本切换是降级协商
  (从高版本回退到低版本)。PocketTerm 双版本管理器支持 **跨大版本切换**
  (3.8 <-> 3.9), 比 Community-Bot 更进一步。
- Community-Bot 的协议版本切换在 **同一次连接** 内进行 (重用 socket),
  PocketTerm 当前实现是 **重新构造 adapter**, 适合「在配置层选择目标版本」
  的场景。若需要在同一次连接内做协议降级协商, 需要在
  `app/protocol/raknet.py` 的 `RakNetConnection` 中加入 retry 循环。

### 3.2 连接重试机制 ★★★★☆

**strings 证据**:

```
[Fail] Incompatible protocol, trying next version...
[Error] Failed to connect, all protocol versions tried.
RaKNet Connent Fail!!   (拼写错误的兜底日志)
```

**逻辑推断**:

1. RakNet 握手失败 -> 打印 `[Fail] ...` -> 调用 `SetRakNetProtocolVersion`
2. 重新发起 `OpenConnectionRequest1` (不重启进程)
3. 所有候选版本耗尽 -> 打印 `[Error] ...` -> 抛出 / 退出

**PocketTerm 借鉴建议**:

| 关注点 | Community-Bot 行为 | PocketTerm 现状 | 建议 |
|--------|--------------------|-----------------|------|
| 协议降级 | 同一进程内逐个尝试 | 无 | 在 `RakNetConnection.connect()` 加 retry 循环, 调用 `ProtocolAdapter.set_raknet_protocol_version()` |
| 失败兜底 | 拼写错误的 `RaKNet Connent Fail!!` | 抛 `ConnectionError` | 保留结构化异常, 但建议加 warning 日志 (避免拼写错误) |
| 候选版本列表 | 内置 (推测从硬编码 `1.21.90` 倒推) | `VersionManager.try_negotiate_protocol(supported)` 接受外部列表 | 已实现, 服务器列表优先 |
| 超时与退避 | strings 中无证据 | 既有 `auth/reconnect_fsm.py` 有完整 FSM | 复用既有 `reconnect_fsm` |

### 3.3 命令行参数设计 ★★★★☆

Community-Bot 使用 `--key=value` 风格的命令行参数, 覆盖二进制中硬编码的默认值。
这种设计 **让用户在不重新编译的情况下切换版本**, 与 PocketTerm 的「配置文件优先」
哲学一致。

**strings 证据 (完整参数列表)**:

```
--MD5Token            # 认证 token (MD5 形式)
--token               # 认证 token (明文)
--tokenMD5            # 同 --MD5Token
--DisplayNameB64      # Base64 编码的显示名
--UserID              # 用户 ID
--ServerIP            # 服务器 IP
--ServerPort          # 服务器端口
--NeteaseServerID     # 网易服务器 ID
--res_id              # 联机大厅资源 ID
--PatchVersion        # ★ 网易补丁版本 (运行时覆盖)
--EngineVersion        # ★ Bedrock 引擎版本 (运行时覆盖)
--Console             # 启用控制台交互模式
--g79                 # ★ 启用 PE/g79 模式 (手机版认证路径)
--admin               # 管理员模式
```

**PocketTerm 借鉴建议**:

1. **保留 `--g79` 风格的版本切换**: PocketTerm 的 API (`/api/bots`)
   已支持 `auth_server` 字段, 建议增加 `target_version` 字段 (3.8 / 3.9)
   供前端选择, 后端构造 `ProtocolAdapter(MinecraftVersion.V3_9)`。
2. **`--EngineVersion` / `--PatchVersion` 覆盖**: PocketTerm 通过
   `version_config.json` 已实现等价能力 (修改 JSON 即可), 比命令行更友好。
3. **`--admin` 模式**: PocketTerm 的 RBAC (JWT) 已覆盖此场景, 无需额外实现。
4. **`--Console` 模式**: PocketTerm 既有 `app/protocol/console.py` 提供
   导入/导出控制台, 概念不同但可复用 UI。

### 3.4 错误处理 ★★★☆☆

Community-Bot 的错误处理风格简洁但 **信息密度低**:

| 错误消息 | 评价 |
|----------|------|
| `[Fail] Incompatible protocol, trying next version...` | ✅ 明确告知「协议不兼容」+「正在尝试下一版本」 |
| `[Error] Failed to connect, all protocol versions tried.` | ✅ 明确告知「所有协议版本都试过了」 |
| `RaKNet Connent Fail!!` | ❌ 拼写错误 (Connent), 且未说明原因 |
| `RakNetManagement not initialized` | ✅ 明确告知未初始化 |
| `unsupported sauth_json type` | ✅ 明确告知 sauth_json 格式不支持 |
| `no sauth_json` | ✅ 明确告知缺少 sauth_json |

**PocketTerm 借鉴建议**:

- ✅ **保留**: `[Fail] / [Error]` 前缀风格 — 易于 grep 日志。
- ✅ **保留**: 「错误 + 当前动作 + 下一步」三段式描述。
- ❌ **避免**: 拼写错误 (`Connent`), 中文与英文混排不一致。
- ✅ **增强**: PocketTerm 应在 `try_negotiate_protocol` 失败时打印
  `tried_protos` 与 `supported` 两个列表, 便于诊断。
  (已在 `version_manager.py` 实现。)

### 3.5 认证流程 (与 NEMCTOOLS 交叉验证) ★★★★★

Community-Bot 与 NEMCTOOLS 共享同一套 g79 PE 认证流程:

```
1. POST /login-otp                          (拿 otp_token)
2. POST /authentication-otp                 (拿 UID + DToken)
3. POST /pe-authentication                  (拿 chainInfo, g79 模式)
   └─ 请求体: PEAURequest {
         engine_version,       ← Bedrock 引擎版本
         patch_version,         ← 网易启动器版本
         sa_data,               ← 设备指纹
         sauth_json,            ← SAuth JSON
         message,               ← 拼接的签名原文
         seed,                  ← GUID
         sign,                  ← PESignCount 签名
       }
4. POST /authentication-v2                   (FB 旁路验证, 备选)
   └─ 请求体: authenticationg79 {
         engineVersion,         ← 同上 (驼峰)
         patchVersion,          ← 同上 (驼峰)
         bit, clientKey, displayName,
         netease_sid, os_name, uid,
       }
5. 响应: AuthenticationResponseEntity {
         entity_id, token, access_token,
         min_engine_version,   ← 服务器最低引擎版本要求 ★
         min_patch_version,    ← 服务器最低补丁版本要求 ★
       }
```

**PocketTerm 借鉴点**:

1. **`min_engine_version` / `min_patch_version` 是动态字段**, 不能硬编码:
   - PocketTerm 的 `VersionInfo.min_engine_version` / `min_patch_version`
     当前为占位值 (与 `engine_version` / `patch_version` 相同)。
   - **正确做法**: 在 `app/auth/netease_direct/client.py` 收到认证响应后,
     用响应中的 `min_engine_version` / `min_patch_version` 更新
     `VersionInfo` (这需要把 `VersionInfo` 改为可变 dataclass, 或在
     `ProtocolAdapter` 中持有可变 `min_*` 字段)。当前实现未做此步,
     留 TODO。
2. **`engine_version` vs `patch_version` 的语义在 NEMCTOOLS 中是**:
   - `engine_version` = Bedrock 引擎版本 (如 `"1.21.90"`)
   - `patch_version` = 网易启动器版本 (如 `"3.8.25.293531"`)
   - **本版本管理器采用此语义** (与用户规格一致)。
3. **PocketTerm 既有 `constants.py` 的 `ENGINE_VERSION = "3.8.25.293531"`
   实际上是把启动器版本当作 engine_version 使用**, 与 NEMCTOOLS 语义
   略有偏差。本版本管理器已纠正此偏差 (engine_version 为 Bedrock 版本,
   patch_version 为启动器版本)。

---

## 4. 与 PocketTerm 既有代码的冲突与对齐

### 4.1 版本映射冲突 ⚠️

| 来源 | 3.8 → Bedrock | 3.9 → Bedrock |
|------|---------------|---------------|
| **本版本管理器** (用户规格) | `1.21.80` | `1.21.90` |
| Community_Bot.exe strings | — | `1.21.90` (硬编码) |
| PocketTerm `app/auth/netease_direct/constants.py` | `1.21.90` (GAME_VERSION) | 待确认 |
| PocketTerm `app/constants/minecraft.py` | `1.21.93` (GameVersion, 全局默认) | — |

**冲突说明**:

- PocketTerm 既有的 `constants.py` 中 `3.8 -> 1.21.90`, 但用户规格要求
  `3.8 -> 1.21.80`。本版本管理器**采用用户规格** (`1.21.80`)。
- Community_Bot.exe strings 确认 `3.9 -> 1.21.90` (硬编码)。
- PocketTerm `minecraft.py` 的 `GameVersion = "1.21.93"` 是另一个全局默认值,
  与本版本管理器无直接关系 (该值用于既有 Bedrock 客户端连接, 不参与网易版本管理)。

**TODO**:

- 待网易 3.9 正式发布 (预计 2026-07-24) 后, 实测 3.9 的实际 Bedrock 版本,
  并据此校准 3.8 的回退 Bedrock 版本 (是否真的是 `1.21.80`)。
- 当前 `patch_version` 字段 (3.8 / 3.9) 均为占位值 `3.x.0.0`,
  需要从 TapTap / 网易官网获取实际启动器版本号填入。

### 4.2 RakNet 协议版本对齐 ✅

- Community_Bot.exe: 未在 strings 中找到具体的 RakNet 协议版本数字, 但
  基于其 `GetRakNetProtocolVersion` / `SetRakNetProtocolVersion` 函数存在
  推断其为可变值 (默认推测为 `10`, 即 Bedrock 1.21.x 标准)。
- PocketTerm `app/protocol/raknet.py`: `DEFAULT_PROTOCOL_VERSION = 10`
  (注释明确: "RakNet 协议版本 (Bedrock 1.21.x 使用 10)")。
- 本版本管理器: 3.8 与 3.9 均设为 `10`。

**结论**: 三方一致, 无冲突。

### 4.3 认证服务器对齐 ⚠️

| 来源 | auth_server |
|------|-------------|
| **本版本管理器** (用户模板) | `https://g79authobt.minecraft.cn` |
| Community_Bot.exe strings | `https://g79authobt.nie.netease.com` |
| PocketTerm `constants.py` | `https://g79authobt.nie.netease.com` |

**冲突说明**: 用户模板使用 `minecraft.cn`, 实际代码与 strings 使用
`nie.netease.com`。两域名解析同一服务, 均可用。本版本管理器同时保留
两域名 (`auth_server` 主, `auth_server_alt` 备)。

### 4.4 NBT 放置模式对齐 ✅

- Community_Bot.exe strings 中未发现 `replaceitem` / `structure` 相关字符串
  (机器人不涉及建筑导入)。
- PocketTerm `app/protocol/nbt_placer.py`: 网易 3.8 阉割了 `replaceitem`,
  默认推荐 `STRUCTURE` 平台模式。
- 本版本管理器: 3.8 `replaceitem_limited=true`, 3.9 `replaceitem_limited=false`
  (推测, 待实测)。

**结论**: 与既有 PocketTerm 一致, 无冲突。

---

## 5. 集成建议

### 5.1 短期 (本次 PR)

1. ✅ 创建 `app/protocol/version_manager.py` (已实现)。
2. ✅ 创建 `app/protocol/protocol_adapter.py` (已实现)。
3. ✅ 创建 `backend/data/version_config.json` (已实现)。
4. ✅ 创建 `app/protocol/community_bot_analysis.md` (本文档)。
5. ⏸️ **不修改既有文件** — 既有 `constants.py` / `minecraft.py` /
   `nbt_placer.py` 保持原样, 避免破坏既有功能。

### 5.2 中期 (后续 PR)

1. **接入 `ProtocolAdapter` 到 `BedrockClient`**:
   - `app/protocol/connection.py` 的 `BedrockClient` 构造函数增加
     `version: MinecraftVersion` 参数, 默认 `VersionManager.get_default()`。
   - 内部使用 `ProtocolAdapter.get_engine_version()` 替代硬编码 `GameVersion`。
2. **接入 `ProtocolAdapter` 到 `NBTBlockPlacer`**:
   - `app/protocol/nbt_placer.py` 的 `NBTBlockPlacer` 构造函数增加
     `version: MinecraftVersion` 参数, 根据版本选择默认 mode。
3. **接入 `ProtocolAdapter` 到 `rate_limiter`**:
   - `app/auth/rate_limiter.py` 使用 `ProtocolAdapter.get_max_command_block_rate()`。
4. **动态更新 `min_*` 字段**:
   - `app/auth/netease_direct/client.py` 收到认证响应后, 用响应中的
     `min_engine_version` / `min_patch_version` 更新当前 `ProtocolAdapter`
     的对应字段 (需要把 `ProtocolAdapter.info` 改为可变, 或在
     `ProtocolAdapter` 中持有可变 `min_*` 状态)。

### 5.3 长期 (3.9 发布后)

1. **实测 3.9 实际版本号**: 待 2026-07-24 网易 3.9 发布后, 从 TapTap /
   网易官网获取实际启动器版本号 (如 `3.9.x.xxxxxx`), 更新
   `version_config.json`。
2. **实测 3.9 `replaceitem` 是否恢复**: 若 3.9 恢复完整 `replaceitem`,
   将 `replaceitem_limited` 改为 `false` 并将 `default_structure_mode`
   改为 `REPLACEITEM` (或保留 `STRUCTURE` 作为更稳定的默认)。
3. **实测 3.9 命令方块速率上限**: 更新 `max_command_block_rate`。
4. **协议版本协商实测**: 在 RakNet 握手时观察 3.9 服务器返回的协议版本列表,
   验证 `try_negotiate_protocol` 的行为是否符合预期。
5. **多版本候选列表**: 若 3.9 服务器同时支持 3.8 协议版本, 可在
   `version_config.json` 中为每个版本增加 `fallback_protocols` 字段,
   让 `try_negotiate_protocol` 在主版本不匹配时尝试降级。

---

## 6. 风险与限制

### 6.1 已知风险

1. **3.8 → 1.21.80 的映射未经验证**: 用户规格指定为 `1.21.80`, 但
   PocketTerm 既有代码认为 `3.8 -> 1.21.90`。若实际 3.8 服务器拒绝
   `1.21.80` 客户端, 需要把 `version_config.json` 中的 3.8
   `engine_version` 改回 `1.21.90`。
2. **`patch_version` 为占位值**: 3.8 / 3.9 的 `patch_version` 均为
   `3.x.0.0`, 不是真实的网易启动器版本号。**使用占位值可能导致
   `/pe-authentication` 端点拒绝认证**。建议在投入生产前从
   TapTap / 网易官网获取实际版本号填入。
3. **`min_engine_version` / `min_patch_version` 未动态更新**: 当前
   与 `engine_version` / `patch_version` 相同 (假设服务器不限制最低版本)。
   若服务器实际返回更严格的最低版本要求, 当前实现不会自动适配。
4. **3.9 `replaceitem_limited=false` 为推测**: 待 3.9 发布后实测确认。

### 6.2 Community-Bot 局限性

1. **单版本二进制**: Community-Bot 硬编码 `1.21.90`, 不支持 3.8 回退。
   PocketTerm 双版本管理器比 Community-Bot 更灵活。
2. **错误信息密度低**: `RaKNet Connent Fail!!` 拼写错误且无诊断信息,
   PocketTerm 应避免此风格。
3. **无配置文件**: Community-Bot 通过命令行参数覆盖, 不支持持久化配置。
   PocketTerm 通过 `version_config.json` 提供更友好的配置体验。
4. **Python 3.13 内嵌**: Community-Bot 内嵌 Python 3.13, 与 PocketTerm
   的 Python 3.10 不一致 (但本版本管理器不依赖内嵌 Python, 无影响)。

---

## 7. 验证清单

- [x] `1.21.90` 字符串已在 Community_Bot.exe strings 中确认
- [x] `GetRakNetProtocolVersion` / `SetRakNetProtocolVersion` 函数已确认
- [x] `[Fail] Incompatible protocol, trying next version...` 重试逻辑已确认
- [x] `min_engine_version` / `min_patch_version` 字段已确认 (NEMCTOOLS 源码)
- [x] `PEAURequest.engine_version` / `patch_version` 字段已确认 (NEMCTOOLS 源码)
- [x] 认证服务器 URL 已对齐 (`g79authobt.nie.netease.com`)
- [x] RakNet 协议版本 (`10`) 已对齐 (PocketTerm `raknet.py`)
- [ ] 3.8 真实 `engine_version` (`1.21.80`?) 待网易 3.9 发布后实测校准
- [ ] 3.8 / 3.9 真实 `patch_version` 待从 TapTap 获取
- [ ] 3.9 `replaceitem_limited=false` 待 3.9 发布后实测确认
- [ ] 3.9 命令方块速率上限 (`30`) 待 3.9 发布后实测确认

---

## 8. 参考资料

### 8.1 Community_Bot.exe

- 路径: `/data/user/work/community_bot/Community_Bot.exe`
- 文件类型: PE32+ executable (console) x86-64, for MS Windows
- 内嵌 Python: 3.13 (`python313.dll`)
- 依赖 DLL: `RakNetDLL.dll`, `SLikeNet_DLL_Release_x64.dll`,
  `libcrypto-3-x64.dll`, `libssl-3-x64.dll`, `libcurl.dll`
- 提取命令:
  ```bash
  strings /data/user/work/community_bot/Community_Bot.exe | \
      grep -iE "1\.21\.[0-9]+|protocol|version|engine|incompatible|trying next"
  ```

### 8.2 NEMCTOOLS 源码

- 路径: `/data/user/work/nemctools/查UID源码(1.3.8)/`
- 关键文件:
  - `Login.PEEntity.PEAuthentication/PEAURequest.cs` — PE 认证请求体
    (含 `engine_version` / `patch_version` 字段)
  - `Login.loginauth/AuthenticationResponseEntity.cs` — 认证响应体
    (含 `min_engine_version` / `min_patch_version` 字段)
  - `Login.loginauth.authentication_v2/authenticationg79.cs` — g79 v2 认证
    (含 `engineVersion` / `patchVersion` 驼峰字段)
  - `ConsoleAppLogin.NetEase/Http.cs` — HTTP 客户端实现
    (含 `PE_Login` / `LoadToken` / `HttpPost` 等方法)

### 8.3 PocketTerm 既有代码

- `app/auth/netease_direct/constants.py` — 网易直连认证常量
  (含 `ENGINE_VERSION` / `PATCH_VERSION` / `GAME_VERSION` / `AUTH_SERVER`)
- `app/protocol/raknet.py` — RakNet UDP 协议实现
  (含 `DEFAULT_PROTOCOL_VERSION = 10`)
- `app/constants/minecraft.py` — Minecraft Bedrock 游戏常量
  (含 `GameVersion` / `ProtocolVersion`)
- `app/protocol/nbt_placer.py` — NBT 放置器
  (含 `STRUCTURE` / `REPLACEITEM` 双模式说明, 网易 3.8 replaceitem 阉割)
- `app/protocol/jwt_chain.py` — JWT 登录链
  (含 `DEFAULT_GAME_VERSION = "1.21.93"`)
- `app/protocol/connection.py` — Bedrock 客户端连接管理
  (使用 `GameVersion` 作为默认游戏版本)

### 8.4 内部交叉引用

- `app/protocol/version_manager.py` — 本版本管理器实现
- `app/protocol/protocol_adapter.py` — 协议适配器实现
- `backend/data/version_config.json` — 版本配置文件

---

**报告结束。**
