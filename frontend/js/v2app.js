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
        cardShowRevoked: false,       // 卡密列表 - 是否显示已撤销卡密 (默认 false)
        panelScope: "mine",           // 面板列表范围 (mine/all), 仅管理员可切换为 all
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
        botConfigDirty: false,        // 机器人配置表单是否有未保存的修改
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
        const url = path.startsWith("http") ? path : (path.startsWith("/api/") ? path : API_BASE + path);

        // 构建请求头
        const headers = {};
        if (!(options.body instanceof FormData)) {
            headers["Content-Type"] = "application/json";
        }
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

        // 401 - 未授权 / 登录过期 / 登录请求被拒
        if (response.status === 401) {
            // 尝试解析后端返回的具体原因 (如 "用户名或密码错误")
            let detail = null;
            try {
                const data = await response.json();
                detail = extractApiMessage(data);
            } catch (_) { /* 响应非 JSON */ }
            // 已有会话 -> 视为登录过期并清理; 否则视为登录/认证请求被拒, 仅提示不清理
            if (state.currentUser || state.token) {
                handleUnauthorized(detail);
            } else {
                toastError(detail || "用户名或密码错误");
            }
            throw { type: "auth", status: 401, message: detail || "登录已过期，请重新登录" };
        }

        // 403 - 无权限 / 账号被禁用等
        if (response.status === 403) {
            // 优先展示后端具体原因 (如 "您已被禁止登录")
            let detail = null;
            try {
                const data = await response.json();
                detail = extractApiMessage(data);
            } catch (_) { /* 响应非 JSON */ }
            const msg = detail || "没有权限执行此操作";
            toastError(msg);
            throw { type: "forbidden", status: 403, message: msg };
        }

        // 其他非 2xx 状态码
        if (!response.ok) {
            let msg = `请求失败 (${response.status})`;
            try {
                const data = await response.json();
                const extracted = extractApiMessage(data);
                if (extracted) msg = extracted;
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
    function handleUnauthorized(customMsg) {
        // 登录过期, 主动关闭 WebSocket
        closeWebSocket();
        state.currentUser = null;
        state.token = null;
        localStorage.removeItem(TOKEN_KEY);
        showAuthScreen();
        // 优先使用后端返回的具体原因 (如有)
        toastWarn(customMsg || "登录已过期，请重新登录");
    }

    /**
     * 从后端响应体中提取可读的错误信息
     * 兼容 {detail: "..."} / {message: "..."} / {error: "..."} 以及
     * FastAPI 校验错误 {detail: [{msg: "..."}, ...]}
     * @param {object} data - 已解析的响应 JSON
     * @returns {string|null} 提取出的消息, 无则返回 null
     */
    function extractApiMessage(data) {
        if (!data) return null;
        if (typeof data === "string") return data;
        if (data.detail) {
            if (typeof data.detail === "string") return data.detail;
            if (Array.isArray(data.detail)) {
                // FastAPI 校验错误: [{msg, loc, ...}, ...]
                const parts = data.detail
                    .map((e) => (e && typeof e === "object" && e.msg) ? e.msg : String(e))
                    .filter(Boolean);
                return parts.length ? parts.join("; ") : JSON.stringify(data.detail);
            }
            return JSON.stringify(data.detail);
        }
        if (data.message) return typeof data.message === "string" ? data.message : JSON.stringify(data.message);
        if (data.error) return typeof data.error === "string" ? data.error : JSON.stringify(data.error);
        return null;
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
     * 转义字符串以安全嵌入 onclick 属性 (单引号 JS 字符串 + 双引号 HTML 属性)
     * @param {string} str - 原始字符串
     * @returns {string} 转义后的字符串
     */
    function escAttr(str) {
        if (str === null || str === undefined) return "";
        return String(str)
            .replace(/\\/g, "\\\\")
            .replace(/'/g, "\\'")
            .replace(/&/g, "&amp;")
            .replace(/"/g, "&quot;");
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
        // 即使没有 token, 也尝试通过 cookie 检查会话
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
                // showApp() 内部已通过 switchView("dashboard") -> loadDashboard() 加载数据,
                // 无需在此重复调用, 否则会触发两次 loadStats/loadActivity 造成闪烁与重复请求
                showApp();
            } else {
                // 优先展示后端返回的具体原因 (message / detail), 如 "您已被禁止登录"
                toastError(res.message || res.detail || "登录失败");
            }
        } catch (err) {
            // api() 已对 HTTP 错误 (401/403/其它) 做了具体提示; 此处仅兜底未知错误
            if (err && err.message && err.type === "unknown") {
                toastError(err.message);
            }
        } finally {
            btn.disabled = false;
            btn.innerHTML = '<i class="fas fa-sign-in-alt"></i> 登录';
        }
    }

    /** 邮箱验证码倒计时定时器句柄 */
    let emailCodeTimer = null;

    /**
     * 发送邮箱验证码
     * - 校验邮箱格式
     * - 调用 POST /api/v2/auth/email/send
     * - 成功后启动 60 秒倒计时, 期间禁用按钮
     */
    async function sendEmailCode() {
        const emailInput = $("regEmail");
        const btn = $("sendEmailCodeBtn");
        const textEl = $("sendEmailCodeText");
        if (!emailInput || !btn || !textEl) return;

        const email = emailInput.value.trim();
        // 基础邮箱格式校验
        if (!email) {
            toastWarn("请先输入QQ邮箱");
            emailInput.focus();
            return;
        }
        if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
            toastWarn("邮箱格式不正确");
            emailInput.focus();
            return;
        }
        // 倒计时进行中则忽略
        if (emailCodeTimer) return;

        btn.disabled = true;
        textEl.textContent = "发送中...";

        try {
            const res = await api("/auth/email/send", {
                method: "POST",
                body: { email },
            });
            if (res.success) {
                toastSuccess("验证码已发送至邮箱，请查收");
                startEmailCodeCountdown(60);
            } else {
                toastError(res.message || res.detail || "验证码发送失败");
                btn.disabled = false;
                textEl.textContent = "发送验证码";
            }
        } catch (err) {
            // api() 已提示具体错误
            btn.disabled = false;
            textEl.textContent = "发送验证码";
        }
    }

    /**
     * 启动邮箱验证码倒计时
     * @param {number} seconds - 倒计时秒数
     */
    function startEmailCodeCountdown(seconds) {
        const btn = $("sendEmailCodeBtn");
        const textEl = $("sendEmailCodeText");
        if (!btn || !textEl) return;
        let remaining = seconds;
        btn.disabled = true;
        textEl.textContent = `重新发送 (${remaining}s)`;
        emailCodeTimer = setInterval(() => {
            remaining -= 1;
            if (remaining <= 0) {
                clearInterval(emailCodeTimer);
                emailCodeTimer = null;
                btn.disabled = false;
                textEl.textContent = "发送验证码";
            } else {
                textEl.textContent = `重新发送 (${remaining}s)`;
            }
        }, 1000);
    }

    /**
     * 处理注册表单提交
     */
    async function handleRegister(e) {
        e.preventDefault();
        const username = $("regUsername").value.trim();
        const password = $("regPassword").value;
        const cardKey = $("regCardKey").value.trim();
        const email = $("regEmail") ? $("regEmail").value.trim() : "";
        const emailCode = $("regEmailCode") ? $("regEmailCode").value.trim() : "";
        const captchaAnswer = $("regCaptchaInput").value.trim();
        const captchaId = $("regCaptchaId").value;
        const btn = $("registerBtn");

        if (!username || !password || !cardKey || !captchaAnswer) {
            toastWarn("请填写所有必填项");
            return;
        }
        if (!email) {
            toastWarn("请输入QQ邮箱");
            return;
        }
        if (!emailCode) {
            toastWarn("请输入邮箱验证码");
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
                    email,
                    email_code: emailCode,
                    captcha_answer: captchaAnswer,
                    captcha_id: captchaId,
                },
            });
            if (res.success) {
                toastSuccess("注册成功，请登录");
                // 清空注册表单
                $("registerForm").reset();
                state.captchaId = null;
                // 清除邮箱验证码倒计时
                if (emailCodeTimer) {
                    clearInterval(emailCodeTimer);
                    emailCodeTimer = null;
                }
                // 切换到登录
                switchAuthTab("login");
                $("loginUsername").value = username;
                $("loginPassword").focus();
            } else {
                toastError(res.message || res.detail || "注册失败");
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
        state.users = [];
        state.cards = [];
        state.cardStats = { total: 0, used: 0, unused: 0, revoked: 0 };
        state.cardFilterType = "";
        state.cardFilterStatus = "";
        state.cardShowRevoked = false;
        state.panelScope = "mine";
        state.logFilterLevel = "";
        state.panelDetail = null;
        state.panelBot = null;
        state.terminalHistory = [];
        state.botConfigDirty = false;
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
        const annCreateBtn = $("annCreateBtn");
        if (annCreateBtn) annCreateBtn.style.display = isAdmin ? "" : "none";
        // 面板范围切换标签 (我的面板/全部面板) 仅管理员可见
        const panelScopeTabs = $("panelScopeTabs");
        if (panelScopeTabs) panelScopeTabs.style.display = isAdmin ? "flex" : "none";
        // 普通用户强制使用 "我的面板" 范围, 并重置高亮
        if (!isAdmin) {
            state.panelScope = "mine";
            $$("[data-panel-scope]").forEach((t) => {
                t.classList.toggle("active", t.dataset.panelScope === "mine");
            });
        }
    }

    /* ======================================================================
       8. 导航与视图切换
       ====================================================================== */

    /**
     * 切换主视图
     * @param {string} view - 视图名称
     */
    function switchView(view) {
        // 客户端访问控制: 非管理员不能访问 admin-* 视图
        if (view && view.startsWith("admin-") && !isAdmin()) {
            toastWarn("没有权限访问该页面");
            switchView("dashboard");
            return;
        }
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
            case "announcements":
                loadAnnouncements();
                break;
            case "admin-ann-logs":
                loadAnnouncementLogs();
                break;
            case "shop":
                loadShop();
                break;
            case "files":
                loadFiles();
                break;
            case "admin-orders":
                loadAdminOrders();
                break;
            case "admin-review":
                loadReviewFiles();
                break;
            case "admin-balance":
                loadUsersBalance();
                break;
        }
    }

    async function loadActivityLog() {
        try {
            const data = await api("/api/v2/auth/activity-log");
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

    /* ======================================================================
       文件管理 / 插件管理
       ====================================================================== */

    /**
     * 发起旧版 API 请求 (不带 /api/v2 前缀, 直接使用 /api/... 完整路径)
     * 用于面板详情中的文件/插件管理接口 (后端旧版 API 位于 /api/ 而非 /api/v2/)
     * @param {string} path - 完整路径 (如 "/api/files/123")
     * @param {object} options - fetch 选项 {method, body, headers, ...}
     * @returns {Promise<object>} 解析后的 JSON
     */
    async function legacyApi(path, options = {}) {
        const headers = options.headers ? Object.assign({}, options.headers) : {};
        // 认证头: 优先使用内存中的 token, 回退到 localStorage
        const token = state.token || localStorage.getItem(TOKEN_KEY);
        if (token) headers["Authorization"] = "Bearer " + token;
        // FormData 不设置 Content-Type, 让浏览器自动设置 boundary
        if (!(options.body instanceof FormData) && options.body !== undefined && options.body !== null) {
            if (!headers["Content-Type"]) headers["Content-Type"] = "application/json";
        }
        const fetchOpts = {
            method: options.method || "GET",
            credentials: "include",
            headers,
        };
        if (options.body !== undefined && options.body !== null) {
            fetchOpts.body = options.body;
        }
        let response;
        try {
            response = await fetch(path, fetchOpts);
        } catch (err) {
            throw { message: "网络连接失败", type: "network" };
        }
        if (!response.ok) {
            let msg = `请求失败 (${response.status})`;
            try {
                const data = await response.json();
                const extracted = extractApiMessage(data);
                if (extracted) msg = extracted;
            } catch (_) { /* 响应非 JSON */ }
            throw { message: msg, status: response.status };
        }
        // 尝试解析 JSON, 失败则返回成功标记
        try {
            return await response.json();
        } catch (_) {
            return { success: true };
        }
    }

    async function loadPanelFiles() {
        if (!state.currentPanelId) return;
        const pluginId = state.currentPanelId;
        try {
            // 旧版文件 API 位于 /api/files/{plugin_id} (非 /api/v2), 直接使用完整路径
            const res = await legacyApi("/api/files/" + pluginId, { method: "GET" });
            const files = res.data || res.files || [];
            const container = $("fileGrid");
            if (!container) return;
            if (files.length === 0) {
                container.innerHTML = `<div class="empty-state" style="grid-column:1/-1;"><i class="fas fa-folder-open"></i><h3>暂无文件</h3><p>上传建筑文件、配置文件等</p></div>`;
                return;
            }
            container.innerHTML = files.map(f => {
                const icon = f.name.endsWith('.bdx') ? 'fa-cube' : f.name.endsWith('.schematic') ? 'fa-cubes' : f.name.endsWith('.nbt') ? 'fa-cube' : f.name.endsWith('.mcstructure') ? 'fa-layer-group' : f.name.endsWith('.json') ? 'fa-file-code' : 'fa-file';
                return `<div class="card" style="padding:12px;">
                    <div style="display:flex;align-items:center;gap:8px;">
                        <i class="fas ${icon}" style="font-size:20px;color:var(--color-primary);"></i>
                        <div style="flex:1;min-width:0;">
                            <div style="font-weight:500;font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${f.name}</div>
                            <div style="font-size:11px;color:var(--text-tertiary);">${formatFileSize(f.size)}</div>
                        </div>
                        <button class="btn btn-danger btn-sm" onclick="deletePanelFile('${pluginId}','${f.name}')" title="删除"><i class="fas fa-trash"></i></button>
                    </div>
                </div>`;
            }).join("");
        } catch (err) {
            console.error("Load files error:", err);
            const container = $("fileGrid");
            if (container) container.innerHTML = `<div class="empty-state" style="grid-column:1/-1;"><i class="fas fa-exclamation-triangle"></i><h3>加载失败</h3><p>${err.message || '未知错误'}</p></div>`;
        }
    }

    function formatFileSize(bytes) {
        if (!bytes) return '-';
        const units = ['B', 'KB', 'MB', 'GB'];
        let i = 0;
        while (bytes >= 1024 && i < units.length - 1) { bytes /= 1024; i++; }
        return bytes.toFixed(1) + ' ' + units[i];
    }

    async function handleFileUpload(event) {
        const files = event.target.files;
        if (!files || files.length === 0) return;
        if (!state.currentPanelId) return;
        const pluginId = state.currentPanelId;
        for (const file of files) {
            const formData = new FormData();
            formData.append("file", file);
            try {
                appendTerminal(`正在上传文件: ${file.name}...`, "system");
                // 旧版文件上传 API: /api/files/{plugin_id}/upload
                const res = await legacyApi("/api/files/" + pluginId + "/upload", { method: "POST", body: formData });
                if (res.success !== false) {
                    appendTerminal(`文件上传成功: ${file.name}`, "success");
                    toastSuccess(`文件 ${file.name} 上传成功`);
                } else {
                    appendTerminal(`文件上传失败: ${file.name}`, "error");
                    toastError(`文件 ${file.name} 上传失败`);
                }
            } catch (err) {
                appendTerminal(`文件上传出错: ${file.name} - ${err.message}`, "error");
                toastError(`上传失败: ${err.message}`);
            }
        }
        event.target.value = "";
        loadPanelFiles();
    }

    async function deletePanelFile(pluginId, filename) {
        if (!confirm(`确定要删除文件 ${filename} 吗？`)) return;
        try {
            // 旧版文件删除 API: /api/files/{plugin_id}/{filename}
            const res = await legacyApi("/api/files/" + pluginId + "/" + encodeURIComponent(filename), { method: "DELETE" });
            if (res.success !== false) {
                toastSuccess("文件已删除");
                loadPanelFiles();
            }
        } catch (err) {
            toastError(`删除失败: ${err.message}`);
        }
    }

    async function loadPanelPlugins() {
        try {
            // 旧版插件 API 位于 /api/plugins (非 /api/v2), 直接使用完整路径
            const res = await legacyApi("/api/plugins", { method: "GET" });
            const plugins = res.data || res.plugins || [];
            const container = $("pluginList");
            if (!container) return;
            if (plugins.length === 0) {
                container.innerHTML = `<div class="empty-state"><i class="fas fa-puzzle-piece"></i><h3>暂无插件</h3><p>上传 Python/Go/Java 插件</p></div>`;
                return;
            }
            container.innerHTML = plugins.map(p => {
                const statusColor = p.enabled ? 'var(--color-success)' : 'var(--text-tertiary)';
                const statusText = p.enabled ? '已启用' : '已禁用';
                const toggleBtn = p.enabled
                    ? `<button class="btn btn-warning btn-sm" onclick="togglePlugin('${p.plugin_id}','disable')"><i class="fas fa-pause"></i></button>`
                    : `<button class="btn btn-success btn-sm" onclick="togglePlugin('${p.plugin_id}','enable')"><i class="fas fa-play"></i></button>`;
                return `<div class="card" style="padding:12px;display:flex;align-items:center;gap:12px;">
                    <i class="fas fa-puzzle-piece" style="font-size:20px;color:var(--color-primary);"></i>
                    <div style="flex:1;min-width:0;">
                        <div style="font-weight:500;">${p.name || p.plugin_id}</div>
                        <div style="font-size:11px;color:var(--text-tertiary);">${p.language || 'python'} · <span style="color:${statusColor}">${statusText}</span></div>
                    </div>
                    ${toggleBtn}
                    <button class="btn btn-secondary btn-sm" onclick="reloadPlugin('${p.plugin_id}')" title="重载"><i class="fas fa-redo"></i></button>
                </div>`;
            }).join("");
        } catch (err) {
            const container = $("pluginList");
            if (container) container.innerHTML = `<div class="empty-state"><i class="fas fa-exclamation-triangle"></i><h3>加载失败</h3><p>${err.message || '未知错误'}</p></div>`;
        }
    }

    async function handlePluginUpload(event) {
        const files = event.target.files;
        if (!files || files.length === 0) return;
        if (!state.currentPanelId) return;
        const pluginId = state.currentPanelId;
        for (const file of files) {
            const formData = new FormData();
            formData.append("file", file);
            try {
                appendTerminal(`正在安装插件: ${file.name}...`, "system");
                // 旧版插件上传 API: /api/files/{plugin_id}/upload
                const res = await legacyApi("/api/files/" + pluginId + "/upload", { method: "POST", body: formData });
                if (res.success !== false) {
                    appendTerminal(`插件安装成功: ${file.name}`, "success");
                    toastSuccess(`插件 ${file.name} 安装成功`);
                } else {
                    appendTerminal(`插件安装失败: ${file.name}`, "error");
                }
            } catch (err) {
                appendTerminal(`插件安装出错: ${err.message}`, "error");
                toastError(`安装失败: ${err.message}`);
            }
        }
        event.target.value = "";
        loadPanelPlugins();
    }

    async function togglePlugin(pluginId, action) {
        try {
            // 旧版插件启停 API: /api/plugins/{plugin_id}/{action}
            const res = await legacyApi("/api/plugins/" + pluginId + "/" + action, { method: "POST" });
            if (res.success !== false) {
                toastSuccess(action === 'enable' ? '插件已启用' : '插件已禁用');
                loadPanelPlugins();
            }
        } catch (err) {
            toastError(`操作失败: ${err.message}`);
        }
    }

    async function reloadPlugin(pluginId) {
        try {
            // 旧版插件重载 API: /api/plugins/{plugin_id}/reload
            const res = await legacyApi("/api/plugins/" + pluginId + "/reload", { method: "POST" });
            if (res.success !== false) {
                toastSuccess("插件已重载");
            }
        } catch (err) {
            toastError(`重载失败: ${err.message}`);
        }
    }

    // 暴露给内联 onclick 使用的全局函数
    window.deletePanelFile = deletePanelFile;
    window.togglePlugin = togglePlugin;
    window.reloadPlugin = reloadPlugin;
    window.switchView = switchView;
    window.deleteComment = deleteComment;
    // 商店 / 文件 / 管理后台 - 内联按钮调用的函数
    window.purchaseProduct = purchaseProduct;
    window.purchaseFile = purchaseFile;
    window.downloadFile = downloadFile;
    window.approveFile = approveFile;
    window.rejectFile = rejectFile;
    window.copyToClipboard = copyToClipboard;

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
        if (tab === "settings") {
            loadBotConfig();
            loadAccessPointStatus();
        }
        if (tab === "files") loadPanelFiles();
        if (tab === "plugins") loadPanelPlugins();
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
            // 面板范围与 loadPanels 保持一致: 普通用户强制 mine, 管理员按 state.panelScope
            const panelScope = isAdmin() ? (state.panelScope || "mine") : "mine";
            // 并行加载面板与机器人
            const [panelsRes, botsRes] = await Promise.allSettled([
                api(`/panels?scope=${encodeURIComponent(panelScope)}`),
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

    /** localStorage 键名: 最近活动折叠状态 */
    const ACTIVITY_COLLAPSE_KEY = "pocketterm_activity_collapsed";

    /**
     * 切换"最近活动"卡片的折叠/展开状态
     * - 切换 collapsed 类
     * - 更新按钮图标 (chevron-up / chevron-down)
     * - 持久化到 localStorage
     */
    function toggleActivityCollapse() {
        const collapse = $("activityCollapse");
        const icon = $("toggleActivityIcon");
        if (!collapse) return;
        const collapsed = collapse.classList.toggle("collapsed");
        if (icon) {
            icon.className = collapsed ? "fas fa-chevron-down" : "fas fa-chevron-up";
        }
        try {
            localStorage.setItem(ACTIVITY_COLLAPSE_KEY, collapsed ? "1" : "0");
        } catch (_) { /* localStorage 不可用时忽略 */ }
    }

    /** 从 localStorage 恢复"最近活动"折叠状态 (初始化时调用) */
    function restoreActivityCollapse() {
        const collapse = $("activityCollapse");
        const icon = $("toggleActivityIcon");
        if (!collapse) return;
        let collapsed = false;
        try {
            collapsed = localStorage.getItem(ACTIVITY_COLLAPSE_KEY) === "1";
        } catch (_) { /* 忽略 */ }
        collapse.classList.toggle("collapsed", collapsed);
        if (icon) {
            icon.className = collapsed ? "fas fa-chevron-down" : "fas fa-chevron-up";
        }
    }

    /**
     * 加载当前用户余额并更新商店视图余额显示
     * 调用 GET /api/v2/shop/balance, 格式化为 "XX.XX"
     * 仅在商店视图加载时调用 (不再在顶栏显示余额)
     * 接口不可用时静默处理, 不影响主流程
     */
    async function loadBalance() {
        const textEl = $("shopBalanceText");
        if (!textEl) return;
        try {
            const res = await api("/shop/balance");
            if (res && res.success) {
                // 兼容 {balance} / {data: {balance}} / {data: <number>} 等返回结构
                let balance = res.balance;
                if (balance === undefined && res.data !== undefined) {
                    balance = (res.data && res.data.balance !== undefined) ? res.data.balance : res.data;
                }
                const num = parseFloat(balance);
                const formatted = isNaN(num) ? "0.00" : num.toFixed(2);
                textEl.textContent = formatted;
            } else {
                textEl.textContent = "0.00";
            }
        } catch (_) {
            // 余额接口不可用 (如未部署商店模块) -> 静默处理, 不打扰用户
            textEl.textContent = "0.00";
        }
    }

    /* ======================================================================
       11. 面板管理
       ====================================================================== */

    /** 加载面板列表 */
    async function loadPanels() {
        const grid = $("panelsGrid");
        try {
            // 管理员可按 state.panelScope 切换 "我的面板/全部面板"; 普通用户强制使用 mine
            const scope = isAdmin() ? (state.panelScope || "mine") : "mine";
            const res = await api(`/panels?scope=${encodeURIComponent(scope)}`);
            if (res.success) {
                state.panels = res.data || [];
                renderPanels(state.panels);
                // 更新徽章
                $("badgePanels").textContent = state.panels.length;
                $("statPanels").textContent = state.panels.length;
            }
        } catch (err) {
            // 不再静默失败: 在面板网格中展示具体错误信息, 避免用户看到空白而困惑
            const reason = (err && err.message) ? err.message : "未知错误";
            if (grid) {
                grid.innerHTML = `
                    <div class="empty-state" style="grid-column:1/-1;">
                        <i class="fas fa-exclamation-triangle" style="color:var(--color-danger);"></i>
                        <h3>面板加载失败</h3>
                        <p>${escapeHtml(reason)}</p>
                        <button class="btn btn-secondary btn-sm" style="margin-top:12px;" onclick="window.__pockettermReloadPanels && window.__pockettermReloadPanels()">
                            <i class="fas fa-sync-alt"></i> 重试
                        </button>
                    </div>`;
            }
            // 重置徽章与统计, 避免显示陈旧数据
            state.panels = [];
            $("badgePanels").textContent = "0";
            $("statPanels").textContent = "0";
        }
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
                    : "永久";
                // 将面板状态保存到 state, 与 WebSocket 状态组合显示在 consoleInfo
                state.panelInfoText = `状态: ${check.status || "未知"} | 剩余: ${remaining}`;
                refreshConsoleInfo();
                appendTerminal(`面板状态: ${check.status || "未知"}`, "info");
                appendTerminal(`到期时间: ${check.expire_at ? formatTime(check.expire_at) : "永久"}`, "info");
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
            // 更新面板机器人 UI (按钮、状态指示器)
            updatePanelBotUI();
        } catch (_) { /* 已处理 */ }
    }

    /** 更新面板机器人 UI (按钮、状态指示器) */
    function updatePanelBotUI() {
        const startBtn = $("btnStartBot");
        const stopBtn = $("btnStopBot");
        const restartBtn = $("btnRestartBot");
        const botStatus = state.panelBot ? state.panelBot.status : null;
        // 运行中/启动中: 显示停止+重启
        if (botStatus && (botStatus === "running" || botStatus === "connected" || botStatus === "spawned" || botStatus === "starting" || botStatus === "connecting")) {
            startBtn.classList.add("hidden");
            stopBtn.classList.remove("hidden");
            restartBtn.classList.remove("hidden");
        } else if (botStatus === "error") {
            // 错误状态: 显示启动+停止 (允许停止重连尝试)
            startBtn.classList.remove("hidden");
            stopBtn.classList.remove("hidden");
            restartBtn.classList.add("hidden");
        } else {
            // 停止/未知: 仅显示启动
            startBtn.classList.remove("hidden");
            stopBtn.classList.add("hidden");
            restartBtn.classList.add("hidden");
        }
        // 更新面板状态指示器
        const statusDot = $("panelStatusDot");
        const statusText = $("panelStatusText");
        if (statusDot && statusText) {
            if (state.panelBot && (state.panelBot.status === "running" || state.panelBot.status === "connected" || state.panelBot.status === "spawned")) {
                statusDot.style.background = "#22c55e";
                statusText.textContent = "运行中";
                statusText.style.color = "#22c55e";
            } else if (state.panelBot && (state.panelBot.status === "starting" || state.panelBot.status === "connecting")) {
                statusDot.style.background = "#f59e0b";
                statusText.textContent = "启动中...";
                statusText.style.color = "#f59e0b";
            } else if (state.panelBot && state.panelBot.status === "error") {
                statusDot.style.background = "#ef4444";
                statusText.textContent = "错误";
                statusText.style.color = "#ef4444";
            } else {
                statusDot.style.background = "var(--text-tertiary)";
                statusText.textContent = "未启动";
                statusText.style.color = "var(--text-tertiary)";
            }
        }
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
                // 机器人状态变更 - 直接从 WebSocket 数据更新，避免 DB 延迟导致的竞态
                if (state.currentBotId && data.bot_id === state.currentBotId) {
                    if (data.status) {
                        // 映射内存状态到 DB 状态用于显示
                        const statusMap = {
                            idle: "stopped",
                            connecting: "connecting",
                            authenticating: "connecting",
                            connected: "running",
                            spawned: "running",
                            error: "error",
                            banned: "banned",
                            disconnected: "stopped",
                            kicked: "error",
                        };
                        const displayStatus = statusMap[data.status] || data.status;
                        if (state.panelBot) {
                            state.panelBot.status = displayStatus;
                            // 保存最近错误信息
                            if (data.last_error) {
                                state.panelBot.last_error = data.last_error;
                            }
                        }
                        // 错误状态特殊处理: 显示更详细的信息
                        if (displayStatus === "error") {
                            const errMsg = data.last_error || state.panelBot?.last_error || "未知错误";
                            appendTerminal(`机器人状态更新: 错误 (${errMsg})`, "error");
                        } else if (displayStatus === "banned") {
                            appendTerminal(`机器人状态更新: 已封禁`, "error");
                        } else {
                            appendTerminal(`机器人状态更新: ${displayStatus}`, "info");
                        }
                        // 直接更新 UI 而非重新从 API 读取
                        updatePanelBotUI();
                    }
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
        if (!state.currentBotId) {
            appendTerminal("没有可用的机器人，无法发送命令", "error");
            return;
        }

        // 检查机器人是否正在运行
        if (state.panelBot && state.panelBot.status !== "running") {
            appendTerminal("请先启动机器人再发送命令", "warn");
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
        const startBtn = $("btnStartBot");
        try {
            startBtn.disabled = true;
            appendTerminal("正在启动机器人...", "system");
            const res = await api(`/bots/${state.currentBotId}/start`, { method: "POST" });
            if (res.success) {
                appendTerminal("机器人启动成功", "success");
                toastSuccess("机器人已启动");
                // 成功后隐藏启动按钮，显示停止按钮
                $("btnStartBot").classList.add("hidden");
                $("btnStopBot").classList.remove("hidden");
                $("btnRestartBot").classList.remove("hidden");
                await loadPanelBot();
            } else {
                const errMsg = res.detail || res.message || "启动失败 (未知原因)";
                appendTerminal(`启动失败: ${errMsg}`, "error");
                toastError(errMsg);
                // 更新 UI 显示错误状态
                if (state.panelBot) state.panelBot.status = "error";
                updatePanelBotUI();
            }
        } catch (err) {
            const errMsg = err?.message || err?.detail || "启动请求失败";
            appendTerminal(`启动失败: ${errMsg}`, "error");
            toastError(errMsg);
            if (state.panelBot) state.panelBot.status = "error";
            updatePanelBotUI();
        } finally {
            startBtn.disabled = false;
        }
    }

    /** 停止机器人 */
    async function stopBot() {
        if (!ensureBotExists()) return;
        const stopBtn = $("btnStopBot");
        try {
            stopBtn.disabled = true;
            appendTerminal("正在停止机器人...", "system");
            const res = await api(`/bots/${state.currentBotId}/stop`, { method: "POST" });
            if (res.success) {
                appendTerminal("机器人已停止", "success");
                toastSuccess("机器人已停止");
                // 成功后显示启动按钮，隐藏停止按钮
                $("btnStartBot").classList.remove("hidden");
                $("btnStopBot").classList.add("hidden");
                $("btnRestartBot").classList.add("hidden");
                await loadPanelBot();
            } else {
                const errMsg = res.detail || res.message || "停止失败";
                appendTerminal(`停止失败: ${errMsg}`, "error");
                toastError(errMsg);
            }
        } catch (err) {
            const errMsg = err?.message || err?.detail || "停止请求失败";
            appendTerminal(`停止失败: ${errMsg}`, "error");
            toastError(errMsg);
        } finally {
            stopBtn.disabled = false;
        }
    }

    /** 重启机器人 */
    async function restartBot() {
        if (!ensureBotExists()) return;
        const restartBtn = $("btnRestartBot");
        try {
            restartBtn.disabled = true;
            appendTerminal("正在重启机器人...", "system");
            const res = await api(`/bots/${state.currentBotId}/restart`, { method: "POST" });
            if (res.success) {
                appendTerminal("机器人重启成功", "success");
                toastSuccess("机器人已重启");
                await loadPanelBot();
            } else {
                const errMsg = res.detail || res.message || "重启失败";
                appendTerminal(`重启失败: ${errMsg}`, "error");
                toastError(errMsg);
            }
        } catch (err) {
            const errMsg = err?.message || err?.detail || "重启请求失败";
            appendTerminal(`重启失败: ${errMsg}`, "error");
            toastError(errMsg);
        } finally {
            restartBtn.disabled = false;
        }
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
            // 加载可用账号到下拉框
            try {
                const accountsRes = await api("/bots/accounts");
                const accountSelect = $("botConfigAccount");
                if (accountSelect && accountsRes.success && accountsRes.data) {
                    const currentValue = state.panelBot ? state.panelBot.account_id : "";
                    accountSelect.innerHTML = '<option value="">-- 请选择账号 --</option>';
                    accountsRes.data.forEach((acc) => {
                        const opt = document.createElement("option");
                        opt.value = acc.account_id;
                        opt.textContent = `${acc.username || acc.player_name || acc.account_id} (${acc.status})`;
                        if (acc.account_id === currentValue) opt.selected = true;
                        accountSelect.appendChild(opt);
                    });
                }
            } catch (_) {}
            if (state.panelBot) {
                const bot = state.panelBot;
                $("botConfigName").value = bot.name || "";
                $("botConfigAccount").value = bot.account_id || "";
                $("botConfigServerCode").value = bot.server_code || "";
                $("botConfigServerType").value = bot.server_type || "rental";
                $("botConfigAccessPoint").value = bot.access_point_type || bot.access_point || "neomega";
                const gameVersionEl = $("botConfigGameVersion");
                // game_version 可能在 bot.game_version 或 config JSON 中
                let gameVersion = bot.game_version;
                if (!gameVersion && bot.config) {
                    try {
                        const cfg = typeof bot.config === "string" ? JSON.parse(bot.config) : bot.config;
                        gameVersion = cfg.game_version;
                    } catch (_) {}
                }
                if (gameVersionEl && gameVersion) gameVersionEl.value = gameVersion;
                else if (gameVersionEl) gameVersionEl.value = "1.21.93";
                $("botConfigExtra").value = bot.extra_config
                    ? (typeof bot.extra_config === "string"
                        ? bot.extra_config
                        : JSON.stringify(bot.extra_config, null, 2))
                    : "";
            } else {
                // 没有机器人 - 清空表单准备创建
                $("botConfigForm").reset();
            }
            // 表单已重新加载, 清除未保存标记
            state.botConfigDirty = false;
        } catch (_) { /* 已处理 */ }
    }

    /** 保存机器人配置 (创建或更新) */
    async function handleSaveBotConfig(e) {
        e.preventDefault();
        const name = $("botConfigName").value.trim();
        const accountId = $("botConfigAccount").value.trim();
        const serverCode = $("botConfigServerCode").value.trim();
        const serverType = $("botConfigServerType").value;
        const game_version = $("botConfigGameVersion")?.value || "1.21.93";
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
            game_version,
            access_point_type: accessPoint,
            config: extra,
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
                    state.botConfigDirty = false;
                    await loadPanelBot();
                } else {
                    toastError(res.detail || res.message || "保存失败");
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

    /** 检查机器人配置表单是否有未保存的修改 */
    function hasUnsavedConfig() {
        return !!state.botConfigDirty;
    }

    /** 加载接入点状态 */
    async function loadAccessPointStatus() {
        try {
            const res = await api("/system/access-points");
            if (!res.success || !res.data) return;
            const aps = res.data.available || [];
            aps.forEach((ap) => {
                const apType = ap.type || ap.name;
                if (apType === "neomega") {
                    const el = $("apNeomegaStatus");
                    if (el) {
                        el.innerHTML = ap.available
                            ? '状态: <span style="color:#22c55e;font-weight:600;">已安装</span>'
                            : '状态: <span style="color:var(--text-tertiary);">未安装</span>';
                    }
                } else if (apType === "fateark") {
                    const el = $("apFatearkStatus");
                    if (el) {
                        el.innerHTML = ap.available
                            ? '状态: <span style="color:#22c55e;font-weight:600;">已安装</span>'
                            : '状态: <span style="color:var(--text-tertiary);">未安装</span>';
                    }
                }
            });
        } catch (_) { /* 已处理 */ }
    }

    /** 下载接入点二进制 */
    async function downloadAccessPoint(name) {
        const btnId = name === "neomega" ? "btnDownloadNeomega" : "btnDownloadFateark";
        const statusId = name === "neomega" ? "apNeomegaStatus" : "apFatearkStatus";
        const btn = $(btnId);
        const statusEl = $(statusId);
        if (btn) {
            btn.disabled = true;
            btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> 下载中...';
        }
        if (statusEl) {
            statusEl.innerHTML = '状态: <span style="color:#f59e0b;">正在下载...</span>';
        }
        try {
            const res = await api(`/system/access-points/${name}/download`, { method: "POST" });
            if (res.success) {
                toastSuccess(`${name} 下载成功`);
                if (statusEl) {
                    statusEl.innerHTML = '状态: <span style="color:#22c55e;font-weight:600;">已安装</span>';
                }
            } else {
                toastError(`下载失败: ${res.detail || res.message || "未知错误"}`);
                if (statusEl) {
                    statusEl.innerHTML = '状态: <span style="color:#ef4444;">下载失败</span>';
                }
            }
        } catch (e) {
            const msg = e.message || "网络错误";
            toastError(`下载失败: ${msg}`);
            if (statusEl) {
                statusEl.innerHTML = `状态: <span style="color:#ef4444;">${msg}</span>`;
            }
        } finally {
            if (btn) {
                btn.disabled = false;
                btn.innerHTML = '<i class="fas fa-download"></i> 下载';
            }
            await loadAccessPointStatus();
        }
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
            // 默认不返回已撤销卡密 (include_revoked=false), 勾选"显示已撤销"或筛选 revoked 状态时才返回
            const showRevoked = state.cardShowRevoked || state.cardFilterStatus === "revoked";
            params.set("include_revoked", showRevoked ? "true" : "false");
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
                        <select class="filter-select" data-status-select="${escapeHtml(userId)}" ${(!isSuperadmin && user.role === "superadmin") || isSelf ? "disabled" : ""} style="font-size:12px;padding:4px 8px;">
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
                <div style="display:flex;flex-wrap:wrap;gap:4px 10px;padding:8px 12px;border-bottom:1px solid #21262d;font-family:var(--font-mono);font-size:12px;line-height:1.6;writing-mode:horizontal-tb;">
                    <span style="color:#484f58;flex-shrink:0;white-space:nowrap;">${escapeHtml(time)}</span>
                    <span style="color:${color};font-weight:600;flex-shrink:0;white-space:nowrap;text-transform:uppercase;">${escapeHtml(level)}</span>
                    ${source ? `<span style="color:#7d8590;flex-shrink:0;white-space:nowrap;">[${escapeHtml(source)}]</span>` : ""}
                    <span style="color:#e6edf3;word-break:break-word;flex:1 1 240px;min-width:240px;">${escapeHtml(message)}</span>
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
       19b. 商店 / 文件管理 / 管理后台 (Shop / Files / Admin)
       ====================================================================== */

    /* -------------------- 商店 (Shop) -------------------- */

    /**
     * 加载商店数据: 商品列表、余额、订单
     */
    async function loadShop() {
        // 并行加载商品、订单; 余额由 loadBalance() 单独加载 (仅商店视图调用)
        const [productsRes, ordersRes] = await Promise.allSettled([
            api("/shop/products"),
            api("/shop/orders"),
        ]);

        // 渲染余额 (商店视图)
        loadBalance();

        // 渲染商品 (按分类分组) - API 返回 {panel_card: [...], register_card: [...], ...}
        let productsGrouped = {};
        if (productsRes.status === "fulfilled" && productsRes.value) {
            const raw = productsRes.value.data || productsRes.value;
            if (raw && typeof raw === "object" && !Array.isArray(raw)) {
                productsGrouped = raw;
            } else if (Array.isArray(raw)) {
                // 兼容: 如果直接返回数组, 按 category 分组
                raw.forEach((p) => {
                    const cat = p.category || p.type || "other";
                    if (!productsGrouped[cat]) productsGrouped[cat] = [];
                    productsGrouped[cat].push(p);
                });
            }
        }
        const cardProducts = [
            ...(productsGrouped.panel_card || []),
            ...(productsGrouped.register_card || []),
        ];
        const pluginProducts = productsGrouped.plugin_file || [];
        const buildingProducts = productsGrouped.building_file || [];
        renderProducts(cardProducts, "cardProducts");
        renderProducts(pluginProducts, "pluginProducts");
        renderProducts(buildingProducts, "buildingProducts");

        // 渲染订单
        let orders = [];
        if (ordersRes.status === "fulfilled" && ordersRes.value) {
            orders = ordersRes.value.data || ordersRes.value || [];
        }
        if (!Array.isArray(orders)) orders = [];
        renderOrders(orders, "myOrders");
    }

    /**
     * 购买商品
     * @param {number|string} productId - 商品 ID
     * @param {string} productName - 商品名称
     * @param {number} price - 价格
     */
    async function purchaseProduct(productId, productName, price) {
        if (!confirm(`确定要购买「${productName}」吗？将扣除 ${parseFloat(price).toFixed(2)} 余额。`)) return;
        try {
            const res = await api("/shop/purchase", {
                method: "POST",
                body: { product_id: productId },
            });
            if (res.success !== false) {
                toastSuccess("购买成功");
                // 显示卡密 (如果有)
                const cardKey = res.card_key || (res.data && res.data.card_key);
                if (cardKey) {
                    toastInfo("卡密: " + cardKey);
                    copyToClipboard(cardKey);
                }
                // 重新加载商店 (刷新余额与订单)
                loadShop();
            }
        } catch (err) {
            // 错误已由 api() 处理
        }
    }

    /**
     * 渲染商品卡片
     * @param {array} products - 商品数组
     * @param {string} containerId - 容器元素 ID
     */
    function renderProducts(products, containerId) {
        const container = $(containerId);
        if (!container) return;
        // 设置网格布局
        container.style.display = "grid";
        container.style.gridTemplateColumns = "repeat(auto-fill,minmax(200px,1fr))";
        container.style.gap = "12px";
        if (!products || products.length === 0) {
            container.innerHTML = renderEmptyState("fa-box-open", "暂无商品", "该分类下暂无可用商品");
            return;
        }
        container.innerHTML = products.map((p) => {
            const id = p.id || p.product_id;
            const name = p.name || p.product_name || "未命名";
            const desc = p.description || p.desc || "";
            const price = parseFloat(p.price || 0).toFixed(2);
            return `
                <div style="padding:14px;border:1px solid var(--border-muted);border-radius:10px;background:var(--bg-elevated);display:flex;flex-direction:column;gap:8px;">
                    <div style="font-weight:600;font-size:14px;color:var(--text-primary);word-break:break-word;">${escapeHtml(name)}</div>
                    ${desc ? `<div style="font-size:12px;color:var(--text-tertiary);flex:1;word-break:break-word;">${escapeHtml(desc)}</div>` : '<div style="flex:1;"></div>'}
                    <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;">
                        <span style="color:#3fb950;font-weight:700;font-size:15px;"><i class="fas fa-coins"></i> ${escapeHtml(price)}</span>
                        <button class="btn btn-primary btn-sm" onclick="purchaseProduct('${escAttr(id)}','${escAttr(name)}',${escapeHtml(price)})"><i class="fas fa-shopping-cart"></i> 购买</button>
                    </div>
                </div>
            `;
        }).join("");
    }

    /**
     * 渲染订单列表
     * @param {array} orders - 订单数组
     * @param {string} containerId - 容器元素 ID
     */
    function renderOrders(orders, containerId) {
        const container = $(containerId);
        if (!container) return;
        if (!orders || orders.length === 0) {
            container.innerHTML = renderEmptyState("fa-receipt", "暂无订单", "购买的商品订单将显示在这里");
            return;
        }
        container.innerHTML = orders.map((o) => {
            const orderId = o.order_id || o.id || "-";
            const productName = o.product_name || o.name || "-";
            const price = parseFloat(o.price || 0).toFixed(2);
            const time = formatTime(o.created_at || o.created || o.date);
            const cardKey = o.card_key || o.cardkey || "";
            return `
                <div style="padding:12px;border-bottom:1px solid var(--border-muted);">
                    <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px;">
                        <div style="flex:1;min-width:0;">
                            <div style="font-weight:600;font-size:13px;color:var(--text-primary);">${escapeHtml(productName)}</div>
                            <div style="font-size:11px;color:var(--text-tertiary);margin-top:2px;">订单号: ${escapeHtml(orderId)}</div>
                            <div style="font-size:11px;color:var(--text-tertiary);">${escapeHtml(time)}</div>
                            ${cardKey ? `<div style="margin-top:6px;padding:6px 8px;background:var(--bg-secondary);border-radius:6px;font-size:12px;color:var(--color-success);word-break:break-all;"><i class="fas fa-key"></i> ${escapeHtml(cardKey)} <button class="btn btn-secondary btn-sm" style="margin-left:4px;padding:2px 6px;font-size:11px;" onclick="copyToClipboard('${escAttr(cardKey)}')"><i class="fas fa-copy"></i></button></div>` : ""}
                        </div>
                        <span style="color:#3fb950;font-weight:600;font-size:13px;white-space:nowrap;"><i class="fas fa-coins"></i> ${escapeHtml(price)}</span>
                    </div>
                </div>
            `;
        }).join("");
    }

    /* -------------------- 文件管理 (Files) -------------------- */

    /**
     * 加载文件列表: 只加载自己上传的文件 (/files/my)
     * 公开文件 (插件/建筑) 已移至商店视图购买, 不再在文件管理显示
     */
    async function loadFiles() {
        const container = $("myFilesList");
        if (!container) return;
        container.innerHTML = `<div class="empty-state"><i class="fas fa-spinner fa-spin"></i><p style="font-size:13px;">加载中...</p></div>`;
        try {
            const myRes = await api("/files/my");
            let myUploaded = [];
            if (myRes) {
                const d = myRes.data || myRes;
                if (d && typeof d === "object" && !Array.isArray(d)) {
                    // {uploaded: [...], purchased: [...]} -> 只显示自己上传的
                    myUploaded = Array.isArray(d.uploaded) ? d.uploaded : [];
                } else if (Array.isArray(d)) {
                    myUploaded = d;
                }
            }
            renderMyFiles(myUploaded, "myFilesList");
        } catch (err) {
            container.innerHTML = renderEmptyState("fa-exclamation-circle", "加载失败", err.message || "请稍后重试");
        }
    }

    /**
     * 显示/隐藏上传表单
     */
    function toggleUploadForm() {
        const card = $("uploadFormCard");
        if (card) card.style.display = card.style.display === "none" ? "block" : "none";
    }

    /**
     * 处理文件上传 (商店文件)
     */
    async function handleShopFileUpload() {
        const name = $("uploadName").value.trim();
        const category = $("uploadCategory").value;
        const price = $("uploadPrice").value;
        const desc = $("uploadDesc").value.trim();
        const fileInput = $("uploadFile");
        const file = fileInput.files[0];

        if (!name) { toastWarn("请输入文件名称"); return; }
        if (!file) { toastWarn("请选择文件"); return; }
        if (file.size > 512 * 1024) { toastWarn("文件大小不能超过 512KB"); return; }

        const btn = $("confirmUploadBtn");
        const oldHtml = btn.innerHTML;
        btn.disabled = true;
        btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> 上传中...';

        try {
            const formData = new FormData();
            formData.append("file", file);
            formData.append("name", name);
            formData.append("description", desc);
            formData.append("price", price);
            formData.append("category", category);

            const res = await api("/files/upload", { method: "POST", body: formData });
            if (res.success !== false) {
                toastSuccess("文件上传成功，等待审核");
                // 重置表单
                $("uploadName").value = "";
                $("uploadPrice").value = "0";
                $("uploadDesc").value = "";
                fileInput.value = "";
                toggleUploadForm();
                // 上传发生在商店视图, 刷新商店商品列表
                loadShop();
            }
        } catch (err) {
            // 错误已由 api() 处理
        } finally {
            btn.disabled = false;
            btn.innerHTML = oldHtml;
        }
    }

    /**
     * 购买文件
     * @param {number|string} fileId - 文件 ID
     * @param {string} fileName - 文件名称
     * @param {number} price - 价格
     */
    async function purchaseFile(fileId, fileName, price) {
        if (parseFloat(price) > 0) {
            if (!confirm(`确定要购买「${fileName}」吗？将扣除 ${parseFloat(price).toFixed(2)} 余额。`)) return;
        }
        try {
            const res = await api(`/files/${fileId}/purchase`, { method: "POST" });
            if (res.success !== false) {
                toastSuccess("购买成功，现在可以下载该文件");
                loadFiles();
            }
        } catch (err) {
            // 错误已由 api() 处理
        }
    }

    /**
     * 下载文件 (在新标签页打开)
     * @param {number|string} fileId - 文件 ID
     */
    function downloadFile(fileId) {
        window.open("/api/v2/files/" + fileId + "/download", "_blank");
    }

    /**
     * 渲染文件列表 (公开文件 - 插件/建筑)
     * @param {array} files - 文件数组
     * @param {string} containerId - 容器元素 ID
     */
    function renderFileList(files, containerId) {
        const container = $(containerId);
        if (!container) return;
        if (!files || files.length === 0) {
            container.innerHTML = renderEmptyState("fa-folder-open", "暂无文件", "该分类下暂无可用文件");
            return;
        }
        container.innerHTML = files.map((f) => {
            const id = f.id || f.file_id;
            const name = f.name || f.filename || "未命名";
            const desc = f.description || f.desc || "";
            const price = parseFloat(f.price || 0).toFixed(2);
            const size = formatFileSize(f.file_size || f.size);
            const uploader = f.uploader || f.username || f.author || "";
            const purchased = f.purchased || f.owned || false;
            const isFree = parseFloat(f.price || 0) === 0;
            // 免费文件或已购买 -> 可下载; 否则显示购买按钮
            let actionHtml = "";
            if (isFree || purchased) {
                actionHtml = `<button class="btn btn-primary btn-sm" onclick="downloadFile('${escAttr(id)}')"><i class="fas fa-download"></i> 下载</button>`;
            } else {
                actionHtml = `<button class="btn btn-primary btn-sm" onclick="purchaseFile('${escAttr(id)}','${escAttr(name)}',${escapeHtml(price)})"><i class="fas fa-shopping-cart"></i> 购买</button>`;
            }
            return `
                <div style="padding:12px;border:1px solid var(--border-muted);border-radius:10px;background:var(--bg-elevated);margin-bottom:10px;">
                    <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px;">
                        <div style="flex:1;min-width:0;">
                            <div style="font-weight:600;font-size:13px;color:var(--text-primary);word-break:break-word;">${escapeHtml(name)}</div>
                            ${desc ? `<div style="font-size:12px;color:var(--text-tertiary);margin-top:4px;word-break:break-word;">${escapeHtml(desc)}</div>` : ""}
                            <div style="font-size:11px;color:var(--text-tertiary);margin-top:4px;">
                                ${size ? `<span style="margin-right:8px;"><i class="fas fa-file"></i> ${escapeHtml(size)}</span>` : ""}
                                ${uploader ? `<span><i class="fas fa-user"></i> ${escapeHtml(uploader)}</span>` : ""}
                            </div>
                        </div>
                        <div style="text-align:right;white-space:nowrap;">
                            <div style="color:#3fb950;font-weight:600;font-size:13px;margin-bottom:6px;">${isFree ? "免费" : '<i class="fas fa-coins"></i> ' + escapeHtml(price)}</div>
                            ${actionHtml}
                        </div>
                    </div>
                </div>
            `;
        }).join("");
    }

    /**
     * 渲染我的文件列表 (含审核状态)
     * @param {array} files - 文件数组
     * @param {string} containerId - 容器元素 ID
     */
    function renderMyFiles(files, containerId) {
        const container = $(containerId);
        if (!container) return;
        if (!files || files.length === 0) {
            container.innerHTML = renderEmptyState("fa-folder-open", "暂无文件", "您上传的文件将显示在这里");
            return;
        }
        const statusMap = {
            pending: { label: "待审核", color: "#d29922" },
            approved: { label: "已通过", color: "#3fb950" },
            rejected: { label: "已拒绝", color: "#f85149" },
        };
        container.innerHTML = files.map((f) => {
            const name = f.name || f.filename || "未命名";
            const desc = f.description || f.desc || "";
            const price = parseFloat(f.price || 0).toFixed(2);
            const category = f.category || "";
            const status = f.status || "pending";
            const st = statusMap[status] || statusMap.pending;
            const catLabel = category === "plugin" ? "插件" : category === "building" ? "建筑" : category;
            return `
                <div style="padding:12px;border:1px solid var(--border-muted);border-radius:10px;background:var(--bg-elevated);margin-bottom:10px;">
                    <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px;">
                        <div style="flex:1;min-width:0;">
                            <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
                                <span style="font-weight:600;font-size:13px;color:var(--text-primary);word-break:break-word;">${escapeHtml(name)}</span>
                                <span style="font-size:10px;padding:1px 8px;border-radius:9999px;background:${st.color}22;color:${st.color};border:1px solid ${st.color}44;">${escapeHtml(st.label)}</span>
                                <span style="font-size:10px;padding:1px 8px;border-radius:9999px;background:var(--bg-secondary);color:var(--text-tertiary);">${escapeHtml(catLabel)}</span>
                            </div>
                            ${desc ? `<div style="font-size:12px;color:var(--text-tertiary);margin-top:4px;word-break:break-word;">${escapeHtml(desc)}</div>` : ""}
                            ${f.reject_reason ? `<div style="font-size:12px;color:#f85149;margin-top:4px;">拒绝原因: ${escapeHtml(f.reject_reason)}</div>` : ""}
                        </div>
                        <span style="color:#3fb950;font-weight:600;font-size:13px;white-space:nowrap;">${parseFloat(price) === 0 ? "免费" : '<i class="fas fa-coins"></i> ' + escapeHtml(price)}</span>
                    </div>
                </div>
            `;
        }).join("");
    }

    /* -------------------- 管理后台 (Admin) -------------------- */

    /**
     * 加载订单列表 (管理员)
     * @param {string} [searchQuery] - 搜索关键词 (可选)
     */
    async function loadAdminOrders(searchQuery) {
        const container = $("adminOrdersList");
        if (!container) return;
        container.innerHTML = `<div class="empty-state"><i class="fas fa-spinner fa-spin"></i><p style="font-size:13px;">加载中...</p></div>`;
        try {
            let res;
            if (searchQuery && searchQuery.trim()) {
                res = await api("/shop/orders/search?q=" + encodeURIComponent(searchQuery.trim()));
            } else {
                res = await api("/shop/admin/orders");
            }
            let orders = [];
            if (res) {
                orders = res.data || res.orders || res || [];
            }
            if (!Array.isArray(orders)) orders = [];
            if (orders.length === 0) {
                container.innerHTML = renderEmptyState("fa-receipt", "暂无订单", searchQuery ? "未找到匹配的订单" : "所有订单将显示在这里");
                return;
            }
            container.innerHTML = orders.map((o) => {
                const orderId = o.order_id || o.id || "-";
                const productName = o.product_name || o.name || "-";
                const username = o.username || o.user_name || o.user || "-";
                const price = parseFloat(o.price || 0).toFixed(2);
                const time = formatTime(o.created_at || o.created || o.date);
                const cardKey = o.card_key || o.cardkey || "";
                return `
                    <div style="padding:12px 16px;border-bottom:1px solid var(--border-muted);">
                        <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px;flex-wrap:wrap;">
                            <div style="flex:1;min-width:200px;">
                                <div style="font-weight:600;font-size:13px;color:var(--text-primary);">${escapeHtml(productName)}</div>
                                <div style="font-size:11px;color:var(--text-tertiary);margin-top:2px;">
                                    <span style="margin-right:8px;">订单号: ${escapeHtml(orderId)}</span>
                                    <span><i class="fas fa-user"></i> ${escapeHtml(username)}</span>
                                </div>
                                <div style="font-size:11px;color:var(--text-tertiary);margin-top:2px;">${escapeHtml(time)}</div>
                                ${cardKey ? `<div style="margin-top:4px;font-size:12px;color:var(--color-success);word-break:break-all;"><i class="fas fa-key"></i> ${escapeHtml(cardKey)}</div>` : ""}
                            </div>
                            <span style="color:#3fb950;font-weight:600;font-size:13px;white-space:nowrap;"><i class="fas fa-coins"></i> ${escapeHtml(price)}</span>
                        </div>
                    </div>
                `;
            }).join("");
        } catch (err) {
            container.innerHTML = renderEmptyState("fa-exclamation-circle", "加载失败", err.message || "请稍后重试");
        }
    }

    /**
     * 搜索订单
     */
    function searchOrders() {
        const q = $("orderSearchInput").value;
        loadAdminOrders(q);
    }

    /**
     * 加载待审核文件列表 (管理员)
     */
    async function loadReviewFiles() {
        const container = $("reviewFilesList");
        if (!container) return;
        container.innerHTML = `<div class="empty-state"><i class="fas fa-spinner fa-spin"></i><p style="font-size:13px;">加载中...</p></div>`;
        try {
            const res = await api("/files/pending");
            let files = [];
            if (res) {
                files = res.data || res.files || res || [];
            }
            if (!Array.isArray(files)) files = [];
            if (files.length === 0) {
                container.innerHTML = renderEmptyState("fa-check-circle", "暂无待审核文件", "所有文件已审核完毕");
                return;
            }
            container.innerHTML = files.map((f) => {
                const id = f.id || f.file_id;
                const name = f.name || f.filename || "未命名";
                const desc = f.description || f.desc || "";
                const price = parseFloat(f.price || 0).toFixed(2);
                const category = f.category || "";
                const uploader = f.uploader || f.username || f.author || "-";
                const catLabel = category === "plugin" ? "插件" : category === "building" ? "建筑" : category;
                const size = formatFileSize(f.file_size || f.size);
                return `
                    <div style="padding:14px;border:1px solid var(--border-muted);border-radius:10px;background:var(--bg-elevated);margin-bottom:10px;">
                        <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:12px;flex-wrap:wrap;">
                            <div style="flex:1;min-width:200px;">
                                <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
                                    <span style="font-weight:600;font-size:14px;color:var(--text-primary);">${escapeHtml(name)}</span>
                                    <span style="font-size:10px;padding:1px 8px;border-radius:9999px;background:var(--bg-secondary);color:var(--text-tertiary);">${escapeHtml(catLabel)}</span>
                                </div>
                                ${desc ? `<div style="font-size:12px;color:var(--text-tertiary);margin-top:4px;word-break:break-word;">${escapeHtml(desc)}</div>` : ""}
                                <div style="font-size:11px;color:var(--text-tertiary);margin-top:4px;">
                                    <span style="margin-right:8px;"><i class="fas fa-user"></i> ${escapeHtml(uploader)}</span>
                                    ${size ? `<span style="margin-right:8px;"><i class="fas fa-file"></i> ${escapeHtml(size)}</span>` : ""}
                                    <span style="color:#3fb950;font-weight:600;"><i class="fas fa-coins"></i> ${parseFloat(price) === 0 ? "免费" : escapeHtml(price)}</span>
                                </div>
                            </div>
                            <div style="display:flex;gap:8px;">
                                <button class="btn btn-primary btn-sm" onclick="approveFile('${escAttr(id)}')"><i class="fas fa-check"></i> 通过</button>
                                <button class="btn btn-danger btn-sm" onclick="rejectFile('${escAttr(id)}')"><i class="fas fa-times"></i> 拒绝</button>
                            </div>
                        </div>
                    </div>
                `;
            }).join("");
        } catch (err) {
            container.innerHTML = renderEmptyState("fa-exclamation-circle", "加载失败", err.message || "请稍后重试");
        }
    }

    /**
     * 审核通过文件 (管理员)
     * @param {number|string} fileId - 文件 ID
     */
    async function approveFile(fileId) {
        try {
            const res = await api(`/files/${fileId}/approve`, { method: "POST" });
            if (res.success !== false) {
                toastSuccess("文件已通过审核");
                loadReviewFiles();
            }
        } catch (err) {
            // 错误已由 api() 处理
        }
    }

    /**
     * 拒绝文件 (管理员)
     * @param {number|string} fileId - 文件 ID
     */
    async function rejectFile(fileId) {
        const reason = prompt("请输入拒绝原因:");
        if (reason === null) return; // 用户取消
        try {
            const formData = new FormData();
            formData.append("reason", reason || "");
            const res = await api(`/files/${fileId}/reject`, {
                method: "POST",
                body: formData,
            });
            if (res.success !== false) {
                toastSuccess("文件已拒绝");
                loadReviewFiles();
            }
        } catch (err) {
            // 错误已由 api() 处理
        }
    }

    /**
     * 加载用户余额列表 (管理员)
     */
    async function loadUsersBalance() {
        const container = $("usersBalanceList");
        if (!container) return;
        container.innerHTML = `<div class="empty-state"><i class="fas fa-spinner fa-spin"></i><p style="font-size:13px;">加载中...</p></div>`;
        try {
            const res = await api("/auth/users");
            let users = [];
            if (res) {
                users = res.data || res.users || res || [];
            }
            if (!Array.isArray(users)) users = [];
            state.users = users;
            if (users.length === 0) {
                container.innerHTML = renderEmptyState("fa-users", "暂无用户", "用户列表为空");
                return;
            }
            container.innerHTML = users.map((u) => {
                const username = u.username || "-";
                const balance = parseFloat(u.balance != null ? u.balance : (u.wallet_balance || 0)).toFixed(2);
                const role = u.role || "user";
                const roleLabel = ROLE_LABELS[role] || role;
                return `
                    <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;padding:10px 12px;border-bottom:1px solid var(--border-muted);">
                        <div style="display:flex;align-items:center;gap:10px;flex:1;min-width:0;">
                            <div style="width:30px;height:30px;border-radius:50%;background:linear-gradient(135deg,#58a6ff,#a371f7);display:flex;align-items:center;justify-content:center;font-size:12px;color:#fff;font-weight:600;flex-shrink:0;">
                                ${escapeHtml((username || "U").charAt(0).toUpperCase())}
                            </div>
                            <div style="min-width:0;">
                                <div style="font-weight:500;font-size:13px;color:var(--text-primary);">${escapeHtml(username)}</div>
                                <div style="font-size:11px;color:var(--text-tertiary);">${escapeHtml(roleLabel)}</div>
                            </div>
                        </div>
                        <span style="color:#3fb950;font-weight:700;font-size:14px;white-space:nowrap;"><i class="fas fa-coins"></i> ${escapeHtml(balance)}</span>
                    </div>
                `;
            }).join("");
        } catch (err) {
            container.innerHTML = renderEmptyState("fa-exclamation-circle", "加载失败", err.message || "请稍后重试");
        }
    }

    /**
     * 设置用户余额 (管理员)
     */
    async function setUserBalance() {
        const username = $("balanceUsername").value.trim();
        const amount = $("balanceAmount").value;
        if (!username) { toastWarn("请输入用户名"); return; }
        if (amount === "" || amount === null) { toastWarn("请输入余额"); return; }

        // 通过用户名查找 user_id
        const user = state.users.find((u) => u.username === username);
        if (!user) {
            toastError("未找到该用户: " + username);
            return;
        }
        const userId = user.user_id || user.id;
        const btn = $("setBalanceBtn");
        const oldHtml = btn.innerHTML;
        btn.disabled = true;
        btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> 设置中...';

        try {
            const res = await api(`/shop/balance/${userId}`, {
                method: "POST",
                body: { balance: parseFloat(amount) },
            });
            if (res.success !== false) {
                toastSuccess(`已设置 ${username} 的余额为 ${parseFloat(amount).toFixed(2)}`);
                $("balanceUsername").value = "";
                $("balanceAmount").value = "";
                loadUsersBalance();
            }
        } catch (err) {
            // 错误已由 api() 处理
        } finally {
            btn.disabled = false;
            btn.innerHTML = oldHtml;
        }
    }

    /* ======================================================================
       20. 事件绑定
       ====================================================================== */

    /** 加载可用的Cookie池账号 */
    async function loadCookiePoolAccounts() {
        try {
            const res = await api("/bots/accounts?status=active");
            if (res.success && res.data) {
                return res.data;
            }
            return [];
        } catch (_) {
            return [];
        }
    }

    /** 切换创建机器人的账号来源 */
    function toggleCreateBotAccountSource() {
        const source = $("createBotAccountSource");
        if (!source) return;
        const val = source.value;
        $("createBot4399Fields").style.display = val === "new" ? "block" : "none";
        $("createBotPoolInfo").style.display = val === "pool" ? "block" : "none";
        $("createBotManualFields").style.display = val === "manual" ? "block" : "none";
    }

    /** 切换手动输入凭证的认证类型 */
    function toggleManualAuthType() {
        const checked = document.querySelector('input[name="manualAuthType"]:checked');
        if (!checked) return;
        const is4399 = checked.value === "4399";
        $("manual4399Fields").style.display = is4399 ? "block" : "none";
        $("manualCookieFields").style.display = is4399 ? "none" : "block";
    }

    /** 创建机器人 */
    async function handleCreateBot() {
        const accountSource = $("createBotAccountSource").value;

        const payload = {
            account_source: accountSource,
        };

        if (accountSource === "new") {
            // 自动注册新4399账号 - 不需要服务器编号, 后续在面板配置中填写
        } else if (accountSource === "manual") {
            const authType = document.querySelector('input[name="manualAuthType"]:checked');
            if (authType && authType.value === "4399") {
                payload.username_4399 = $("createBotManualUser").value.trim();
                payload.password_4399 = $("createBotManualPass").value.trim();
                if (!payload.username_4399 || !payload.password_4399) {
                    toastError("请填写4399账号密码");
                    return;
                }
            } else {
                payload.sauth_json = $("createBotSauthJson").value.trim();
                if (!payload.sauth_json) {
                    toastError("请粘贴 sauth_json 或 Cookie");
                    return;
                }
            }
        } else if (accountSource === "pool") {
            // 从Cookie池选择 - 服务器编号后续在面板配置中填写
        }

        try {
            const btn = $("btnCreateBot");
            btn.disabled = true;
            btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> 创建中...';

            const res = await api("/bots/create", {
                method: "POST",
                body: JSON.stringify(payload),
            });

            if (res.success) {
                toastSuccess("账号创建成功！");
                $("createBotManualUser") && ($("createBotManualUser").value = "");
                $("createBotManualPass") && ($("createBotManualPass").value = "");
                $("createBotSauthJson") && ($("createBotSauthJson").value = "");
                switchView("bots");
                await loadBots();
            } else {
                toastError(res.detail || "创建失败");
            }
        } catch (e) {
            toastError("创建失败: " + (e.message || "未知错误"));
        } finally {
            const btn = $("btnCreateBot");
            btn.disabled = false;
            btn.innerHTML = '<i class="fas fa-plus"></i> 创建账号';
        }
    }

    /** 获取4399验证码图片 */
    async function fetchCreateBotCaptcha() {
        const imgBox = $("createBotCaptchaImg");
        if (!imgBox) return;
        try {
            imgBox.innerHTML = '<span style="font-size:11px;color:var(--text-tertiary);">加载中...</span>';
            const res = await fetch("/api/accounts/login4399/captcha").then(r => r.json());
            if (res.success && res.data && res.data.image) {
                // 后端返回纯 base64, 需要添加 data URL 前缀
                const imgSrc = res.data.image.startsWith("data:")
                    ? res.data.image
                    : `data:image/jpeg;base64,${res.data.image}`;
                imgBox.innerHTML = `<img src="${imgSrc}" style="width:100%;height:100%;object-fit:cover;" alt="验证码">`;
                imgBox.dataset.captchaId = res.data.id || res.data.captcha_id || "";
            } else {
                imgBox.innerHTML = '<span style="font-size:11px;color:var(--text-tertiary);">点击重试</span>';
            }
        } catch (e) {
            imgBox.innerHTML = '<span style="font-size:11px;color:var(--text-tertiary);">点击重试</span>';
        }
    }

    /** 替换卡密 */
    async function handleReplaceKey() {
        const oldKey = $("replaceKeyOld").value.trim();
        if (!oldKey) {
            toastError("请输入要替换的原卡密");
            return;
        }
        const keyType = $("replaceKeyType").value;
        const duration = $("replaceKeyDuration").value;

        const payload = {
            old_key: oldKey,
            key_type: keyType,
            duration_days: duration ? parseInt(duration) : null,
        };

        try {
            const res = await api("/auth/cards/replace", {
                method: "POST",
                body: JSON.stringify(payload),
            });
            if (res.success) {
                toastSuccess("卡密替换成功！新卡密: " + (res.data?.new_key || ""));
                $("replaceKeyModal").style.display = "none";
                $("replaceKeyOld").value = "";
                $("replaceKeyDuration").value = "";
                await loadCards();
            } else {
                toastError(res.detail || "替换失败");
            }
        } catch (e) {
            toastError("替换失败: " + (e.message || "未知错误"));
        }
    }

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
        // ---- 邮箱验证码发送 ----
        const sendEmailCodeBtn = $("sendEmailCodeBtn");
        if (sendEmailCodeBtn) sendEmailCodeBtn.addEventListener("click", sendEmailCode);

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
        // 最近活动 折叠/展开
        const toggleActivityBtn = $("toggleActivity");
        if (toggleActivityBtn) {
            toggleActivityBtn.addEventListener("click", toggleActivityCollapse);
        }
        // 恢复上次的折叠状态
        restoreActivityCollapse();
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
        // 面板范围切换 (我的面板/全部面板) - 仅管理员可见
        $$("[data-panel-scope]").forEach((tab) => {
            tab.addEventListener("click", () => {
                const scope = tab.dataset.panelScope;
                if (!isAdmin() || scope === state.panelScope) return;
                state.panelScope = scope;
                $$("[data-panel-scope]").forEach((t) => t.classList.toggle("active", t === tab));
                loadPanels();
            });
        });

        // ---- 面板详情 ----
        $("backToPanels").addEventListener("click", () => {
            if (hasUnsavedConfig()) {
                if (!confirm("有未保存的配置，确定要离开吗？")) return;
            }
            switchView("panels");
        });
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

        // ---- 创建机器人 ----
        $("btnCreateBot").addEventListener("click", handleCreateBot);
        $("createBotAccountSource").addEventListener("change", toggleCreateBotAccountSource);
        $("createBotCaptchaImg") && $("createBotCaptchaImg").addEventListener("click", fetchCreateBotCaptcha);
        $("manualAuth4399") && $("manualAuth4399").addEventListener("change", toggleManualAuthType);
        $("manualAuthCookie") && $("manualAuthCookie").addEventListener("change", toggleManualAuthType);

        // ---- 替换Key ----
        $("btnReplaceKey").addEventListener("click", () => {
            $("replaceKeyModal").style.display = "flex";
        });
        $("btnConfirmReplaceKey").addEventListener("click", handleReplaceKey);

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
        // 监听配置表单修改, 标记为未保存
        $("botConfigForm").addEventListener("input", () => { state.botConfigDirty = true; });
        $("botConfigForm").addEventListener("change", () => { state.botConfigDirty = true; });

        // ---- 接入点下载 ----
        $("btnDownloadNeomega").addEventListener("click", () => downloadAccessPoint("neomega"));
        $("btnDownloadFateark").addEventListener("click", () => downloadAccessPoint("fateark"));

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
        // 显示/隐藏已撤销卡密
        $("cardShowRevoked").addEventListener("change", (e) => {
            state.cardShowRevoked = e.target.checked;
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
        $("nv1SetKeyBtn").addEventListener("click", handleNV1SetKey);
        $("nv1SetNbBtn").addEventListener("click", handleNV1SetNovaBuilder);

        // ---- 公告 ----
        $("annCreateBtn").addEventListener("click", () => openModal("modalCreateAnnouncement"));
        $("createAnnouncementSubmit").addEventListener("click", handleCreateAnnouncement);

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

        // ---- 文件管理 ----
        const fileUploadInput = $("fileUploadInput");
        const btnUploadFile = $("btnUploadFile");
        if (btnUploadFile) btnUploadFile.addEventListener("click", () => fileUploadInput && fileUploadInput.click());
        if (fileUploadInput) fileUploadInput.addEventListener("change", (e) => handleFileUpload(e));

        const btnRefreshFiles = $("btnRefreshFiles");
        if (btnRefreshFiles) btnRefreshFiles.addEventListener("click", () => loadPanelFiles());

        // ---- 插件管理 ----
        const pluginUploadInput = $("pluginUploadInput");
        const btnUploadPlugin = $("btnUploadPlugin");
        if (btnUploadPlugin) btnUploadPlugin.addEventListener("click", () => pluginUploadInput && pluginUploadInput.click());
        if (pluginUploadInput) pluginUploadInput.addEventListener("change", (e) => handlePluginUpload(e));

        const btnRefreshPlugins = $("btnRefreshPlugins");
        if (btnRefreshPlugins) btnRefreshPlugins.addEventListener("click", () => loadPanelPlugins());

        // ---- 快捷命令菜单 ----
        const btnQuickCmd = $("btnQuickCmd");
        const quickCmdMenu = $("quickCmdMenu");
        if (btnQuickCmd) btnQuickCmd.addEventListener("click", (e) => {
            e.stopPropagation();
            if (quickCmdMenu) quickCmdMenu.style.display = quickCmdMenu.style.display === "none" ? "block" : "none";
        });
        if (quickCmdMenu) {
            quickCmdMenu.addEventListener("click", (e) => {
                const item = e.target.closest(".quick-cmd-item");
                if (item) {
                    const cmd = item.dataset.cmd;
                    const input = $("terminalInput");
                    if (input) {
                        input.value = cmd + " ";
                        input.focus();
                    }
                    quickCmdMenu.style.display = "none";
                }
            });
            document.addEventListener("click", () => { quickCmdMenu.style.display = "none"; });
        }

        // ---- 商店 / 文件管理 / 管理后台 事件绑定 ----
        // 文件上传表单显示/隐藏
        const showUploadBtn = $("showUploadBtn");
        if (showUploadBtn) showUploadBtn.addEventListener("click", toggleUploadForm);
        // 确认上传
        const confirmUploadBtn = $("confirmUploadBtn");
        if (confirmUploadBtn) confirmUploadBtn.addEventListener("click", handleShopFileUpload);
        // 订单搜索
        const searchOrderBtn = $("searchOrderBtn");
        if (searchOrderBtn) searchOrderBtn.addEventListener("click", searchOrders);
        const orderSearchInput = $("orderSearchInput");
        if (orderSearchInput) orderSearchInput.addEventListener("keypress", (e) => {
            if (e.key === "Enter") searchOrders();
        });
        // 刷新审核列表
        const refreshReviewBtn = $("refreshReviewBtn");
        if (refreshReviewBtn) refreshReviewBtn.addEventListener("click", loadReviewFiles);
        // 设置用户余额
        const setBalanceBtn = $("setBalanceBtn");
        if (setBalanceBtn) setBalanceBtn.addEventListener("click", setUserBalance);

        // ---- 欢迎时间定时刷新 ----
        setInterval(updateWelcomeTime, 1000);
    }

    /* ======================================================================
       20b. NV1 Key 管理 & 公告功能
       ====================================================================== */

    /** 设置 NV1 SAuth Key (管理员) */
    async function handleNV1SetKey() {
        const key = $("nv1KeyInput")?.value.trim();
        const apiToken = $("nv1ApiTokenInput")?.value.trim();
        const expiresInDays = parseInt($("nv1ExpiresInput")?.value || "7");

        if (!key) {
            toastError("请输入 SAuth Key");
            return;
        }

        try {
            const res = await api("/system/nv1/config", {
                method: "POST",
                body: { key, api_token: apiToken, expires_in_days: expiresInDays },
            });
            if (res.success) {
                toastSuccess("Key 设置成功");
                $("nv1KeyInput").value = "";
                $("nv1ApiTokenInput").value = "";
                await loadNV1Status();
            } else {
                toastError("设置失败: " + (res.detail || res.message || "未知错误"));
            }
        } catch (e) {
            toastError("设置失败: " + e.message);
        }
    }

    /** 设置 NovaBuilder 凭据, 启用自动刷新 (管理员) */
    async function handleNV1SetNovaBuilder() {
        const username = $("nv1NbUsername")?.value.trim();
        const password = $("nv1NbPassword")?.value.trim();
        const apiKey = $("nv1NbApiKey")?.value.trim();

        if (!username || !password) {
            toastError("请输入 NovaBuilder 用户名和密码");
            return;
        }

        try {
            const res = await api("/system/nv1/novabuilder-credentials", {
                method: "POST",
                body: { username, password, api_key: apiKey },
            });
            if (res.success) {
                toastSuccess("NovaBuilder 自动刷新已启用");
                await loadNV1Status();
            } else {
                toastError("设置失败: " + (res.detail || res.message || "未知错误"));
            }
        } catch (e) {
            toastError("设置失败: " + e.message);
        }
    }

    /** 格式化时间 */
    function fmtTime(ts) {
        if (!ts) return "-";
        const d = new Date(ts * 1000);
        return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')} ${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}`;
    }

    /** 判断当前用户是否为管理员 */
    function isAdmin() {
        const role = state.currentUser ? state.currentUser.role : "user";
        return role === "admin" || role === "superadmin";
    }

    /** 加载公告列表 */
    async function loadAnnouncements() {
        const container = $("annList");
        if (!container) return;
        container.innerHTML = `<div class="empty-state"><i class="fas fa-bullhorn"></i><p style="font-size:13px;">加载中...</p></div>`;
        try {
            const res = await api("/announcements");
            if (res.success && res.data) {
                if (res.data.length === 0) {
                    container.innerHTML = `<div class="empty-state"><i class="fas fa-bullhorn"></i><h3>暂无公告</h3><p style="font-size:13px;">${isAdmin() ? "点击右上角发布按钮创建公告" : "目前没有公告"}</p></div>`;
                    return;
                }
                // 置顶公告排到最前 (保留后端返回顺序作为次要排序)
                const sorted = [...res.data].sort((a, b) => {
                    const ap = isAnnPinned(a) ? 1 : 0;
                    const bp = isAnnPinned(b) ? 1 : 0;
                    return bp - ap;
                });
                container.innerHTML = sorted.map(ann => renderAnnouncementCard(ann)).join("");
                // 绑定事件
                sorted.forEach(ann => {
                    const likeBtn = document.querySelector(`[data-ann-like="${ann.announcement_id}"]`);
                    const dislikeBtn = document.querySelector(`[data-ann-dislike="${ann.announcement_id}"]`);
                    if (likeBtn) likeBtn.addEventListener("click", () => toggleLike(ann.announcement_id));
                    if (dislikeBtn) dislikeBtn.addEventListener("click", () => toggleDislike(ann.announcement_id));
                    const delBtn = document.querySelector(`[data-ann-delete="${ann.announcement_id}"]`);
                    if (delBtn) delBtn.addEventListener("click", () => deleteAnnouncement(ann.announcement_id));
                    // 置顶/取消置顶按钮 (仅管理员)
                    const pinBtn = document.querySelector(`[data-ann-pin="${ann.announcement_id}"]`);
                    if (pinBtn) pinBtn.addEventListener("click", () => togglePinAnnouncement(ann.announcement_id));
                    const commentToggle = document.querySelector(`[data-ann-comments-toggle="${ann.announcement_id}"]`);
                    if (commentToggle) commentToggle.addEventListener("click", () => toggleComments(ann.announcement_id));
                    const commentInput = document.querySelector(`[data-ann-comment-input="${ann.announcement_id}"]`);
                    const commentBtn = document.querySelector(`[data-ann-comment-btn="${ann.announcement_id}"]`);
                    if (commentBtn) commentBtn.addEventListener("click", () => {
                        const val = commentInput?.value.trim();
                        if (val) addComment(ann.announcement_id, val);
                    });
                    if (commentInput) commentInput.addEventListener("keydown", (e) => {
                        if (e.key === "Enter") {
                            const val = commentInput.value.trim();
                            if (val) addComment(ann.announcement_id, val);
                        }
                    });
                });
            }
        } catch (e) {
            container.innerHTML = `<div class="empty-state"><i class="fas fa-exclamation-circle" style="color:var(--color-danger);"></i><p style="font-size:13px;">加载失败: ${e.message}</p></div>`;
        }
    }

    /** 判断公告是否已置顶 (兼容 is_pinned / pinned 两种字段) */
    function isAnnPinned(ann) {
        return !!(ann && (ann.is_pinned || ann.pinned));
    }

    /** 渲染单个公告卡片 */
    function renderAnnouncementCard(ann) {
        const liked = ann.liked || false;
        const disliked = ann.disliked || false;
        const likeCount = ann.like_count || 0;
        const dislikeCount = ann.dislike_count || 0;
        const canManage = isAdmin();
        const canDelete = canManage;
        const pinned = isAnnPinned(ann);

        return `
        <div class="ann-card${pinned ? ' pinned' : ''}">
            <div class="ann-header">
                <div>
                    <div class="ann-title">${pinned ? `<span class="ann-pin-badge">📌 置顶</span>` : ""}${escapeHtml(ann.title)}</div>
                    <div class="ann-meta">发布者: ${escapeHtml(ann.created_by_username)} &middot; ${fmtTime(ann.created_at)}</div>
                </div>
                <div style="display:flex;align-items:center;gap:12px;flex-shrink:0;">
                    ${canManage ? `<span class="ann-pin-btn${pinned ? ' active' : ''}" data-ann-pin="${ann.announcement_id}" title="${pinned ? '取消置顶' : '置顶'}"><i class="fas fa-thumbtack"></i> ${pinned ? '取消置顶' : '置顶'}</span>` : ""}
                    ${canDelete ? `<span class="ann-delete-btn" data-ann-delete="${ann.announcement_id}"><i class="fas fa-trash"></i> 删除</span>` : ""}
                </div>
            </div>
            <div class="ann-content">${escapeHtml(ann.content)}</div>
            <div class="ann-actions">
                <span class="ann-reaction ${liked ? 'active-like' : ''}" data-ann-like="${ann.announcement_id}">
                    <i class="fas fa-thumbs-up"></i> <span>${likeCount}</span>
                </span>
                <span class="ann-reaction ${disliked ? 'active-dislike' : ''}" data-ann-dislike="${ann.announcement_id}">
                    <i class="fas fa-thumbs-down"></i> <span>${dislikeCount}</span>
                </span>
            </div>
            <div class="ann-comments" id="ann-comments-${ann.announcement_id}" style="display:none;">
                <div id="ann-comments-list-${ann.announcement_id}"></div>
                <div class="ann-comment-input">
                    <input type="text" class="form-input" placeholder="写评论..." data-ann-comment-input="${ann.announcement_id}">
                    <button class="btn btn-primary btn-sm" data-ann-comment-btn="${ann.announcement_id}"><i class="fas fa-paper-plane"></i></button>
                </div>
            </div>
            <div style="margin-top:8px;">
                <span class="ann-comments-toggle" data-ann-comments-toggle="${ann.announcement_id}">查看评论</span>
            </div>
        </div>`;
    }

    /** 切换评论显示 */
    async function toggleComments(annId) {
        const container = $(`ann-comments-${annId}`);
        if (!container) return;
        if (container.style.display === "none") {
            container.style.display = "block";
            await loadComments(annId);
            const toggle = document.querySelector(`[data-ann-comments-toggle="${annId}"]`);
            if (toggle) toggle.textContent = "收起评论";
        } else {
            container.style.display = "none";
            const toggle = document.querySelector(`[data-ann-comments-toggle="${annId}"]`);
            if (toggle) toggle.textContent = "查看评论";
        }
    }

    /** 加载评论 */
    async function loadComments(annId) {
        const listEl = $(`ann-comments-list-${annId}`);
        if (!listEl) return;
        try {
            const res = await api(`/announcements/${annId}/comments`);
            if (res.success && res.data) {
                const canDeleteAny = isAdmin();
                listEl.innerHTML = res.data.map(c => `
                    <div class="ann-comment">
                        <div class="ann-comment-header">
                            <span class="ann-comment-user">${escapeHtml(c.username)}</span>
                            <span>
                                <span class="ann-comment-time">${fmtTime(c.created_at)}</span>
                                ${(canDeleteAny || c.user_id === state.currentUser?.user_id) ? `<span class="ann-comment-delete" onclick="deleteComment('${annId}','${c.comment_id}')">删除</span>` : ""}
                            </span>
                        </div>
                        <div class="ann-comment-content">${escapeHtml(c.content)}</div>
                    </div>
                `).join("") || `<p style="font-size:12px;color:var(--text-tertiary);padding:8px 0;">暂无评论</p>`;
            }
        } catch (e) { /* ignore */ }
    }

    /** 创建公告 */
    async function handleCreateAnnouncement() {
        const title = $("annTitle")?.value.trim();
        const content = $("annContent")?.value.trim();
        if (!title || !content) {
            toastError("请填写标题和内容");
            return;
        }
        try {
            const res = await api("/announcements", {
                method: "POST",
                body: { title, content },
            });
            if (res.success) {
                toastSuccess("公告发布成功");
                closeModal("modalCreateAnnouncement");
                $("annTitle").value = "";
                $("annContent").value = "";
                await loadAnnouncements();
            } else {
                toastError("发布失败: " + (res.detail || res.message || "未知错误"));
            }
        } catch (e) {
            toastError("发布失败: " + e.message);
        }
    }

    /** 删除公告 */
    async function deleteAnnouncement(annId) {
        if (!confirm("确定要删除这条公告吗？所有评论和点赞也将被删除。")) return;
        try {
            const res = await api(`/announcements/${annId}`, { method: "DELETE" });
            if (res.success) {
                toastSuccess("公告已删除");
                await loadAnnouncements();
            } else {
                toastError("删除失败: " + (res.detail || res.message || "未知错误"));
            }
        } catch (e) {
            toastError("删除失败: " + e.message);
        }
    }

    /** 置顶/取消置顶公告 (管理员, 切换式) */
    async function togglePinAnnouncement(annId) {
        try {
            const res = await api(`/announcements/${annId}/pin`, { method: "PUT" });
            if (res.success) {
                toastSuccess(res.message || "操作成功");
                await loadAnnouncements();
            } else {
                toastError("操作失败: " + (res.detail || res.message || "未知错误"));
            }
        } catch (e) {
            toastError("操作失败: " + e.message);
        }
    }

    /** 添加评论 */
    async function addComment(annId, content) {
        try {
            const res = await api(`/announcements/${annId}/comments`, {
                method: "POST",
                body: { content },
            });
            if (res.success) {
                const input = document.querySelector(`[data-ann-comment-input="${annId}"]`);
                if (input) input.value = "";
                await loadComments(annId);
            } else {
                toastError("评论失败: " + (res.detail || res.message || "未知错误"));
            }
        } catch (e) {
            toastError("评论失败: " + e.message);
        }
    }

    /** 删除评论 */
    async function deleteComment(annId, commentId) {
        try {
            const res = await api(`/announcements/${annId}/comments/${commentId}`, { method: "DELETE" });
            if (res.success) {
                toastSuccess("评论已删除");
                await loadComments(annId);
            } else {
                toastError("删除失败: " + (res.detail || res.message || "未知错误"));
            }
        } catch (e) {
            toastError("删除失败: " + e.message);
        }
    }

    /** 点赞 */
    async function toggleLike(annId) {
        try {
            const res = await api(`/announcements/${annId}/like`, { method: "POST" });
            if (res.success) {
                await loadAnnouncements();
                // 展开评论区如果之前展开了
                const container = $(`ann-comments-${annId}`);
                if (container && container.style.display !== "none") {
                    await loadComments(annId);
                }
            }
        } catch (e) { /* ignore */ }
    }

    /** 点差评 */
    async function toggleDislike(annId) {
        try {
            const res = await api(`/announcements/${annId}/dislike`, { method: "POST" });
            if (res.success) {
                await loadAnnouncements();
                const container = $(`ann-comments-${annId}`);
                if (container && container.style.display !== "none") {
                    await loadComments(annId);
                }
            }
        } catch (e) { /* ignore */ }
    }

    /** 加载公告活动日志 (管理员) */
    async function loadAnnouncementLogs() {
        const container = $("annLogsList");
        if (!container) return;
        container.innerHTML = `<div class="empty-state"><i class="fas fa-list"></i><p style="font-size:13px;">加载中...</p></div>`;
        try {
            const res = await api("/announcements/logs");
            if (res.success && res.data) {
                if (res.data.length === 0) {
                    container.innerHTML = `<div class="empty-state"><i class="fas fa-list"></i><h3>暂无记录</h3><p style="font-size:13px;">用户点赞、差评、评论等记录将显示在这里</p></div>`;
                    return;
                }
                container.innerHTML = res.data.map(log => {
                    let iconClass = "", iconHtml = "", actionText = "";
                    if (log.type === "like") {
                        iconClass = "like"; iconHtml = '<i class="fas fa-thumbs-up"></i>';
                        actionText = `赞了公告 <strong>${escapeHtml(log.announcement_title || log.announcement_id)}</strong>`;
                    } else if (log.type === "dislike") {
                        iconClass = "dislike"; iconHtml = '<i class="fas fa-thumbs-down"></i>';
                        actionText = `差评了公告 <strong>${escapeHtml(log.announcement_title || log.announcement_id)}</strong>`;
                    } else if (log.type === "comment") {
                        iconClass = "comment"; iconHtml = '<i class="fas fa-comment"></i>';
                        actionText = `评论了公告 <strong>${escapeHtml(log.announcement_title || log.announcement_id)}</strong>: "${escapeHtml(log.content || '')}"`;
                    } else if (log.type === "create") {
                        iconClass = "create"; iconHtml = '<i class="fas fa-plus"></i>';
                        actionText = `发布了公告 <strong>${escapeHtml(log.announcement_title || log.announcement_id)}</strong>`;
                    }
                    return `
                    <div class="ann-log-item">
                        <div class="ann-log-icon ${iconClass}">${iconHtml}</div>
                        <div style="flex:1;">
                            <span style="font-weight:600;">${escapeHtml(log.username)}</span>
                            <span style="color:var(--text-secondary);"> ${actionText}</span>
                        </div>
                        <span style="font-size:11px;color:var(--text-tertiary);">${fmtTime(log.created_at)}</span>
                    </div>`;
                }).join("");
            }
        } catch (e) {
            container.innerHTML = `<div class="empty-state"><i class="fas fa-exclamation-circle" style="color:var(--color-danger);"></i><p style="font-size:13px;">加载失败: ${e.message}</p></div>`;
        }
    }

    /* ======================================================================
       21. 启动
       ====================================================================== */

    // 暴露部分函数供动态生成的内联按钮 (如加载失败重试) 调用
    window.__pockettermReloadPanels = loadPanels;

    document.addEventListener("DOMContentLoaded", init);

})();
