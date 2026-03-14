#include <Arduino.h>
#include <WiFi.h>
#include <WebServer.h>
#include <Preferences.h>
#include <ESPmDNS.h>
#include <vector>

// ================= 硬件引脚配置 =================
// ESP32-C3 SuperMini 板载 LED
const int PIN_LED = 8; 

// 光耦控制引脚 (对应之前的测试)
const int PIN_OPTO_1 = 0;
const int PIN_OPTO_2 = 1;
const int PIN_OPTO_3 = 2;

// ================= WiFi / 配网相关配置 =================
// 若 NVS 中没有配置，将使用以下默认值
// !! 开源注意：请在此填入你自己的 WiFi 信息，或留空以强制进入 AP 配网模式 !!
const char* DEFAULT_SSID     = "";   // 留空则每次上电自动进入 AP 热点配网
const char* DEFAULT_PASSWORD = "";

// AP 模式参数
const char* AP_SSID = "ESP32_Config";
const char* AP_PASS = "12345678";   // 可按需修改，至少 8 位

// 控制端口配置
const uint16_t TCP_CONTROL_PORT = 12345;   // TCP 控制端口（PC 连接）

// 设备类型（固定标识），具体设备 ID 和友好名称会从 NVS / MAC 动态生成
const char* DEVICE_TYPE   = "SwitchBot";
const char* MDNS_HOSTNAME = "esp32-control";  // PC 通过 esp32-control.local 访问

// ================= 全局对象 =================
WiFiServer controlServer(TCP_CONTROL_PORT);
WiFiClient controlClient;

WebServer webServer(80);      // 配网与状态查看 HTTP 服务器
Preferences preferences;      // 存储 WiFi / 设备参数等

// 当前 WiFi 工作模式标记
bool isApMode = false;

// 设备唯一 ID 与用户友好名称（运行时使用这两个）
String gDeviceId;       // 例如 "SwitchBot_ABCDEF"
String gFriendlyName;   // 例如 "客厅灯开关"

// ================= 辅助函数 =================

// 非阻塞按键动作状态
struct ButtonTask {
    int pin;
    unsigned long activeTime;
    bool isActive;
};

ButtonTask currentTask = { -1, 0, false };

// ================= 二进制宏脚本系统 =================
// 说明：
// - 使用紧凑的字节码表示宏脚本
// - 通过 TCP 二进制帧 0xA0(定义宏) / 0xA1(运行宏) 下发
// - 同时保留原有的文本命令（btn1/btn2/btn3/get_status），便于调试

// 最多支持的宏数量（需与 Python 端保持一致，默认 10）
const int MAX_MACROS = 10;

// 存储原始二进制字节码
std::vector<uint8_t> macroBytes[MAX_MACROS];

// 每个宏在设备端允许存储的最大字节数（与 Python 端保持同一数量级，用于保护 NVS 空间）
const size_t MAX_MACRO_BYTES = 256;
// 用于在 NVS 中保存宏脚本的命名空间与 key 前缀
const char* MACRO_NVS_NAMESPACE = "macros";

// 将指定宏写入 NVS（掉电保持）
bool saveMacroToNVS(uint8_t id, const std::vector<uint8_t>& data) {
    if (id >= MAX_MACROS) return false;
    if (data.size() > MAX_MACRO_BYTES) {
        Serial.printf("Macro %u too large to save to NVS (%u bytes, max=%u)\n",
                      (unsigned)id, (unsigned)data.size(), (unsigned)MAX_MACRO_BYTES);
        return false;
    }

    preferences.begin(MACRO_NVS_NAMESPACE, false);
    char key[8];
    snprintf(key, sizeof(key), "m%u", (unsigned)id);
    if (data.empty()) {
        // 空宏时，直接清除 key，避免占用空间
        preferences.remove(key);
    } else {
        preferences.putBytes(key, data.data(), data.size());
    }
    preferences.end();
    Serial.printf("Macro %u saved to NVS, size=%u bytes\n",
                  (unsigned)id, (unsigned)data.size());
    return true;
}

