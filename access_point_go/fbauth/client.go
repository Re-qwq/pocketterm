package fbauth

// fbauth 包实现了与 FastBuilder 认证服务器之间的通信。
// 原始实现位于 PhoenixBuilder 的 fastbuilder/cv4/auth 目录，依赖 pterm、
// bridge_fmt、args、environment、i18n 等内部包。此处将其改写为自包含版本，
// 仅依赖标准库与 gorilla/websocket，并把 panic 改为返回 error，便于接入点统一以
// JSON 形式向外输出错误。
//
// 通信流程概要：
//  1. 通过 WebSocket 连接认证服务器(默认 wss://api.fastbuilder.pro:2053/)
//  2. 发送 enable_encryption_v2 + 本端 ECDH 公钥，服务器回送其公钥，双方建立加密会话
//     (若服务器返回 no_encryption，则后续明文通信)
//  3. 所有业务消息经 gzip 压缩后以 BinaryMessage 收发；加密时先加密再压缩，解密反之
//  4. phoenix::get-token  —— 用账号密码换取 FBToken
//  5. phoenix::login      —— 用服务器编号/密码 + 公钥 + FBToken 换取进入租赁服所需的
//                            chainInfo 与游戏服务器地址(以 "|" 分隔)
//
// 注：本文件刻意保持与原始实现一致的字节级行为，以确保与认证服务器兼容。

import (
	"bytes"
	"compress/gzip"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"sync"

	"github.com/gorilla/websocket"
)

// DefaultAuthServer 是 FastBuilder 官方认证服务器的默认地址。
const DefaultAuthServer = "wss://api.fastbuilder.pro:2053/"

// Client 表示一个与认证服务器通信的客户端。
type Client struct {
	privateKey *ecdsa.PrivateKey // 本端 ECDH 私钥(P-384)

	salt   []byte            // 固定盐值，参与加密密钥派生
	client *websocket.Conn   // 底层 WebSocket 连接

	peerNoEncryption bool             // 服务器声明不使用加密
	encryptor        *encryptionSession // 加密会话(为 nil 且 peerNoEncryption=false 表示尚未协商完毕)
	serverResponse   chan map[string]interface{} // 业务响应投递通道

	closed bool     // 连接是否已关闭
	mu     sync.Mutex // 保护 SendMessage 的并发写
}

// NewClient 建立到 authServer 的 WebSocket 连接并完成加密握手。
// 握手完成(收到服务器公钥并初始化加密会话，或服务器同意明文通信)后返回客户端。
func NewClient(authServer string) (*Client, error) {
	if authServer == "" {
		authServer = DefaultAuthServer
	}

	privateKey, err := ecdsa.GenerateKey(elliptic.P384(), rand.Reader)
	if err != nil {
		return nil, fmt.Errorf("生成 ECDH 私钥失败: %w", err)
	}
	salt := []byte("2345678987654321")

	authclient := &Client{
		privateKey:     privateKey,
		salt:           salt,
		serverResponse: make(chan map[string]interface{}, 16),
		closed:         false,
	}

	// 拨号认证服务器
	cl, _, err := websocket.DefaultDialer.Dial(authServer, nil)
	if err != nil {
		return nil, fmt.Errorf("连接认证服务器 %s 失败: %w", authServer, err)
	}
	authclient.client = cl

	encrypted := make(chan struct{})
	go func() {
		defer func() {
			authclient.mu.Lock()
			authclient.closed = true
			authclient.mu.Unlock()
		}()
		for {
			_, msg, err := cl.ReadMessage()
			if err != nil {
				break
			}
			// 先 gzip 解压
			var outbuf bytes.Buffer
			inbuf := bytes.NewBuffer(msg)
			reader, err := gzip.NewReader(inbuf)
			if err != nil {
				// 可能是非 gzip 的明文，直接尝试当作原始消息
				msg = inbuf.Bytes()
			} else {
				io.Copy(&outbuf, reader)
				reader.Close()
				msg = outbuf.Bytes()
			}
			// 若已建立加密会话则解密
			if authclient.encryptor != nil {
				authclient.encryptor.decrypt(msg)
			}
			var message map[string]interface{}
			if err := json.Unmarshal(msg, &message); err != nil {
				// 无法解析的消息直接丢弃，避免阻塞读取循环
				continue
			}
			msgaction, _ := message["action"].(string)
			switch msgaction {
			case "encryption":
				// 服务器回送其 ECDH 公钥，据此建立加密会话
				spub := new(ecdsa.PublicKey)
				keyb64, _ := message["publicKey"].(string)
				keydata, err := base64.StdEncoding.DecodeString(keyb64)
				if err != nil {
					continue
				}
				spp, err := x509.ParsePKIXPublicKey(keydata)
				if err != nil {
					continue
				}
				ek, ok := spp.(*ecdsa.PublicKey)
				if !ok {
					continue
				}
				*spub = *ek
				authclient.encryptor = &encryptionSession{
					serverPrivateKey: privateKey,
					clientPublicKey:  spub,
					salt:             authclient.salt,
				}
				if err := authclient.encryptor.init(); err != nil {
					continue
				}
				close(encrypted)
				continue
			case "no_encryption":
				// 服务器不支持加密，改为明文通信
				authclient.peerNoEncryption = true
				_ = authclient.SendMessage([]byte(`{"action":"accept_no_encryption"}`))
				close(encrypted)
				continue
			case "world_chat":
				// 世界聊天广播消息，本接入点不处理认证服务器的世界聊天，直接忽略
				continue
			}
			// 其它业务消息投递给等待的调用方
			select {
			case authclient.serverResponse <- message:
			default:
				// 没有等待者则丢弃，避免 goroutine 阻塞
			}
		}
	}()

	// 发送本端公钥以发起加密握手
	pubb, err := x509.MarshalPKIXPublicKey(&privateKey.PublicKey)
	if err != nil {
		cl.Close()
		return nil, fmt.Errorf("序列化本端公钥失败: %w", err)
	}
	pubStr := base64.StdEncoding.EncodeToString(pubb)
	var inbuf bytes.Buffer
	wr := gzip.NewWriter(&inbuf)
	wr.Write([]byte(`{"action":"enable_encryption_v2","publicKey":"` + pubStr + `"}`))
	wr.Close()
	if err := cl.WriteMessage(websocket.BinaryMessage, inbuf.Bytes()); err != nil {
		cl.Close()
		return nil, fmt.Errorf("发送加密握手请求失败: %w", err)
	}

	// 等待握手完成
	<-encrypted
	return authclient, nil
}

