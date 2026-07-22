package main

// PocketTerm 接入点 (pocketterm_ap)
//
// 本程序是一个能真正进入 Minecraft 网易版(NetEase)租赁服的接入点。
// 它复用 FastBuilder 的 gophertunnel(MCBE 协议库)与 fbauth(网易认证库)实现，
// 并支持两种认证模式：
//
//   一、预认证模式(推荐，mode=="pre_auth")：
//      Python 端先通过 HTTP 连接 fatalder 认证服务器(fatalder.yeah114.top)完成认证，
//      获取 chainInfo 与 server_address，再通过 stdin 传给本二进制。本二进制跳过
//      fbauth 流程，仅负责 RakNet 连接与 MCPE 登录。配置示例：
//        {
//          "mode": "pre_auth",
//          "chain_info": "eyJ...(JWT chain data)...",
//          "server_address": "123.45.67.89:19132",
//          "bot_name": "PT_123456",
//          "device_model": "Xiaomi 13"
//        }
//
//   二、传统 fbauth 模式(向后兼容)：
//      通过 WebSocket 连接 FastBuilder 认证服务器(api.fastbuilder.pro)完成认证。
//      1. 从 stdin 读取一行 JSON 配置(含服务器编号、密码、认证服务器、FBToken、
//         账号密码、机器人名称、设备型号等)。
//      2. 通过 WebSocket 连接 FastBuilder 认证服务器并完成 ECDH 加密握手。
//      3. 若配置中提供了 username+password 而无 fb_token，先调用 phoenix::get-token
//         换取 FBToken；若已提供 fb_token 则直接使用。
//      4. 调用 phoenix::login 获取进入游戏服务器所需的 chainInfo 与服务器地址。
//
//   两种模式在拿到 chainInfo 与服务器地址后，共用以下流程：
//   5. 通过 RakNet 连接游戏服务器，发送 Login 数据包完成 MCBE 登录握手。
//   6. 等待 PlayStatus(LoginSuccess -> 资源包 -> StartGame -> PlayerSpawn) 完成进服。
//   7. 进入主循环：从 stdin 读取命令/聊天/移动等指令，向 stdout 输出聊天/事件/日志/错误。
//
// 通信协议(每行一个 JSON)：
//   入站(stdin，首行为配置，后续为指令):
//     {"type":"command","data":{"command":"/say hello"}}
//     {"type":"chat","data":{"message":"hello"}}
//     {"type":"move","data":{"x":256.0,"y":64.0,"z":128.0,"pitch":0.0,"yaw":0.0}}
//     {"type":"disconnect"}
//   出站(stdout):
//     {"type":"log","level":"info","message":"已进入游戏"}
//     {"type":"event","name":"spawn","data":{"bot_name":"PT_123456"}}
//     {"type":"chat","data":{"sender":"Steve","message":"hi"}}
//     {"type":"error","message":"认证失败","detail":"..."}

import (
	"bufio"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"sync"

	"pocketterm/fbauth"
	"pocketterm/minecraft"
	"pocketterm/minecraft/protocol"
	"pocketterm/minecraft/protocol/packet"

	"github.com/go-gl/mathgl/mgl32"
	"github.com/google/uuid"
)

// ===== 配置与消息结构定义 =====

