# PocketTerm Hugging Face Spaces 部署教程

## 什么是 Hugging Face Spaces

Hugging Face 是一个 AI 社区平台，提供免费的 Spaces（应用托管）。
完全免费，不需要银行卡，只需要邮箱注册。

## 限制说明

- 免费版：重启后数据会重置（SQLite 数据库不持久）
- 适合测试、演示、开发使用
- 以后有银行卡了可以迁移到 Fly.io 获得持久存储

## 部署步骤

### 第 1 步：注册 Hugging Face 账号

1. 打开 https://huggingface.co/join
2. 用邮箱注册（完全免费，不需要银行卡）
3. 完成邮箱验证

### 第 2 步：创建新 Space

1. 打开 https://huggingface.co/new-space
2. 填写信息：
   - Space name: `pocketterm`
   - License: MIT
   - SDK: **Docker**
   - Hardware: **CPU basic (Free)**
   - Visibility: **Public**（任何人可访问）或 Private（仅自己可访问）
3. 点击 "Create Space"

### 第 3 步：上传项目文件

有两种方式：

#### 方式 A：通过网页上传（简单）

1. 在刚创建的 Space 页面，点击 "Files"
2. 逐个上传以下文件：
   - `Dockerfile`
   - `docker-entrypoint.sh`
   - `fly.toml`（不需要）
3. 创建文件夹并上传：
   - `backend/` 目录下所有文件
   - `frontend/` 目录下所有文件
   - `plugins/` 目录下所有文件

#### 方式 B：通过 Git 上传（推荐，更完整）

在终端执行：

```bash
# 安装 git lfs（处理大文件）
git lfs install

# 克隆空 Space
git clone https://huggingface.co/spaces/你的用户名/pocketterm

# 复制项目文件
cp -r /workspace/PocketTerm/* pocketterm/

# 进入目录
cd pocketterm

# 添加所有文件
git add .

# 提交
git commit -m "Initial deployment"

# 推送
git push
```

推送后会自动构建并部署。

### 第 4 步：设置环境变量

在 Space 页面：
1. 点击 "Settings"
2. 找到 "Repository secrets"
3. 添加以下变量：

| 名称 | 值 |
|------|-----|
| POCKETTERM_ENV | production |
| POCKETTERM_JWT_SECRET | （填一个随机字符串，如 abc123xyz789） |
| POCKETTERM_CORS_ORIGINS | https://你的用户名-pocketterm.hf.space |
| POCKETTERM_DEBUG | （留空，不设置） |

### 第 5 步：等待构建

上传文件后，Hugging Face 会自动构建 Docker 镜像。
构建大约需要 3-5 分钟，在 Space 页面可以看到构建日志。

### 第 6 步：访问你的网站

构建完成后，你的网站地址是：

```
https://你的用户名-pocketterm.hf.space
```

任何人通过这个地址都能访问你的 PocketTerm。

### 第 7 步：更新 CORS 配置

部署成功后，确认环境变量 `POCKETTERM_CORS_ORIGINS` 设置为正确的地址。
如果登录失败，可能是 CORS 没配置好，检查 Settings 中的 secret。

## 默认管理员账号

- 用户名：admin
- 密码：admin123
- **登录后请立即修改密码！**

## 常见问题

### Q: 构建失败怎么办

查看构建日志（Space 页面 → Logs），常见原因：
- 文件没上传完整
- Dockerfile 格式错误

### Q: 访问报 502 错误

应用可能还在启动中，等待 30 秒后重试。

### Q: 重启后数据丢失

这是免费版的限制。解决方案：
1. 定期备份：通过管理后台导出数据
2. 升级到 Fly.io（需要银行卡）获得持久存储
3. 使用外部数据库（如 Supabase 免费版）

### Q: 如何更新代码

重新推送 Git 即可，Hugging Face 会自动重新构建：

```bash
cd pocketterm
git add .
git commit -m "Update"
git push
```

### Q: Space 会休眠吗

免费版 Space 在长时间不访问后会自动休眠，下次访问时自动唤醒（约需 30 秒）。
