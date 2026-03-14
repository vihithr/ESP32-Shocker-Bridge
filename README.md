# ESP32 Shocker Bridge

**将任意可编程事件映射为现实电击反馈的本地网络执行器。**  
**A local-network HTTP executor that maps any programmable event to real-world haptic shock feedback.**

---

## 安全声明

- 本项目仅供技术学习与个人娱乐，使用者需自行承担风险
- 务必使用光耦进行电气隔离，禁止将 ESP32 GPIO 直接连接人体
- 推荐使用经过认证的 TENS 理疗仪作为执行器，不要使用高压升压模块
- 心脏病患者、孕妇、装有心脏起搏器者禁止使用
- 使用前请将理疗仪功率调至最低档位测试

---

## License

MIT License — 详见 [LICENSE](LICENSE) 文件。

---

## 项目起源

起源于一次弹幕射击游戏群里的玩笑：*"有没有 miss 一次就电你一下的电击项圈？"*  
从玩笑到原型机，从洞洞板焊接到自制 PCB，验证了「游戏事件 → 网络 API → 物理反馈」这条完整链路的可行性。

详细环境搭建步骤见 [`docs/SETUP_GUIDE.md`](docs/SETUP_GUIDE.md)。
核心设计思路

本项目的核心不是「STG游戏电击器」，而是一个通用的**物理电击执行器网络化方案**：

```
[ 任意触发源 ]  ──HTTP POST──▶  [ 本地服务 :5000 ]  ──TCP──▶  [ ESP32 ]  ──光耦──▶  [ 理疗仪 ]

触发源示例：
  - 游戏内存 Hook（Frida）
  - 任何支持 Webhook 的应用
  - Shell 脚本 / curl
  - 其他编程语言的 HTTP 客户端
  - 浏览器 Web 控制台（内置）
```

ESP32 通过 WiFi 接入局域网，将理疗仪的三个物理按键（开机/加强/关机）映射为标准 HTTP API，任何能发出 HTTP 请求的程序都可以作为触发端。

---

## 系统架构

```
┌─────────────────────────────────────────────────────────────────┐
│                        触发端（可替换）                          │
│  Frida Game Hook  │  curl / script  │  Webhook  │  Web UI       │
└────────────────────────────┬────────────────────────────────────┘
                             │ HTTP POST
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│              本地服务层  testSever/testSever.py                   │
│                                                                  │
│  • GET  /devices          — 查询在线设备                         │
│  • POST /control          — 发送原始指令 (btn1/btn2/btn3)        │
│  • GET  /macros           — 查询宏列表                           │
│  • POST /macros/{id}      — 定义宏脚本                           │
│  • POST /macros/{id}/run  — 运行宏脚本                           │
│  • GET  /                 — Web 控制台（内置 HTML）              │
│                                                                  │
│  mDNS 自动发现 esp32-control.local，TCP 长连接保持               │
└────────────────────────────┬────────────────────────────────────┘
                             │ TCP :12345
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│              ESP32-C3 固件  src/main.cpp                         │
│                                                                  │
│  • WiFi STA（NVS配置）/ AP 热点配网（失败自动切换）              │
│  • mDNS 响应 esp32-control.local                                 │
│  • TCP 控制服务 :12345 + HTTP 状态页 :80                         │
│  • 二进制宏引擎（非阻塞，掉电NVS持久化）                        │
│  • GPIO 光耦控制（PIN 0/1/2）                                    │
└────────────────────────────┬────────────────────────────────────┘
                             │ 光耦隔离
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│              物理执行层                                          │
│  廉价 TENS 理疗仪（三按键：开机/加强/关机）                     │
│  TRRS 接口引出，与控制板电气隔离                                │
└─────────────────────────────────────────────────────────────────┘
```

---

## 硬件 BOM

| 器件 | 规格 | 数量 | 参考价格 |
|------|------|------|----------|
| 微控制器 | ESP32-C3 SuperMini | 1 | ~¥8 |
| 光耦 | PC817 × 3 | 1组 | ~¥1 |
| 限流电阻 | 220Ω × 3 | 1组 | <¥1 |
| 执行器 | TENS 廉价理疗仪（三按键款） | 1 | ~¥15 |
| 接口 | TRRS 3.5mm 插头/插座 | 1 | ~¥2 |
| PCB | 嘉立创打板（Gerber 见 hardware/） | 1 | ~¥5 |
| **合计** | | | **~¥30** |

> **安全说明**：必须使用光耦进行电气隔离。请勿使用高压升压模块直接接触人体。TENS 理疗仪为量产医疗器械，安全性已经过认证。