// Config 是从 stdin 首行读取的启动配置。
type Config struct {
	// ===== 预认证模式字段 =====
	// 当 Python 端已通过 HTTP 连接 fatalder 认证服务器完成认证时，会直接把
	// chainInfo 与游戏服务器地址传给本二进制，此时跳过 fbauth 流程。
	Mode          string `json:"mode"`           // 模式标识，"pre_auth" 表示预认证模式
	ChainInfo     string `json:"chain_info"`     // 预认证模式: 由 Python 端获取的 JWT chain 数据
	ServerAddress string `json:"server_address"` // 预认证模式: 游戏服务器地址，例如 "123.45.67.89:19132"

	// ===== 传统 fbauth 模式字段 =====
	ServerCode     string `json:"server_code"`     // 网易租赁服编号，例如 "1895088"
	ServerPassword string `json:"server_password"` // 租赁服密码，可为空
	AuthServer     string `json:"auth_server"`     // 认证服务器地址，默认 wss://api.fastbuilder.pro:2053/
	FBToken        string `json:"fb_token"`        // 已有的 FastBuilder Token，若为空则需要账号密码换取
	Username       string `json:"username"`        // FastBuilder 用户中心账号
	Password       string `json:"password"`        // FastBuilder 用户中心密码
	BotName        string `json:"bot_name"`        // 机器人在游戏内显示的名称(标识用)
	DeviceModel    string `json:"device_model"`    // 伪装的设备型号，例如 "Xiaomi 13"

	// ===== C-1 修复: 设备指纹持久化字段 =====
	// 之前这些字段为空时, Go 端会用 uuid.New() / rand.Int63() 每次随机生成,
	// 导致网易每次看到的都是"新设备", 4 次后判定异常 → 封号。
	// 现在由 Python 端从 device_fingerprint uqholder 注入, 保证跨会话一致。
	DeviceID         string `json:"device_id"`          // Minecraft DeviceId (UUID 格式), 每账号固定
	ClientRandomID   int64  `json:"client_random_id"`   // ClientRandomId, 每账号固定
	PlayerUUID       string `json:"player_uuid"`         // 玩家 UUID, 每账号固定
	DeviceOS         int32  `json:"device_os"`           // 设备 OS: 1=Android, 2=iOS, 3=Win10, 等
	GameVersion      string `json:"game_version"`       // 游戏版本, 例如 "1.20.81"
	LanguageCode     string `json:"language_code"`      // 语言代码, 例如 "zh_CN"
	CurrentInputMode int32  `json:"current_input_mode"` // 当前输入模式: 0=键盘, 1=触屏, 2=手柄
	DefaultInputMode int32  `json:"default_input_mode"` // 默认输入模式
	UIProfile        int32  `json:"ui_profile"`         // UI 配置: 0=经典, 1=口袋
}

// isPreAuth 判断当前配置是否为预认证模式。
// 当显式声明 mode=="pre_auth"，或同时提供了 chain_info 与 server_address 时即为预认证模式。
func (c *Config) isPreAuth() bool {
	return c.Mode == "pre_auth" || (c.ChainInfo != "" && c.ServerAddress != "")
}

// PreAuthenticator 实现 minecraft.Authenticator 接口，用于预认证模式。
// 它不连接任何认证服务器，直接返回由 Python 端预先获取的 chainInfo 与游戏服务器地址。
// Dialer.DialContext 会调用 GetAccess 传入客户端公钥，本实现忽略该公钥，
// 直接返回已有的 server_address 与 chain_info，从而跳过 FastBuilder 认证流程。
type PreAuthenticator struct {
	ChainInfo     string // 预先获取的 JWT chain 数据
	ServerAddress string // 游戏服务器地址(host:port)
}

// GetAccess 实现 minecraft.Authenticator 接口。
// publicKey 参数在本模式下被忽略(加密握手所需的 ECDSA 密钥对由 Dialer 内部生成)。
func (pa *PreAuthenticator) GetAccess(publicKey []byte) (address string, chainInfo string, err error) {
	if pa.ChainInfo == "" || pa.ServerAddress == "" {
		return "", "", errors.New("预认证数据不完整: 缺少 chain_info 或 server_address")
	}
	return pa.ServerAddress, pa.ChainInfo, nil
}

// IncomingMessage 是从 stdin 读取的指令消息。
type IncomingMessage struct {
	Type string          `json:"type"` // command / chat / move / disconnect
	Data json.RawMessage `json:"data"` // 具体载荷，按 Type 解析
}

// CommandData 是 type=="command" 时的载荷。
type CommandData struct {
	Command string `json:"command"` // 要执行的命令，例如 "/say hello"
}

// ChatData 是 type=="chat" 时的载荷。
type ChatData struct {
	Message string `json:"message"` // 要发送的聊天消息
}

// MoveData 是 type=="move" 时的载荷。
type MoveData struct {
	X     float32 `json:"x"`
	Y     float32 `json:"y"`
	Z     float32 `json:"z"`
	Pitch float32 `json:"pitch"`
	Yaw   float32 `json:"yaw"`
}

// OutgoingMessage 是输出到 stdout 的消息。所有出站消息都通过它序列化。
type OutgoingMessage struct {
	Type   string      `json:"type"`             // log / event / chat / error / command_output
	Level  string      `json:"level,omitempty"`   // log 级别: info/warn/error
	Name   string      `json:"name,omitempty"`    // event 名称
	Data   interface{} `json:"data,omitempty"`    // 事件/聊天载荷
	Message string     `json:"message,omitempty"` // log/error 的消息文本
	Detail string      `json:"detail,omitempty"`  // error 的附加详情
}

