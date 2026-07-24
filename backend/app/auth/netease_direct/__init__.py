"""网易直连认证模块。

直接与网易服务器通信完成认证，无需第三方认证服务器。

主要组件:
    - constants: 加密密钥、服务器URL、设备指纹
    - crypto: AES-CBC 加密/解密 + 动态令牌计算
    - client: 认证客户端 (登录 + 租赁服 + auth v2)
    - login_4399: 4399 账号密码登录 (ptlogin 直登, 4399pc 频道)
    - login_4399_oauth2: 4399 OAuth2 登录 (NovaBuilder 方案, 4399com 频道)
    - phoenix_guest_auth: Phoenix 访客认证 (Fatalder 兼容, netease 频道)
    - dynamic_ip: 动态公网 IP 获取工具
    - fever_to_sauth: MPay/Fever Token 转 sauth_json
"""
from .client import DirectAuthResult, NeteaseDirectClient, generate_sauth_json
from .constants import (
    AUTH_SERVER,
    APIGATEWAYOBT_PC,
    APIGATEWAYOBT_PE,
    COREOBT_PC,
    COREOBT_PE,
    KEYS,
    KEYS_G79V3,
    KEYS_G79V12,
)
from .crypto import (
    compute_dynamic_token,
    http_decrypt,
    http_decrypt_g79v12,
    http_decrypt_g79v3,
    http_encrypt,
    http_encrypt_g79v12,
    http_encrypt_g79v3,
)
from .login_4399_oauth2 import (
    Login4399OAuth2,
    OAuth2Result,
    login_4399_oauth2,
)
from .phoenix_guest_auth import (
    DEFAULT_AUTH_SERVER as PHOENIX_DEFAULT_AUTH_SERVER,
    PhoenixAuthResult,
    PhoenixGuestAuthClient,
    phoenix_guest_login,
)
from .dynamic_ip import get_public_ip as get_dynamic_ip
from .fever_to_sauth import fever_to_sauth

__all__ = [
    # 核心客户端
    "NeteaseDirectClient",
    "DirectAuthResult",
    "generate_sauth_json",
    # 加密
    "http_encrypt",
    "http_decrypt",
    "http_encrypt_g79v3",
    "http_decrypt_g79v3",
    "http_encrypt_g79v12",
    "http_decrypt_g79v12",
    "compute_dynamic_token",
    # 服务器常量
    "AUTH_SERVER",
    "COREOBT_PC",
    "COREOBT_PE",
    "APIGATEWAYOBT_PC",
    "APIGATEWAYOBT_PE",
    "KEYS",
    "KEYS_G79V3",
    "KEYS_G79V12",
    # 4399 OAuth2 (WPFLauncher_Hook 方案, 4399com 频道)
    "Login4399OAuth2",
    "OAuth2Result",
    "login_4399_oauth2",
    # Phoenix 访客认证 (Fatalder 兼容, netease 频道, 绕过 code=32)
    "PhoenixGuestAuthClient",
    "PhoenixAuthResult",
    "phoenix_guest_login",
    "PHOENIX_DEFAULT_AUTH_SERVER",
    # 动态 IP
    "get_dynamic_ip",
    # MPay Token 转换
    "fever_to_sauth",
]
