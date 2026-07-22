"""
PocketTerm 示例插件 - 欢迎插件
演示插件系统的基本用法

事件回调签名: async def callback(self, data: dict) -> None
  - data 是包含事件相关数据的字典
  - PLAYER_JOIN: {"player": "玩家名", ...}
  - PLAYER_LEAVE: {"player": "玩家名", ...}
  - CHAT: {"sender": "发送者", "message": "消息内容", ...}
"""
from app.plugins.base import PluginBase, PluginEvent


class WelcomePlugin(PluginBase):
    """欢迎插件 - 玩家加入时自动欢迎"""

    name = "WelcomePlugin"
    author = "PocketTerm"
    version = "1.0.0"
    description = "玩家加入游戏时自动发送欢迎消息"

    async def on_load(self) -> bool:
        """插件加载时注册事件。"""
        self.context.log("info", "欢迎插件已加载！")

        # 注册事件
        self.register_event(PluginEvent.PLAYER_JOIN, self.on_player_join)
        self.register_event(PluginEvent.PLAYER_LEAVE, self.on_player_leave)
        self.register_event(PluginEvent.CHAT, self.on_chat)
        return True

    async def on_unload(self) -> bool:
        """插件卸载时清理资源。"""
        self.context.log("info", "欢迎插件已卸载")
        return True

    async def on_player_join(self, data: dict) -> None:
        """玩家加入时发送欢迎消息。"""
        player = data.get("player", data.get("player_name", "未知玩家"))
        self.context.log("info", f"玩家 {player} 加入了游戏")
        # 通过上下文发送欢迎消息
        await self.context.send_chat(f"欢迎来到服务器，{player}！")

    async def on_player_leave(self, data: dict) -> None:
        """玩家离开时记录日志。"""
        player = data.get("player", data.get("player_name", "未知玩家"))
        self.context.log("info", f"玩家 {player} 离开了游戏")

    async def on_chat(self, data: dict) -> None:
        """收到聊天消息时处理。"""
        sender = data.get("sender", data.get("player", "未知"))
        message = data.get("message", "")
        self.context.log("info", f"[聊天] {sender}: {message}")

        # 示例：如果有人发 "hello"，回复 "Hi!"
        if message.lower() == "hello":
            await self.context.send_chat("Hi!")