// ===== 全局输出与生命周期管理 =====

var (
	outMu  sync.Mutex // 保护 stdout 的并发写
	done   = make(chan struct{})
	doneMu sync.Once
)

// emit 向 stdout 输出一条 JSON 消息(单行)。所有输出都经过此函数以保证线程安全与格式统一。
func emit(msg OutgoingMessage) {
	outMu.Lock()
	defer outMu.Unlock()
	b, err := json.Marshal(msg)
	if err != nil {
		// 序列化失败时降级输出一条 log
		b, _ = json.Marshal(OutgoingMessage{Type: "log", Level: "error", Message: "序列化输出消息失败: " + err.Error()})
	}
	fmt.Println(string(b))
}

// emitLog 输出一条日志。
func emitLog(level, message string) {
	emit(OutgoingMessage{Type: "log", Level: level, Message: message})
}

// emitError 输出一条错误。
func emitError(message, detail string) {
	emit(OutgoingMessage{Type: "error", Message: message, Detail: detail})
}

// finish 结束整个接入点生命周期。多次调用安全。
func finish() {
	doneMu.Do(func() {
		close(done)
	})
}

func main() {
	// 捕获致命 panic，转成 error 输出后退出
	defer func() {
		if r := recover(); r != nil {
			emitError("接入点发生致命错误", fmt.Sprintf("%v", r))
		}
		os.Exit(0)
	}()

	// 第一步：从 stdin 读取配置
	cfg, err := readConfig()
	if err != nil {
		emitError("读取配置失败", err.Error())
		return
	}

	// 根据是否为预认证模式分流。
	// 预认证模式: Python 端已通过 HTTP 连接 fatalder 认证服务器完成认证，
	//   并把 chainInfo 与 server_address 传给本二进制，跳过 fbauth 流程。
	// 传统模式: 通过 WebSocket 连接 FastBuilder 认证服务器完成认证(向后兼容)。
	if cfg.isPreAuth() {
		runPreAuth(cfg)
		return
	}

	runFBAuth(cfg)
}

// runPreAuth 以预认证模式运行接入点。
// 该模式不连接任何认证服务器，直接使用 Python 端预先获取的 chainInfo 与 server_address，
// 通过 RakNet 连接游戏服务器并发送 Login 数据包完成 MCBE 登录握手。
func runPreAuth(cfg *Config) {
	emitLog("info", fmt.Sprintf("预认证模式: 服务器地址=%s, 机器人名称=%s, 设备型号=%s",
		cfg.ServerAddress, cfg.BotName, orDefault(cfg.DeviceModel, "PocketTerm")))

	dialer := minecraft.Dialer{
		// 使用预认证 Authenticator：直接返回 Python 端提供的 server_address 与 chain_info，
		// 不连接任何认证服务器。Dialer 内部会生成 ECDSA 密钥对用于加密握手。
		Authenticator: &PreAuthenticator{
			ChainInfo:     cfg.ChainInfo,
			ServerAddress: cfg.ServerAddress,
		},
	}
	applyDisplayData(&dialer, cfg)

	emitLog("info", "正在通过 RakNet 连接游戏服务器(预认证模式)...")
	runDialer(cfg, dialer)
}

