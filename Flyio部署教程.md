# PocketTerm Fly.io 部署教程

## 什么是 Fly.io

Fly.io 是一个云平台，可以把你的项目部署成公网可访问的网站。免费额度足够个人使用。

## 前置条件

- 一个邮箱（注册用）
- 一张银行卡/信用卡（验证用，免费额度内不扣费）
  - 支持国内 Visa/Mastercard
  - 也支持虚拟卡（如 Depay）

## 部署步骤

### 第 1 步：注册 Fly.io 账号

1. 打开 https://fly.io/app/sign-up
2. 用邮箱注册
3. 填写银行卡信息验证身份（免费额度内不扣费）
4. 完成注册

### 第 2 步：安装 flyctl 命令行工具

在终端执行（如果你用的是 TRAE 或其他在线 IDE）：

```bash
curl -L https://fly.io/install.sh | sh
```

安装后添加到 PATH：

```bash
export FLYCTL_INSTALL="/root/.fly"
export PATH="$FLYCTL_INSTALL/bin:$PATH"
```

### 第 3 步：登录 Fly.io

```bash
flyctl auth login
```

浏览器会打开登录页面，完成授权。

### 第 4 步：创建应用

进入项目目录：

```bash
cd /workspace/PocketTerm
```

创建应用（会自动生成 fly.toml，已有则跳过）：

```bash
flyctl launch --no-deploy
```

- 选择区域：推荐 `nrt`（东京）或 `hkg`（香港）
- 选择是否创建 Postgres：选 No
- 选择是否创建 Redis：选 No

### 第 5 步：创建持久化存储卷

SQLite 数据库需要持久存储，创建一个 1GB 的卷：

```bash
flyctl volumes create pocketterm_data --size 1
```

### 第 6 步：设置环境变量（密钥）

```bash
# 生成随机 JWT 密钥
JWT_SECRET=$(openssl rand -hex 32)

# 设置密钥
flyctl secrets set POCKETTERM_JWT_SECRET="$JWT_SECRET"

# 设置允许的来源（部署后会获得域名）
# 先用占位符，部署后更新
flyctl secrets set POCKETTERM_CORS_ORIGINS="https://你的应用名.fly.dev"
```

### 第 7 步：部署

```bash
flyctl deploy
```

首次部署会自动构建 Docker 镜像并推送到 Fly.io，大约需要 3-5 分钟。

### 第 8 步：获取访问地址

部署完成后，你的网站地址是：

```
https://你的应用名.fly.dev
```

任何人通过这个地址都能访问你的 PocketTerm。

### 第 9 步：更新 CORS 配置

部署成功后，更新 CORS 允许的来源为实际域名：

```bash
flyctl secrets set POCKETTERM_CORS_ORIGINS="https://你的应用名.fly.dev"
flyctl deploy
```

## 常用运维命令

```bash
# 查看应用状态
flyctl status

# 查看实时日志
flyctl logs

# 打开 SSH 到容器
flyctl ssh console

# 重启应用
flyctl apps restart

# 停止应用
flyctl scale count 0

# 启动应用
flyctl scale count 1
```

## 默认管理员账号

- 用户名：admin
- 密码：admin123
- **登录后请立即修改密码！**（点击右上角用户菜单 → 修改密码）

## 免费额度说明

Fly.io 免费额度：
- 3 个共享 CPU 虚拟机（256MB 内存）
- 3GB 持久存储
- 每月 160GB 出站流量
- 超出后才会扣费

## 常见问题

### Q: 部署后访问报 502/503 错误

应用可能还在启动中，等待 30 秒后重试。也可以用 `flyctl logs` 查看日志。

### Q: 数据会丢失吗

不会。SQLite 数据库存储在持久化卷中，重启不丢失。

### Q: 如何绑定自己的域名

```bash
flyctl certs add your-domain.com
```

然后按提示添加 DNS 记录即可。

### Q: 忘记了 admin 密码

通过 SSH 进入容器重置：

```bash
flyctl ssh console
# 在容器内执行
cd /app/backend
python -c "
import asyncio
from app.database import get_db
async def reset():
    db = await get_db()
    await db.update_user_password('u_9204e2a5f454', 'admin123')
    print('密码已重置为 admin123')
asyncio.run(reset())
"
```