---

## 目录结构

```
.
├── src/
│   └── main.cpp                        # ESP32 固件主程序
├── testSever/
│   ├── testSever.py                    # 本地服务层（核心）
│   ├── monitor.py                      # 设备状态监控工具
│   ├── Universal_STGB_HP_Hook.py       # STGB 引擎通用 Hook（触发端示例）
│   ├── Universal_STGB_HP_Hook_Lite.py  # 精简版 Hook
│   ├── monito_HP_Feature_STGB.py       # HP 特征码辅助工具
│   ├── Universal_STGB_HP_Hook_config.json  # Hook 配置文件
│   ├── macros_storage.json             # 宏持久化存储（运行时生成，已 gitignore）
│   └── requirements.txt               # Python 依赖
├── hardware/
│   └── Gerber_New-Project_*.zip        # PCB 生产文件
├── docs/
│   ├── LOCAL_CONTROL_STACK.md         # 技术栈详细说明
│   └── SETUP_GUIDE.md                 # 环境搭建与复现指南
├── platformio.ini                      # PlatformIO 工程配置
└── README.md
```

---

## 快速开始

### 1. 烧录固件

```bash
# 安装 PlatformIO CLI 或使用 VS Code PlatformIO 插件
pip install platformio

# 编译并上传（ESP32-C3 通过 USB 连接）
pio run --target upload
```

首次上电若 NVS 中无 WiFi 配置，设备将自动开启 `ESP32_Config` 热点（密码 `12345678`），连接后访问 `http://192.168.4.1` 完成 WiFi 配置。

> **注意**：`src/main.cpp` 中 `DEFAULT_SSID` 和 `DEFAULT_PASSWORD` 已置空，请通过 AP 热点配网或自行修改后编译。

### 2. 启动本地服务

```bash
cd testSever
pip install -r requirements.txt
python testSever.py
```

服务启动后访问 `http://127.0.0.1:5000` 打开内置 Web 控制台。

### 3. 触发电击（三种方式）

**方式一：Web 控制台**

浏览器打开 `http://127.0.0.1:5000`，点击按钮直接控制。

**方式二：curl / HTTP 请求（集成到任意脚本）**

```bash
# 触发一次电击（btn1 = 开机/加强）
curl -X POST http://127.0.0.1:5000/control \
  -H "Content-Type: application/json" \
  -d '{"command": "btn1"}'

# 运行预设宏 #0（自定义按键序列）
curl -X POST http://127.0.0.1:5000/macros/0/run
```

**方式三：Frida Game Hook（STG 游戏示例）**

```bash
# 编辑配置文件，填写目标进程名和特征码
vim testSever/Universal_STGB_HP_Hook_config.json

# 启动 Hook（游戏需已运行）
python testSever/Universal_STGB_HP_Hook.py
```

Hook 捕获到 HP 变化时，自动向 `/macros/0/run` 发送请求，触发电击。

---

## HTTP API 参考

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/devices` | 列出在线 ESP32 设备 |
| `POST` | `/control` | 发送原始指令，body: `{"command": "btn1"}` |
| `GET` | `/macros` | 获取所有宏定义 |
| `POST` | `/macros/{id}` | 定义宏，body: `{"steps": [{"action": "btn1", "duration": 100}, ...]}` |
| `POST` | `/macros/{id}/run` | 运行指定宏 |

**宏 step 字段说明：**

| action | 说明 |
|--------|------|
| `btn1` | 触发按键 1（通常为：开机 / 增强功率） |
| `btn2` | 触发按键 2（通常为：模式切换） |
| `btn3` | 触发按键 3（通常为：关机 / 降低功率） |
| `delay` | 等待，duration 单位毫秒 |

---

## 自定义触发端

只要能发 HTTP POST 请求即可接入，无需修改固件或服务层：

```python
import requests

# 触发宏 #0
requests.post("http://127.0.0.1:5000/macros/0/run")

# 或直接发按键
requests.post("http://127.0.0.1:5000/control", json={"command": "btn1"})
```

对接 Webhook 类工具（如 n8n、Home Assistant、OBS 脚本等）时，只需将 Webhook URL 指向 `http://127.0.0.1:5000/macros/{id}/run` 即可。

---

## 硬件电路说明

PCB 生产文件位于 `hardware/` 目录（嘉立创 EDA 工程 + Gerber）。

核心电路：ESP32-C3 GPIO → 220Ω 限流电阻 → PC817 光耦 → 理疗仪按键焊点。

三路光耦共用 GND，控制线通过 TRRS 3.5mm 接口引出，与理疗仪电气隔离。

---

## 