// runFBAuth 以传统 fbauth 模式运行接入点(向后兼容)。
// 通过 WebSocket 连接 FastBuilder 认证服务器完成加密握手与登录，获取 chainInfo 与服务器地址。
func runFBAuth(cfg *Config) {
	emitLog("info", fmt.Sprintf("已读取配置: 服务器编号=%s, 认证服务器=%s, 机器人名称=%s",
		cfg.ServerCode, cfg.AuthServer, cfg.BotName))

	// 连接认证服务器并完成加密握手
	emitLog("info", "正在连接认证服务器...")
	authClient, err := fbauth.NewClient(cfg.AuthServer)
	if err != nil {
		emitError("连接认证服务器失败", err.Error())
		return
	}
	defer authClient.Close()
	emitLog("info", "已连接认证服务器，加密握手完成")

	// 获取 FBToken(若有账号密码且无现成 token)
	fbToken := cfg.FBToken
	if fbToken == "" && cfg.Username != "" && cfg.Password != "" {
		emitLog("info", "未提供 FBToken，正在使用账号密码换取 Token...")
		token, err := authClient.GetToken(cfg.Username, cfg.Password)
		if err != nil {
			emitError("获取 FBToken 失败", err.Error())
			return
		}
		fbToken = token
		emitLog("info", "已成功获取 FBToken")
		// 把 token 回传给宿主，便于下次复用
		emit(OutgoingMessage{Type: "event", Name: "fb_token", Data: map[string]string{"fb_token": fbToken}})
	}

	if fbToken == "" {
		emitError("缺少 FBToken", "既没有提供 fb_token，也没有提供有效的 username+password")
		return
	}

	// 通过认证服务器登录游戏服务器(内部会发送 phoenix::login 获取 chainInfo 与地址)
	emitLog("info", "正在通过认证服务器登录游戏服务器...")
	dialer := minecraft.Dialer{
		Authenticator: fbauth.NewAccessWrapper(authClient, cfg.ServerCode, cfg.ServerPassword, fbToken),
	}
	applyDisplayData(&dialer, cfg)

	runDialer(cfg, dialer)
}

// applyDisplayData 设置设备型号、机器人名称及全部设备指纹到 Dialer 上。
// 两种模式共用。C-1 修复: 之前只设 DeviceModel + BotName,
// 其余字段 (DeviceId/ClientRandomId/UUID 等) 全部随机生成导致封号。
func applyDisplayData(d *minecraft.Dialer, cfg *Config) {
	// 设备型号 (DisplayData)
	if cfg.DeviceModel != "" {
		d.ClientData.DeviceModel = cfg.DeviceModel
	} else {
		d.ClientData.DeviceModel = "PocketTerm"
	}

	// 机器人名称 (IdentityData + ClientData)
	if cfg.BotName != "" {
		d.IdentityData.DisplayName = cfg.BotName
		d.ClientData.ThirdPartyName = cfg.BotName
	}

	// C-1 修复: 注入持久化设备指纹 (每账号固定, 跨会话一致)
	// 这些字段若为空, gophertunnel 会用 uuid.New() / rand.Int63() 随机生成,
	// 导致网易每次看到的都是"新设备" → 触发反作弊 → 封号
	if cfg.DeviceID != "" {
		d.ClientData.DeviceID = cfg.DeviceID
	}
	if cfg.ClientRandomID != 0 {
		d.ClientData.ClientRandomID = cfg.ClientRandomID
	}
	if cfg.PlayerUUID != "" {
		// IdentityData.Identity 是玩家 UUID, 必须与 DeviceID 一致
		d.IdentityData.Identity = cfg.PlayerUUID
	}
	if cfg.DeviceOS != 0 {
		d.ClientData.DeviceOS = protocol.DeviceOS(cfg.DeviceOS)
	}
	if cfg.GameVersion != "" {
		d.ClientData.GameVersion = cfg.GameVersion
	}
	if cfg.LanguageCode != "" {
		d.ClientData.LanguageCode = cfg.LanguageCode
	}
	if cfg.CurrentInputMode != 0 {
		d.ClientData.CurrentInputMode = int(cfg.CurrentInputMode)
	}
	if cfg.DefaultInputMode != 0 {
		d.ClientData.DefaultInputMode = int(cfg.DefaultInputMode)
	}
	if cfg.UIProfile != 0 {
		d.ClientData.UIProfile = int(cfg.UIProfile)
	}
}

