"""NEMC 认证模块.

从网易 MCStudio 启动器 (NEMCTOOLS) C# 源码移植.

子模块
------
- :mod:`nemc_client`      -- 认证客户端 (Http.cs)
- :mod:`link_connection`   -- TCP 连接到游戏服务器 (LinkConnection.cs)
- :mod:`fever_to_sauth`    -- FeverToken 转 sauth_json (FeverAuth.cs)
- :mod:`cookie_pool`       -- Cookie 账号池管理
"""

from . import nemc_client, link_connection, fever_to_sauth, cookie_pool
from .nemc_client import (
    LoginResult,
    PEResult,
    NemcClient,
    PC_API_SERVER,
    PE_API_SERVER,
    LOBBY_SERVER,
    TRANSFER_SERVER,
    AUTH_SERVER,
)
from .link_connection import (
    LinkConnection,
    SERVER_PUBLIC_KEY_XML,
    CLIENT_PUBLIC_KEY_MD5,
    CLIENT_PRIVATE_KEY_XML,
    LINKSERVER_LIST_URL,
)
from .fever_to_sauth import (
    FeverToken,
    SauthResult,
    FeverAuth,
    CREATE_TICKET_URL,
    LOGIN_TICKET_URL,
)
from .cookie_pool import (
    CookieStatus,
    CookieEntry,
    PoolStatus,
    CookiePool,
    DEFAULT_POOL_FILE,
)

__all__ = [
    # 子模块
    "nemc_client",
    "link_connection",
    "fever_to_sauth",
    "cookie_pool",
    # 认证客户端
    "LoginResult",
    "PEResult",
    "NemcClient",
    "PC_API_SERVER",
    "PE_API_SERVER",
    "LOBBY_SERVER",
    "TRANSFER_SERVER",
    "AUTH_SERVER",
    # TCP 连接
    "LinkConnection",
    "SERVER_PUBLIC_KEY_XML",
    "CLIENT_PUBLIC_KEY_MD5",
    "CLIENT_PRIVATE_KEY_XML",
    "LINKSERVER_LIST_URL",
    # Fever Token
    "FeverToken",
    "SauthResult",
    "FeverAuth",
    "CREATE_TICKET_URL",
    "LOGIN_TICKET_URL",
    # Cookie 池
    "CookieStatus",
    "CookieEntry",
    "PoolStatus",
    "CookiePool",
    "DEFAULT_POOL_FILE",
]