// 从 NVS 恢复所有宏到内存中的 macroBytes 数组
void loadMacrosFromNVS() {
    preferences.begin(MACRO_NVS_NAMESPACE, true);
    for (uint8_t id = 0; id < MAX_MACROS; ++id) {
        char key[8];
        snprintf(key, sizeof(key), "m%u", (unsigned)id);
        size_t len = preferences.getBytesLength(key);
        if (len == 0) {
            // 没有存储或被清空
            macroBytes[id].clear();
            continue;
        }
        if (len > MAX_MACRO_BYTES) {
            // 超过当前允许的最大值，忽略并清掉内存中的数据
            Serial.printf("Macro %u in NVS too large (%u bytes), ignoring\n",
                          (unsigned)id, (unsigned)len);
            macroBytes[id].clear();
            continue;
        }
        macroBytes[id].resize(len);
        size_t readLen = preferences.getBytes(key, macroBytes[id].data(), len);
        if (readLen != len) {
            Serial.printf("Macro %u read length mismatch (%u/%u), clearing\n",
                          (unsigned)id, (unsigned)readLen, (unsigned)len);
            macroBytes[id].clear();
        } else {
            Serial.printf("Macro %u loaded from NVS, size=%u bytes\n",
                          (unsigned)id, (unsigned)len);
        }
    }
    preferences.end();
}

// 指令集常量
const uint8_t OP_BTN1_MS = 0x01;
const uint8_t OP_BTN2_MS = 0x02;
const uint8_t OP_BTN3_MS = 0x03;
const uint8_t OP_BTN1_L  = 0x11;
const uint8_t OP_BTN2_L  = 0x12;
const uint8_t OP_BTN3_L  = 0x13;
const uint8_t OP_DELAY_MS = 0x20;
const uint8_t OP_DELAY_L  = 0x21;
const uint8_t OP_DELAY_S  = 0x22;

// 工具：将宏字节码转为十六进制字符串，便于通过 JSON 返回
String macroToHexString(const std::vector<uint8_t>& data) {
    if (data.empty()) return "";
    String hex;
    hex.reserve(data.size() * 2);
    char buf[3];
    for (uint8_t b : data) {
        snprintf(buf, sizeof(buf), "%02X", b);
        hex += buf;
    }
    return hex;
}

// 宏执行状态机
struct MacroEngineState {
    bool isRunning;
    int  macroId;
    int  pc;                // Program Counter：当前读到的字节位置
    unsigned long stepStartTime;
    uint32_t currentDuration;   // 当前动作持续时间(ms)
    uint8_t currentAction;      // 0=空闲,1=Btn1,2=Btn2,3=Btn3,4=Delay
    bool waitingForTime;        // true=正在等待当前动作结束
};

MacroEngineState macroEngine = { false, 0, 0, 0, 0, 0, false };

// 非阻塞触发：只设置状态，不使用 delay
void trigger_button_nonblocking(int pin, const char* name) {
    if (currentTask.isActive) return;  // 正在按，忽略新的请求

    Serial.printf(">> Action: Triggering %s (GPIO %d)\n", name, pin);

    digitalWrite(PIN_LED, LOW);  // 亮
    digitalWrite(pin, HIGH);     // 闭合光耦

    currentTask.pin = pin;
    currentTask.activeTime = millis();
    currentTask.isActive = true;
}

// 在 loop 中调用，检查是否到时间松开
void update_button_state() {
    if (!currentTask.isActive) return;

    if (millis() - currentTask.activeTime > 100) {  // 200ms 后松开
        digitalWrite(currentTask.pin, LOW);
        digitalWrite(PIN_LED, HIGH);  // 灭
        currentTask.isActive = false;
        Serial.println(">> Action: Released.");
    }
}

