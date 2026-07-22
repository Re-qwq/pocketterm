"""
PocketTerm 示例插件 - 无权限命令插件
演示机器人无需OP权限也能使用的功能

事件回调签名: async def callback(self, data: dict) -> None
  - CHAT: {"sender": "发送者", "message": "消息内容", ...}
  - COMMAND: {"sender": "发送者", "command": "命令", ...}
"""
from app.plugins.base import PluginBase, PluginEvent


class NoOpPlugin(PluginBase):
    """
    无权限命令插件 - 让机器人像普通玩家一样操作

    机器人不需要OP权限，通过模拟玩家行为实现：
    - 发送聊天消息（/me、/tell 等非OP命令）
    - 回复命令
    """

    name = "NoOpPlugin"
    author = "PocketTerm"
    version = "1.0.0"
    description = "让无权限机器人也能像普通玩家一样操作"

    async def on_load(self) -> bool:
        """插件加载时注册事件。"""
        self.context.log("info", "无权限命令插件已加载！")

        # 注册聊天事件，监听命令
        self.register_event(PluginEvent.CHAT, self.on_chat)
        return True

    async def on_unload(self) -> bool:
        """插件卸载时清理资源。"""
        self.context.log("info", "无权限命令插件已卸载")
        return True

    async def on_chat(self, data: dict) -> None:
        """监听聊天，可以做一些自动响应。"""
        sender = data.get("sender", data.get("player", "未知"))
        message = data.get("message", "")

        # 普通玩家也能用的功能
        if message == "!help":
            # /me 命令不需要OP权限
            await self.context.send_command("/me 展示了帮助信息")
            await self.context.send_chat("可用命令: !help !pos !drop !follow")

        elif message == "!pos":
            # 报告机器人位置
            pos = data.get("position")
            if pos:
                await self.context.send_command(
                    f"/me 当前位置: ({pos.get('x', 0):.1f}, {pos.get('y', 0):.1f}, {pos.get('z', 0):.1f})"
                )
            else:
                await self.context.send_chat("无法获取当前位置")

        elif message == "!drop":
            # 丢弃物品 - 玩家行为，不需要权限
            await self.context.send_command("/drop")
            await self.context.send_chat("已丢弃物品！")

        elif message.startswith("!follow"):
            # 跟随玩家 - 通过移动实现
            target = message.split(" ", 1)[1] if " " in message else sender
            await self.context.send_chat(f"好的 {target}，我跟着你！")
            # TODO: 获取目标玩家位置并移动
