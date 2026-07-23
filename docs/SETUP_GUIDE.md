# 环境搭建与复现指南

本文档提供从零开始复现本项目的完整教学路径，分为三条独立的技术线，可按需单独完成，也可全部串联。

---

## 零门槛烧录（推荐新手）

如果你只想烧录固件、不想搭建编译环境，使用预编译 `.bin` + GUI 上位机或 `flash.py` 脚本即可，**只需要 Python 3**。

### 方式 A：使用 GUI 上位机（最简单）

从 GitHub Releases 下载 `Test_elec_Release_v1.0.zip`，解压后双击 `ESP32_Flasher_v1.0/ESP32_Flasher.exe`，**完全免安装**（已自带 esptool + PyInstaller 打包的 Python）：

```text
Test_elec_Release_v1.0/
└── ESP32_Flasher_v1.0/
    ├── ESP32_Flasher.exe                ← 双击运行
    ├── README.md
    └── firmware/                        ← 自带 4 段固件
        ├── bootloader.bin   (0x0)
        ├── partitions.bin   (0x8000)
        ├── boot_app0.bin    (0xe000)
        └── firmware.bin     (0x10000)
```

操作步骤：选串口 → 确认 `✓ firmware.bin · 4 段分段烧录` → 勾选「烧录前先擦除 Flash」→ 点「开始烧录」→ 等 ~5 秒完成。

### 方式 B：使用 flash.py 命令行

### 步骤

**1. 下载固件**