// 运行二进制宏脚本（在 loop() 中周期调用，非阻塞）
void run_binary_macro_engine() {
    if (!macroEngine.isRunning) return;

    // 1. 如果当前在等待某个动作/延时结束
    if (macroEngine.waitingForTime) {
        if (millis() - macroEngine.stepStartTime >= macroEngine.currentDuration) {
            // 时间到，松开按键（如果是按键动作）
            if (macroEngine.currentAction == 1) digitalWrite(PIN_OPTO_1, LOW);
            if (macroEngine.currentAction == 2) digitalWrite(PIN_OPTO_2, LOW);
            if (macroEngine.currentAction == 3) digitalWrite(PIN_OPTO_3, LOW);
            if (macroEngine.currentAction != 4) {
                // 非纯延时动作，恢复 LED
                digitalWrite(PIN_LED, HIGH);
            }

            macroEngine.waitingForTime = false; // 准备读取下一条指令
        } else {
            // 还在计时中，直接返回，保持非阻塞
            return;
        }
    }

    // 2. 读取下一条指令
    if (macroEngine.macroId < 0 || macroEngine.macroId >= MAX_MACROS) {
        macroEngine.isRunning = false;
        return;
    }

    std::vector<uint8_t>& code = macroBytes[macroEngine.macroId];

    if (macroEngine.pc >= (int)code.size()) {
        // 脚本结束
        macroEngine.isRunning = false;
        Serial.println("Macro Finished (EOF).");
        return;
    }

    uint8_t opcode = code[macroEngine.pc++];
    uint32_t duration = 0;
    uint8_t action = 0; // 0=None,1=Btn1,2=Btn2,3=Btn3,4=Delay

    // 根据指令类型解析参数
    if (opcode == OP_BTN1_MS || opcode == OP_BTN2_MS || opcode == OP_BTN3_MS ||
        opcode == OP_DELAY_MS || opcode == OP_DELAY_S) {
        // 后面跟 1 字节参数
        if (macroEngine.pc >= (int)code.size()) {
            macroEngine.isRunning = false;
            return;
        }
        uint8_t val = code[macroEngine.pc++];
        if (opcode == OP_DELAY_S) {
            duration = (uint32_t)val * 1000UL;   // 秒 -> 毫秒
        } else {
            duration = val;                      // 直接 ms
        }
    } else if (opcode == OP_BTN1_L || opcode == OP_BTN2_L || opcode == OP_BTN3_L ||
               opcode == OP_DELAY_L) {
        // 后面跟 2 字节参数（大端）
        if (macroEngine.pc + 1 >= (int)code.size()) {
            macroEngine.isRunning = false;
            return;
        }
        uint8_t b1 = code[macroEngine.pc++];
        uint8_t b2 = code[macroEngine.pc++];
        duration = ((uint32_t)b1 << 8) | (uint32_t)b2;
    } else {
        // 未知指令，终止宏，避免死循环
        Serial.printf("Unknown OpCode in macro: 0x%02X\n", opcode);
        macroEngine.isRunning = false;
        return;
    }

    // 映射为动作类型
    if (opcode == OP_BTN1_MS || opcode == OP_BTN1_L)      action = 1;
    else if (opcode == OP_BTN2_MS || opcode == OP_BTN2_L) action = 2;
    else if (opcode == OP_BTN3_MS || opcode == OP_BTN3_L) action = 3;
    else                                                  action = 4; // Delay

    macroEngine.currentAction   = action;
    macroEngine.currentDuration = duration;
    macroEngine.stepStartTime   = millis();
    macroEngine.waitingForTime  = true;

    // 执行动作（按键或延时）
    if (action >= 1 && action <= 3) {
        int pin = (action == 1) ? PIN_OPTO_1 : (action == 2 ? PIN_OPTO_2 : PIN_OPTO_3);
        digitalWrite(pin, HIGH);     // 按下
        digitalWrite(PIN_LED, LOW);  // 亮灯表示正在执行宏
        Serial.printf("Exec Macro: Btn%d for %lu ms\n", action, (unsigned long)duration);
    } else {
        Serial.printf("Exec Macro: Delay %lu ms\n", (unsigned long)duration);
    }
}

// ================= Web 配置页 & WiFi 管理 =================

