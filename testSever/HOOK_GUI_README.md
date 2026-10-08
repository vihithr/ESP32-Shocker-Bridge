# Universal Hook GUI

基于 Frida 的"通用内存 Hook + Webhook"工具的图形界面版本，
可选择 **配置类型**（特征码扫描 / 固定地址偏移）。

## 文件清单

| 文件 | 作用 |
|---|---|
| `hook_engine.py` | 核心 Hook 引擎（无 GUI 依赖，可在任意脚本里 `import`） |
| `hook_gui.py` | Tkinter 图形界面 |
| `Universal_STGB_HP_Hook_config.json` | 默认配置（GUI 启动时会自动加载） |

## 安装

```bash
pip install frida frida-tools psutil requests
```

Tkinter 是 Python 自带，Windows 一般已包含。

## 启动

```bash
python hook_gui.py
```

## GUI 操作流程

1. **进程名** —— 可点击「选择...」打开进程列表（双击选择），或手动输入 `th15.exe` 等
2. **配置类型** —— 二选一：
   - **特征码扫描**：填写 Pattern（如 `A1 ?? ?? ?? ?? 48 A3 ?? ?? ?? ?? 78`）+ 字节偏移
   - **固定地址偏移**：填写十六进制地址偏移（如 `0x12345`），将自动按"主模块基址 + 偏移"定位
3. **目标 URL / 方法 / 超时 / 防抖** —— HTTP 推送相关
4. **Payload (JSON)** —— 支持 `$VAL`（捕获到的寄存器值）和 `$TIME`（Unix 秒）占位符
5. **测试发送** —— 不启动 Frida，直接发一次 payload 验证 webhook 是否通畅
6. **▶ 启动 Hook** —— 附加到目标进程，自动识别指令寄存器，每次触发都会发送到 webhook
7. **■ 停止** —— 卸载脚本并从进程分离

## 配置加载机制

- **完全兼容** `Universal_STGB_HP_Hook_config.json`：GUI 启动时会自动加载
- **额外增强**：
  - schema 校验（保存/启动前自动检查字段）
  - 进程选择器（不用手敲 exe 名）
  - 测试发送按钮
  - 实时日志带颜色等级
  - Payload 编辑器（JSON 格式）
  - GUI 关闭 / 停止时自动卸载脚本

## 配置字段参考

```json
{
  "settings": {
    "process_name":      "th15.exe",
    "target_url":        "http://127.0.0.1:5000/macros/0/run",
    "method":            "POST",       // 或 "GET"
    "timeout":           2,            // 秒
    "debounce_seconds":  1.0           // 两次发送最小间隔
  },
  "scan": {
    "address_offset":    "",           // 非空时启用"固定地址偏移"模式
    "pattern":           "A1 ?? ?? ?? ?? 48 A3 ?? ?? ?? ?? 78",
    "offset_bytes":      0             // 特征匹配后向前/后偏移字节再 Hook
  },
  "headers": { "Content-Type": "application/json" },
  "payload": { "event": "miss", "hp": "$VAL" }
}
```

模式判定规则：
- `address_offset` 非空 → 固定地址偏移
- 否则看 `pattern` → 特征码扫描
- 两者都空 → 报错

## 程序化使用

```python
from hook_engine import HookEngine, validate_config

cfg = {
    "settings": {"process_name": "th15.exe",
                 "target_url":   "http://127.0.0.1:5000/x",
                 "method":       "POST",
                 "timeout":      2,
                 "debounce_seconds": 0.5},
    "scan":     {"pattern": "A1 ?? ?? ?? ?? 48 A3 ?? ?? ?? ?? 78"},
    "payload":  {"event": "miss", "hp": "$VAL"},
}
validate_config(cfg)  # 校验；不合法抛 ConfigError

def my_log(level, msg):
    print(f"[{level}] {msg}")

engine = HookEngine(cfg, log_cb=my_log)
engine.start(in_thread=True)
# ... 之后随时 ...
engine.stop()
```