从仓库 [Releases](https://github.com/vihithr/ESP32-Shocker-Bridge/releases) 页面下载最新固件压缩包，解压到项目 `firmware/` 目录：

```
firmware/
├── bootloader.bin
├── partitions.bin
├── boot_app0.bin
└── firmware.bin
```

**2. 安装依赖（仅首次）**

```bash
pip install esptool pyserial
```

**3. 运行烧录脚本**

```bash
python flash.py
```

脚本会自动：
- 检测 ESP32-C3 串口
- 检查 / 自动安装 `esptool`
- 区分「完整烧录」（含 bootloader）和「仅更新固件」两种模式
- 烧录完成后提示配网步骤

> **如果自动检测串口失败**：打开设备管理器，找到 `USB JTAG/serial debug unit` 对应的 COM 口，手动输入。

> **如果烧录失败**：按住 ESP32-C3 上的 `BOOT` 键，再次运行脚本。

### 开发者：如何生成预编译固件

用 VS Code + PlatformIO 插件编译后，从以下路径收集文件：

```
# 编译产物
.pio/build/esp32-c3-supermini/firmware.bin      → firmware/firmware.bin
.pio/build/esp32-c3-supermini/bootloader.bin    → firmware/bootloader.bin
.pio/build/esp32-c3-supermini/partitions.bin    → firmware/partitions.bin

# boot_app0.bin（框架自带，路径因系统而异）
# Windows:
%USERPROFILE%\.platformio\packages\framework-arduinoespressif32@*/
    tools\partitions\boot_app0.bin              → firmware/boot_app0.bin
```

将这四个文件打包为 zip 发布到 GitHub Releases，用户即可通过 `flash.py` 一键烧录。

---

## 总览：三条技术线

```
线路 A：固件线
  └─ 安装 PlatformIO → 编译固件 → 烧录 ESP32 → 配网上线

线路 B：服务线
  └─ 安装 Python → 启动本地服务 → 通过 Web UI / curl 控制设备

线路 C：Hook 线（可选，依赖 A+B 已完成）
  └─ 安装 Frida → 修改 Hook 配置 → 挂载目标进程 → 游戏事件触发电击
```

最小可用路径：**A → B**（可以通过 Web UI 手动触发，无需游戏 Hook）

---

## 线路 A：固件烧录

### 前置条件
- Windows / macOS / Linux 均可
- ESP32-C3 SuperMini 通过 USB 连接电脑
- 驱动：ESP32-C3 SuperMini 使用内置 USB-JTAG，**无需额外安装驱动**（Windows 10/11 自动识别）

### A-1 安装 VS Code + PlatformIO 插件

1. 下载安装 [VS Code](https://code.visualstudio.com/)
2. 在扩展市场搜索 **PlatformIO IDE** 并安装
3. 等待 PlatformIO Core 自动安装完成（首次约 3-5 分钟）

> 也可使用纯命令行：`pip install platformio`

### A-2 打开工程

用 VS Code 打开本仓库根目录，PlatformIO 会自动识别 `platformio.ini`。

工程配置说明（`platformio.ini`）：

```ini
[env:esp32-c3-supermini]
platform = espressif32
board = esp32-c3-devkitm-1       ; ESP32-C3 SuperMini 兼容此 board
framework = arduino

build_flags =
    -D ARDUINO_USB_MODE=1          ; 启用内置 USB 串口
    -D ARDUINO_USB_CDC_ON_BOOT=1   ; 上电自动激活 CDC，无需外部串口芯片
```

### A-3 编译并烧录

```bash
# 命令行方式
pio run --target upload

# 或在 VS Code 底部工具栏点击 → 按钮（Upload）
```

烧录完成后，打开串口监视器（波特率 115200）查看启动日志：

```bash
pio device monitor --baud 115200
```

### A-4 WiFi 配网

**首次使用**（NVS 中无 WiFi 记录）：

1. 设备上电后串口会输出 `Starting AP config mode...`
2. 手机/电脑连接热点 `ESP32_Config`，密码 `12345678`
3. 浏览器访问 `http://192.168.4.1`，填入目标 WiFi SSID 和密码，点击保存
4. 设备自动重启并连入 WiFi，串口输出 `WiFi Connected!` 及分配的 IP

**后续使用**：设备会自动从 NVS 读取配置连接，无需重复配网。

**验证连接**：

```bash
# 在同一局域网的电脑上执行，替换为实际 IP
curl http://192.168.1.xxx/status
# 应返回 JSON：{"mode":"STA", "device_id":"SwitchBot_XXXXXX", ...}
```

---

## 线路 B：本地服务

### 前置条件
- Python 3.10 或以上
- 与 ESP32 处于同一局域网

### B-1 安装依赖

```bash
cd testSever
pip install -r requirements.txt
```

依赖清单：

| 库 | 用途 |
|----|------|
| `aiohttp` | 异步 HTTP 服务框架，提供 API 和 Web UI |
| `frida` | 游戏内存 Hook（线路 C 需要，线路 B 不强制） |
| `frida-tools` | Frida 辅助工具（GUI 依赖） |
| `requests` | Hook 脚本向本地服务发送 HTTP 请求 |
| `psutil` | Hook GUI 进程选择器 |
| `pyserial` | 烧录 GUI 串口监视器 / esptool |

> 如果只用线路 B（不做 Hook），可以只安装 `aiohttp`：`pip install aiohttp`

### B-2 启动服务

```bash
python testSever.py
```

启动成功输出：

```
Web Server: http://127.0.0.1:5000
Starting Persistent Monitor...
```

### B-3 验证服务

**方式一：浏览器**

打开 `http://127.0.0.1:5000`，页面顶部指示灯变绿表示设备在线。

**方式二：curl**

```bash
# 查询在线设备
curl http://127.0.0.1:5000/devices

# 触发按键 1
curl -X POST http://127.0.0.1:5000/control \
  -H "Content-Type: application/json" \
  -d "{\"command\": \"btn1\"}"
```

### B-4 配置宏脚本

宏是一组有序的按键+延时序列，用于实现「开机 → 等待 → 触发电击 → 关机」等组合动作。

通过 Web UI 配置（推荐）：
1. 访问 `http://127.0.0.1:5000`
2. 在「脚本宏配置」区域点击「快速添加」按钮构建步骤序列
3. 点击「保存」下发到设备（同时持久化到 `macros_storage.json`）

通过 API 配置（适合脚本集成）：

```bash
# 定义宏 #0：开机(btn1 100ms) → 等待500ms → 触发(btn1 100ms) → 关机(btn3 100ms)
curl -X POST http://127.0.0.1:5000/macros/0 \
  -H "Content-Type: application/json" \
  -d '{
    "steps": [
      {"action": "btn1", "duration": 100},
      {"action": "delay", "duration": 500},
      {"action": "btn1", "duration": 100},
      {"action": "btn3", "duration": 100}
    ]
  }'

# 运行宏 #0
curl -X POST http://127.0.0.1:5000/macros/0/run
```

**按键说明（根据实际理疗仪型号可能有差异）：**

| 指令 | 物理按键 | 典型效果 |
|------|----------|----------|
| `btn1` | GPIO 0 → 光耦 1 | 开机 / 增强功率 |
| `btn2` | GPIO 1 → 光耦 2 | 模式切换 |
| `btn3` | GPIO 2 → 光耦 3 | 关机 / 降低功率 |

---

## 线路 C：游戏 Hook（推荐用 GUI 上位机）

> 依赖线路 A + B 已完成，宏 #0 已配置好电击序列。

### 前置条件
- Windows（Frida 在 Windows 上对用户态进程注入最稳定）
- 目标游戏已运行
- 以**管理员权限**运行 HookGUI（Frida attach 需要较高权限）

### C-1 安装 Frida（仅源码运行需要）

如果你打算直接用 Release 里的 `HookGUI.exe`，**跳过此步**——EXE 已自带 Frida。

源码运行 / 重新打包时：

```bash
pip install frida frida-tools
```

验证安装：

```bash
frida --version
```

### C-2 获取目标进程特征码

本项目针对基于 **STGB（Shooting Game Builder）** 引擎制作的弹幕游戏，利用引擎硬编码的内存特征码进行通用 Hook。

`Universal_STGB_HP_Hook_config.json` 示例：

```json
{
  "settings": {
    "process_name": "GAME.EXE",         // 目标进程名（任务管理器中查看）
    "target_url": "http://127.0.0.1:5000/macros/0/run",
    "method": "POST",
    "timeout": 2,
    "debounce_seconds": 0.5             // 防抖：0.5s 内重复触发只计一次
  },
  "scan": {
    "pattern": "8B ?? 08 8B ?? ?? ?? ?? ?? 83 ?? FF ...",  // 内存特征码
    "offset_bytes": 9                   // 特征码匹配地址的偏移量
  }
}
```

仓库已附带 3 份示例配置，可直接加载使用：

| 配置文件 | 适用 |
|----------|------|
| `Universal_STGB_HP_Hook_config.json` | 通用 STGB 引擎（特征码扫描） |
| `th11_MISS_Hook_config.json` | 东方 STGB 引擎（Touhou 11 及类似作品） |
| `th06_EoSD_MISS_Hook_config.json` | 东方红魔乡 (Touhou 6) |

**如何为其他游戏获取特征码（使用 Cheat Engine）：**

1. 运行游戏，用 Cheat Engine 附加进程
2. 搜索「当前残机数」的内存地址
3. 对该地址右键 →「查看是什么改写了这个地址」
4. 触发 Miss，找到写入该地址的指令
5. 在 Cheat Engine 反汇编窗口中，选中该指令 → 右键 → 「在内存查看器中显示」
6. 向上下各取约 10 字节，将不确定的字节替换为 `??`，构成特征码
7. 记录「特征码起始位置」到「目标指令」的字节偏移，填入 `offset_bytes`

### C-3 启动 Hook GUI

从 Releases 下载 `HookGUI_v1.0/HookGUI.exe` 双击即可，**免安装**。

或从源码运行：

```bash
cd testSever
pip install psutil
python hook_gui.py
```

GUI 启动后：

1. 切到「Hook 配置」页签
2. 点「加载配置」选 `th06_EoSD_MISS_Hook_config.json`（或其他）
3. 点「选择…」选游戏进程（如 `th11.exe`）
4. 点「▶ 启动 Hook」

启动后日志类似：

```
==========================================
 Hook 已附加到进程: th11.exe
 模式: 特征码扫描 + 函数入口 Hook
 实际 Pattern: '89 15 18574A00'
 [JS] 特征匹配: 0x12345678
 [JS] 提取全局地址: 0x4E7450
 [JS] 校验通过: push ebp (函数头确认)
 [JS] Hook 已就绪 -> 0x12345670 (函数入口模式)
==========================================
```

游戏发生 Miss 时：

```
[EVENT] capture = 3
[NET] POST http://127.0.0.1:5000/macros/0/run OK  payload={'event': 'miss', 'value': 3}
```

同时本地服务会转发给 ESP32，触发宏脚本，光耦导通，理疗仪产生电击。

GUI 还支持：
- 嵌入式 testSever（同一 GUI 内启动 aiohttp + monitor_task）
- 宏可视化编辑（直接写入 ESP32 NVS）
- 进程列表选择（不用手敲 exe 名）

详细见 `testSever/HOOK_GUI_README.md`。

---

## 对接其他触发源

任何能发 HTTP 请求的程序都可以替代 Frida Hook 作为触发端，无需修改固件或服务层。

### Python 脚本

```python
import requests

def trigger_shock():
    requests.post("http://127.0.0.1:5000/macros/0/run", timeout=2)
```

### PowerShell

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:5000/macros/0/run" -Method POST
```

### AutoHotkey（按键触发）

```ahk
F9::
    Run, curl -X POST http://127.0.0.1:5000/macros/0/run
return
```

### n8n / Home Assistant / 其他 Webhook 平台

将 Webhook URL 配置为 `http://127.0.0.1:5000/macros/{id}/run`，方法 `POST`，无需 body。

---

## 常见问题

**Q: 设备无法被发现，`/devices` 返回空 `{}`**

- 确认 ESP32 和运行服务的 PC 在同一局域网
- 检查 ESP32 串口是否输出 `WiFi Connected`
- 尝试直接 `curl http://<ESP32-IP>/status` 确认 HTTP 服务可达
- mDNS 解析失败时，可在 `testSever.py` 顶部将 `TARGET_HOSTNAME` 改为 ESP32 的实际 IP

**Q: 烧录时提示找不到设备**

- ESP32-C3 SuperMini 首次烧录可能需要按住 `BOOT` 键再插 USB
- 确认设备管理器中出现 `USB JTAG/serial debug unit`
- 尝试降低烧录速度：`platformio.ini` 中 `upload_speed = 115200`

**Q: Frida attach 失败**

- 以管理员权限运行脚本
- 确认进程名与任务管理器中完全一致（包括大小写）
- 部分游戏有反调试保护，Frida 可能无法附加

**Q: 宏执行了但理疗仪没反应**

- 确认理疗仪电池有电，且处于开机状态
- 用万用表测量光耦输出端，确认导通时有通路
- 检查 TRRS 接口接线：Sleeve（最底端）= GND，其余三节对应三个按键

---

## 硬件连接参考

```
ESP32-C3          PC817 光耦            理疗仪按键

GPIO 0 ──220Ω──┐
               ├─ 阳极(1)  阴极(2) ── GND
               │  集电极(4)        ── 按键 A 端
GND ───────────┘  发射极(3)        ── 按键 B 端（共 GND）

GPIO 1 ──220Ω── [光耦2] ── 按键2
GPIO 2 ──220Ω── [光耦3] ── 按键3

所有按键的另一端共接 GND（通过 TRRS Sleeve 引出）
```

TRRS 接口接线：

| TRRS 节 | 连接 |
|---------|------|
| Tip（最顶端） | 按键 1 信号端 |
| Ring 1 | 按键 2 信号端 |
| Ring 2 | 按键 3 信号端 |
| Sleeve（最底端） | GND（共地） |