// 简单的 HTML 配网页面（内嵌，避免依赖文件系统）
String buildConfigPage(const String& msg = "") {
    String html =
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>ESP32 WiFi Config</title>"
        "<style>"
        "body{font-family:Arial;margin:0;padding:0;background:#f5f5f5;}"
        ".card{max-width:400px;margin:40px auto;padding:20px;background:#fff;"
        "box-shadow:0 2px 8px rgba(0,0,0,0.1);border-radius:8px;}"
        "h2{text-align:center;margin-top:0;}"
        "label{display:block;margin:12px 0 4px;}"
        "input{width:100%;padding:8px 10px;border:1px solid #ccc;border-radius:4px;}"
        "button{width:100%;margin-top:16px;padding:10px;background:#1976d2;color:#fff;"
        "border:none;border-radius:4px;font-size:16px;cursor:pointer;}"
        "button:hover{background:#1565c0;}"
        ".msg{margin-top:10px;color:#d32f2f;text-align:center;font-size:14px;}"
        ".info{font-size:12px;color:#666;text-align:center;margin-top:10px;}"
        "</style></head><body><div class='card'>"
        "<h2>WiFi 配置</h2>"
        "<form method='POST' action='/save'>"
        "<label>WiFi SSID</label>"
        "<input type='text' name='ssid' required>"
        "<label>WiFi 密码</label>"
        "<input type='password' name='password' required>"
        "<label>设备名称（友好名称，可选）</label>"
        "<input type='text' name='name' value='" + gFriendlyName + "'>"
        "<button type='submit'>保存并重启</button>"
        "</form>";

    if (msg.length() > 0) {
        html += "<div class='msg'>" + msg + "</div>";
    }

    html += "<div class='info'>当前模式: ";
    html += (isApMode ? "AP (配置热点)" : "STA");
    html += "</div></div></body></html>";

    return html;
}

void handleRoot() {
    webServer.send(200, "text/html; charset=utf-8", buildConfigPage());
}

void handleSave() {
    if (!webServer.hasArg("ssid") || !webServer.hasArg("password")) {
        webServer.send(400, "text/plain; charset=utf-8", "缺少 ssid 或 password");
        return;
    }

    String ssid = webServer.arg("ssid");
    String pass = webServer.arg("password");
    String name = webServer.hasArg("name") ? webServer.arg("name") : "";

    preferences.begin("wifi", false);
    preferences.putString("ssid", ssid);
    preferences.putString("password", pass);
    if (name.length() > 0) {
        preferences.putString("name", name);
    }
    preferences.end();

    webServer.send(200, "text/html; charset=utf-8",
                   "<html><body><h3>保存成功，设备将重启并尝试连接新的 WiFi...</h3></body></html>");

    delay(1000);
    ESP.restart();
}

void handleStatus() {
    String json = "{";
    json += "\"mode\":\"";
    json += (isApMode ? "AP" : "STA");
    json += "\",";
    json += "\"device_id\":\"" + gDeviceId + "\",";
    json += "\"friendly_name\":\"" + gFriendlyName + "\",";
    json += "\"device_type\":\"" + String(DEVICE_TYPE) + "\",";
    json += "\"tcp_port\":" + String(TCP_CONTROL_PORT) + ",";
    if (WiFi.getMode() & WIFI_STA) {
        json += "\"sta_ip\":\"" + WiFi.localIP().toString() + "\",";
    }
    if (WiFi.getMode() & WIFI_AP) {
        json += "\"ap_ip\":\"" + WiFi.softAPIP().toString() + "\",";
    }
    json += "\"uptime_ms\":" + String(millis());
    json += "}";

    webServer.send(200, "application/json", json);
}

void startWebServer() {
    webServer.on("/", HTTP_GET, handleRoot);
    webServer.on("/save", HTTP_POST, handleSave);
    webServer.on("/status", HTTP_GET, handleStatus);
    webServer.begin();
    Serial.println("WebServer started on port 80");
}

// 初始化设备唯一 ID 与友好名称（从 NVS 读取，如无则基于 MAC 生成）
void initDeviceIdentity() {
    preferences.begin("device", false);
    String id = preferences.getString("id", "");
    String name = preferences.getString("name", "");
    preferences.end();

    if (id.length() == 0) {
        // 基于 MAC 地址生成唯一 ID，例如 SwitchBot_ABCDEF
        String mac = WiFi.macAddress();          // 形式：AA:BB:CC:DD:EE:FF
        mac.replace(":", "");
        String suffix = mac.substring(6);        // 取后 6 位
        id = String(DEVICE_TYPE) + "_" + suffix;

        preferences.begin("device", false);
        preferences.putString("id", id);
        preferences.end();

        Serial.printf("Generated new device id: %s\n", id.c_str());
    }

    if (name.length() == 0) {
        // 默认友好名称与 ID 相同
        name = id;
        preferences.begin("device", false);
        preferences.putString("name", name);
        preferences.end();
    }

    gDeviceId = id;
    gFriendlyName = name;

    Serial.printf("Device ID: %s, Friendly Name: %s\n", gDeviceId.c_str(), gFriendlyName.c_str());
}

