## 本地控制技术栈概览

该方案实现了一套完全本地化、免云端的多设备控制链路，覆盖 **ESP32 端固件**、**PC 端调度服务**、以及 **Web 前端控制台**。整体流程如下：

1. ESP32 通过 WiFi STA/AP 模式切换完成联网配置，并通过本地 HTTP 接口暴露自身信息。
2. PC 端服务主动扫描局域网 IP（HTTP /status），自动发现设备并基于 TCP 建立可靠控制通道，同时暴露本地 HTTP API。
3. Web 前端控制台运行在同一 PC 服务内，实时展示在线设备、转发控制指令、查看执行结果。

---

## ESP32 固件能力（`src/main.cpp`）

### 1. WiFi 配网与模式管理
- **STA 优先**：从 NVS 读取 WiFi SSID/Password（若无则使用默认值），15s 内尝试连接。
- **失败自动切 AP**：连接失败后开启 `ESP32_Config` 热点，启动内嵌 Web 配网页面（/、/save、/status）。
- **友好名称配置**：Web 表单可额外设置“设备名称”，存储在 NVS 中，便于多设备区分。
- **状态保持**：进入 AP 模式后不再重复切换，直至用户保存配置并自动重启。

### 2. 设备身份与持久化
- 运行时生成唯一 `gDeviceId = {DEVICE_TYPE}_{MAC后6位}`，并写入 NVS，确保多设备不冲突。
- 友好名称 `gFriendlyName` 默认为设备 ID，可通过 Web 页面自定义。
- `/status` 页面与 TCP `get_status` 响应都会附带 `device_id`、`friendly_name`、`device_type` 以及 `tcp_port`。

### 3. 本地网络通信
- **HTTP 状态接口**：始终运行在端口 80，PC 通过主动扫描 `http://<ip>/status` 来确认设备在线并获取元数据。
- **TCP 控制**：在 `TCP_CONTROL_PORT=12345` 上监听；每条指令为单行文本，执行完即回 JSON 结果。
  - 支持指令：`btn1/btn2/btn3`（触发光耦）与 `get_status`（返回在线状态、IP、运行时间等）。
- **Web 配页**：内置简单 HTML/CSS，不依赖额外文件系统，提供 SSID/密码/名称的配置入口。

---

## PC 端服务能力（`testSever/testSever.py`）

### 1. 核心进程
- 使用 `asyncio + aiohttp`。
- **HTTP 子网扫描**：默认每 10 秒扫描一次推断出的 /24 子网（若无法推断则使用 `192.168.100.x`）。对每个 IP 并发请求 `/status`，只要响应中 `device_id/device_type` 匹配就登记为在线（60 秒无响应即视为离线）。
- **TCP 连接池**：为每个设备维护 `StreamReader/Writer`，检测断连并自动重建，所有命令走请求-响应模型。
- **HTTP API**：
  - `GET /devices`：列出在线设备及最近发现信息。
  - `POST /control`：向指定设备发送指令并返回执行结果（直接透传 ESP32 的 JSON）。
  - `GET /status?device_id=`：向设备发送 `get_status` 并返回实时状态。

### 2. Web 前端
- `GET /` 返回的单页 Web 控制台（纯静态 HTML/JS/CSS）具备：
  - 实时刷新在线设备列表（5 秒轮询 `/devices`）。
  - 显示设备名称、ID、类型、IP 与上次广播时间。
  - 提供 `btn1/btn2/btn3/get_status` 按钮，点击后调用 `/control` 并在下方状态框展示响应。
- 相同服务内同时提供 API 与 Web UI，既便于调试也方便最终用户直接操作。

---

## 运行与使用步骤

1. **烧录 ESP32 固件**
   - 使用 PlatformIO 上传 `src/main.cpp`。
   - 首次上电若无 WiFi 配置，将自动开启 `ESP32_Config` 热点，按页面提示完成 SSID/密码/名称配置。

2. **启动 PC 端服务**
   ```bash
   cd testSever
   pip install aiohttp
   python testSever.py
   ```
   - 控制台将提示 HTTP 扫描范围、发现/剔除的设备，以及 Web/API 端口。

3. **访问本地 Web 控制台**
   - 浏览器打开 `http://127.0.0.1:5000/`
   - 确保 ESP32 与 PC 在同一局域网下，即可看到在线设备并直接点击控制按钮。

4. **对接其他上位机/脚本**
   - 通过 HTTP API 集成，例如：
     ```bash
     # 触发按键
     curl -X POST http://127.0.0.1:5000/control \
       -H "Content-Type: application/json" \
       -d "{\"device_id\":\"SwitchBot_AB12CD\",\"command\":\"btn1\"}"

     # 查询状态
     curl "http://127.0.0.1:5000/status?device_id=SwitchBot_AB12CD"
     ```

---

## 目录与文件说明

| 路径 | 说明 |
| ---- | ---- |
| `src/main.cpp` | ESP32 端主程序：WiFi 配网、HTTP 状态接口、TCP 控制、按键逻辑。 |
| `testSever/testSever.py` | PC 端本地服务器：HTTP 子网扫描、TCP 控制、HTTP API 与 Web 控制台。 |
| `docs/LOCAL_CONTROL_STACK.md` | 当前文档，描述整体技术栈与使用方法。 |

---

## 后续可扩展方向

- **ESP32 端**：增加更多传感/执行命令、JSON 命令协议、OTA 升级、HTTPS 配置页等。
- **PC 端**：引入数据库存储设备元数据／操作日志；为 Web UI 增加身份认证、操作权限；或提供 WebSocket 实时推送。
- **跨平台客户端**：在其他设备（如树莓派、边缘服务器）部署相同 Python 服务，或将 API 集成进现有系统。

该技术栈已经覆盖“发现 → 配置 → 控制 → 可视化”全流程，具备良好的扩展性，可根据业务需求继续演进。 

