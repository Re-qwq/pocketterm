package main

// PocketTerm Go 插件示例
// 通过 stdin/stdout JSON 行协议与 PocketTerm 通信
//
// 收到的事件格式：{"type":"event","name":"player_join","data":{"player_name":"Steve"}}
// 发送的请求格式：{"type":"request","id":"uuid","name":"send_command","data":{"command":"/say hello"}}

import (
	"bufio"
	"encoding/json"
	"fmt"
	"os"
)

type Message struct {
	Type string          `json:"type"`
	Name string          `json:"name"`
	Data json.RawMessage `json:"data"`
	ID   string          `json:"id"`
}

type PlayerJoinData struct {
	PlayerName string `json:"player_name"`
}

type SendCommandData struct {
	Command string `json:"command"`
}

func main() {
	// 通知宿主已就绪
	sendReady()

	scanner := bufio.NewScanner(os.Stdin)
	for scanner.Scan() {
		line := scanner.Text()
		var msg Message
		if err := json.Unmarshal([]byte(line), &msg); err != nil {
			continue
		}

		switch msg.Type {
		case "event":
			handleEvent(msg)
		}
	}
}

func sendReady() {
	msg := Message{Type: "ready"}
	data, _ := json.Marshal(msg)
	fmt.Println(string(data))
}

func sendLog(level, message string) {
	data, _ := json.Marshal(map[string]interface{}{
		"type":    "log",
		"level":   level,
		"message": message,
	})
	fmt.Println(string(data))
}

func sendCommand(command string) {
	data, _ := json.Marshal(map[string]interface{}{
		"type": "request",
		"name": "send_command",
		"data": map[string]string{
			"command": command,
		},
	})
	fmt.Println(string(data))
}

func handleEvent(msg Message) {
	switch msg.Name {
	case "plugin_load":
		sendLog("info", "Go 插件已加载！")

	case "player_join":
		var data PlayerJoinData
		json.Unmarshal(msg.Data, &data)
		sendLog("info", fmt.Sprintf("玩家 %s 加入了游戏", data.PlayerName))
		sendCommand(fmt.Sprintf("/say 欢迎 %s！", data.PlayerName))

	case "player_leave":
		var data PlayerJoinData
		json.Unmarshal(msg.Data, &data)
		sendLog("info", fmt.Sprintf("玩家 %s 离开了游戏", data.PlayerName))

	case "chat":
		sendLog("info", fmt.Sprintf("收到聊天消息: %s", string(msg.Data)))
	}
}