// 尝试以 STA 模式连接 WiFi，使用 NVS 中的配置；失败返回 false
bool connectWiFiFromNVS() {
    preferences.begin("wifi", true);
    String ssid = preferences.getString("ssid", DEFAULT_SSID);
    String pass = preferences.getString("password", DEFAULT_PASSWORD);
    preferences.end();

    if (ssid.length() == 0) {
        Serial.println("No SSID stored in NVS.");
        return false;
    }

    Serial.printf("Trying WiFi SSID from NVS: %s\n", ssid.c_str());

    WiFi.mode(WIFI_STA);
    WiFi.begin(ssid.c_str(), pass.c_str());

    unsigned long startAttemptTime = millis();
    const unsigned long wifiTimeoutMs = 15000;  // 15 秒超时

    while (WiFi.status() != WL_CONNECTED && millis() - startAttemptTime < wifiTimeoutMs) {
        delay(500);
        Serial.print(".");
        digitalWrite(PIN_LED, !digitalRead(PIN_LED));
    }

    if (WiFi.status() != WL_CONNECTED) {
        Serial.println("\nWiFi connect failed with stored config.");
        return false;
    }

    Serial.println("\nWiFi Connected (NVS config)!");
    Serial.print("IP: "); 
    Serial.println(WiFi.localIP());
    digitalWrite(PIN_LED, HIGH);
    return true;
}

// 启动 AP 配网模式
void startApConfigMode() {
    Serial.println("Starting AP config mode...");
    isApMode = true;

    WiFi.mode(WIFI_AP);
    WiFi.softAP(AP_SSID, AP_PASS);

    Serial.print("AP SSID: ");
    Serial.println(AP_SSID);
    Serial.print("AP PASS: ");
    Serial.println(AP_PASS);
    Serial.print("AP IP: ");
    Serial.println(WiFi.softAPIP());

    startWebServer();
}

// 高层 WiFi 启动流程：优先 STA（NVS 配置），失败则启用 AP 配网
void setup_wifi() {
    delay(10);
    Serial.println("\n===== WiFi Setup Start =====");

    // 确保设备 ID / 名称已初始化（依赖 WiFi.macAddress，需要先初始化 WiFi）
    if (gDeviceId.length() == 0) {
        // 先打开 WiFi 以便读取 MAC（不建立连接）
        WiFi.mode(WIFI_STA);
        initDeviceIdentity();
    }

    bool ok = connectWiFiFromNVS();
    if (!ok) {
        startApConfigMode();
    } else {
        isApMode = false;
        startWebServer();  // 即使在 STA 下也提供 /status 和可选配置

        // 启动 mDNS，应答 esp32-control.local
        if (MDNS.begin(MDNS_HOSTNAME)) {
            Serial.printf("mDNS responder started: %s.local\n", MDNS_HOSTNAME);
            MDNS.addService("switchbot", "tcp", TCP_CONTROL_PORT);
            MDNS.addServiceTxt("switchbot", "tcp", "device_id", gDeviceId.c_str());
            MDNS.addServiceTxt("switchbot", "tcp", "friendly_name", gFriendlyName.c_str());
            MDNS.addServiceTxt("switchbot", "tcp", "device_type", DEVICE_TYPE);
        } else {
            Serial.println("Error setting up mDNS responder!");
        }
    }

    Serial.println("===== WiFi Setup Done =====");
}