// CanSendMessage 返回当前是否可以向认证服务器发送消息(已建立加密或明文通道，且未关闭)。
func (client *Client) CanSendMessage() bool {
	client.mu.Lock()
	defer client.mu.Unlock()
	return (client.encryptor != nil || client.peerNoEncryption) && !client.closed
}

// SendMessage 将一条(可能已加密的)业务消息经 gzip 压缩后发送给认证服务器。
func (client *Client) SendMessage(data []byte) error {
	client.mu.Lock()
	defer client.mu.Unlock()

	if client.encryptor == nil && !client.peerNoEncryption {
		return errors.New("加密会话尚未建立，无法发送消息")
	}
	if client.closed {
		return errors.New("连接已关闭，无法发送消息")
	}
	if !client.peerNoEncryption {
		client.encryptor.encrypt(data)
	}
	var inbuf bytes.Buffer
	wr := gzip.NewWriter(&inbuf)
	wr.Write(data)
	wr.Close()
	return client.client.WriteMessage(websocket.BinaryMessage, inbuf.Bytes())
}

// Close 关闭与认证服务器的连接。
func (client *Client) Close() error {
	client.mu.Lock()
	client.closed = true
	client.mu.Unlock()
	if client.client != nil {
		return client.client.Close()
	}
	return nil
}

// AuthRequest 是 phoenix::login 请求的结构。FBToken 字段不带 json tag，
// 原始实现将其序列化为空的 "FBToken": "" ——此处保持一致以兼容服务器。
type AuthRequest struct {
	Action         string `json:"action"`
	ServerCode     string `json:"serverCode"`
	ServerPassword string `json:"serverPassword"`
	Key            string `json:"publicKey"`
	FBToken        string
}

// AuthResult 保存 phoenix::login 成功后从服务器获取的关键信息。
type AuthResult struct {
	ChainInfo        string // 用于登录游戏服务器的 JWT chain
	Username        string // FastBuilder 用户中心用户名
	UID             string // FastBuilder 用户 UID
	PrivateSigningKey string // 服务器签发的私钥(用于证书签名，本接入点暂不使用)
	Prove            string // 证书证明
	CertSigning      bool   // 是否启用证书签名
}