// runDialer 使用已配置好的 Dialer 完成: RakNet 连接 -> MCPE 登录 -> 等待进服 -> 主循环。
// 两种认证模式(fbauth / 预认证)在拿到有效的 Dialer 后共用本函数。
func runDialer(cfg *Config, dialer minecraft.Dialer) {
	conn, err := dialer.Dial("raknet")
	if err != nil {
		emitError("连接游戏服务器失败", err.Error())
		return
	}
	defer conn.Close()
	emitLog("info", fmt.Sprintf("已连接游戏服务器，登录成功: 玩家=%s, 运行时ID=%d",
		conn.IdentityData().DisplayName, conn.GameData().EntityRuntimeID))

	// 关闭客户端缓存(与 FastBuilder 行为一致)
	if err := conn.WritePacket(&packet.ClientCacheStatus{Enabled: false}); err != nil {
		emitError("发送 ClientCacheStatus 失败", err.Error())
		return
	}

	// 等待进入游戏(完成 spawn 序列)
	emitLog("info", "正在等待进入游戏(PlayStatus.PlayerSpawn)...")
	if err := conn.DoSpawn(); err != nil {
		emitError("进入游戏失败", err.Error())
		return
	}
	botName := conn.IdentityData().DisplayName
	if botName == "" {
		botName = cfg.BotName
	}
	emitLog("info", "已进入游戏")
	emit(OutgoingMessage{
		Type: "event",
		Name: "spawn",
		Data: map[string]interface{}{
			"bot_name":          botName,
			"entity_runtime_id": conn.GameData().EntityRuntimeID,
			"entity_unique_id":  conn.GameData().EntityUniqueID,
			"world_name":        conn.GameData().WorldName,
			"position": map[string]float32{
				"x": conn.GameData().PlayerPosition.X(),
				"y": conn.GameData().PlayerPosition.Y(),
				"z": conn.GameData().PlayerPosition.Z(),
			},
		},
	})

	// 启动主循环:
	//   - 一个 goroutine 持续读取游戏数据包并上报事件
	//   - 主 goroutine 持续从 stdin 读取指令并下发
	go packetLoop(conn)
	stdinLoop(conn)

	<-done
	emitLog("info", "接入点已退出")
}

// orDefault 当 v 为空时返回 defVal，否则返回 v。
func orDefault(v, defVal string) string {
	if v == "" {
		return defVal
	}
	return v
}

// ===== 配置读取 =====

// readConfig 从 stdin 读取第一行非空内容并解析为 Config。
func readConfig() (*Config, error) {
	reader := bufio.NewReader(os.Stdin)
	for {
		line, err := reader.ReadString('\n')
		if len(line) == 0 && err != nil {
			if err == io.EOF {
				return nil, errors.New("stdin 已结束，未读取到配置")
			}
			return nil, fmt.Errorf("读取 stdin 失败: %w", err)
		}
		line = trimLine(line)
		if line == "" {
			if err != nil {
				return nil, errors.New("stdin 已结束，未读取到配置")
			}
			continue
		}
		var cfg Config
		if err := json.Unmarshal([]byte(line), &cfg); err != nil {
			return nil, fmt.Errorf("解析配置 JSON 失败: %w", err)
		}
		if cfg.AuthServer == "" {
			cfg.AuthServer = fbauth.DefaultAuthServer
		}
		// 把带缓冲的 reader 传给后续 stdinLoop 复用，避免丢失后续输入
		stdinReader = reader
		return &cfg, nil
	}
}

// stdinReader 是 readConfig 之后剩下的带缓冲 stdin reader，供 stdinLoop 复用。
var stdinReader io.Reader

// trimLine 去掉行尾的换行符与空白。
func trimLine(s string) string {
	for len(s) > 0 {
		c := s[len(s)-1]
		if c == '\n' || c == '\r' || c == ' ' || c == '\t' {
			s = s[:len(s)-1]
			continue
		}
		break
	}
	return s
}

// ===== 数据包读取循环 =====

// packetLoop 持续从游戏连接读取数据包，并将感兴趣的事件以 JSON 形式输出到 stdout。
func packetLoop(conn *minecraft.Conn) {
	defer finish()
	for {
		select {
		case <-done:
			return
		default:
		}
		pk, err := conn.ReadPacket()
		if err != nil {
			// 连接断开：尝试获取踢出/封禁消息
			msg := conn.DisconnectMessage()
			if msg != "" {
				emitError("已从服务器断开连接", "服务器消息: "+msg)
			} else {
				emitError("已从服务器断开连接", err.Error())
			}
			return
		}
		handlePacket(conn, pk)
	}
}