// 处理来自 PC 的 TCP 控制指令
// 协议约定：每条指令一行文本，以 '\n' 结尾，例如：
//   btn1
//   btn2
//   btn3
//   get_status
void handle_tcp_control() {
    if (WiFi.status() != WL_CONNECTED) return;

    // 如果当前没有已连接客户端，或现有客户端已断开，则接受新连接
    if (!controlClient || !controlClient.connected()) {
        WiFiClient newClient = controlServer.available();
        if (newClient) {
            controlClient.stop();       // 关闭旧连接（如果有的话）
            controlClient = newClient;  // 切换为新客户端
            controlClient.setNoDelay(true);
            Serial.print("TCP client connected: ");
            Serial.println(controlClient.remoteIP());
        }
    }

    if (!controlClient || !controlClient.connected()) {
        return;
    }

    // 循环处理接收缓冲区中的数据：
    // - 若首字节是 0xA0/0xA1，则按二进制宏协议解析
    // - 否则按照原来的按行文本协议解析
    while (controlClient.available()) {
        int firstByte = controlClient.peek();
        if (firstByte < 0) {
            return;
        }

        // 处理二进制宏帧： [CMD(1B)=0xA0/0xA1][ID(1B)][LEN(2B)][PAYLOAD...]
        if (firstByte == 0xA0 || firstByte == 0xA1 || firstByte == 0xA2) {
            // 确保头部 4 字节已全部到达
            if (controlClient.available() < 4) {
                // 等待更多数据下次再处理
                return;
            }

            uint8_t cmd = controlClient.read();
            uint8_t id  = controlClient.read();
            uint8_t lenHi = controlClient.read();
            uint8_t lenLo = controlClient.read();
            uint16_t dataLen = ((uint16_t)lenHi << 8) | (uint16_t)lenLo;

            // 简单超时等待 payload 到齐
            unsigned long startWait = millis();
            while (controlClient.available() < dataLen &&
                   millis() - startWait < 500) {
                delay(1);
            }

            if (controlClient.available() < dataLen) {
                Serial.println("Binary macro packet timeout/incomplete, flushing.");
                while (controlClient.available()) controlClient.read();
                return;
            }

            if (cmd == 0xA0) {  // 定义宏
                if (id >= MAX_MACROS) {
                    // ID 无效，丢弃 payload
                    for (uint16_t i = 0; i < dataLen; ++i) {
                        controlClient.read();
                    }
                    controlClient.println("{\"status\":\"error\",\"msg\":\"invalid_macro_id\"}");
                } else if (dataLen > MAX_MACRO_BYTES) {
                    // 超过单个宏允许的最大字节数，丢弃并报错
                    for (uint16_t i = 0; i < dataLen; ++i) {
                        controlClient.read();
                    }
                    Serial.printf("Macro %u too large from client (%u bytes, max=%u)\n",
                                  (unsigned)id, (unsigned)dataLen, (unsigned)MAX_MACRO_BYTES);
                    controlClient.println("{\"status\":\"error\",\"msg\":\"macro_too_large\"}");
                } else {
                    macroBytes[id].clear();
                    macroBytes[id].reserve(dataLen);
                    for (uint16_t i = 0; i < dataLen; ++i) {
                        macroBytes[id].push_back((uint8_t)controlClient.read());
                    }
                    Serial.printf("Macro %u defined, size=%u bytes\n",
                                  (unsigned)id, (unsigned)dataLen);
                    // 尝试保存到 NVS，实现掉电保持
                    saveMacroToNVS(id, macroBytes[id]);
                    // 回复一行 JSON（带换行），方便 Python 端用 readline() 读取
                    controlClient.println("{\"status\":\"ok\",\"msg\":\"macro_defined\"}");
                }
            } else if (cmd == 0xA1) {   // 运行宏
                // 一般情况下 dataLen 为 0，但还是要把 payload 读掉
                for (uint16_t i = 0; i < dataLen; ++i) {
                    controlClient.read();
                }

                if (id < MAX_MACROS && !macroBytes[id].empty()) {
                    macroEngine.isRunning   = true;
                    macroEngine.macroId     = id;
                    macroEngine.pc          = 0;
                    macroEngine.waitingForTime = false;
                    macroEngine.currentAction  = 0;
                    Serial.printf("Start running macro %u\n", (unsigned)id);
                    controlClient.println("{\"status\":\"ok\",\"msg\":\"macro_started\"}");
                } else {
                    controlClient.println("{\"status\":\"error\",\"msg\":\"macro_not_defined\"}");
                }
            } else if (cmd == 0xA2) {   // 查询宏
                for (uint16_t i = 0; i < dataLen; ++i) {
                    controlClient.read();
                }
                if (id < MAX_MACROS) {
                    bool defined = !macroBytes[id].empty();
                    String hex = macroToHexString(macroBytes[id]);
                    controlClient.print("{\"status\":\"ok\",\"macro_id\":");
                    controlClient.print(id);
                    controlClient.print(",\"defined\":");
                    controlClient.print(defined ? "true" : "false");
                    controlClient.print(",\"payload_hex\":\"");
                    controlClient.print(hex);
                    controlClient.println("\"}");
                } else {
                    controlClient.println("{\"status\":\"error\",\"msg\":\"invalid_macro_id\"}");
                }
            }

            // 处理完一帧后继续看缓冲区是否还有数据
            continue;
        }

        // 否则按文本行协议处理
        String line = controlClient.readStringUntil('\n');
        line.trim();
        if (line.length() == 0) continue;

        Serial.printf("TCP Recv: %s\n", line.c_str());

        if (line == "btn1") {
            trigger_button_nonblocking(PIN_OPTO_1, "Button 1");
            controlClient.println("{\"status\":\"ok\",\"cmd\":\"btn1\"}");
        } else if (line == "btn2") {
            trigger_button_nonblocking(PIN_OPTO_2, "Button 2");
            controlClient.println("{\"status\":\"ok\",\"cmd\":\"btn2\"}");
        } else if (line == "btn3") {
            trigger_button_nonblocking(PIN_OPTO_3, "Button 3");
            controlClient.println("{\"status\":\"ok\",\"cmd\":\"btn3\"}");
        } else if (line == "get_status") {
            String resp = "{";
            resp += "\"status\":\"online\",";
            resp += "\"device_id\":\"" + gDeviceId + "\",";
            resp += "\"friendly_name\":\"" + gFriendlyName + "\",";
            resp += "\"device_type\":\"" + String(DEVICE_TYPE) + "\",";
            resp += "\"ip\":\"" + WiFi.localIP().toString() + "\",";
            resp += "\"tcp_port\":" + String(TCP_CONTROL_PORT) + ",";
            resp += "\"uptime_ms\":" + String(millis());
            resp += "}";
            controlClient.println(resp);
        } else {
            Serial.println(">> Warning: Unknown Command");
            controlClient.println("{\"status\":\"error\",\"msg\":\"unknown_command\"}");
        }
    }
}

