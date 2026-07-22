package fbauth

// 本文件实现 AccessWrapper，它作为 minecraft.Dialer 的 Authenticator 接口实现。
// Dialer.Dial 会调用 GetAccess 传入客户端公钥，要求返回游戏服务器地址与 chainInfo。
// AccessWrapper 内部转发给 fbauth.Client 的 phoenix::login 流程完成这一工作。

import (
	"encoding/base64"
	"errors"
	"fmt"
	"strings"
)

// AccessWrapper 封装了进入网易租赁服所需的凭据与认证客户端。
type AccessWrapper struct {
	ServerCode string // 网易租赁服编号
	Password   string // 租赁服密码(可为空)
	Token      string // FastBuilder FBToken
	Client     *Client // 与认证服务器的会话
}

// NewAccessWrapper 创建一个 AccessWrapper。
func NewAccessWrapper(Client *Client, ServerCode, Password, Token string) *AccessWrapper {
	return &AccessWrapper{
		Client:     Client,
		ServerCode: ServerCode,
		Password:   Password,
		Token:      Token,
	}
}

// GetAccess 实现 minecraft.Authenticator 接口。
// 它将客户端公钥编码后发送 phoenix::login，得到形如 "chainInfo|address" 的响应，
// 再拆分为游戏服务器地址与用于登录的 chainInfo。
func (aw *AccessWrapper) GetAccess(publicKey []byte) (address string, chainInfo string, err error) {
	if aw.Client == nil {
		return "", "", errors.New("认证客户端未初始化")
	}
	pubKeyData := base64.StdEncoding.EncodeToString(publicKey)
	chainAddr, code, err := aw.Client.Auth(aw.ServerCode, aw.Password, pubKeyData, aw.Token)
	if err != nil {
		// code == -3 表示 FBToken 失效，调用方可据此提示用户重新登录
		if code == -3 {
			return "", "", fmt.Errorf("FBToken 失效，请重新获取(code=-3): %w", err)
		}
		return "", "", err
	}
	chainAndAddr := strings.Split(chainAddr, "|")
	if chainAndAddr == nil || len(chainAndAddr) != 2 {
		return "", "", fmt.Errorf("认证服务器返回的 chainInfo 格式异常: %q", chainAddr)
	}
	chainInfo = chainAndAddr[0]
	address = chainAndAddr[1]
	return address, chainInfo, nil
}
