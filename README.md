# ESP32 Shocker Bridge

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Platform: ESP32-C3](https://img.shields.io/badge/Platform-ESP32--C3-blue)](https://www.espressif.com/en/products/socs/esp32-c3)
[![Release v1.0](https://img.shields.io/badge/Release-v1.0-green)](https://github.com/vihithr/ESP32-Shocker-Bridge/releases)

**将任意可编程事件映射为现实电击反馈的本地网络执行器。**  
*A local-network HTTP executor that maps any programmable event to real-world haptic shock feedback.*

> ⚠️ **WARNING — READ BEFORE USE**
>
> 本项目通过 **GPIO + 光耦 + TENS 理疗仪** 的方式产生物理电击反馈。请严格遵守：
>
> 1. **必须** 使用光耦（PC817 等）进行电气隔离，**禁止** 把 ESP32 GPIO 直接连接人体或理疗仪按键焊点之外的电路
> 2. **务必使用经过认证的量产 TENS 理疗仪** 作为执行器，**禁止** 自制高压升压模块直接接触人体
> 3. **心脏病患者、孕妇、装有心脏起搏器者、有癫痫病史者绝对禁止使用**
> 4. 首次使用请将理疗仪调至**最低档位**并有人在旁监护
> 5. 错误使用可能导致**严重伤害甚至死亡**，开发者不对任何误用后果负责
>
> 使用本项目即表示你已阅读并同意 [LICENSE](LICENSE) 中的责任豁免条款。

---

## ✨ 特性

- 🎮 **Frida 通用游戏 Hook** — 基于 STGB 引擎特征码的通用方案，支持东方系列（EoSD、STGB 等）
- 🖱️ **双 GUI 上位机** — `HookGUI`（Frida 控制台）+ `ESP32_Flasher`（烧录上位机），免安装、零配置
- 🌐 **HTTP API 控制** — 任何能发 HTTP POST 的程序都可作为触发端（curl、n8n、Home Assistant、OBS 脚本……）
- 💡 **嵌入式服务器** — HookGUI 内置 testSever，**无需另开终端**
- 📝 **可视化宏编辑** — 多按键序列宏脚本，掉电持久化存储在 ESP32 NVS
- 🔌 **Web 配网** — 首次上电自动开 `ESP32_Config` 热点（密码 `12345678`），无需硬编码 WiFi 密码
- 🔋 **AP 自动回退** — NVS 无 WiFi 配置时自动切回热点模式，配网失败不"变砖"
- 🪛 **光耦隔离 + TRRS 接口** — 控制板与理疗仪完全电气隔离，可热插拔

---

## 🚀 30 秒上手

3 步完成整个链路：

| 步骤 | 操作 | 工具 |
|------|------|------|
| 1. 烧录固件 | 双击 `ESP32_Flasher.exe` → 选串口 → 点烧录 | [Release v1.0](https://github.com/vihithr/ESP32-Shocker-Bridge/releases) |
| 2. 配网 | ESP32 上电后连接 `ESP32_Config` 热点（密码 `12345678`）→ 浏览器开 `http://192.168.4.1` | 手机 / 电脑 |
| 3. 触发 | 双击 `HookGUI.exe` → 选游戏进程 → 点「▶ 启动 Hook」 | [Release v1.0](https://github.com/vihithr/ESP32-Shocker-Bridge/releases) |

> 详细步骤见 [📖 快速开始](#-快速开始) 或 [docs/SETUP_GUIDE.md](docs/SETUP_GUIDE.md)。

---

## 📦 预编译发布（Release v1.0）

下载 [`Test_elec_Release_v1.0.7z`](https://github.com/vihithr/ESP32-Shocker-Bridge/releases/tag/v1.0)（73 MB）后解压：

```
Test_elec_Release_v1.0/
├── ESP32_Flasher_v1.0/
│   ├── ESP32_Flasher.exe                ← ① 烧录上位机（双击即用）
│   ├── README.md
│   └── firmware/                        ← 自带 4 段预编译固件
│       ├── bootloader.bin   (0x0)
│       ├── partitions.bin   (0x8000)
│       ├── boot_app0.bin    (0xe000)
│       └── firmware.bin     (0x10000)
└── HookGUI_v1.0/
    ├── HookGUI.exe                      ← ② Hook 控制台（双击即用）
    ├── hook_engine.py
    └── Universal_STGB_HP_Hook_config.json
```

**SHA256 校验**（防止下载过程中损坏或被篡改）：

| 文件 | SHA256 |
|------|--------|
| `Test_elec_Release_v1.0.7z` | `1390cd864d005db684f151b03af3d52d7168dfff16a9962553699942308e3c6d` |

---

## 🏗️ 系统架构

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

## 📁 目录结构

```
.
├── src/
│   └── main.cpp                        # ESP32 固件主程序
├── testSever/
│   ├── testSever.py                    # 本地服务层（核心 HTTP API）
│   ├── hook_engine.py                  # Hook 引擎核心（GUI 共用）
│   ├── hook_gui.py                     # Hook GUI 上位机（推荐）
│   ├── esp32_flasher.py                # 烧录模块（多段固件 + esptool 封装）
│   ├── esp32_flash_gui.py              # 烧录 GUI 上位机（推荐）
│   ├── ESP32_Flasher.spec              # PyInstaller 烧录 EXE 配置
│   ├── HookGUI.spec                    # PyInstaller HookGUI EXE 配置
│   ├── create_package.py               # 一键打包脚本（开发者用）
│   ├── Universal_STGB_HP_Hook_config.json  # 示例配置：通用 STGB 引擎（特征码扫描）
│   ├── th11_MISS_Hook_config.json      # 示例配置：东方 STGB 引擎 (Touhou 11)
│   ├── th06_EoSD_MISS_Hook_config.json # 示例配置：东方红魔乡 (Touhou 6)
│   ├── HOOK_GUI_README.md              # Hook GUI 详细说明
│   ├── macros_storage.json             # 宏持久化存储（运行时生成，已 gitignore）
│   └── requirements.txt                # Python 依赖
├── hardware/
│   └── Gerber_New-Project_*.zip        # PCB 生产文件
├── docs/
│   ├── LOCAL_CONTROL_STACK.md         # 技术栈详细说明
│   └── SETUP_GUIDE.md                 # 环境搭建与复现指南
├── platformio.ini                      # PlatformIO 工程配置
├── LICENSE                             # MIT License
└── README.md
```

> **预编译 EXE**：`ESP32_Flasher.exe` 和 `HookGUI.exe` 由 [GitHub Releases](https://github.com/vihithr/ESP32-Shocker-Bridge/releases) 单独上传发布，本仓库只保留源码与配置。

---

## 🚀 快速开始

### 1. 烧录固件（推荐用 GUI）

从 [GitHub Releases](https://github.com/vihithr/ESP32-Shocker-Bridge/releases) 下载 `Test_elec_Release_v1.0.7z`，解压后双击 `ESP32_Flasher_v1.0/ESP32_Flasher.exe`：

1. 选串口（自动列出）
2. 确认固件 `✓ firmware.bin · 4 段分段烧录`
3. 勾选「烧录前先擦除 Flash」（默认勾选）
4. 点「开始烧录」→ 等 ~5 秒完成

如果想从源码编译固件（开发者）：

```bash
pip install platformio
pio run --target upload
```

首次上电若 NVS 中无 WiFi 配置，设备将自动开启 `ESP32_Config` 热点（密码 `12345678`），连接后访问 `http://192.168.4.1` 完成 WiFi 配置。

> **注意**：`src/main.cpp` 中 `DEFAULT_SSID` 和 `DEFAULT_PASSWORD` 已置空，请通过 AP 热点配网或自行修改后编译。

### 2. 启动 Hook GUI（推荐）

下载 Releases 中的 `HookGUI_v1.0/HookGUI.exe` 双击运行。GUI 内已嵌入 `testSever.py`，所以 **无需额外启动本地服务**。

也可从源码运行：

```bash
cd testSever
pip install -r requirements.txt

# Hook GUI（推荐，自带嵌入服务器）
python hook_gui.py

# 或者只跑本地服务（不用 GUI）
python testSever.py
```

GUI 内完成：选进程 → 加载配置 → 点「▶ 启动 Hook」。

### 3. 触发电击（GUI / HTTP 两种方式）

**方式一：Hook GUI 内的「服务器」标签页**

GUI 内已嵌入 testSever，点「打开网页」可直接弹出 Web 控制台（`http://127.0.0.1:5000`），点击按钮直接控制。

**方式二：HTTP / curl**

```bash
# 触发一次电击（btn1 = 开机/加强）
curl -X POST http://127.0.0.1:5000/control \
  -H "Content-Type: application/json" \
  -d '{"command": "btn1"}'

# 运行预设宏 #0
curl -X POST http://127.0.0.1:5000/macros/0/run
```

---

## 🖥️ GUI 上位机详情

两个开箱即用的 Windows GUI 上位机，源码在仓库中，EXE 由 GitHub Releases 单独发布。

### 1. ESP32_Flasher — 烧录上位机

把 `.bin` 烧录到 ESP32-C3，支持 **4 段分段烧录 + 烧录前自动擦除**，自带串口监视器：

| 功能 | 说明 |
|------|------|
| 自动检测串口 | 列出所有可用 COM 口 |
| 分段固件识别 | 自动发现 `bootloader / partitions / boot_app0 / firmware` |
| 烧录前擦除（推荐） | 解决旧 bootloader 残留导致的反复重启 |
| 串口监视器 | 115200 实时查看 ESP32 输出 |
| esptool 自动安装 | 检测到缺失时一键 `pip install esptool` |

### 2. HookGUI — Frida Hook 控制台

基于 Frida 的"通用内存 Hook + Webhook"图形界面，集成了 **进程选择 / 配置编辑 / 嵌入式 testSever / 宏编辑** 四大功能：

| 功能 | 说明 |
|------|------|
| 进程选择器 | 通过 `psutil` 列出系统进程，双击选择（不用手敲 exe 名） |
| 双模式 Hook | **特征码扫描** / **固定地址偏移** 切换 |
| 自动寄存器识别 | Hook 函数入口而非中途指令，避免 A2/A0 等隐含操作数指令误判 |
| Schema 校验 | 启动前自动校验配置字段 |
| 嵌入式 testSever | 同一 GUI 内启动 aiohttp + monitor_task，无需另开终端 |
| 宏编辑 | 可视化编辑 + 下发到 ESP32 NVS |

详细说明见 [testSever/HOOK_GUI_README.md](testSever/HOOK_GUI_README.md)。

### 从源码运行 GUI

```bash
cd testSever
pip install -r requirements.txt

# 启动烧录 GUI
python esp32_flash_gui.py

# 启动 Hook GUI
python hook_gui.py
```

### 重新打包 EXE

```bash
cd testSever

# 烧录 EXE（约 16 MB）
pyinstaller --onefile --windowed --name "ESP32_Flasher" \
  --add-data "firmware/bootloader.bin;firmware" \
  --add-data "firmware/partitions.bin;firmware" \
  --add-data "firmware/firmware.bin;firmware" \
  --add-data "firmware/boot_app0.bin;firmware" \
  --hidden-import "serial.tools.list_ports" \
  --collect-all "esptool" \
  esp32_flash_gui.py

# HookGUI EXE（约 57 MB）
pyinstaller --onefile --windowed --name "HookGUI" \
  --hidden-import "frida" --hidden-import "aiohttp.web" \
  --collect-all "frida" --collect-all "aiohttp" \
  hook_gui.py
```

或使用项目自带的 spec 文件：

```bash
pyinstaller ESP32_Flasher.spec
pyinstaller HookGUI.spec
```

---

## 📡 HTTP API 参考

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

## 🔌 自定义触发端

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

## 🛠️ 硬件电路说明

PCB 生产文件位于 `hardware/` 目录（嘉立创 EDA 工程 + Gerber）。

核心电路：ESP32-C3 GPIO → 220Ω 限流电阻 → PC817 光耦 → 理疗仪按键焊点。

三路光耦共用 GND，控制线通过 TRRS 3.5mm 接口引出，与理疗仪电气隔离。

### 硬件 BOM

| 器件 | 规格 | 数量 | 参考价格 |
|------|------|------|----------|
| 微控制器 | ESP32-C3 SuperMini | 1 | ~¥8 |
| 光耦 | PC817 × 3 | 1组 | ~¥1 |
| 限流电阻 | 220Ω × 3 | 1组 | <¥1 |
| 执行器 | TENS 廉价理疗仪（三按键款） | 1 | ~¥15 |
| 接口 | TRRS 3.5mm 插头/插座 | 1 | ~¥2 |
| PCB | 嘉立创打板（Gerber 见 hardware/） | 1 | ~¥5 |
| **合计** | | | **~¥30** |

---

## 🧰 项目起源

起源于一次弹幕射击游戏群里的玩笑：*"有没有 miss 一次就电你一下的电击项圈？"*  
从玩笑到原型机，从洞洞板焊接到自制 PCB，验证了「游戏事件 → 网络 API → 物理反馈」这条完整链路的可行性。

详细环境搭建步骤见 [docs/SETUP_GUIDE.md](docs/SETUP_GUIDE.md)。

---

## 🤝 贡献

欢迎 PR / Issue。由于项目涉及硬件安全，提交前请确认：

1. 任何修改都不得绕过光耦隔离
2. 默认配置不得包含任何个人 WiFi / 串口信息
3. 示例配置中的特征码需注明出处（来自哪个游戏 / 反汇编文件）

---

## 📄 License

MIT License — Copyright (c) 2026 vihi。详见 [LICENSE](LICENSE)。

本项目提供的预编译 EXE 中可能包含以下第三方库的运行时绑定：
- **Frida** (BSD-2-Clause) — [frida.re](https://frida.re)
- **esptool** (GPL-2.0) — [github.com/esphome/esptool](https://github.com/esphome/esptool)
- **aiohttp** (Apache-2.0) — [github.com/aio-libs/aiohttp](https://github.com/aio-libs/aiohttp)
- **psutil** (BSD-3-Clause) — [github.com/giampaolo/psutil](https://github.com/giampaolo/psutil)

---

## 🙏 致谢

- 嘉立创 EDA — PCB 打板
- ChatGPT / Cursor — 协助调试与文档
- 群友们的"再电狠一点"精神支持
