/* ==========================================================================
   PocketTerm v2 - 前端主应用 (Vanilla JS SPA)
   --------------------------------------------------------------------------
   - API 调用封装 (fetch + credentials: include + Bearer token)
   - 启动序列 / 认证流程 (登录 / 注册 / 验证码 / 登出)
   - 视图路由 (仪表盘 / 面板 / 机器人 / 卡密 / 用户 / 日志)
   - SW 风格控制台 (终端 / 日志 / 文件 / 插件 / 设置)
   - Toast 通知 / 模态框 / 确认对话框
   ========================================================================== */
(function () {
    "use strict";

    /* ======================================================================
       0. 常量与全局状态
       ====================================================================== */

    /** 根据 ID 获取元素的简写 */
    const $ = (id) => document.getElementById(id);
    /** querySelectorAll 简写，返回数组 */
    const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

    /** API 基础路径 (同源) */
    const API_BASE = "/api/v2";

    /** Token 在 localStorage 中的键名 */
    const TOKEN_KEY = "pocketterm_token";

    /** WebSocket 基础路径 (同源, 自动适配 ws/wss) */
    const WS_BASE = (location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws";

    /** 终端输出最大行数, 超出后自动删除最旧的行, 防止长时间运行内存占用过高 */
    const MAX_TERMINAL_LINES = 1000;

    /** WebSocket 指数退避: 初始重连间隔 (毫秒) */
    const WS_RECONNECT_INITIAL_MS = 1000;
    /** WebSocket 指数退避: 最大重连间隔 (毫秒) */
    const WS_RECONNECT_MAX_MS = 60000;

    /** 日志级别 -> 颜色 */
    const LOG_COLORS = {
        success: "#3fb950",
        error: "#f85149",
        warn: "#d29922",
        info: "#e6edf3",
        system: "#58a6ff",
        debug: "#7d8590",
    };

    /** 状态 -> 徽章颜色 */
    const STATUS_COLORS = {
        active: "#3fb950",
        expired: "#f85149",
        suspended: "#d29922",
        stopped: "#7d8590",
        running: "#3fb950",
        error: "#f85149",
        unused: "#58a6ff",
        used: "#7d8590",
        revoked: "#f85149",
        idle: "#7d8590",
        connecting: "#d29922",
        connected: "#58a6ff",
        disconnected: "#7d8590",
    };

    // 状态中文标签映射
    const STATUS_LABELS = {
        active: "活跃",
        expired: "已过期",
        suspended: "已暂停",
        stopped: "已停止",
        running: "运行中",
        error: "错误",
        unused: "未使用",
        used: "已使用",
        revoked: "已撤销",
        idle: "空闲",
        connecting: "连接中",
        connected: "已连接",
        disconnected: "已断开",
    };

    /** 卡密类型 -> 中文标签 */
    const CARD_TYPE_LABELS = {
        register: "注册卡密",
        panel: "面板卡密",
        renewal: "续期卡密",
    };

    /** 角色中文标签 */
    const ROLE_LABELS = {
        user: "普通用户",
        admin: "管理员",
        superadmin: "超级管理员",
    };

    /** 全局状态对象 */
    const state = {
        currentUser: null,            // 当前登录用户信息
        token: null,                  // 登录令牌 (Bearer)
        currentPanelId: null,         // 当前面板 ID (详情视图)
        currentBotId: null,           // 当前机器人 ID
        currentView: "dashboard",     // 当前视图名称
        currentConsoleTab: "console", // 当前控制台 Tab
        panels: [],                   // 面板列表
        bots: [],                     // 机器人列表
        users: [],                    // 用户列表
        cards: [],                    // 卡密列表
        cardStats: null,              // 卡密统计
        panelDetail: null,            // 面板详情数据
        panelBot: null,               // 面板关联的机器人
        captchaId: null,              // 当前验证码 ID
        consoleAutoscroll: true,      // 终端是否自动滚动
        confirmCallback: null,        // 确认对话框回调
        terminalHistory: [],          // 终端命令历史
        terminalHistoryIndex: -1,     // 历史浏览索引
        cardFilterType: "",           // 卡密筛选 - 类型
        cardFilterStatus: "",         // 卡密筛选 - 状态
        logFilterLevel: "",           // 日志筛选 - 级别
        // -- WebSocket 连接状态 --
        ws: null,                     // WebSocket 实例
        wsConnected: false,           // 是否已连接
        wsReconnectAttempts: 0,       // 当前重连尝试次数
        wsReconnectTimer: null,       // 重连定时器句柄
        wsStatusText: "未连接",        // 当前 WS 状态文本 (用于 UI 显示)
        panelInfoText: "",            // 面板状态信息文本 (用于 consoleInfo 组合显示)
        wsManuallyClosed: false,      // 是否主动关闭 (登出/401), 主动关闭时不自动重连
        theme: 'dark',
    };

    /* ======================================================================
       1. API Helper
       封装 fetch：自动携带 Cookie 与 Bearer token，统一错误处理
       ====================================================================== */

    /**
     * 发起 API 请求
     * @param {string} path - API 路径 (不含 /api/v2 前缀，或完整 URL)
     * @param {object} options - fetch 选项 {method, body, headers, ...}
     * @returns {Promise<object|string>} 解析后的 JSON 或文本
     */
    async function api(path, options = {}) {
        const url = path.startsWith("http") ? path : API_BASE + path;

        // 构建请求头
        const headers = { "Content-Type": "application/json" };
        if (state.token) headers["Authorization"] = "Bearer " + state.token;
        if (options.headers) Object.assign(headers, options.headers);

        // 构建 fetch 选项
        const fetchOpts = {
            method: options.method || "GET",
            credentials: "include",
            headers,
        };

        // 处理请求体
        if (options.body !== undefined && options.body !== null) {
            if (typeof options.body === "object" && !(options.body instanceof FormData)) {
                fetchOpts.body = JSON.stringify(options.body);
            } else {
                fetchOpts.body = options.body;
            }
        }

        // 发起请求
        let response;
        try {
            response = await fetch(url, fetchOpts);
        } catch (err) {
            toastError("网络连接失败，请检查网络");
            throw { type: "network", message: "网络连接失败" };
        }

        // 401 - 未授权 / 登录过期
        if (response.status === 401) {
            handleUnauthorized();
            throw { type: "auth", message: "登录已过期，请重新登录" };
        }

        // 403 - 无权限
        if (response.status === 403) {
            toastError("没有权限执行此操作");
            throw { type: "forbidden", message: "没有权限" };
        }

        // 其他非 2xx 状态码
        if (!response.ok) {
            let msg = `请求失败 (${response.status})`;
            try {
                const data = await response.json();
                if (data.detail) {
                    msg = typeof data.detail === "string"
                        ? data.detail
                        : JSON.stringify(data.detail);
                } else if (data.message) {
                    msg = data.message;
                } else if (data.error) {
                    msg = data.error;
                }
            } catch (_) { /* 响应非 JSON，使用默认消息 */ }
            toastError(msg);
            throw { type: "http", status: response.status, message: msg };
        }

        // 解析响应体
        const contentType = response.headers.get("content-type") || "";
        if (contentType.includes("application/json")) {
            return await response.json();
        }
        return await response.text();
    }

    /** 处理未授权 (401) - 清除状态并返回登录界面 */
    function handleUnauthorized() {
        // 登录过期, 主动关闭 WebSocket
        closeWebSocket();
        state.currentUser = null;
        state.token = null;
        localStorage.removeItem(TOKEN_KEY);
        showAuthScreen();
        toastWarn("登录已过期，请重新登录");
    }

    /* ======================================================================
       2. 工具函数
       ====================================================================== */

    /**
     * 格式化时间戳为可读字符串
     * @param {number|string|Date} timestamp - Unix 秒级时间戳 / ISO 字符串 / Date
     * @param {boolean} withSeconds - 是否包含秒
     * @returns {string} 格式化后的时间
     */
    function formatTime(timestamp, withSeconds = true) {
        if (timestamp === null || timestamp === undefined || timestamp === 0) return "-";
        let date;
        if (timestamp instanceof Date) {
            date = timestamp;
        } else if (typeof timestamp === "number") {
            // 兼容秒级与毫秒级时间戳
            date = new Date(timestamp < 1e12 ? timestamp * 1000 : timestamp);
        } else {
            date = new Date(timestamp);
        }
        if (isNaN(date.getTime())) return "-";
        const opts = {
            year: "numeric", month: "2-digit", day: "2-digit",
            hour: "2-digit", minute: "2-digit",
        };
        if (withSeconds) opts.second = "2-digit";
        return date.toLocaleString("zh-CN", opts);
    }

    /**
     * 格式化时长
     * @param {number|string|null} duration - 天数 / 预设字符串 ("1d"/"6h"/"permanent")
     * @returns {string} "永久" / "X 天" / "X 小时"
     */
    function formatDuration(duration) {
        if (duration === null || duration === undefined || duration === "") return "永久";
        if (typeof duration === "string") {
            if (duration === "permanent" || duration === "0") return "永久";
            const m = duration.match(/^(\d+)\s*(d|h|day|hour)$/i);
            if (m) {
                const n = parseInt(m[1], 10);
                return /^h/i.test(m[2]) ? `${n} 小时` : `${n} 天`;
            }
            return duration;
        }
        // 数字 - 按天处理
        if (duration === 0) return "永久";
        if (duration < 1) return `${Math.round(duration * 24)} 小时`;
        return `${duration} 天`;
    }

    /**
     * 生成状态徽章 HTML
     * @param {string} status - 状态名称
     * @returns {string} 徽章 HTML
     */
    function getStatusBadge(status) {
        const normalized = (status || "").toLowerCase();
        const color = STATUS_COLORS[normalized] || "#7d8590";
        const label = STATUS_LABELS[normalized] || status || "未知";
        return `<span class="badge" style="background:${color}22;color:${color};border:1px solid ${color}44;padding:2px 10px;border-radius:9999px;font-size:11px;font-weight:600;">${escapeHtml(label)}</span>`;
    }

    /**
     * HTML 转义 - 防止 XSS
     * @param {string} str - 原始字符串
     * @returns {string} 转义后的字符串
     */
    function escapeHtml(str) {
        if (str === null || str === undefined) return "";
        return String(str)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#39;");
    }

    /**
     * 复制文本到剪贴板
     * @param {string} text - 要复制的文本
     */
    async function copyToClipboard(text) {
        try {
            if (navigator.clipboard && window.isSecureContext) {
                await navigator.clipboard.writeText(text);
            } else {
                // 回退方案
                const textarea = document.createElement("textarea");
                textarea.value = text;
                textarea.style.position = "fixed";
                textarea.style.opacity = "0";
                document.body.appendChild(textarea);
                textarea.select();
                document.execCommand("copy");
                document.body.removeChild(textarea);
            }
            toastSuccess("已复制到剪贴板");
        } catch (err) {
            toastError("复制失败，请手动复制");
        }
    }

    /**
     * 获取剩余时间描述
     * @param {number} seconds - 剩余秒数
     * @returns {string} "X天 X小时 X分钟" 格式
     */
    function formatRemaining(seconds) {
        if (seconds <= 0) return "已过期";
        const days = Math.floor(seconds / 86400);
        const hours = Math.floor((seconds % 86400) / 3600);
        const mins = Math.floor((seconds % 3600) / 60);
        const parts = [];
        if (days > 0) parts.push(`${days} 天`);
        if (hours > 0) parts.push(`${hours} 小时`);
        if (mins > 0) parts.push(`${mins} 分钟`);
        return parts.join(" ") || "不足 1 分钟";
    }

    /* ======================================================================
       3. Toast 通知
       ====================================================================== */

    /** Toast 图标与颜色映射 */
    const TOAST_CONFIG = {
        success: { icon: "fa-check-circle", color: "#3fb950" },
        error:   { icon: "fa-times-circle", color: "#f85149" },
        warn:    { icon: "fa-exclamation-triangle", color: "#d29922" },
        info:    { icon: "fa-info-circle", color: "#58a6ff" },
    };

    /**
     * 显示 Toast 通知
     * @param {string} message - 消息内容
     * @param {string} type - 类型: success/error/warn/info
     */
    function toast(message, type = "info") {
        const container = $("toastContainer");
        if (!container) return;
        const config = TOAST_CONFIG[type] || TOAST_CONFIG.info;
        const el = document.createElement("div");
        el.className = "toast toast-" + type;
        el.style.cssText = `
            display:flex;align-items:center;gap:10px;
            background:#161b22;border:1px solid #30363d;
            border-left:3px solid ${config.color};
            border-radius:10px;padding:12px 16px;
            margin-bottom:10px;min-width:280px;max-width:420px;
            box-shadow:0 8px 24px rgba(0,0,0,0.4);
            color:#e6edf3;font-size:13px;
            transform:translateX(120%);opacity:0;
            transition:transform 0.3s cubic-bezier(0.16,1,0.3,1),opacity 0.3s;
        `;
        el.innerHTML = `
            <i class="fas ${config.icon}" style="color:${config.color};font-size:16px;flex-shrink:0;"></i>
            <span style="flex:1;word-break:break-word;">${escapeHtml(message)}</span>
        `;
        container.appendChild(el);
        // 触发滑入动画
        requestAnimationFrame(() => {
            el.style.transform = "translateX(0)";
            el.style.opacity = "1";
        });
        // 3 秒后自动消失
        setTimeout(() => {
            el.style.transform = "translateX(120%)";
            el.style.opacity = "0";
            setTimeout(() => el.remove(), 300);
        }, 3000);
    }

    /** 成功通知 */
    function toastSuccess(msg) { toast(msg, "success"); }
    /** 错误通知 */
    function toastError(msg) { toast(msg, "error"); }
    /** 警告通知 */
    function toastWarn(msg) { toast(msg, "warn"); }
    /** 信息通知 */
    function toastInfo(msg) { toast(msg, "info"); }

    /* ======================================================================
       4. 模态框
       ====================================================================== */

    /**
     * 打开模态框
     * @param {string} id - 模态框元素 ID
     */
    function openModal(id) {
        const modal = $(id);
        if (modal) modal.classList.add("visible");
    }

    /**
     * 关闭模态框
     * @param {string} id - 模态框元素 ID
     */
    function closeModal(id) {
        const modal = $(id);
        if (modal) modal.classList.remove("visible");
    }

    /** 关闭所有模态框 */
    function closeAllModals() {
        $$(".modal-overlay").forEach((m) => m.classList.remove("visible"));
    }

    /**
     * 显示确认对话框
     * @param {string} title - 标题 HTML
     * @param {string} message - 消息 HTML
     * @param {function} callback - 确认后执行的回调
     */
    function confirmAction(title, message, callback) {
        $("confirmTitle").innerHTML = title || '<i class="fas fa-exclamation-triangle"></i> 确认操作';
        $("confirmMessage").innerHTML = message || "";
        state.confirmCallback = callback;
        openModal("modalConfirm");
    }

    /* ======================================================================
       5. 启动序列
       ====================================================================== */

    /**
     * 应用初始化入口 (DOMContentLoaded)
     */
    async function init() {
        // 恢复本地存储的 token
        const savedToken = localStorage.getItem(TOKEN_KEY);
        if (savedToken) state.token = savedToken;

        // 绑定所有事件
        bindEvents();

        // Load saved theme
        const savedTheme = localStorage.getItem('pocketterm-theme') || 'dark';
        applyTheme(savedTheme);

        // 启动序列：显示 boot 屏幕 2 秒
        await sleep(2000);

        // 淡出 boot 屏幕
        $("bootScreen").classList.add("fade-out");

        // 尝试检查已有会话
        const loggedIn = await checkSession();
        if (loggedIn) {
            // 已登录 - 直接进入应用
            showApp();
            await loadDashboard();
        } else {
            // 未登录 - 显示认证界面
            showAuthScreen();
        }
    }

    function applyTheme(theme) {
        state.theme = theme;
        document.documentElement.setAttribute('data-theme', theme);
        localStorage.setItem('pocketterm-theme', theme);
        const icon = document.querySelector('#themeToggle i');
        if (icon) {
            icon.className = theme === 'dark' ? 'fas fa-moon' : 'fas fa-sun';
        }
    }

    function toggleTheme() {
        applyTheme(state.theme === 'dark' ? 'light' : 'dark');
    }

    /** Promise 延时工具 */
    function sleep(ms) {
        return new Promise((resolve) => setTimeout(resolve, ms));
    }

    /**
     * 检查已有登录会话
     * @returns {Promise<boolean>} 是否已登录
     */
    async function checkSession() {
        if (!state.token) return false;
        try {
            const res = await api("/auth/me");
            if (res.success && res.data) {
                state.currentUser = res.data;
                return true;
            }
        } catch (_) {
            // 令牌无效 - 清除
            state.token = null;
            localStorage.removeItem(TOKEN_KEY);
        }
        return false;
    }

    /* ======================================================================
       6. 认证流程 (登录 / 注册 / 验证码 / 登出)
       ====================================================================== */

    /**
     * 切换认证 Tab (登录 / 注册)
     * @param {string} tab - "login" 或 "register"
     */
    function switchAuthTab(tab) {
        const isLogin = tab === "login";
        // 更新 Tab 高亮
        $("tabLogin").classList.toggle("active", isLogin);
        $("tabRegister").classList.toggle("active", !isLogin);
        // 显示/隐藏表单
        $("loginForm").classList.toggle("hidden", !isLogin);
        $("registerForm").classList.toggle("hidden", isLogin);
        // 更新底部文字
        $("authFooterText").textContent = isLogin ? "还没有账号？" : "已有账号？";
        $("authToggle").textContent = isLogin ? "立即注册" : "立即登录";
        // 切换到注册时自动加载验证码
        if (!isLogin && !state.captchaId) {
            loadCaptcha();
        }
    }

    /**
     * 加载图形验证码
     */
    async function loadCaptcha() {
        const box = $("captchaImgBox");
        try {
            box.innerHTML = '<span class="captcha-placeholder">加载中...</span>';
            const res = await api("/auth/captcha");
            if (res.success && res.data) {
                state.captchaId = res.data.captcha_id;
                $("regCaptchaId").value = res.data.captcha_id;
                // 兼容 data URL 与裸 base64
                const imgSrc = res.data.image.startsWith("data:")
                    ? res.data.image
                    : `data:image/png;base64,${res.data.image}`;
                box.innerHTML = `<img src="${imgSrc}" alt="验证码" />`;
            }
        } catch (_) {
            box.innerHTML = '<span class="captcha-placeholder">加载失败，点击重试</span>';
        }
    }

    /**
     * 处理登录表单提交
     */
    async function handleLogin(e) {
        e.preventDefault();
        const username = $("loginUsername").value.trim();
        const password = $("loginPassword").value;
        const btn = $("loginBtn");

        if (!username || !password) {
            toastWarn("请输入用户名和密码");
            return;
        }

        btn.disabled = true;
        btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> 登录中...';

        try {
            const res = await api("/auth/login", {
                method: "POST",
                body: { username, password },
            });
            if (res.success) {
                // 存储 token
                state.token = res.token || null;
                if (state.token) localStorage.setItem(TOKEN_KEY, state.token);
                // 获取用户信息
                if (res.data) {
                    state.currentUser = res.data;
                } else {
                    // 如果登录响应不含用户数据，单独获取
                    const meRes = await api("/auth/me");
                    if (meRes.success) state.currentUser = meRes.data;
                }
                toastSuccess("登录成功");
                showApp();
                await loadDashboard();
            } else {
                toastError(res.message || "登录失败");
            }
        } catch (err) {
            // 错误已由 api() 统一处理
        } finally {
            btn.disabled = false;
            btn.innerHTML = '<i class="fas fa-sign-in-alt"></i> 登录';
        }
    }

    /**
     * 处理注册表单提交
     */
    async function handleRegister(e) {
        e.preventDefault();
        const username = $("regUsername").value.trim();
        const password = $("regPassword").value;
        const cardKey = $("regCardKey").value.trim();
        const captchaAnswer = $("regCaptchaInput").value.trim();
        const captchaId = $("regCaptchaId").value;
        const btn = $("registerBtn");

        if (!username || !password || !cardKey || !captchaAnswer) {
            toastWarn("请填写所有必填项");
            return;
        }

        btn.disabled = true;
        btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> 注册中...';

        try {
            const res = await api("/auth/register", {
                method: "POST",
                body: {
                    username,
                    password,
                    card_key: cardKey,
                    captcha_answer: captchaAnswer,
                    captcha_id: captchaId,
                },
            });
            if (res.success) {
                toastSuccess("注册成功，请登录");
                // 清空注册表单
                $("registerForm").reset();
                state.captchaId = null;
                // 切换到登录
                switchAuthTab("login");
                $("loginUsername").value = username;
                $("loginPassword").focus();
            } else {
                toastError(res.message || "注册失败");
                // 刷新验证码
                loadCaptcha();
            }
        } catch (_) {
            // 刷新验证码
            loadCaptcha();
        } finally {
            btn.disabled = false;
            btn.innerHTML = '<i class="fas fa-user-plus"></i> 注册';
        }
    }

    /**
     * 处理登出
     */
    async function handleLogout() {
        try {
            await api("/auth/logout", { method: "POST" });
        } catch (_) { /* 忽略登出 API 错误 */ }
        // 主动关闭 WebSocket, 不再自动重连
        closeWebSocket();
        // 清除状态
        state.currentUser = null;
        state.token = null;
        state.currentPanelId = null;
        state.currentBotId = null;
        state.panels = [];
        state.bots = [];
        localStorage.removeItem(TOKEN_KEY);
        toastInfo("已退出登录");
        showAuthScreen();
        // 重置认证表单
        $("loginForm").reset();
        $("registerForm").reset();
        switchAuthTab("login");
    }

    /* ======================================================================
       7. 屏幕切换 (Auth / App)
       ====================================================================== */

    /** 显示认证界面 */
    function showAuthScreen() {
        $("authScreen").classList.remove("fade-out", "hidden");
        $("app").classList.add("hidden");
        $("app").classList.remove("visible");
    }

    /** 显示主应用 */
    function showApp() {
        $("authScreen").classList.add("fade-out", "hidden");
        $("app").classList.remove("hidden");
        // 触发重排后再添加 visible 以启用过渡动画
        requestAnimationFrame(() => {
            $("app").classList.add("visible");
        });
        // 更新用户信息 UI
        updateUserUI();
        // 更新管理员区域可见性
        updateAdminVisibility();
        // 建立 WebSocket 实时连接 (指数退避重连)
        initWebSocket();
        // 默认切换到仪表盘
        switchView("dashboard");
    }

    /**
     * 更新顶部栏与用户菜单中的用户信息
     */
    function updateUserUI() {
        const user = state.currentUser;
        if (!user) return;
        const displayName = user.username || "用户";
        const role = user.role || "user";
        const roleLabel = ROLE_LABELS[role] || role;

        $("currentUserName").textContent = displayName;
        $("currentUserRole").textContent = roleLabel;
        $("userAvatar").textContent = displayName.charAt(0).toUpperCase();
        $("umName").textContent = displayName;
        $("umId").textContent = "ID: " + (user.user_id || user.id || "-");
    }

    /**
     * 根据角色显示/隐藏管理后台区域
     */
    function updateAdminVisibility() {
        const role = state.currentUser ? state.currentUser.role : "user";
        const isAdmin = role === "admin" || role === "superadmin";
        $("adminDivider").style.display = isAdmin ? "" : "none";
        $("adminSection").style.display = isAdmin ? "" : "none";
        $("quickCards").style.display = isAdmin ? "" : "none";
    }

    /* ======================================================================
       8. 导航与视图切换
       ====================================================================== */

    /**
     * 切换主视图
     * @param {string} view - 视图名称
     */
    function switchView(view) {
        state.currentView = view;

        // 切换导航项高亮
        $$(".nav-item").forEach((item) => {
            item.classList.toggle("active", item.dataset.view === view);
        });

        // 切换视图显示
        $$(".view").forEach((v) => v.classList.remove("active"));
        const viewEl = $("view-" + view);
        if (viewEl) viewEl.classList.add("active");

        // 移动端关闭侧边栏
        closeSidebar();

        // 按视图加载数据
        switch (view) {
            case "dashboard":
                loadDashboard();
                break;
            case "panels":
                loadPanels();
                break;
            case "bots":
                loadBots();
                break;
            case "admin-cards":
                loadCardStats();
                loadCards();
                loadCardCreationLogs();
                break;
            case "admin-users":
                loadUsers();
                break;
            case "admin-logs":
                loadSystemLogs();
                break;
            case "admin-activity":
                loadActivityLog();
                break;
            case "admin-system":
                loadSystemAdmin();
                break;
        }
    }

    async function loadActivityLog() {
        try {
            const res = await fetch("/api/v2/auth/activity-log", {
                headers: { Authorization: `Bearer ${state.token}` },
            });
            if (!res.ok) throw new Error("Failed to load activity log");
            const data = await res.json();
            const logs = data.data || [];
            const container = $("activityLogList");
            if (logs.length === 0) {
                container.innerHTML = `<div class="empty-state"><i class="fas fa-history"></i><h3>暂无活动记录</h3><p>用户登录、注册等活动记录将显示在这里</p></div>`;
                return;
            }
            container.innerHTML = logs.map(log => {
                const icon = log.action === 'login' ? 'fa-sign-in-alt' : log.action === 'register' ? 'fa-user-plus' : log.action === 'logout' ? 'fa-sign-out-alt' : 'fa-info-circle';
                const color = log.action === 'login' ? 'var(--color-success)' : log.action === 'register' ? 'var(--color-primary)' : log.action === 'logout' ? 'var(--text-secondary)' : 'var(--text-tertiary)';
                return `<div style="display:flex;align-items:center;gap:12px;padding:12px 16px;border-bottom:1px solid var(--border-muted);">
                <div style="width:36px;height:36px;border-radius:50%;background:var(--bg-elevated);display:flex;align-items:center;justify-content:center;color:${color};font-size:14px;"><i class="fas ${icon}"></i></div>
                <div style="flex:1;min-width:0;">
                    <div style="font-weight:500;color:var(--text-primary);">${log.username || 'Unknown'} - ${log.action_desc || log.action}</div>
                    <div style="font-size:12px;color:var(--text-tertiary);">${new Date(log.timestamp * 1000).toLocaleString('zh-CN')}</div>
                </div>
                <div style="font-size:11px;color:var(--text-tertiary);background:var(--bg-elevated);padding:2px 8px;border-radius:var(--radius-sm);">${log.ip || '-'}</div>
            </div>`;
            }).join("");
        } catch (err) {
            console.error("Load activity log error:", err);
        }
    }

    /**
     * 切换控制台 Tab
     * @param {string} tab - "console"/"logs"/"files"/"plugins"/"settings"
     */
    function switchConsoleTab(tab) {
        state.currentConsoleTab = tab;
        $$(".console-tab").forEach((t) => {
            t.classList.toggle("active", t.dataset.consoleTab === tab);
        });
        ["console", "logs", "files", "plugins", "settings"].forEach((name) => {
            const panel = $("ctab-" + name);
            if (panel) panel.classList.toggle("active", name === tab);
        });
        // 按需加载数据
        if (tab === "logs") loadPanelLogs();
        if (tab === "settings") loadBotConfig();
    }

    /* ======================================================================
       9. 侧边栏 / 用户菜单 (移动端)
       ====================================================================== */

    /** 打开移动端侧边栏 */
    function openSidebar() {
        $("sidebar").classList.add("open");
        $("sidebarBackdrop").classList.add("visible");
    }

    /** 关闭移动端侧边栏 */
    function closeSidebar() {
        $("sidebar").classList.remove("open");
        $("sidebarBackdrop").classList.remove("visible");
    }

    /** 切换移动端侧边栏 */
    function toggleSidebar() {
        if ($("sidebar").classList.contains("open")) {
            closeSidebar();
        } else {
            openSidebar();
        }
    }

    /** 切换用户菜单下拉 */
    function toggleUserMenu() {
        $("userMenu").classList.toggle("visible");
    }

    /** 关闭用户菜单 */
    function closeUserMenu() {
        $("userMenu").classList.remove("visible");
    }

    /* ======================================================================
       10. Dashboard (仪表盘)
       ====================================================================== */

    /** 加载仪表盘数据 */
    async function loadDashboard() {
        updateWelcomeTime();
        await Promise.allSettled([loadStats(), loadActivity()]);
    }

    /** 更新欢迎时间 */
    function updateWelcomeTime() {
        const now = new Date();
        const timeStr = now.toLocaleString("zh-CN", {
            year: "numeric", month: "2-digit", day: "2-digit",
            hour: "2-digit", minute: "2-digit", second: "2-digit",
        });
        if ($("welcomeTime")) $("welcomeTime").textContent = timeStr;

        // 更新欢迎语
        if (state.currentUser) {
            const name = state.currentUser.username || "用户";
            const hour = now.getHours();
            let greeting = "晚上好";
            if (hour < 6) greeting = "凌晨好";
            else if (hour < 12) greeting = "早上好";
            else if (hour < 14) greeting = "中午好";
            else if (hour < 18) greeting = "下午好";
            $("welcomeTitle").textContent = `${greeting}，${name}`;
        }
    }

    /** 加载统计数据 */
    async function loadStats() {
        try {
            // 并行加载面板与机器人
            const [panelsRes, botsRes] = await Promise.allSettled([
                api("/panels"),
                api("/bots"),
            ]);

            let panelCount = 0, botCount = 0, accountCount = 0;

            if (panelsRes.status === "fulfilled" && panelsRes.value.success) {
                state.panels = panelsRes.value.data || [];
                panelCount = state.panels.length;
            }
            if (botsRes.status === "fulfilled" && botsRes.value.success) {
                state.bots = botsRes.value.data || [];
                botCount = state.bots.filter((b) => b.status === "running" || b.status === "active").length;
                // 统计唯一游戏账号
                const accounts = new Set();
                state.bots.forEach((b) => {
                    if (b.account_id) accounts.add(b.account_id);
                });
                accountCount = accounts.size;
            }

            // 更新数字
            $("statPanels").textContent = panelCount;
            $("statActiveBots").textContent = botCount;
            $("statAccounts").textContent = accountCount;

            // 更新侧边栏徽章
            $("badgePanels").textContent = panelCount;
            $("badgeBots").textContent = state.bots.length;

            // 卡密数量 (仅管理员可见)
            const role = state.currentUser ? state.currentUser.role : "user";
            if (role === "admin" || role === "superadmin") {
                try {
                    const statsRes = await api("/cards/stats");
                    if (statsRes.success && statsRes.data) {
                        state.cardStats = statsRes.data;
                        $("statCards").textContent = statsRes.data.total || 0;
                    }
                } catch (_) {
                    $("statCards").textContent = "-";
                }
            } else {
                $("statCards").textContent = "-";
            }
        } catch (_) { /* 已由 api() 处理 */ }
    }

    /** 加载最近活动 (用户日志) */
    async function loadActivity() {
        const list = $("activityList");
        try {
            if (!state.currentUser) return;
            const userId = state.currentUser.user_id || state.currentUser.id;
            if (!userId) {
                list.innerHTML = renderEmptyState("fa-inbox", "暂无活动", "最近的操作记录将显示在这里");
                return;
            }
            const res = await api(`/logs/user/${userId}`);
            if (res.success && res.data && res.data.length > 0) {
                // 取最近 10 条
                const logs = res.data.slice(0, 10);
                list.innerHTML = logs.map((log) => {
                    const color = LOG_COLORS[log.level] || LOG_COLORS.info;
                    const time = formatTime(log.created_at || log.timestamp);
                    return `
                        <div class="activity-item" style="display:flex;gap:12px;padding:10px 0;border-bottom:1px solid #21262d;">
                            <div style="width:8px;height:8px;border-radius:50%;background:${color};margin-top:6px;flex-shrink:0;"></div>
                            <div style="flex:1;min-width:0;">
                                <div style="font-size:13px;color:#e6edf3;word-break:break-word;">${escapeHtml(log.message || log.action || "")}</div>
                                <div style="font-size:11px;color:#7d8590;margin-top:2px;">${escapeHtml(time)}</div>
                            </div>
                        </div>
                    `;
                }).join("");
            } else {
                list.innerHTML = renderEmptyState("fa-inbox", "暂无活动", "最近的操作记录将显示在这里");
            }
        } catch (_) {
            list.innerHTML = renderEmptyState("fa-inbox", "暂无活动", "最近的操作记录将显示在这里");
        }
    }

    /* ======================================================================
       11. 面板管理
       ====================================================================== */

    /** 加载面板列表 */
    async function loadPanels() {
        const grid = $("panelsGrid");
        try {
            const res = await api("/panels");
            if (res.success) {
                state.panels = res.data || [];
                renderPanels(state.panels);
                // 更新徽章
                $("badgePanels").textContent = state.panels.length;
                $("statPanels").textContent = state.panels.length;
            }
        } catch (_) { /* 已处理 */ }
    }

    /**
     * 渲染面板卡片列表
     * @param {array} panels - 面板数组
     */
    function renderPanels(panels) {
        const grid = $("panelsGrid");
        if (!panels || panels.length === 0) {
            grid.innerHTML = `
                <div class="empty-state" id="panelsEmpty">
                    <i class="fas fa-inbox"></i>
                    <h3>暂无面板</h3>
                    <p>点击"创建面板"按钮，使用面板卡密创建您的第一个面板</p>
                </div>`;
            return;
        }
        grid.innerHTML = panels.map((panel) => {
            const panelId = panel.panel_id || panel.id;
            const status = panel.status || "active";
            const expireAt = formatTime(panel.expire_at);
            const createdAt = formatTime(panel.created_at);
            const remaining = panel.remaining_seconds ? formatRemaining(panel.remaining_seconds) : null;
            return `
                <div class="card panel-card" data-panel-id="${escapeHtml(panelId)}" style="cursor:pointer;transition:transform 0.2s,border-color 0.2s;" onmouseover="this.style.transform='translateY(-2px)';this.style.borderColor='#58a6ff';" onmouseout="this.style.transform='';this.style.borderColor='';">
                    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;">
                        <div style="display:flex;align-items:center;gap:8px;">
                            <i class="fas fa-server" style="color:#58a6ff;"></i>
                            <span style="font-size:15px;font-weight:600;">${escapeHtml(panel.name || "未命名面板")}</span>
                        </div>
                        ${getStatusBadge(status)}
                    </div>
                    <div style="display:flex;flex-direction:column;gap:6px;font-size:12px;color:#7d8590;">
                        <div style="display:flex;justify-content:space-between;">
                            <span>面板 ID</span>
                            <span class="mono" style="color:#e6edf3;">${escapeHtml(panelId)}</span>
                        </div>
                        <div style="display:flex;justify-content:space-between;">
                            <span>到期时间</span>
                            <span style="color:#e6edf3;">${escapeHtml(expireAt)}</span>
                        </div>
                        ${remaining ? `<div style="display:flex;justify-content:space-between;"><span>剩余时间</span><span style="color:#3fb950;">${escapeHtml(remaining)}</span></div>` : ""}
                        <div style="display:flex;justify-content:space-between;">
                            <span>创建时间</span>
                            <span>${escapeHtml(createdAt)}</span>
                        </div>
                    </div>
                    <div style="display:flex;gap:8px;margin-top:14px;border-top:1px solid #21262d;padding-top:12px;">
                        <button class="btn btn-secondary btn-sm" style="flex:1;" data-action="renew" data-panel-id="${escapeHtml(panelId)}">
                            <i class="fas fa-sync-alt"></i> 续费
                        </button>
                        <button class="btn btn-danger btn-sm" style="flex:1;" data-action="delete-panel" data-panel-id="${escapeHtml(panelId)}" data-panel-name="${escapeHtml(panel.name || '')}">
                            <i class="fas fa-trash"></i> 删除
                        </button>
                    </div>
                </div>
            `;
        }).join("");

        // 绑定面板卡片点击事件
        $$(".panel-card", grid).forEach((card) => {
            card.addEventListener("click", (e) => {
                // 如果点击的是按钮，不触发卡片点击
                if (e.target.closest("button")) return;
                openPanelDetail(card.dataset.panelId);
            });
        });
        // 绑定续费按钮
        $$('[data-action="renew"]', grid).forEach((btn) => {
            btn.addEventListener("click", (e) => {
                e.stopPropagation();
                openRenewPanelModal(btn.dataset.panelId);
            });
        });
        // 绑定删除按钮
        $$('[data-action="delete-panel"]', grid).forEach((btn) => {
            btn.addEventListener("click", (e) => {
                e.stopPropagation();
                const panelId = btn.dataset.panelId;
                const panelName = btn.dataset.panelName;
                confirmAction(
                    '<i class="fas fa-exclamation-triangle"></i> 删除面板',
                    `确定要删除面板 <strong>${escapeHtml(panelName)}</strong> 吗？此操作不可撤销，面板下所有机器人将被一并删除。`,
                    () => handleDeletePanel(panelId)
                );
            });
        });
    }

    /** 打开创建面板模态框 */
    function openCreatePanelModal() {
        $("createPanelForm").reset();
        openModal("modalCreatePanel");
        setTimeout(() => $("createPanelName").focus(), 100);
    }

    /** 处理创建面板 */
    async function handleCreatePanel(e) {
        e.preventDefault();
        const name = $("createPanelName").value.trim();
        const cardKey = $("createPanelCardKey").value.trim();
        const btn = $("createPanelSubmit");

        if (!name || !cardKey) {
            toastWarn("请填写面板名称和卡密");
            return;
        }

        btn.disabled = true;
        btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> 创建中...';

        try {
            const res = await api("/panels", {
                method: "POST",
                body: { name, card_key: cardKey },
            });
            if (res.success) {
                toastSuccess("面板创建成功");
                closeModal("modalCreatePanel");
                // 刷新面板列表
                await loadPanels();
                // 如果返回了面板 ID，直接进入详情
                if (res.data && (res.data.panel_id || res.data.id)) {
                    openPanelDetail(res.data.panel_id || res.data.id);
                }
            } else {
                toastError(res.message || "创建失败");
            }
        } catch (_) { /* 已处理 */ } finally {
            btn.disabled = false;
            btn.innerHTML = '<i class="fas fa-check"></i> 创建';
        }
    }

    /**
     * 打开面板详情视图
     * @param {string} panelId - 面板 ID
     */
    function openPanelDetail(panelId) {
        state.currentPanelId = panelId;
        switchView("panel-detail");
        loadPanelDetail();
    }

    /** 加载面板详情 */
    async function loadPanelDetail() {
        const panelId = state.currentPanelId;
        if (!panelId) return;

        // 重置控制台
        clearTerminal();
        // 清空上一面板的状态信息, 避免与新面板混淆
        state.panelInfoText = "";
        refreshConsoleInfo();
        appendTerminal("正在加载面板信息...", "system");
        switchConsoleTab("console");

        try {
            // 并行加载面板详情与到期检查
            const [panelRes, checkRes] = await Promise.allSettled([
                api(`/panels/${panelId}`),
                api(`/panels/${panelId}/check`, { method: "POST" }),
            ]);

            if (panelRes.status === "fulfilled" && panelRes.value.success) {
                state.panelDetail = panelRes.value.data;
                const panel = state.panelDetail;
                $("detailPanelName").textContent = panel.name || "未命名面板";
                $("detailPanelStatus").textContent = panel.status || "active";
                $("detailPanelStatus").className = "badge";
                $("detailPanelStatus").style.cssText = `background:${(STATUS_COLORS[panel.status] || "#7d8590")}22;color:${STATUS_COLORS[panel.status] || "#7d8590"};border:1px solid ${(STATUS_COLORS[panel.status] || "#7d8590")}44;padding:2px 10px;border-radius:9999px;font-size:11px;font-weight:600;`;
                $("detailPanelId").textContent = panel.panel_id || panel.id || panelId;
            }

            // 显示到期检查信息
            if (checkRes.status === "fulfilled" && checkRes.value.success) {
                const check = checkRes.value.data;
                const remaining = check.remaining_seconds != null
                    ? formatRemaining(check.remaining_seconds)
                    : "未知";
                // 将面板状态保存到 state, 与 WebSocket 状态组合显示在 consoleInfo
                state.panelInfoText = `状态: ${check.status || "未知"} | 剩余: ${remaining}`;
                refreshConsoleInfo();
                appendTerminal(`面板状态: ${check.status || "未知"}`, "info");
                appendTerminal(`到期时间: ${formatTime(check.expire_at)}`, "info");
                appendTerminal(`剩余时间: ${remaining}`, "info");
                if (check.remaining_seconds != null && check.remaining_seconds <= 0) {
                    appendTerminal("警告: 面板已过期，请续费后使用", "warn");
                }
            }

            // 加载面板关联的机器人
            await loadPanelBot();
        } catch (_) { /* 已处理 */ }
    }

    /** 加载面板关联的机器人 */
    async function loadPanelBot() {
        const panelId = state.currentPanelId;
        if (!panelId) return;
        try {
            const res = await api(`/bots?panel_id=${encodeURIComponent(panelId)}`);
            if (res.success && res.data && res.data.length > 0) {
                state.panelBot = res.data[0];
                state.currentBotId = state.panelBot.bot_id || state.panelBot.id;
                appendTerminal(`机器人已加载: ${state.panelBot.name || "未命名"}`, "success");
                appendTerminal(`机器人状态: ${state.panelBot.status || "unknown"}`, "info");
            } else {
                state.panelBot = null;
                state.currentBotId = null;
                appendTerminal('该面板尚未创建机器人，请在「设置」中配置并创建', "warn");
            }
        } catch (_) { /* 已处理 */ }
    }

    /**
     * 打开续费面板模态框
     * @param {string} panelId - 面板 ID
     */
    function openRenewPanelModal(panelId) {
        const panel = state.panels.find((p) => (p.panel_id || p.id) === panelId);
        $("renewPanelName").value = panel ? (panel.name || "") : "";
        $("renewPanelId").value = panelId;
        $("renewCardKey").value = "";
        openModal("modalRenewPanel");
        setTimeout(() => $("renewCardKey").focus(), 100);
    }

    /** 处理面板续费 */
    async function handleRenewPanel(e) {
        e.preventDefault();
        const panelId = $("renewPanelId").value;
        const cardKey = $("renewCardKey").value.trim();
        const btn = $("renewPanelSubmit");

        if (!cardKey) {
            toastWarn("请输入续期卡密");
            return;
        }

        btn.disabled = true;
        btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> 续费中...';

        try {
            const res = await api(`/panels/${panelId}/renew`, {
                method: "POST",
                body: { card_key: cardKey },
            });
            if (res.success) {
                toastSuccess("面板续费成功");
                closeModal("modalRenewPanel");
                await loadPanels();
                // 如果当前在详情页，刷新详情
                if (state.currentView === "panel-detail" && state.currentPanelId === panelId) {
                    loadPanelDetail();
                }
            } else {
                toastError(res.message || "续费失败");
            }
        } catch (_) { /* 已处理 */ } finally {
            btn.disabled = false;
            btn.innerHTML = '<i class="fas fa-check"></i> 确认续费';
        }
    }

    /**
     * 处理删除面板
     * @param {string} panelId - 面板 ID
     */
    async function handleDeletePanel(panelId) {
        try {
            const res = await api(`/panels/${panelId}`, { method: "DELETE" });
            if (res.success || res === true) {
                toastSuccess("面板已删除");
                await loadPanels();
                // 如果当前在详情页，返回列表
                if (state.currentView === "panel-detail" && state.currentPanelId === panelId) {
                    switchView("panels");
                }
            } else {
                toastError(res.message || "删除失败");
            }
        } catch (_) { /* 已处理 */ }
    }

    /* ======================================================================
       11.5 WebSocket 连接管理 (指数退避重连)
       --------------------------------------------------------------------------
       - 登录成功后建立全局 WebSocket 连接, 用于接收机器人日志/状态/聊天广播
       - 断线后使用指数退避重连: 初始 1s, 每次翻倍, 上限 60s
       - 连接成功后重置退避计数
       - 在终端与 consoleInfo 工具栏实时显示重连状态
       ====================================================================== */

    /**
     * 初始化 WebSocket 连接 (幂等: 已连接或正在连接时直接返回)
     */
    function initWebSocket() {
        // 已存在且处于连接/已连接状态, 跳过
        if (state.ws && (state.ws.readyState === WebSocket.OPEN || state.ws.readyState === WebSocket.CONNECTING)) {
            return;
        }
        // 未登录则不连接
        if (!state.token) return;

        state.wsManuallyClosed = false;
        const url = WS_BASE + "?token=" + encodeURIComponent(state.token);
        updateWsStatus(false, "连接中...");

        let ws;
        try {
            ws = new WebSocket(url);
        } catch (e) {
            // 构造失败 (如协议不支持), 安排重连
            updateWsStatus(false, "连接失败");
            scheduleReconnect();
            return;
        }
        state.ws = ws;

        ws.onopen = () => {
            state.wsConnected = true;
            // 连接成功 -> 重置退避计数
            state.wsReconnectAttempts = 0;
            if (state.wsReconnectTimer) {
                clearTimeout(state.wsReconnectTimer);
                state.wsReconnectTimer = null;
            }
            updateWsStatus(true, "已连接");
            appendTerminal("WebSocket 已连接", "success");
            // 请求一次状态快照
            try { ws.send(JSON.stringify({ action: "status" })); } catch (e) { /* ignore */ }
        };

        ws.onmessage = (ev) => handleWsMessage(ev.data);

        ws.onclose = () => {
            state.wsConnected = false;
            // 主动关闭 (登出/401) 时不自动重连
            if (state.wsManuallyClosed) {
                updateWsStatus(false, "已断开");
                return;
            }
            const attempt = state.wsReconnectAttempts + 1;
            appendTerminal(`WebSocket 连接已断开, 开始重连 (第 ${attempt} 次尝试)`, "warn");
            scheduleReconnect();
        };

        ws.onerror = () => {
            // onclose 会随后触发, 由其负责重连; 这里仅更新状态
            updateWsStatus(false, "连接错误");
        };
    }

    /**
     * 主动关闭 WebSocket (登出 / 登录过期时调用), 不会触发自动重连
     */
    function closeWebSocket() {
        state.wsManuallyClosed = true;
        if (state.wsReconnectTimer) {
            clearTimeout(state.wsReconnectTimer);
            state.wsReconnectTimer = null;
        }
        state.wsReconnectAttempts = 0;
        state.wsConnected = false;
        if (state.ws) {
            try {
                state.ws.onclose = null;  // 阻止 onclose 触发重连
                state.ws.close();
            } catch (_) { /* ignore */ }
            state.ws = null;
        }
        updateWsStatus(false, "未连接");
    }

    /**
     * 指数退避重连调度
     * 间隔 = min(INITIAL * 2^attempts, MAX), 即 1s, 2s, 4s, 8s ... 60s
     */
    function scheduleReconnect() {
        // 主动关闭后不再重连
        if (state.wsManuallyClosed) return;
        // 未登录不再重连
        if (!state.token) return;
        // 已有定时器在等待
        if (state.wsReconnectTimer) return;

        state.wsReconnectAttempts++;
        const attempts = state.wsReconnectAttempts;
        // 每次翻倍: 1s, 2s, 4s, 8s ... 上限 60s
        const delay = Math.min(
            WS_RECONNECT_INITIAL_MS * Math.pow(2, attempts - 1),
            WS_RECONNECT_MAX_MS
        );
        const delaySec = Math.round(delay / 1000);
        updateWsStatus(false, `重连中... (第 ${attempts} 次尝试, ${delaySec}s)`);

        state.wsReconnectTimer = setTimeout(() => {
            state.wsReconnectTimer = null;
            if (state.wsManuallyClosed || !state.token) return;
            initWebSocket();
        }, delay);
    }

    /**
     * 更新 WebSocket 连接状态 (UI + 内部状态)
     * @param {boolean} connected - 是否已连接
     * @param {string} text - 状态描述文本
     */
    function updateWsStatus(connected, text) {
        state.wsConnected = connected;
        state.wsStatusText = text;
        refreshConsoleInfo();
    }

    /**
     * 刷新控制台工具栏的连接/面板状态显示
     * 将 WebSocket 状态与面板信息组合显示在 consoleInfo 中
     */
    function refreshConsoleInfo() {
        const node = $("consoleInfo");
        if (!node) return;
        const wsPart = `WS: ${state.wsStatusText || "未连接"}`;
        if (state.panelInfoText) {
            node.textContent = `${wsPart} | ${state.panelInfoText}`;
        } else {
            node.textContent = wsPart;
        }
    }

    /**
     * 处理 WebSocket 接收到的消息
     * @param {string} raw - 原始消息文本 (JSON)
     */
    function handleWsMessage(raw) {
        let msg;
        try {
            msg = JSON.parse(raw);
        } catch (_) { return; }
        const type = msg.type;
        const data = msg.data || {};
        switch (type) {
            case "pong":
                // 心跳响应, 忽略
                break;
            case "bot_status": {
                // 机器人状态变更 - 若当前面板机器人匹配则刷新终端显示
                if (state.currentBotId && data.bot_id === state.currentBotId) {
                    if (data.status) {
                        appendTerminal(`机器人状态更新: ${data.status}`, "info");
                    }
                    loadPanelBot();
                }
                break;
            }
            case "logs": {
                // 机器人日志广播
                const logs = data.logs || [];
                if (Array.isArray(logs)) {
                    logs.forEach((l) => {
                        appendTerminal(l.message || l, l.level || "info");
                    });
                }
                break;
            }
            case "chat": {
                // 游戏内聊天消息
                if (data.username && data.message) {
                    appendTerminal(`<${data.username}> ${data.message}`, "info");
                }
                break;
            }
            default:
                // 未知消息类型, 调试时可在终端查看
                break;
        }
    }

    /* ======================================================================
       12. 面板详情 - 控制台 (终端)
       ====================================================================== */

    /**
     * 向终端追加输出
     * @param {string} text - 输出文本
     * @param {string} level - 级别: system/info/success/error/warn
     */
    function appendTerminal(text, level = "info") {
        const output = $("terminalOutput");
        if (!output) return;
        const color = LOG_COLORS[level] || LOG_COLORS.info;
        const prefix = level === "system" ? "[SYSTEM]" : level === "error" ? "[ERROR]" : level === "warn" ? "[WARN]" : level === "success" ? "[OK]" : "[INFO]";
        const time = new Date().toLocaleTimeString("zh-CN", { hour12: false });
        const line = document.createElement("div");
        line.className = "terminal-line " + level;
        line.style.cssText = `color:${color};padding:2px 0;font-family:var(--font-mono);font-size:13px;line-height:1.5;word-break:break-word;white-space:pre-wrap;`;
        line.innerHTML = `<span class="ts" style="color:#484f58;margin-right:6px;">[${time}]</span><span style="font-weight:600;margin-right:4px;">${prefix}</span>${escapeHtml(text)}`;
        output.appendChild(line);

        // 终端输出上限: 超过 MAX_TERMINAL_LINES 时, 从最旧的行开始批量删除,
        // 一次删除到上限以下, 避免每次追加都触发 DOM 操作带来的性能开销。
        const overflow = output.childElementCount - MAX_TERMINAL_LINES;
        if (overflow > 0) {
            for (let i = 0; i < overflow; i++) {
                if (output.firstChild) output.removeChild(output.firstChild);
            }
        }

        // 自动滚动
        if (state.consoleAutoscroll) {
            output.scrollTop = output.scrollHeight;
        }
    }

    /** 清空终端输出 */
    function clearTerminal() {
        const output = $("terminalOutput");
        if (output) output.innerHTML = "";
    }

    /** 切换自动滚动 */
    function toggleAutoscroll() {
        state.consoleAutoscroll = !state.consoleAutoscroll;
        const btn = $("consoleAutoscrollBtn");
        btn.style.color = state.consoleAutoscroll ? "#3fb950" : "#7d8590";
        toastInfo(`自动滚动已${state.consoleAutoscroll ? "开启" : "关闭"}`);
    }

    /**
     * 发送终端命令
     * @param {string} cmd - 命令文本
     */
    async function sendTerminalCommand(cmd) {
        const cmdTrim = cmd.trim();
        if (!cmdTrim) return;

        // 回显命令
        appendTerminal(`$ ${cmdTrim}`, "system");

        // 记录历史
        state.terminalHistory.push(cmdTrim);
        state.terminalHistoryIndex = state.terminalHistory.length;

        // 内置命令
        if (cmdTrim === "clear" || cmdTrim === "cls") {
            clearTerminal();
            return;
        }
        if (cmdTrim === "help") {
            appendTerminal("可用命令: clear (清屏), help (帮助), status (查看状态)", "info");
            return;
        }
        if (cmdTrim === "status") {
            if (state.panelBot) {
                appendTerminal(`机器人: ${state.panelBot.name} | 状态: ${state.panelBot.status}`, "info");
            } else {
                appendTerminal("当前无机器人", "warn");
            }
            return;
        }

        // 如果有机器人，尝试发送命令
        // 注意: 当前 API 未定义终端命令端点，此处做本地回显
        // 如后端支持，可在此处调用 POST /bots/{bot_id}/command 等
        if (!state.currentBotId) {
            appendTerminal("没有可用的机器人，无法发送命令", "error");
            return;
        }

        appendTerminal(`命令已发送: ${cmdTrim}`, "info");
        // TODO: 后端支持后，可在此调用:
        // try {
        //     const res = await api(`/bots/${state.currentBotId}/command`, {
        //         method: "POST",
        //         body: { command: cmdTrim },
        //     });
        //     if (res.success && res.data && res.data.output) {
        //         appendTerminal(res.data.output, "info");
        //     }
        // } catch (_) {}
    }

    /** 启动机器人 */
    async function startBot() {
        if (!ensureBotExists()) return;
        try {
            appendTerminal("正在启动机器人...", "system");
            const res = await api(`/bots/${state.currentBotId}/start`, { method: "POST" });
            if (res.success) {
                appendTerminal("机器人启动成功", "success");
                toastSuccess("机器人已启动");
                await loadPanelBot();
            }
        } catch (_) { /* 已处理 */ }
    }

    /** 停止机器人 */
    async function stopBot() {
        if (!ensureBotExists()) return;
        try {
            appendTerminal("正在停止机器人...", "system");
            const res = await api(`/bots/${state.currentBotId}/stop`, { method: "POST" });
            if (res.success) {
                appendTerminal("机器人已停止", "success");
                toastSuccess("机器人已停止");
                await loadPanelBot();
            }
        } catch (_) { /* 已处理 */ }
    }

    /** 重启机器人 */
    async function restartBot() {
        if (!ensureBotExists()) return;
        try {
            appendTerminal("正在重启机器人...", "system");
            const res = await api(`/bots/${state.currentBotId}/restart`, { method: "POST" });
            if (res.success) {
                appendTerminal("机器人重启成功", "success");
                toastSuccess("机器人已重启");
                await loadPanelBot();
            }
        } catch (_) { /* 已处理 */ }
    }

    /** 检查是否存在机器人 */
    function ensureBotExists() {
        if (!state.currentBotId) {
            toastWarn('该面板尚未创建机器人，请先在「设置」中配置');
            appendTerminal("操作失败: 没有可用的机器人", "error");
            return false;
        }
        return true;
    }

    /* ======================================================================
       13. 面板详情 - 日志
       ====================================================================== */

    /** 加载面板日志 */
    async function loadPanelLogs() {
        const viewer = $("panelLogViewer");
        if (!state.currentPanelId) return;
        try {
            const res = await api(`/logs/panel/${state.currentPanelId}`);
            if (res.success && res.data && res.data.length > 0) {
                renderLogs(res.data, viewer);
            } else {
                viewer.innerHTML = renderEmptyState("fa-file-alt", "暂无日志", "面板操作日志将显示在这里");
            }
        } catch (_) {
            viewer.innerHTML = renderEmptyState("fa-file-alt", "暂无日志", "面板操作日志将显示在这里");
        }
    }

    /* ======================================================================
       14. 面板详情 - 机器人配置 (设置)
       ====================================================================== */

    /** 加载机器人配置到表单 */
    async function loadBotConfig() {
        try {
            if (state.panelBot) {
                const bot = state.panelBot;
                $("botConfigName").value = bot.name || "";
                $("botConfigAccount").value = bot.account_id || "";
                $("botConfigServerCode").value = bot.server_code || "";
                $("botConfigServerType").value = bot.server_type || "rental";
                $("botConfigAccessPoint").value = bot.access_point || "neomega";
                $("botConfigExtra").value = bot.extra_config
                    ? (typeof bot.extra_config === "string"
                        ? bot.extra_config
                        : JSON.stringify(bot.extra_config, null, 2))
                    : "";
            } else {
                // 没有机器人 - 清空表单准备创建
                $("botConfigForm").reset();
            }
        } catch (_) { /* 已处理 */ }
    }

    /** 保存机器人配置 (创建或更新) */
    async function handleSaveBotConfig(e) {
        e.preventDefault();
        const name = $("botConfigName").value.trim();
        const accountId = $("botConfigAccount").value.trim();
        const serverCode = $("botConfigServerCode").value.trim();
        const serverType = $("botConfigServerType").value;
        const accessPoint = $("botConfigAccessPoint").value;
        const extraRaw = $("botConfigExtra").value.trim();

        if (!name) {
            toastWarn("请填写机器人名称");
            return;
        }

        // 解析额外配置 JSON
        let extra = {};
        if (extraRaw) {
            try {
                extra = JSON.parse(extraRaw);
            } catch (_) {
                toastError("额外配置 JSON 格式错误");
                return;
            }
        }

        const payload = {
            name,
            account_id: accountId,
            server_code: serverCode,
            server_type: serverType,
            access_point: accessPoint,
            ...extra,
        };

        try {
            if (state.currentBotId) {
                // 更新已有机器人配置
                const res = await api(`/bots/${state.currentBotId}/config`, {
                    method: "PUT",
                    body: payload,
                });
                if (res.success) {
                    toastSuccess("配置已保存");
                    await loadPanelBot();
                }
            } else {
                // 创建新机器人
                if (!state.currentPanelId) {
                    toastError("缺少面板 ID");
                    return;
                }
                payload.panel_id = state.currentPanelId;
                const res = await api("/bots", {
                    method: "POST",
                    body: payload,
                });
                if (res.success) {
                    toastSuccess("机器人创建成功");
                    if (res.data) {
                        state.currentBotId = res.data.bot_id || res.data.id;
                    }
                    await loadPanelBot();
                    await loadBotConfig();
                }
            }
        } catch (_) { /* 已处理 */ }
    }

    /** 重置机器人配置表单 */
    function resetBotConfig() {
        loadBotConfig();
        toastInfo("配置已重置");
    }

    /* ======================================================================
       15. 机器人管理
       ====================================================================== */

    /** 加载机器人列表 */
    async function loadBots() {
        const list = $("botsList");
        try {
            const res = await api("/bots");
            if (res.success) {
                state.bots = res.data || [];
                renderBots(state.bots);
                // 更新徽章
                $("badgeBots").textContent = state.bots.length;
            }
        } catch (_) { /* 已处理 */ }
    }

    /**
     * 渲染机器人列表
     * @param {array} bots - 机器人数组
     */
    function renderBots(bots) {
        const list = $("botsList");
        if (!bots || bots.length === 0) {
            list.innerHTML = `
                <div class="empty-state" id="botsEmpty">
                    <i class="fas fa-robot"></i>
                    <h3>暂无机器人</h3>
                    <p>前往面板详情页面创建机器人实例</p>
                </div>`;
            return;
        }
        list.innerHTML = bots.map((bot) => {
            const botId = bot.bot_id || bot.id;
            const status = bot.status || "idle";
            const isRunning = status === "running" || status === "active" || status === "connected";
            return `
                <div class="card" style="display:flex;align-items:center;justify-content:space-between;gap:16px;padding:16px 20px;margin-bottom:12px;">
                    <div style="display:flex;align-items:center;gap:14px;flex:1;min-width:0;">
                        <div style="width:40px;height:40px;border-radius:10px;background:linear-gradient(135deg,#58a6ff,#a371f7);display:flex;align-items:center;justify-content:center;flex-shrink:0;">
                            <i class="fas fa-robot" style="color:#fff;font-size:18px;"></i>
                        </div>
                        <div style="flex:1;min-width:0;">
                            <div style="display:flex;align-items:center;gap:8px;">
                                <span style="font-size:14px;font-weight:600;">${escapeHtml(bot.name || "未命名")}</span>
                                ${getStatusBadge(status)}
                            </div>
                            <div style="font-size:12px;color:#7d8590;margin-top:4px;">
                                <span class="mono">${escapeHtml(botId)}</span>
                                ${bot.account_id ? ` · 账号: ${escapeHtml(bot.account_id)}` : ""}
                                ${bot.server_code ? ` · 服务器: ${escapeHtml(bot.server_code)}` : ""}
                            </div>
                        </div>
                    </div>
                    <div style="display:flex;gap:8px;flex-shrink:0;">
                        <button class="btn btn-success btn-sm" data-bot-action="start" data-bot-id="${escapeHtml(botId)}" ${isRunning ? "disabled" : ""}>
                            <i class="fas fa-play"></i> 启动
                        </button>
                        <button class="btn btn-danger btn-sm" data-bot-action="stop" data-bot-id="${escapeHtml(botId)}" ${!isRunning ? "disabled" : ""}>
                            <i class="fas fa-stop"></i> 停止
                        </button>
                        <button class="btn btn-secondary btn-sm" data-bot-action="detail" data-bot-id="${escapeHtml(botId)}" data-panel-id="${escapeHtml(bot.panel_id || '')}">
                            <i class="fas fa-arrow-right"></i>
                        </button>
                    </div>
                </div>
            `;
        }).join("");

        // 绑定按钮事件
        $$('[data-bot-action]', list).forEach((btn) => {
            btn.addEventListener("click", async () => {
                const action = btn.dataset.botAction;
                const botId = btn.dataset.botId;
                try {
                    if (action === "start") {
                        const res = await api(`/bots/${botId}/start`, { method: "POST" });
                        if (res.success) { toastSuccess("机器人已启动"); await loadBots(); }
                    } else if (action === "stop") {
                        const res = await api(`/bots/${botId}/stop`, { method: "POST" });
                        if (res.success) { toastSuccess("机器人已停止"); await loadBots(); }
                    } else if (action === "detail") {
                        const panelId = btn.dataset.panelId;
                        if (panelId) {
                            openPanelDetail(panelId);
                        } else {
                            toastInfo("该机器人未关联面板");
                        }
                    }
                } catch (_) { /* 已处理 */ }
            });
        });
    }

    /* ======================================================================
       16. 卡密管理
       ====================================================================== */

    /** 加载卡密统计 */
    async function loadCardStats() {
        try {
            const res = await api("/cards/stats");
            if (res.success && res.data) {
                state.cardStats = res.data;
                $("cardStatTotal").textContent = res.data.total || 0;
                $("cardStatUnused").textContent = res.data.unused || 0;
                $("cardStatUsed").textContent = res.data.used || 0;
                $("cardStatRevoked").textContent = res.data.revoked || 0;
            }
        } catch (_) { /* 已处理 */ }
    }

    /** 加载卡密列表 (带筛选) */
    async function loadCards() {
        const tbody = $("cardsTableBody");
        try {
            const params = new URLSearchParams();
            if (state.cardFilterType) params.set("key_type", state.cardFilterType);
            if (state.cardFilterStatus) params.set("status", state.cardFilterStatus);
            const query = params.toString() ? `?${params.toString()}` : "";
            const res = await api(`/cards${query}`);
            if (res.success) {
                state.cards = res.data || [];
                renderCards(state.cards);
            }
        } catch (_) {
            tbody.innerHTML = `<tr><td colspan="7" style="text-align:center;padding:24px;color:#7d8590;">加载失败，请重试</td></tr>`;
        }
    }

    /**
     * 渲染卡密表格
     * @param {array} cards - 卡密数组
     */
    function renderCards(cards) {
        const tbody = $("cardsTableBody");
        if (!cards || cards.length === 0) {
            tbody.innerHTML = `<tr><td colspan="7" style="text-align:center;padding:24px;color:#7d8590;">暂无卡密数据</td></tr>`;
            return;
        }
        tbody.innerHTML = cards.map((card) => {
            const cardId = card.card_id || card.id;
            const key = card.key || card.card_key || "";
            const type = card.key_type || "register";
            const status = (card.status || "unused").toLowerCase();
            const duration = formatDuration(card.duration_days);
            const createdAt = formatTime(card.created_at);
            const usedAt = card.used_at ? formatTime(card.used_at) : "-";
            const typeLabel = CARD_TYPE_LABELS[type] || type;
            return `
                <tr>
                    <td>
                        <span class="mono" style="cursor:pointer;color:#58a6ff;" title="点击复制" data-copy="${escapeHtml(key)}">${escapeHtml(key)}</span>
                    </td>
                    <td>${escapeHtml(typeLabel)}</td>
                    <td>${getStatusBadge(status)}</td>
                    <td>${escapeHtml(duration)}</td>
                    <td>${escapeHtml(createdAt)}</td>
                    <td>${escapeHtml(usedAt)}</td>
                    <td>
                        ${status === "unused"
                            ? `<button class="btn btn-danger btn-sm" data-revoke="${escapeHtml(cardId)}"><i class="fas fa-ban"></i> 撤销</button>`
                            : `<span style="color:#484f58;font-size:12px;">-</span>`}
                    </td>
                </tr>
            `;
        }).join("");

        // 绑定复制事件
        $$("[data-copy]", tbody).forEach((el) => {
            el.addEventListener("click", () => copyToClipboard(el.dataset.copy));
        });
        // 绑定撤销事件
        $$("[data-revoke]", tbody).forEach((btn) => {
            btn.addEventListener("click", () => {
                const cardId = btn.dataset.revoke;
                confirmAction(
                    '<i class="fas fa-ban"></i> 撤销卡密',
                    `确定要撤销此卡密吗？撤销后不可恢复。`,
                    () => revokeCard(cardId)
                );
            });
        });
    }

    /** 打开生成卡密模态框 */
    function openCreateCardModal() {
        $("createCardForm").reset();
        $("cardKeyDuration").value = "permanent";
        $("cardKeyCount").value = "1";
        $("cardKeyResults").style.display = "none";
        $("cardKeyResultsList").innerHTML = "";
        openModal("modalCreateCard");
    }

    /** 处理生成卡密 */
    async function handleCreateCard(e) {
        e.preventDefault();
        const keyType = $("cardKeyType").value;
        const duration = $("cardKeyDuration").value;
        const count = parseInt($("cardKeyCount").value, 10);
        const expires = $("cardKeyExpires").value;
        const btn = $("createCardSubmit");

        if (!keyType || !duration || !count || count < 1 || count > 100) {
            toastWarn("请检查表单填写 (数量 1-100)");
            return;
        }

        btn.disabled = true;
        btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> 生成中...';

        try {
            const payload = { key_type: keyType, duration, count };
            if (expires) payload.expires_at = new Date(expires).toISOString();

            const res = await api("/cards", {
                method: "POST",
                body: payload,
            });
            if (res.success && res.data && res.data.cards) {
                toastSuccess(`成功生成 ${res.data.cards.length} 个卡密`);
                // 显示结果
                $("cardKeyResults").style.display = "block";
                $("cardKeyResultsList").innerHTML = res.data.cards.map((card) => {
                    const key = card.key || card.card_key || "";
                    return `
                        <div style="display:flex;align-items:center;justify-content:space-between;gap:10px;padding:10px 12px;background:#0d1117;border:1px solid #30363d;border-radius:8px;">
                            <span class="mono" style="font-size:13px;color:#58a6ff;word-break:break-all;flex:1;">${escapeHtml(key)}</span>
                            <button class="btn btn-secondary btn-sm" data-copy="${escapeHtml(key)}" style="flex-shrink:0;">
                                <i class="fas fa-copy"></i> 复制
                            </button>
                        </div>
                    `;
                }).join("");
                // 绑定复制按钮
                $$("[data-copy]", $("cardKeyResultsList")).forEach((el) => {
                    el.addEventListener("click", () => copyToClipboard(el.dataset.copy));
                });
                // 刷新卡密列表与统计
                await loadCardStats();
                await loadCards();
                await loadCardCreationLogs();
            } else {
                toastError(res.message || "生成失败");
            }
        } catch (_) { /* 已处理 */ } finally {
            btn.disabled = false;
            btn.innerHTML = '<i class="fas fa-magic"></i> 生成卡密';
        }
    }

    /**
     * 撤销卡密
     * @param {string} cardId - 卡密 ID
     */
    async function revokeCard(cardId) {
        try {
            const res = await api(`/cards/${cardId}/revoke`, { method: "POST" });
            if (res.success) {
                toastSuccess("卡密已撤销");
                await loadCardStats();
                await loadCards();
            }
        } catch (_) { /* 已处理 */ }
    }

    /** 加载卡密创建日志 */
    async function loadCardCreationLogs() {
        const container = $("cardCreationLogs");
        try {
            const res = await api("/cards/logs/creation");
            if (res.success && res.data && res.data.length > 0) {
                const logs = res.data;
                container.innerHTML = logs.slice(0, 20).map((log) => {
                    const time = formatTime(log.created_at || log.timestamp);
                    // 解析 details JSON 获取创建信息
                    let count = 0, type = "", duration = "";
                    try {
                        const details = typeof log.details === "string" 
                            ? JSON.parse(log.details) 
                            : (log.details || {});
                        count = details.count || 0;
                        type = details.key_type || "";
                        duration = details.duration || "";
                    } catch (_) {
                        // 回退: 从 message 中解析
                        const match = (log.message || "").match(/创建\s*(\d+)\s*个\s*(\w+)\s*卡密/);
                        if (match) {
                            count = parseInt(match[1], 10) || 0;
                            type = match[2] || "";
                        }
                    }
                    const typeLabel = CARD_TYPE_LABELS[type] || type || "";
                    const durLabel = duration === "permanent" ? "永久" : (duration || "");
                    return `
                        <div style="display:flex;align-items:center;gap:12px;padding:10px 0;border-bottom:1px solid #21262d;">
                            <i class="fas fa-key" style="color:#d29922;flex-shrink:0;"></i>
                            <div style="flex:1;min-width:0;">
                                <div style="font-size:13px;">
                                    生成 <strong style="color:#3fb950;">${count}</strong> 个
                                    ${typeLabel ? `<span style="color:#58a6ff;margin-left:4px;">${escapeHtml(typeLabel)}</span>` : ""}
                                    ${durLabel ? `<span style="color:#7d8590;margin-left:4px;">(${escapeHtml(durLabel)})</span>` : ""}
                                </div>
                                <div style="font-size:11px;color:#7d8590;margin-top:2px;">${escapeHtml(time)}</div>
                            </div>
                        </div>
                    `;
                }).join("");
            } else {
                container.innerHTML = renderEmptyState("fa-file-alt", "暂无日志", "卡密创建操作记录将显示在这里");
            }
        } catch (_) {
            container.innerHTML = renderEmptyState("fa-file-alt", "暂无日志", "卡密创建操作记录将显示在这里");
        }
    }

    /* ======================================================================
       17. 用户管理
       ====================================================================== */

    /** 加载用户列表 */
    async function loadUsers() {
        const tbody = $("usersTableBody");
        try {
            const res = await api("/auth/users");
            if (res.success) {
                state.users = res.data || [];
                renderUsers(state.users);
            }
        } catch (_) {
            tbody.innerHTML = `<tr><td colspan="7" style="text-align:center;padding:24px;color:#7d8590;">加载失败</td></tr>`;
        }
    }

    /**
     * 渲染用户表格
     * @param {array} users - 用户数组
     */
    function renderUsers(users) {
        const tbody = $("usersTableBody");
        if (!users || users.length === 0) {
            tbody.innerHTML = `<tr><td colspan="7" style="text-align:center;padding:24px;color:#7d8590;">暂无用户</td></tr>`;
            return;
        }
        const currentRole = state.currentUser ? state.currentUser.role : "user";
        const isSuperadmin = currentRole === "superadmin";
        tbody.innerHTML = users.map((user) => {
            const userId = user.user_id || user.id;
            const role = user.role || "user";
            const status = user.status || "active";
            const roleLabel = ROLE_LABELS[role] || role;
            const createdAt = formatTime(user.created_at);
            const lastLogin = user.last_login ? formatTime(user.last_login) : "从未登录";
            const expireAt = user.expire_at ? formatTime(user.expire_at) : "永久";
            const isSelf = state.currentUser && (state.currentUser.user_id === userId || state.currentUser.id === userId);
            return `
                <tr>
                    <td>
                        <div style="display:flex;align-items:center;gap:8px;">
                            <div style="width:28px;height:28px;border-radius:50%;background:linear-gradient(135deg,#58a6ff,#a371f7);display:flex;align-items:center;justify-content:center;font-size:12px;color:#fff;font-weight:600;">
                                ${escapeHtml((user.username || "U").charAt(0).toUpperCase())}
                            </div>
                            <span>${escapeHtml(user.username || "")}</span>
                            ${isSelf ? '<span style="font-size:10px;color:#58a6ff;background:#58a6ff22;padding:1px 6px;border-radius:9999px;">你</span>' : ""}
                        </div>
                    </td>
                    <td>
                        <select class="filter-select" data-role-select="${escapeHtml(userId)}" ${!isSuperadmin || isSelf ? "disabled" : ""} style="font-size:12px;padding:4px 8px;">
                            <option value="user" ${role === "user" ? "selected" : ""}>普通用户</option>
                            <option value="admin" ${role === "admin" ? "selected" : ""}>管理员</option>
                            <option value="superadmin" ${role === "superadmin" ? "selected" : ""}>超级管理员</option>
                        </select>
                    </td>
                    <td>
                        <select class="filter-select" data-status-select="${escapeHtml(userId)}" ${isSelf ? "disabled" : ""} style="font-size:12px;padding:4px 8px;">
                            <option value="active" ${status === "active" ? "selected" : ""}>正常</option>
                            <option value="suspended" ${status === "suspended" ? "selected" : ""}>封禁</option>
                            <option value="expired" ${status === "expired" ? "selected" : ""}>过期</option>
                        </select>
                    </td>
                    <td style="font-size:12px;">${escapeHtml(createdAt)}</td>
                    <td style="font-size:12px;">${escapeHtml(lastLogin)}</td>
                    <td style="font-size:12px;">${escapeHtml(expireAt)}</td>
                    <td>
                        ${isSuperadmin && !isSelf
                            ? `<button class="btn btn-danger btn-sm" data-delete-user="${escapeHtml(userId)}" data-username="${escapeHtml(user.username || '')}"><i class="fas fa-trash"></i></button>`
                            : `<span style="color:#484f58;font-size:12px;">-</span>`}
                    </td>
                </tr>
            `;
        }).join("");

        // 绑定角色变更
        $$("[data-role-select]", tbody).forEach((sel) => {
            sel.addEventListener("change", () => changeUserRole(sel.dataset.roleSelect, sel.value));
        });
        // 绑定状态变更
        $$("[data-status-select]", tbody).forEach((sel) => {
            sel.addEventListener("change", () => changeUserStatus(sel.dataset.statusSelect, sel.value));
        });
        // 绑定删除用户
        $$("[data-delete-user]", tbody).forEach((btn) => {
            btn.addEventListener("click", () => {
                const userId = btn.dataset.deleteUser;
                const username = btn.dataset.username;
                confirmAction(
                    '<i class="fas fa-user-times"></i> 删除用户',
                    `确定要删除用户 <strong>${escapeHtml(username)}</strong> 吗？此操作不可撤销。`,
                    () => deleteUser(userId)
                );
            });
        });
    }

    /** 打开创建用户模态框 */
    function openCreateUserModal() {
        $("createUserForm").reset();
        $("newUserRole").value = "user";
        openModal("modalCreateUser");
        setTimeout(() => $("newUsername").focus(), 100);
    }

    /** 处理创建用户 */
    async function handleCreateUser(e) {
        e.preventDefault();
        const username = $("newUsername").value.trim();
        const password = $("newPassword").value;
        const role = $("newUserRole").value;
        const durationDays = $("newUserDuration").value;
        const btn = $("createUserSubmit");

        if (!username || !password) {
            toastWarn("请填写用户名和密码");
            return;
        }

        btn.disabled = true;
        btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> 创建中...';

        try {
            const payload = { username, password, role };
            if (durationDays) payload.duration_days = parseInt(durationDays, 10);

            const res = await api("/auth/users", {
                method: "POST",
                body: payload,
            });
            if (res.success) {
                toastSuccess("用户创建成功");
                closeModal("modalCreateUser");
                await loadUsers();
            } else {
                toastError(res.message || "创建失败");
            }
        } catch (_) { /* 已处理 */ } finally {
            btn.disabled = false;
            btn.innerHTML = '<i class="fas fa-check"></i> 创建';
        }
    }

    /**
     * 删除用户
     * @param {string} userId - 用户 ID
     */
    async function deleteUser(userId) {
        try {
            const res = await api(`/auth/users/${userId}`, { method: "DELETE" });
            if (res.success || res === true) {
                toastSuccess("用户已删除");
                await loadUsers();
            }
        } catch (_) { /* 已处理 */ }
    }

    /**
     * 修改用户角色
     * @param {string} userId - 用户 ID
     * @param {string} role - 新角色
     */
    async function changeUserRole(userId, role) {
        try {
            const res = await api(`/auth/users/${userId}/role?role=${encodeURIComponent(role)}`, {
                method: "PUT",
            });
            if (res.success) {
                toastSuccess("角色已更新");
            } else {
                toastError("更新失败");
                await loadUsers();
            }
        } catch (_) {
            await loadUsers();
        }
    }

    /**
     * 修改用户状态
     * @param {string} userId - 用户 ID
     * @param {string} status - 新状态
     */
    async function changeUserStatus(userId, status) {
        try {
            const res = await api(`/auth/users/${userId}/status?status=${encodeURIComponent(status)}`, {
                method: "PUT",
            });
            if (res.success) {
                toastSuccess("状态已更新");
            } else {
                toastError("更新失败");
                await loadUsers();
            }
        } catch (_) {
            await loadUsers();
        }
    }

    /* ======================================================================
       18. 系统日志
       ====================================================================== */

    /** 加载系统日志 */
    async function loadSystemLogs() {
        const viewer = $("systemLogViewer");
        try {
            const params = new URLSearchParams();
            if (state.logFilterLevel) params.set("level", state.logFilterLevel);
            const query = params.toString() ? `?${params.toString()}` : "";
            const res = await api(`/logs/system${query}`);
            if (res.success && res.data && res.data.length > 0) {
                renderLogs(res.data, viewer);
            } else {
                viewer.innerHTML = renderEmptyState("fa-file-alt", "暂无日志", "系统日志将显示在这里");
            }
        } catch (_) {
            viewer.innerHTML = renderEmptyState("fa-file-alt", "暂无日志", "系统日志将显示在这里");
        }
    }

    /**
     * 渲染日志列表到容器
     * @param {array} logs - 日志数组
     * @param {HTMLElement} container - 容器元素
     */
    function renderLogs(logs, container) {
        if (!logs || logs.length === 0) {
            container.innerHTML = renderEmptyState("fa-file-alt", "暂无日志", "");
            return;
        }
        container.innerHTML = logs.map((log) => {
            const level = log.level || "info";
            const color = LOG_COLORS[level] || LOG_COLORS.info;
            const time = formatTime(log.created_at || log.timestamp);
            const message = log.message || log.action || log.detail || "";
            const source = log.source || log.target_type || "";
            return `
                <div style="display:flex;gap:10px;padding:8px 12px;border-bottom:1px solid #21262d;font-family:var(--font-mono);font-size:12px;line-height:1.6;">
                    <span style="color:#484f58;flex-shrink:0;min-width:140px;">${escapeHtml(time)}</span>
                    <span style="color:${color};font-weight:600;flex-shrink:0;min-width:60px;text-transform:uppercase;">${escapeHtml(level)}</span>
                    ${source ? `<span style="color:#7d8590;flex-shrink:0;min-width:80px;">[${escapeHtml(source)}]</span>` : ""}
                    <span style="color:#e6edf3;word-break:break-word;flex:1;">${escapeHtml(message)}</span>
                </div>
            `;
        }).join("");
        // 滚动到最新
        container.scrollTop = 0;
    }

    /* ======================================================================
       18b. 系统管理 (nv1 + 封号检测 + 统计)
       ====================================================================== */

    /** 加载系统管理页面数据 */
    async function loadSystemAdmin() {
        await Promise.allSettled([loadNV1Status(), loadBanStatus(), loadSystemStatsDetail()]);
    }

    /** 加载 nv1 SAuth Key 状态 */
    async function loadNV1Status() {
        try {
            const res = await api("/system/nv1/status");
            if (res.success && res.data) {
                const d = res.data;
                const modeBadge = $("nv1ModeBadge");
                modeBadge.textContent = d.mode === "mock" ? "模拟模式" : "真实模式";
                modeBadge.className = "badge " + (d.mode === "mock" ? "badge-success" : "badge-warning");

                $("nv1Status").textContent = d.valid ? "有效" : "无效";
                $("nv1Status").style.color = d.valid ? "var(--color-success)" : "var(--color-danger)";

                $("nv1KeyPreview").textContent = d.key_preview || "-";
                $("nv1Remaining").textContent = d.remaining_days !== null ? d.remaining_days + " 天" : "永久";

                const needsRefresh = d.needs_refresh;
                $("nv1NeedsRefresh").textContent = needsRefresh ? "是" : "否";
                $("nv1NeedsRefresh").style.color = needsRefresh ? "var(--color-warning)" : "var(--text-secondary)";
            }
        } catch (e) {
            console.error("加载nv1状态失败:", e);
        }
    }

    /** 手动刷新 nv1 Key */
    async function handleNV1Refresh() {
        try {
            const res = await api("/system/nv1/refresh", { method: "POST" });
            if (res.success) {
                toastSuccess("nv1 Key 刷新成功");
                await loadNV1Status();
            } else {
                toastError("刷新失败: " + (res.error || res.message || "未知错误"));
            }
        } catch (e) {
            toastError("刷新失败: " + e.message);
        }
    }

    /** 加载封号检测状态 */
    async function loadBanStatus() {
        try {
            const res = await api("/system/ban/status");
            if (res.success && res.data) {
                const d = res.data;
                $("banTracked").textContent = d.total_tracked || 0;
                $("banSuspected").textContent = d.suspected_bans || 0;
                $("banThreshold").textContent = (d.threshold || 3) + " 次";

                const badge = $("banBadge");
                if (d.suspected_bans > 0) {
                    badge.textContent = d.suspected_bans + " 个封号";
                    badge.className = "badge badge-danger";
                } else {
                    badge.textContent = "正常";
                    badge.className = "badge badge-success";
                }
            }

            // 加载封号账号列表
            const accountsRes = await api("/system/ban/accounts");
            if (accountsRes.success && accountsRes.data && accountsRes.data.length > 0) {
                const listEl = $("bannedAccountsList");
                listEl.innerHTML = accountsRes.data.map((acc) => {
                    const time = formatTime(acc.last_failure_at);
                    return `
                        <div style="padding:8px;border:1px solid #21262d;border-radius:6px;margin-bottom:6px;">
                            <div style="display:flex;justify-content:space-between;align-items:center;">
                                <span class="mono" style="font-size:11px;">${escapeHtml(acc.account_id)}</span>
                                <button class="btn btn-ghost btn-sm clear-ban-btn" style="padding:2px 8px;font-size:11px;"
                                    data-account-id="${escapeHtml(acc.account_id)}">
                                    <i class="fas fa-times"></i> 解除
                                </button>
                            </div>
                            <div style="font-size:10px;color:#7d8590;margin-top:2px;">
                                失败 ${acc.failure_count} 次 | ${escapeHtml(time)}
                            </div>
                        </div>
                    `;
                }).join("");
                // 使用事件委托绑定点击事件 (避免 XSS)
                listEl.querySelectorAll(".clear-ban-btn").forEach((btn) => {
                    btn.addEventListener("click", () => {
                        window._clearBanFlag(btn.getAttribute("data-account-id"));
                    });
                });
            } else {
                $("bannedAccountsList").innerHTML = `
                    <div class="empty-state" style="padding:16px;">
                        <i class="fas fa-check-circle" style="color:var(--color-success);"></i>
                        <p style="font-size:13px;margin-top:4px;">暂无封号记录</p>
                    </div>
                `;
            }
        } catch (e) {
            console.error("加载封号状态失败:", e);
        }
    }

    /** 解除封号标记 */
    window._clearBanFlag = async function(accountId) {
        try {
            const res = await api(`/system/ban/${accountId}/clear`, { method: "POST" });
            if (res.success) {
                toastSuccess("封号标记已解除");
                await loadBanStatus();
            } else {
                toastError("操作失败: " + (res.message || ""));
            }
        } catch (e) {
            toastError("操作失败: " + e.message);
        }
    };

    /** 加载系统详细统计 */
    async function loadSystemStatsDetail() {
        try {
            const res = await api("/system/stats");
            if (res.success && res.data) {
                const d = res.data;

                // 更新统计卡片
                $("sysStatUsers").textContent = d.users.total;
                $("sysStatPanels").textContent = d.panels.total;
                $("sysStatBots").textContent = d.bots.total;
                $("sysStatCards").textContent = d.cards.total;

                // 详细统计
                const detailEl = $("systemStatsDetail");
                const items = [
                    { label: "活跃用户", value: d.users.active, icon: "fa-user-check", color: "var(--color-success)" },
                    { label: "管理员", value: d.users.admins, icon: "fa-user-shield", color: "var(--color-primary)" },
                    { label: "封禁用户", value: d.users.banned, icon: "fa-user-slash", color: "var(--color-danger)" },
                    { label: "活跃面板", value: d.panels.active, icon: "fa-check-circle", color: "var(--color-success)" },
                    { label: "过期面板", value: d.panels.expired, icon: "fa-clock", color: "var(--color-warning)" },
                    { label: "运行中机器人", value: d.bots.running, icon: "fa-play-circle", color: "var(--color-success)" },
                    { label: "停止机器人", value: d.bots.stopped, icon: "fa-stop-circle", color: "var(--text-secondary)" },
                    { label: "错误机器人", value: d.bots.error, icon: "fa-exclamation-triangle", color: "var(--color-danger)" },
                    { label: "未使用卡密", value: d.cards.unused, icon: "fa-key", color: "var(--color-primary)" },
                    { label: "已使用卡密", value: d.cards.used, icon: "fa-check", color: "var(--color-success)" },
                    { label: "已撤销卡密", value: d.cards.revoked, icon: "fa-ban", color: "var(--color-danger)" },
                    { label: "nv1模式", value: d.nv1.mode === "mock" ? "模拟" : "真实", icon: "fa-shield-alt", color: "var(--color-primary)" },
                ];

                detailEl.innerHTML = items.map((item) => `
                    <div style="padding:12px;background:rgba(255,255,255,0.03);border-radius:8px;border:1px solid #21262d;">
                        <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px;">
                            <i class="fas ${item.icon}" style="color:${item.color};font-size:14px;"></i>
                            <span style="font-size:12px;color:#7d8590;">${escapeHtml(item.label)}</span>
                        </div>
                        <div style="font-size:18px;font-weight:700;color:#e6edf3;">${escapeHtml(String(item.value))}</div>
                    </div>
                `).join("");
            }
        } catch (e) {
            console.error("加载系统统计失败:", e);
        }
    }

    /* ======================================================================
       19. 通用渲染辅助
       ====================================================================== */

    /**
     * 生成空状态 HTML
     * @param {string} icon - FontAwesome 图标类
     * @param {string} title - 标题
     * @param {string} desc - 描述
     * @returns {string} HTML
     */
    function renderEmptyState(icon, title, desc) {
        return `
            <div class="empty-state">
                <i class="fas ${icon}"></i>
                <h3>${escapeHtml(title)}</h3>
                ${desc ? `<p>${escapeHtml(desc)}</p>` : ""}
            </div>
        `;
    }

    /* ======================================================================
       20. 事件绑定
       ====================================================================== */

    function bindEvents() {
        // ---- 认证 Tab 切换 ----
        $("tabLogin").addEventListener("click", () => switchAuthTab("login"));
        $("tabRegister").addEventListener("click", () => switchAuthTab("register"));
        $("authToggle").addEventListener("click", () => {
            const isLoginVisible = !$("loginForm").classList.contains("hidden");
            switchAuthTab(isLoginVisible ? "register" : "login");
        });

        // ---- 验证码刷新 ----
        $("captchaImgBox").addEventListener("click", loadCaptcha);

        // ---- 登录 / 注册表单 ----
        $("loginForm").addEventListener("submit", handleLogin);
        $("registerForm").addEventListener("submit", handleRegister);

        // ---- 顶部栏 ----
        $("menuToggle").addEventListener("click", toggleSidebar);
        const themeToggle = $("themeToggle");
        if (themeToggle) themeToggle.addEventListener("click", toggleTheme);
        $("topbarUser").addEventListener("click", (e) => {
            e.stopPropagation();
            toggleUserMenu();
        });
        // 点击其他区域关闭用户菜单
        document.addEventListener("click", (e) => {
            if (!e.target.closest("#userMenu") && !e.target.closest("#topbarUser")) {
                closeUserMenu();
            }
        });
        // 用户菜单项
        $$("[data-action]").forEach((item) => {
            if (item.id === "logoutBtn") return; // 登出单独绑定
            item.addEventListener("click", () => {
                closeUserMenu();
                const action = item.dataset.action;
                if (action === "change-password") {
                    openModal("modalChangePassword");
                } else if (action === "dashboard" || action === "profile") {
                    switchView("dashboard");
                }
            });
        });
        $("logoutBtn").addEventListener("click", handleLogout);

        // 修改密码提交
        $("changePwdSubmit").addEventListener("click", async () => {
            const oldPwd = $("changePwdOld").value;
            const newPwd = $("changePwdNew").value;
            const confirmPwd = $("changePwdConfirm").value;
            if (!oldPwd || !newPwd || !confirmPwd) {
                toastError("请填写所有字段");
                return;
            }
            if (newPwd.length < 6) {
                toastError("新密码至少 6 位");
                return;
            }
            if (newPwd !== confirmPwd) {
                toastError("两次输入的新密码不一致");
                return;
            }
            if (newPwd === oldPwd) {
                toastError("新密码不能与当前密码相同");
                return;
            }
            try {
                $("changePwdSubmit").disabled = true;
                const res = await api("/auth/change-password", {
                    method: "POST",
                    body: JSON.stringify({ old_password: oldPwd, new_password: newPwd }),
                });
                toastSuccess("密码修改成功");
                closeModal("modalChangePassword");
                $("changePwdOld").value = "";
                $("changePwdNew").value = "";
                $("changePwdConfirm").value = "";
            } catch (e) {
                toastError(e.message || "密码修改失败");
            } finally {
                $("changePwdSubmit").disabled = false;
            }
        });

        // ---- 侧边栏 ----
        $("sidebarBackdrop").addEventListener("click", closeSidebar);
        // 导航项
        $$(".nav-item").forEach((item) => {
            item.addEventListener("click", () => switchView(item.dataset.view));
        });

        // ---- Dashboard ----
        $("refreshActivity").addEventListener("click", loadActivity);
        // 快捷操作按钮
        $$("[data-quick]").forEach((btn) => {
            btn.addEventListener("click", () => {
                const target = btn.dataset.quick;
                if (target === "panels") {
                    switchView("panels");
                    setTimeout(openCreatePanelModal, 300);
                } else if (target === "bots") {
                    switchView("bots");
                } else if (target === "admin-cards") {
                    switchView("admin-cards");
                    setTimeout(openCreateCardModal, 300);
                }
            });
        });

        // ---- 面板列表 ----
        $("refreshPanels").addEventListener("click", loadPanels);
        $("btnCreatePanel").addEventListener("click", openCreatePanelModal);

        // ---- 面板详情 ----
        $("backToPanels").addEventListener("click", () => switchView("panels"));
        $("btnStartBot").addEventListener("click", startBot);
        $("btnStopBot").addEventListener("click", stopBot);
        $("btnRestartBot").addEventListener("click", restartBot);
        $("btnRenewPanel").addEventListener("click", () => {
            if (state.currentPanelId) openRenewPanelModal(state.currentPanelId);
        });
        $("btnDeletePanel").addEventListener("click", () => {
            if (state.currentPanelId) {
                const panel = state.panelDetail;
                confirmAction(
                    '<i class="fas fa-exclamation-triangle"></i> 删除面板',
                    `确定要删除面板 <strong>${escapeHtml(panel ? panel.name : "")}</strong> 吗？此操作不可撤销。`,
                    () => handleDeletePanel(state.currentPanelId)
                );
            }
        });

        // ---- 控制台 Tab ----
        $$(".console-tab").forEach((tab) => {
            tab.addEventListener("click", () => switchConsoleTab(tab.dataset.consoleTab));
        });

        // ---- 终端 ----
        $("consoleClearBtn").addEventListener("click", clearTerminal);
        $("consoleAutoscrollBtn").addEventListener("click", toggleAutoscroll);
        $("terminalSendBtn").addEventListener("click", () => {
            const input = $("terminalInput");
            sendTerminalCommand(input.value);
            input.value = "";
            state.terminalHistoryIndex = state.terminalHistory.length;
        });
        $("terminalInput").addEventListener("keydown", (e) => {
            if (e.key === "Enter") {
                e.preventDefault();
                const input = $("terminalInput");
                sendTerminalCommand(input.value);
                input.value = "";
                state.terminalHistoryIndex = state.terminalHistory.length;
            } else if (e.key === "ArrowUp") {
                // 浏览历史命令
                e.preventDefault();
                if (state.terminalHistory.length > 0 && state.terminalHistoryIndex > 0) {
                    state.terminalHistoryIndex--;
                    $("terminalInput").value = state.terminalHistory[state.terminalHistoryIndex];
                }
            } else if (e.key === "ArrowDown") {
                // 浏览历史命令
                e.preventDefault();
                if (state.terminalHistoryIndex < state.terminalHistory.length - 1) {
                    state.terminalHistoryIndex++;
                    $("terminalInput").value = state.terminalHistory[state.terminalHistoryIndex];
                } else {
                    state.terminalHistoryIndex = state.terminalHistory.length;
                    $("terminalInput").value = "";
                }
            }
        });

        // ---- 面板日志 ----
        $("refreshPanelLogs").addEventListener("click", loadPanelLogs);

        // ---- 机器人配置 ----
        $("botConfigForm").addEventListener("submit", handleSaveBotConfig);
        $("resetBotConfig").addEventListener("click", resetBotConfig);

        // ---- 机器人列表 ----
        $("refreshBots").addEventListener("click", loadBots);

        // ---- 卡密管理 ----
        $("refreshCards").addEventListener("click", () => {
            loadCardStats();
            loadCards();
        });
        $("btnCreateCard").addEventListener("click", openCreateCardModal);
        $("cardFilterType").addEventListener("change", (e) => {
            state.cardFilterType = e.target.value;
            loadCards();
        });
        $("cardFilterStatus").addEventListener("change", (e) => {
            state.cardFilterStatus = e.target.value;
            loadCards();
        });
        $("refreshCardLogs").addEventListener("click", loadCardCreationLogs);

        // ---- 用户管理 ----
        $("refreshUsers").addEventListener("click", loadUsers);
        $("btnCreateUser").addEventListener("click", openCreateUserModal);

        // ---- 系统日志 ----
        $("logFilterLevel").addEventListener("change", (e) => {
            state.logFilterLevel = e.target.value;
            loadSystemLogs();
        });
        $("refreshSysLogs").addEventListener("click", loadSystemLogs);

        // ---- 系统管理 ----
        $("refreshSystemAdmin").addEventListener("click", loadSystemAdmin);
        $("nv1RefreshBtn").addEventListener("click", handleNV1Refresh);

        // ---- 模态框 ----
        // 关闭按钮 (data-modal-close)
        $$("[data-modal-close]").forEach((el) => {
            el.addEventListener("click", () => {
                const modal = el.closest(".modal-overlay");
                if (modal) modal.classList.remove("visible");
            });
        });
        // 点击遮罩关闭模态框
        $$(".modal-overlay").forEach((overlay) => {
            overlay.addEventListener("click", (e) => {
                if (e.target === overlay) overlay.classList.remove("visible");
            });
        });
        // ESC 关闭所有模态框
        document.addEventListener("keydown", (e) => {
            if (e.key === "Escape") closeAllModals();
        });

        // ---- 创建面板表单 ----
        $("createPanelForm").addEventListener("submit", handleCreatePanel);
        $("createPanelSubmit").addEventListener("click", () => {
            $("createPanelForm").requestSubmit();
        });

        // ---- 创建卡密表单 ----
        $("createCardForm").addEventListener("submit", handleCreateCard);
        $("createCardSubmit").addEventListener("click", () => {
            $("createCardForm").requestSubmit();
        });

        // ---- 创建用户表单 ----
        $("createUserForm").addEventListener("submit", handleCreateUser);
        $("createUserSubmit").addEventListener("click", () => {
            $("createUserForm").requestSubmit();
        });

        // ---- 续费面板表单 ----
        $("renewPanelForm").addEventListener("submit", handleRenewPanel);
        $("renewPanelSubmit").addEventListener("click", () => {
            $("renewPanelForm").requestSubmit();
        });

        // ---- 确认对话框 ----
        $("confirmOk").addEventListener("click", () => {
            closeModal("modalConfirm");
            if (typeof state.confirmCallback === "function") {
                const cb = state.confirmCallback;
                state.confirmCallback = null;
                cb();
            }
        });

        // ---- 欢迎时间定时刷新 ----
        setInterval(updateWelcomeTime, 1000);
    }

    /* ======================================================================
       21. 启动
       ====================================================================== */

    document.addEventListener("DOMContentLoaded", init);

})();