// handlePacket 处理单个游戏数据包。
func handlePacket(conn *minecraft.Conn, pk packet.Packet) {
	switch p := pk.(type) {
	// ---- 聊天/文本消息 ----
	case *packet.Text:
		switch p.TextType {
		case packet.TextTypeChat, packet.TextTypeWhisper, packet.TextTypeAnnouncement:
			// 聊天消息：上报发送者与内容
			emit(OutgoingMessage{
				Type: "chat",
				Data: map[string]string{
					"sender":  p.SourceName,
					"message": p.Message,
				},
			})
		case packet.TextTypeRaw, packet.TextTypeSystem, packet.TextTypeTip, packet.TextTypePopup, packet.TextTypeJukeboxPopup:
			// 系统消息：作为日志事件上报
			emit(OutgoingMessage{
				Type: "event",
				Name: "system_message",
				Data: map[string]string{
					"subtype": textTypeName(p.TextType),
					"message": p.Message,
				},
			})
		}

	// ---- 命令输出 ----
	case *packet.CommandOutput:
		emit(OutgoingMessage{
			Type: "command_output",
			Data: map[string]interface{}{
				"origin_uuid": p.CommandOrigin.UUID.String(),
				"success":     p.SuccessCount,
				"messages":    commandOutputMessages(p),
			},
		})

	// ---- 玩家列表(加入/离开) ----
	case *packet.PlayerList:
		switch p.ActionType {
		case packet.PlayerListActionAdd:
			for _, entry := range p.Entries {
				emit(OutgoingMessage{
					Type: "event",
					Name: "player_join",
					Data: map[string]interface{}{
						"username": entry.Username,
						"uuid":     entry.UUID.String(),
						"xuid":     entry.XUID,
					},
				})
			}
		case packet.PlayerListActionRemove:
			for _, entry := range p.Entries {
				emit(OutgoingMessage{
					Type: "event",
					Name: "player_leave",
					Data: map[string]interface{}{
						"uuid": entry.UUID.String(),
						"xuid": entry.XUID,
					},
				})
			}
		}

	// ---- 玩家移动(含自己) ----
	case *packet.MovePlayer:
		// 仅上报自己以外的玩家移动，避免噪音过大；自己的位置变化也作为事件上报
		emit(OutgoingMessage{
			Type: "event",
			Name: "move_player",
			Data: map[string]interface{}{
				"entity_runtime_id": p.EntityRuntimeID,
				"position": map[string]float32{
					"x": p.Position.X(),
					"y": p.Position.Y(),
					"z": p.Position.Z(),
				},
				"pitch": p.Pitch,
				"yaw":   p.Yaw,
				"on_ground": p.OnGround,
			},
		})

	// ---- 物品/库存操作 ----
	case *packet.InventoryTransaction:
		emit(OutgoingMessage{
			Type: "event",
			Name: "inventory_transaction",
			Data: map[string]interface{}{
				"legacy_request_id": p.LegacyRequestID,
				"actions_count":     len(p.Actions),
			},
		})

	// ---- 断开连接(踢出/封禁) ----
	// 注意：Conn 内部会拦截 Disconnect 并关闭连接，这里通常不会收到；
	// 但若以原始字节方式收到则上报。断开原因通过 DisconnectMessage() 获取。
	case *packet.Disconnect:
		emitError("被服务器断开连接", p.Message)

	// ---- 玩家加入世界(AddPlayer) ----
	case *packet.AddPlayer:
		emit(OutgoingMessage{
			Type: "event",
			Name: "add_player",
			Data: map[string]interface{}{
				"username":          p.Username,
				"entity_runtime_id": p.EntityRuntimeID,
				"uuid":              p.UUID.String(),
			},
		})
	}
}

// textTypeName 将 TextType 数值转为可读名称。
func textTypeName(t byte) string {
	switch t {
	case packet.TextTypeRaw:
		return "raw"
	case packet.TextTypeChat:
		return "chat"
	case packet.TextTypeTranslation:
		return "translation"
	case packet.TextTypePopup:
		return "popup"
	case packet.TextTypeJukeboxPopup:
		return "jukebox_popup"
	case packet.TextTypeTip:
		return "tip"
	case packet.TextTypeSystem:
		return "system"
	case packet.TextTypeWhisper:
		return "whisper"
	case packet.TextTypeAnnouncement:
		return "announcement"
	default:
		return fmt.Sprintf("unknown(%d)", t)
	}
}

// commandOutputMessages 提取 CommandOutput 中的所有输出消息文本。
func commandOutputMessages(p *packet.CommandOutput) []string {
	out := make([]string, 0, len(p.OutputMessages))
	for _, m := range p.OutputMessages {
		text := m.Message
		if len(m.Parameters) > 0 {
			text = text + " " + joinStrings(m.Parameters, " ")
		}
		out = append(out, text)
	}
	return out
}

