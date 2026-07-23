"""
Universal Hook Engine - GUI 后端核心
====================================
重写自 test_1.py，完全采用「特征码定位 → 提取全局变量地址 → 回溯函数入口 → 验证 → Hook」策略。
相比旧版直接 Hook 匹配指令，此方式：
  1. 不依赖指令寄存器解析，避免 A2/A0 等隐含操作数指令误判
  2. Hook 函数入口而非中途指令，完全避免游戏崩溃
  3. 从全局变量直接读值，数值更准确

使用方式（与旧版完全兼容）:
    from hook_engine import HookEngine
    engine = HookEngine(config_dict, log_callback=my_log_fn)
    engine.start()      # 同步阻塞（应放入线程）
    engine.stop()
"""

import sys
import os
import json
import time
import threading
import urllib.request
import urllib.error

try:
    import frida
except ImportError:
    frida = None


REQUIRED_SETTINGS = {"process_name", "target_url"}
REQUIRED_SCAN_FIELDS = {"pattern"}


def normalize_pattern(pattern: str) -> str:
    """
    Frida 的 gum_match_pattern_seal (gum/gummemory.c) 强制要求:
        "pattern 的最后一个 token 不能是 WILDCARD, 否则抛 invalid match pattern"
    常见踩坑: "a2 ?? ?? ?? ??" 末尾是 ?? → 直接报错。
    这里做一次轻量自动修复: 若末尾是 "??", 追加一个占位字节 " 00"
    (绝大多数 X86 32 位 PE 全局地址低字节不为 0, 因此追加 00 仍可匹配)。
    若末尾已是 EXACT 字节, 则保持原样。

    注意: 若用户配置的 pattern 真的就匹配 0x??, 请用 nibble 通配 "?0" 之类。
    """
    if not isinstance(pattern, str):
        return pattern
    s = pattern.strip()
    if s.endswith("??"):
        return s + " 00"
    return pattern


class ConfigError(ValueError):
    """配置校验失败时抛出。"""


def validate_config(cfg: dict) -> dict:
    """校验配置，补全默认值，返回标准化 cfg。"""
    if not isinstance(cfg, dict):
        raise ConfigError("配置根节点必须是 JSON 对象")

    settings = cfg.get("settings")
    if not isinstance(settings, dict):
        raise ConfigError("缺少 settings 节")
    missing = REQUIRED_SETTINGS - settings.keys()
    if missing:
        raise ConfigError(f"settings 缺少字段: {missing}")

    settings.setdefault("method", "POST")
    settings.setdefault("timeout", 2)
    settings.setdefault("debounce_seconds", 0.0)
    settings["method"] = str(settings["method"]).upper()

    scan = cfg.get("scan", {})
    if not isinstance(scan, dict):
        raise ConfigError("缺少 scan 节")
    has_offset = bool(scan.get("address_offset", "").strip())
    has_pattern = bool(scan.get("pattern", "").strip())
    if not has_offset and not has_pattern:
        raise ConfigError("scan.pattern 或 scan.address_offset 至少需要配置一项")
    if has_pattern:
        scan["pattern"] = normalize_pattern(scan["pattern"])
    scan.setdefault("offset_bytes", 0)
    scan.setdefault("func_entry_offset", -34)
    scan.setdefault("read_type", "int")

    cfg.setdefault("headers", {})
    cfg.setdefault("payload", {"event": "trigger", "value": "$VAL"})

    return cfg


def replace_placeholders(data, val):
    """递归替换 $VAL / $TIME 占位符。"""
    if isinstance(data, dict):
        return {k: replace_placeholders(v, val) for k, v in data.items()}
    if isinstance(data, list):
        return [replace_placeholders(i, val) for i in data]
    if isinstance(data, str):
        if data == "$VAL":
            return val
        if data == "$TIME":
            return int(time.time())
        return data
    return data


def send_request(url, payload, headers, method, timeout):
    try:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=body, method=method)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        with urllib.request.urlopen(req, timeout=timeout):
            return True, None
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code} {e.reason}"
    except Exception as e:
        return False, str(e)


