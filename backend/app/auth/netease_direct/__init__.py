"""网易直连认证模块。

直接与网易服务器通信完成认证，无需第三方认证服务器。

主要组件:
    - constants: 加密密钥、服务器URL、设备指纹
    - crypto: AES-CBC 加密/解密 + 动态令牌计算
    - client: 认证客户端 (登录 + 租赁服 + auth v2)
    - login_4399: 4399 账号密码登录 (ptlogin 直登, 4399pc 频道)
    - login_4399_oauth2: 4399 OAuth2 登录 (NovaBuilder 方案, 4399com 频道)
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

__all__ = [
    "NeteaseDirectClient",
    "DirectAuthResult",
    "generate_sauth_json",
    "http_encrypt",
    "http_decrypt",
    "http_encrypt_g79v3",
    "http_decrypt_g79v3",
    "http_encrypt_g79v12",
    "http_decrypt_g79v12",
    "compute_dynamic_token",
    "AUTH_SERVER",
    "COREOBT_PC",
    "COREOBT_PE",
    "APIGATEWAYOBT_PC",
    "APIGATEWAYOBT_PE",
    "KEYS",
    "KEYS_G79V3",
    "KEYS_G79V12",
    # 4399 OAuth2 (WPFLauncher_Hook 方案)
    "Login4399OAuth2",
    "OAuth2Result",
    "login_4399_oauth2",
]