// joinStrings 用 sep 拼接字符串切片。
func joinStrings(ss []string, sep string) string {
	out := ""
	for i, s := range ss {
		if i > 0 {
			out += sep
		}
		out += s
	}
	return out
}

// ===== stdin 指令读取循环 =====

// stdinLoop 持续从 stdin 读取指令并下发到游戏服务器。
func stdinLoop(conn *minecraft.Conn) {
	defer finish()
	reader := bufio.NewReader(stdinReader)
	for {
		select {
		case <-done:
			return
		default:
		}
		line, err := reader.ReadString('\n')
		if len(line) == 0 && err != nil {
			if err == io.EOF {
				emitLog("info", "stdin 已关闭，准备断开连接")
			} else {
				emitLog("warn", "读取 stdin 出错: "+err.Error())
			}
			return
		}
		line = trimLine(line)
		if line == "" {
			if err != nil {
				return
			}
			continue
		}
		var msg IncomingMessage
		if err := json.Unmarshal([]byte(line), &msg); err != nil {
			emitError("解析指令 JSON 失败", fmt.Sprintf("行内容=%s, 错误=%v", line, err))
			continue
		}
		if !handleIncoming(conn, &msg) {
			return
		}
	}
}

// handleIncoming 处理一条 stdin 指令。返回 false 表示应当退出(如 disconnect)。
func handleIncoming(conn *minecraft.Conn, msg *IncomingMessage) bool {
	switch msg.Type {
	case "command":
		var data CommandData
		if err := json.Unmarshal(msg.Data, &data); err != nil {
			emitError("解析 command 数据失败", err.Error())
			return true
		}
		if err := sendCommand(conn, data.Command); err != nil {
			emitError("发送命令失败", err.Error())
		}
	case "chat":
		var data ChatData
		if err := json.Unmarshal(msg.Data, &data); err != nil {
			emitError("解析 chat 数据失败", err.Error())
			return true
		}
		if err := sendChat(conn, data.Message); err != nil {
			emitError("发送聊天失败", err.Error())
		}
	case "move":
		var data MoveData
		if err := json.Unmarshal(msg.Data, &data); err != nil {
			emitError("解析 move 数据失败", err.Error())
			return true
		}
		if err := sendMove(conn, data); err != nil {
			emitError("发送移动失败", err.Error())
		}
	case "disconnect":
		emitLog("info", "收到 disconnect 指令，正在断开连接")
		return false
	default:
		emitError("未知的指令类型", msg.Type)
	}
	return true
}

// sendCommand 发送一条游戏命令(CommandRequest 数据包)。
// 命令源使用 CommandOriginPlayer，并带一个随机 UUID 以便匹配 CommandOutput。
func sendCommand(conn *minecraft.Conn, command string) error {
	originUUID, _ := uuid.NewUUID()
	origin := protocol.CommandOrigin{
		Origin:         protocol.CommandOriginPlayer,
		UUID:           originUUID,
		RequestID:      "96045347-a6a3-4114-94c0-1bc4cc561694",
		PlayerUniqueID: 0,
	}
	return conn.WritePacket(&packet.CommandRequest{
		CommandLine:   command,
		CommandOrigin: origin,
		Internal:      false,
		UnLimited:     false,
	})
}

// sendChat 发送一条聊天消息(Text 数据包)。
func sendChat(conn *minecraft.Conn, message string) error {
	idData := conn.IdentityData()
	return conn.WritePacket(&packet.Text{
		TextType:       packet.TextTypeChat,
		NeedsTranslation: false,
		SourceName:     idData.DisplayName,
		Message:        message,
		XUID:           idData.XUID,
		PlayerRuntimeID: fmt.Sprintf("%d", conn.GameData().EntityUniqueID),
	})
}

// sendMove 发送一个 MovePlayer 数据包以移动机器人。
func sendMove(conn *minecraft.Conn, data MoveData) error {
	return conn.WritePacket(&packet.MovePlayer{
		EntityRuntimeID: conn.GameData().EntityRuntimeID,
		Position:        mgl32.Vec3{data.X, data.Y, data.Z},
		Pitch:           data.Pitch,
		Yaw:             data.Yaw,
		HeadYaw:         data.Yaw,
		Mode:            packet.MoveModeTeleport,
		OnGround:        true,
	})
}
