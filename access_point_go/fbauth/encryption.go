package fbauth

import (
	"crypto/aes"
	"crypto/cipher"
	"crypto/ecdsa"
	"crypto/sha256"
	"fmt"
)

// encryptionSession 表示与认证服务器之间的一个加密会话。
// FastBuilder 认证服务器使用 ECDH (P-384) 协商共享密钥，再通过 SHA-256 派生
// AES-256 密钥与初始 IV，最后采用 CFB 模式对收发的 WebSocket 消息逐字节加解密。
type encryptionSession struct {
	serverPrivateKey *ecdsa.PrivateKey // 本端 ECDH 私钥
	clientPublicKey  *ecdsa.PublicKey  // 对端(认证服务器) ECDH 公钥
	salt             []byte            // 固定盐值，参与密钥派生

	sharedSecret   []byte     // ECDH 计算出的共享密钥(仅 x 坐标)
	secretKeyBytes [32]byte   // 由共享密钥与盐派生出的 AES-256 密钥
	cipherBlock    cipher.Block // AES 加密块

	encryptIV []byte // 加密使用的初始向量，随每次加密滚动更新
	decryptIV []byte // 解密使用的初始向量，随每次解密滚动更新
}

// init 初始化加密会话：计算共享密钥并据此派生出密钥与 IV。
func (session *encryptionSession) init() error {
	session.computeSharedSecret()
	return session.computeIVs()
}

// computeSharedSecret 计算 ECDH 共享密钥。FastBuilder 仅使用曲线点的 x 坐标作为共享密钥。
func (session *encryptionSession) computeSharedSecret() {
	x, _ := session.clientPublicKey.Curve.ScalarMult(
		session.clientPublicKey.X, session.clientPublicKey.Y,
		session.serverPrivateKey.D.Bytes(),
	)
	session.sharedSecret = x.Bytes()
}

// computeIVs 计算用于 CFB 模式的 IV 与 AES 密码块。
// 这里复刻 FastBuilder 的派生方式：取共享密钥前 12 字节，拼接固定后缀后再与盐一起做 SHA-256。
func (session *encryptionSession) computeIVs() error {
	var err error

	first12 := append([]byte(nil), session.sharedSecret[:12]...)
	sec := append(first12, 0, 0, 1, 228)
	session.secretKeyBytes = sha256.Sum256(append(sec, session.salt...))
	session.cipherBlock, err = aes.NewCipher(session.secretKeyBytes[:])
	if err != nil {
		return fmt.Errorf("创建 AES 密码块失败: %v", err)
	}

	// 加解密 IV 均初始化为派生密钥的前 16 字节(aes.BlockSize)。
	session.encryptIV = append([]byte{}, session.secretKeyBytes[:aes.BlockSize]...)
	session.decryptIV = append([]byte{}, session.secretKeyBytes[:aes.BlockSize]...)
	return nil
}

// encrypt 就地对传入的字节切片进行加密。
// 每加密一个字节后，IV 会向前滚动(丢弃首字节，把刚产生的密文追加到末尾)，
// 这是 CFB 自同步特性的体现。
func (session *encryptionSession) encrypt(data []byte) {
	for i := range data {
		cipherFeedback := cipher.NewCFBEncrypter(session.cipherBlock, session.encryptIV)
		cipherFeedback.XORKeyStream(data[i:i+1], data[i:i+1])
		session.encryptIV = append(session.encryptIV[1:], data[i])
	}
}

// decrypt 就地对传入的字节切片进行解密。逻辑与 encrypt 对称。
func (session *encryptionSession) decrypt(data []byte) {
	for i, b := range data {
		cipherFeedback := cipher.NewCFBDecrypter(session.cipherBlock, session.decryptIV)
		cipherFeedback.XORKeyStream(data[i:i+1], data[i:i+1])
		session.decryptIV = append(session.decryptIV[1:], b)
	}
}