class HookEngine:
    """单个 Hook 实例，可启动/停止。所有日志通过 log_cb 回调。"""

    def __init__(self, config: dict, log_cb=None):
        if frida is None:
            raise RuntimeError("frida 未安装，请先 pip install frida-tools")

        self.config = validate_config(config)
        self.log_cb = log_cb or (lambda lvl, msg: None)

        self.settings = self.config["settings"]
        self.scan = self.config["scan"]
        self._last_trigger = 0.0
        self._stop_event = threading.Event()
        self._session = None
        self._script = None
        self._thread = None
        self._stopped = threading.Event()
        self._stopped.set()

    # ---------- 公开 API ----------
    def start(self, in_thread: bool = True):
        """启动 Hook。in_thread=True 时非阻塞。"""
        if not self._stopped.is_set():
            self.log("WARN", "引擎已在运行")
            return
        self._stop_event.clear()
        self._stopped.clear()
        if in_thread:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        else:
            self._run()

    def stop(self, timeout: float = 2.0):
        self.log("INFO", "正在停止...")
        self._stop_event.set()
        try:
            if self._session is not None:
                self._session.detach()
        except Exception:
            pass
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self._stopped.set()
        self.log("INFO", "已停止")

    def is_running(self) -> bool:
        return not self._stopped.is_set()

    # ---------- 内部 ----------
    def log(self, level: str, msg: str):
        self.log_cb(level, msg)

    def _run(self):
        proc = self.settings["process_name"]
        try:
            self._session = frida.attach(proc)
            self.log("OK", f"已附加到进程: {proc}")
        except Exception as e:
            self.log("ERROR", f"无法附加到进程 {proc}: {e}")
            self._stopped.set()
            return

        js = self._build_js()
        # 打印 JS 里实际的 pattern 值，方便诊断
        import re
        m = re.search(r'var CONFIG_PATTERN = ([^;]+);', js)
        if m:
            self.log("INFO", f"[JS] Pattern 源码片段: {m.group(0)}")
        try:
            self._script = self._session.create_script(js)
            self._script.on("message", self._on_message)
            self._script.load()
        except Exception as e:
            self.log("ERROR", f"脚本加载失败: {e}")
            self._stopped.set()
            return

        self.log("INFO", "引擎运行中... (点击停止按钮结束)")

        while not self._stop_event.is_set():
            time.sleep(0.1)

        try:
            if self._script is not None:
                self._script.unload()
        except Exception:
            pass
        try:
            if self._session is not None:
                self._session.detach()
        except Exception:
            pass
        self._stopped.set()

    def _build_js(self) -> str:
        """生成 JS 代码：模式1=特征码扫描 / 模式2=固定地址偏移"""
        address_offset_str = self.scan.get("address_offset", "").strip()
        is_offset_mode = bool(address_offset_str)

        if is_offset_mode:
            addr_offset = int(address_offset_str, 0) if address_offset_str.lower().startswith("0x") else int(address_offset_str)
            offset_bytes = int(self.scan.get("offset_bytes", 0))
            read_type = self.scan.get("read_type", "int")
            optional_func_entry = self.scan.get("optional_func_entry", False)
            return f"""
var HOOK_ADDR_OFFSET = {addr_offset};
var READ_FROM_OFFSET = {offset_bytes};
var READ_TYPE = {json.dumps(read_type)};
var OPTIONAL_FUNC_ENTRY = {json.dumps(optional_func_entry)};

function sendLog(level, msg) {{
    send({{type: 'log', level: level, msg: msg}});
}}

function initHook() {{
    var module = Process.enumerateModules()[0];
    sendLog("INFO", "[JS] 主模块: " + module.name + " 基址: " + module.base);
    sendLog("INFO", "[JS] 模式: 固定地址偏移");

    var hookAddr = module.base.add(ptr(HOOK_ADDR_OFFSET));
    sendLog("INFO", "[JS] Hook 目标地址: " + hookAddr);

    var readAddr = hookAddr.add(ptr(READ_FROM_OFFSET));
    sendLog("INFO", "[JS] 读取全局变量偏移: +" + READ_FROM_OFFSET + " -> " + readAddr);

    var readFn;
    if (READ_TYPE === "byte" || READ_TYPE === "sbyte") readFn = function() {{ return readAddr.readU8(); }};
    else if (READ_TYPE === "short") readFn = function() {{ return readAddr.readShort(); }};
    else readFn = function() {{ return readAddr.readInt(); }};

    // 验证是否为函数入口 (push ebp)，防止 hook 到指令中间导致崩溃
    if (!OPTIONAL_FUNC_ENTRY) {{
        try {{
            var checkByte = hookAddr.readU8();
            if (checkByte === 0x55) {{
                sendLog("OK", "[JS] 校验通过: push ebp (函数入口确认)");
            }} else {{
                sendLog("WARN", "[JS] 目标不是函数入口 (Opcode: " + checkByte.toString(16) + ")，跳过 Hook");
                return;
            }}
        }} catch(e) {{
            sendLog("ERROR", "[JS] 读取验证失败: " + e);
            return;
        }}
    }} else {{
        sendLog("INFO", "[JS] 跳过函数入口校验 (optional_func_entry=true)");
    }}

    Interceptor.attach(hookAddr, {{
        onEnter: function(args) {{
            try {{
                var v = readFn();
                send({{type: 'trigger', val: v}});
            }} catch(e) {{}}
        }}
    }});
    sendLog("OK", "[JS] Hook 已就绪 -> " + hookAddr);
}}

setTimeout(initHook, 500);
"""
        # --- pattern 模式 ---
        pattern = self.scan["pattern"]
        func_entry_offset = int(self.scan.get("func_entry_offset", -0x22))
        read_type = self.scan.get("read_type", "int")

        return f"""
var CONFIG_PATTERN = {json.dumps(pattern)};
var FUNC_ENTRY_OFFSET = {func_entry_offset};
var READ_TYPE = {json.dumps(read_type)};

function sendLog(level, msg) {{
    send({{type: 'log', level: level, msg: msg}});
}}

function extractGlobalAddr(address) {{
    var op1 = address.readU8();

    // 0xA1 / 0xA0 / 0xA3 / 0xA2: opcode 紧跟 4 字节绝对地址 (隐含操作数)
    //   A1 [addr]  : mov eax, [addr]
    //   A0 [addr]  : mov al,  [addr]
    //   A3 [addr]  : mov [addr], eax
    //   A2 [addr]  : mov [addr], al    <- 本次目标指令
    if (op1 === 0xA1 || op1 === 0xA0 || op1 === 0xA3 || op1 === 0xA2) {{
        var kind = (op1 === 0xA0 || op1 === 0xA2) ? 'u8' : 'i32';
        return {{ addr: address.add(1).readPointer(), kind: kind }};
    }}

    // 0F BE / 0F BF: 两字节 opcode movsx r/m8 (ModRM 在 address+1，disp32 在 address+2)
    //   0F BE [ModRM] [disp32] -> movsx r32, byte ptr [addr]
    //   0F BF [ModRM] [disp32] -> movsx r32, word ptr [addr]
    if (op1 === 0x0F) {{
        var op2 = address.add(1).readU8();
        var modrm = address.add(1).readU8();
        // mod=00, r/m=101 -> [disp32]
        if ((modrm & 0xC7) === 0x05) {{
            var kind = (op2 === 0xBE) ? 'u8' : 'i16';
            return {{ addr: address.add(2).readPointer(), kind: kind }};
        }}
    }}

    // 0x8B / 0x89 / 0x88 / 0xC6: ModRM byte 在 address+1，disp32 在 +2
    if (op1 === 0x8B || op1 === 0x89 || op1 === 0x88 || op1 === 0xC6) {{
        var modrm = address.add(1).readU8();
        if ((modrm & 0xC7) === 0x05) {{
            return {{ addr: address.add(2).readPointer(), kind: (op1 === 0xC6 || op1 === 0x88) ? 'u8' : 'i32' }};
        }}
    }}
    return null;
}}

function makeReader(addr, kind) {{
    if (kind === 'u8')  return function() {{ return addr.readU8(); }};
    if (kind === 'i16') return function() {{ return addr.readShort(); }};
    return function() {{ return addr.readInt(); }};
}}

function attachSafeHook(funcEntry, readFn) {{
    Interceptor.attach(funcEntry, {{
        onEnter: function(args) {{
            try {{
                var v = readFn();
                send({{type: 'trigger', val: v}});
            }} catch(e) {{}}
        }}
    }});
    sendLog("OK", "[JS] Hook 已就绪 -> " + funcEntry + " (函数入口模式)");
}}

function initHook() {{
    var module = Process.enumerateModules()[0];
    sendLog("INFO", "[JS] 主模块: " + module.name + " 基址: " + module.base);

    if (!CONFIG_PATTERN) {{
        sendLog("ERROR", "[JS] 未配置 Pattern");
        return;
    }}

    sendLog("INFO", "[JS] 模式: 特征码扫描 + 函数入口 Hook");
    sendLog("INFO", "[JS] 实际 Pattern: '" + CONFIG_PATTERN + "'");

    Memory.scan(module.base, module.size, CONFIG_PATTERN, {{
        onError: function(reason) {{
            sendLog("ERROR", "[JS] Memory.scan 失败: " + reason);
        }},
        onMatch: function(address, size) {{
            sendLog("OK", "[JS] 特征匹配: " + address);

            var info = extractGlobalAddr(address);
            var readFn;
            var globalAddr;

            if (info !== null) {{
                globalAddr = info.addr;
                readFn = makeReader(globalAddr, info.kind);
            }} else {{
                // 兜底：address 本身就是全局变量
                globalAddr = address;
                readFn = makeReader(globalAddr, READ_TYPE === 'byte' ? 'u8' : 'i32');
            }}

            sendLog("OK", "[JS] 提取全局地址: " + globalAddr);

            var funcEntry = address.add(ptr(FUNC_ENTRY_OFFSET));
            sendLog("INFO", "[JS] 回溯函数入口: " + funcEntry + " (偏移 " + FUNC_ENTRY_OFFSET.toString(16) + ")");

            try {{
                var checkByte = funcEntry.readU8();
                if (checkByte === 0x55) {{
                    sendLog("OK", "[JS] 校验通过: push ebp (函数头确认)");
                }} else {{
                    sendLog("WARN", "[JS] 校验跳过: " + checkByte.toString(16) + " (非 push ebp，继续)");
                }}
            }} catch(e) {{
                sendLog("ERROR", "[JS] 验证读取失败: " + e);
                return 'stop';
            }}

            attachSafeHook(funcEntry, readFn);
            return 'stop';
        }},
        onComplete: function() {{
            sendLog("INFO", "[JS] 扫描结束");
        }}
    }});
}}

setTimeout(initHook, 500);
"""

    def _on_message(self, message, data):
        if message.get("type") == "send":
            payload = message.get("payload") or {}
            ptype = payload.get("type")
            if ptype == "log":
                self.log(payload.get("level", "INFO"), payload.get("msg", ""))
            elif ptype == "trigger":
                self._handle_trigger(payload)
        elif message.get("type") == "error":
            self.log("ERROR", f"[Frida] {message.get('stack', message)}")

    def _handle_trigger(self, payload):
        val = payload.get("val")
        self.log("EVENT", f"capture = {val}")

        debounce = float(self.settings.get("debounce_seconds", 0))
        now = time.time()
        if now - self._last_trigger < debounce:
            return

        custom = replace_placeholders(self.config.get("payload", {}), val)
        url = self.settings["target_url"]
        method = self.settings["method"]
        headers = self.config.get("headers", {})
        timeout = self.settings["timeout"]

        ok, err = send_request(url, custom, headers, method, timeout)
        if ok:
            self.log("NET", f"POST {url} OK  payload={custom}")
        else:
            self.log("ERROR", f"POST {url} FAIL: {err}")
        self._last_trigger = now