// Auth 发送 phoenix::login 请求，获取进入游戏服务器所需的 chainInfo。
// 返回的 chainInfo 实际为 "chainInfo|address" 形式，由调用方拆分。
// 当 code != 0 时，err 描述失败原因；特殊地 code == -3 表示 FBToken 失效。
func (client *Client) Auth(serverCode string, serverPassword string, key string, fbtoken string) (string, int, error) {
	authreq := &AuthRequest{
		Action:         "phoenix::login",
		ServerCode:     serverCode,
		ServerPassword: serverPassword,
		Key:            key,
		FBToken:        fbtoken,
	}
	msg, err := json.Marshal(authreq)
	if err != nil {
		return "", -1, fmt.Errorf("编码 phoenix::login 请求失败: %w", err)
	}
	if err := client.SendMessage(msg); err != nil {
		return "", -1, err
	}
	resp, ok := <-client.serverResponse
	if !ok {
		return "", -1, errors.New("认证服务器连接已断开，未收到 phoenix::login 响应")
	}
	codeF, _ := resp["code"].(float64)
	code := int(codeF)
	if code != 0 {
		errMsg, _ := resp["message"].(string)
		if trans, ok := resp["translation"].(float64); ok {
			// 原始实现通过 i18n 翻译错误码，这里直接返回原始 message
			errMsg = fmt.Sprintf("(translation=%v) %s", uint16(trans), errMsg)
		}
		if errMsg == "" {
			errMsg = "未知错误"
		}
		return "", code, errors.New(errMsg)
	}
	str, _ := resp["chainInfo"].(string)
	return str, 0, nil
}

// AuthFull 与 Auth 类似，但同时返回完整认证结果(含用户名、UID、签名密钥等)。
func (client *Client) AuthFull(serverCode string, serverPassword string, key string, fbtoken string) (*AuthResult, int, error) {
	authreq := &AuthRequest{
		Action:         "phoenix::login",
		ServerCode:     serverCode,
		ServerPassword: serverPassword,
		Key:            key,
		FBToken:        fbtoken,
	}
	msg, err := json.Marshal(authreq)
	if err != nil {
		return nil, -1, fmt.Errorf("编码 phoenix::login 请求失败: %w", err)
	}
	if err := client.SendMessage(msg); err != nil {
		return nil, -1, err
	}
	resp, ok := <-client.serverResponse
	if !ok {
		return nil, -1, errors.New("认证服务器连接已断开，未收到 phoenix::login 响应")
	}
	codeF, _ := resp["code"].(float64)
	code := int(codeF)
	if code != 0 {
		errMsg, _ := resp["message"].(string)
		if trans, ok := resp["translation"].(float64); ok {
			errMsg = fmt.Sprintf("(translation=%v) %s", uint16(trans), errMsg)
		}
		if errMsg == "" {
			errMsg = "未知错误"
		}
		return nil, code, errors.New(errMsg)
	}
	result := &AuthResult{}
	result.ChainInfo, _ = resp["chainInfo"].(string)
	result.Username, _ = resp["username"].(string)
	result.UID, _ = resp["uid"].(string)
	result.PrivateSigningKey, _ = resp["privateSigningKey"].(string)
	result.Prove, _ = resp["prove"].(string)
	result.CertSigning = result.PrivateSigningKey != "" && result.Prove != ""
	return result, 0, nil
}

// FTokenRequest 是 phoenix::get-token 请求结构。
type FTokenRequest struct {
	Action   string `json:"action"`
	Username string `json:"username"`
	Password string `json:"password"`
}

// GetToken 用 FastBuilder 用户中心的账号密码换取 FBToken。
// 当 username 为空时，password 应为序列化好的 token JSON(见 ProcessTokenDefault)，
// 服务器会返回一个有效的 token 字符串。
func (client *Client) GetToken(username string, password string) (string, error) {
	rspreq := &FTokenRequest{
		Action:   "phoenix::get-token",
		Username: username,
		Password: password,
	}
	msg, err := json.Marshal(rspreq)
	if err != nil {
		return "", fmt.Errorf("编码 phoenix::get-token 请求失败: %w", err)
	}
	if err := client.SendMessage(msg); err != nil {
		return "", err
	}
	resp, ok := <-client.serverResponse
	if !ok {
		return "", errors.New("认证服务器连接已断开，未收到 phoenix::get-token 响应")
	}
	codeF, _ := resp["code"].(float64)
	if int(codeF) != 0 {
		errMsg, _ := resp["message"].(string)
		if errMsg == "" {
			errMsg = "获取 FBToken 失败"
		}
		return "", errors.New(errMsg)
	}
	token, _ := resp["token"].(string)
	if token == "" {
		return "", errors.New("服务器返回的 FBToken 为空")
	}
	return token, nil
}