// ================= 主程序 =================

void setup() {
    // 1. 初始化 LED
    pinMode(PIN_LED, OUTPUT);
    digitalWrite(PIN_LED, HIGH); // 默认灭

    // 2. 初始化光耦引脚
    pinMode(PIN_OPTO_1, OUTPUT); digitalWrite(PIN_OPTO_1, LOW);
    pinMode(PIN_OPTO_2, OUTPUT); digitalWrite(PIN_OPTO_2, LOW);
    pinMode(PIN_OPTO_3, OUTPUT); digitalWrite(PIN_OPTO_3, LOW);

    // 3. 初始化串口
    Serial.begin(115200);
    delay(2000); 

    // 4. 从 NVS 恢复之前保存的宏定义（如果有），保证设备重启后宏仍然可用
    loadMacrosFromNVS();

    // 5. 网络与 WiFi 电源管理
    WiFi.setSleep(false);  // 关闭省电，降低延迟与断连概率
    setup_wifi();

    if (WiFi.status() == WL_CONNECTED && !isApMode) {
        controlServer.begin();
        controlServer.setNoDelay(true);
        Serial.printf("TCP control   on port %u\n", TCP_CONTROL_PORT);
    } else {
        Serial.println("WiFi not connected, network services not started.");
    }
}

void loop() {
    // 处理 Web 配置 / 状态页面
    webServer.handleClient();

    // 检查 WiFi 状态，如果断开且不在 AP 模式，则重启连接逻辑
    if (!isApMode && WiFi.status() != WL_CONNECTED) {
        setup_wifi();
        if (WiFi.status() == WL_CONNECTED && !isApMode) {
            controlServer.begin();
            controlServer.setNoDelay(true);
            Serial.printf("TCP control   on port %u\n", TCP_CONTROL_PORT);
        }
    }

    // 处理 TCP 控制指令
    handle_tcp_control();

    // 更新非阻塞按键状态（单次点按）
    update_button_state();

    // 驱动二进制宏脚本引擎（非阻塞执行长脚本）
    run_binary_macro_engine();

    // 可选：非常小的 delay，避免空转过快
    delay(1);
}