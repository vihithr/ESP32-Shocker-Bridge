import asyncio
import json
import time
import socket
import os
from aiohttp import web

TCP_PORT = 12345
TARGET_HOSTNAME = "esp32-control.local"
POLL_INTERVAL = 5

# 全局状态
device_state = {
    "online": False,
    "info": {},       # 存储 get_status 的结果
    "last_seen": 0
}

# 宏系统配置（数量可通过环境变量 MAX_MACROS 调整，默认 10，需要与设备端一致）
MAX_MACROS = max(1, int(os.getenv("MAX_MACROS", "10")))
macros_cache = [[] for _ in range(MAX_MACROS)]  # 记录最近一次成功下发的宏脚本

# 宏持久化存储配置（Python 端本地文件，相当于划出一片固定大小的“区域”）
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MACRO_STORAGE_PATH = os.path.join(BASE_DIR, "macros_storage.json")
# 单个宏最大允许的字节码长度（不含 4 字节协议头），以及所有宏总字节上限
MAX_MACRO_PAYLOAD_BYTES = 256
MAX_TOTAL_MACRO_PAYLOAD_BYTES = 1024

# ================= 二进制宏协议常量 =================
# 帧格式: [CMD(1B)][ID(1B)][LEN(2B)][PAYLOAD...]
CMD_DEFINE = 0xA0
CMD_RUN    = 0xA1
CMD_QUERY  = 0xA2

# 操作码
OP_BTN1_MS = 0x01
OP_BTN2_MS = 0x02
OP_BTN3_MS = 0x03
OP_BTN1_L  = 0x11
OP_BTN2_L  = 0x12
OP_BTN3_L  = 0x13
OP_DELAY_MS = 0x20
OP_DELAY_L  = 0x21
OP_DELAY_S  = 0x22

# ================= 长连接管理类 =================
class PersistentClient:
    def __init__(self):
        self.reader = None
        self.writer = None
        self.lock = asyncio.Lock()  # 确保心跳和按钮指令不会撞车
        self.ip = None

    def _resolve_ip(self):
        """尝试解析域名，如果解析不到返回 None"""
        try:
            return socket.gethostbyname(TARGET_HOSTNAME)
        except:
            return None

    async def connect(self):
        """建立连接（如果未连接）"""
        if self.writer:
            return True # 已经连着

        # 1. 解析 IP (优先使用 IP 连接，比域名快且稳)
        if not self.ip:
            self.ip = self._resolve_ip()
            if not self.ip:
                print(f"Resolving {TARGET_HOSTNAME} failed...")
                return False
        
        # 2. 建立 TCP 连接
        try:
            print(f"Connecting to {self.ip}...")
            self.reader, self.writer = await asyncio.wait_for(
                asyncio.open_connection(self.ip, TCP_PORT), 
                timeout=3.0
            )
            print("Connected!")
            return True
        except Exception as e:
            print(f"Connection failed: {e}")
            self.ip = None # IP 可能变了，下次重解析
            await self._close_socket()
            return False

    async def send_packet(self, packet: bytes, expect_reply: bool = True):
        """
        发送原始二进制帧。
        - 用于: 宏定义 / 宏运行 等二进制协议
        - 若 expect_reply=True，则读取一行 JSON 回复（以 \\n 结束）
        """
        if not await self.connect():
            raise RuntimeError("Device offline")

        async with self.lock:
            try:
                self.writer.write(packet)
                await self.writer.drain()

                if not expect_reply:
                    return None

                # 设备端仍然用 println 输出 JSON，一行一条
                raw = await asyncio.wait_for(self.reader.readline(), timeout=2.0)
                if not raw:
                    raise ConnectionResetError("No reply from device")
                txt = raw.decode().strip()
                try:
                    return json.loads(txt)
                except Exception:
                    return {"raw": txt}
            except (BrokenPipeError, ConnectionResetError, asyncio.TimeoutError) as e:
                print(f"Packet socket error ({e}), reconnecting next time...")
                await self._close_socket()
                raise
            except Exception as e:
                print(f"Packet unexpected error: {e}")
                await self._close_socket()
                raise

    async def _close_socket(self):
        """安全关闭清理"""
        if self.writer:
            try:
                self.writer.close()
                await self.writer.wait_closed()
            except:
                pass
        self.reader = None
        self.writer = None

    async def send_command(self, cmd: str):
        """发送指令并等待回复（核心逻辑）"""
        # 文本行协议仍然保持不变：cmd + '\\n'
        data_to_send = (cmd.strip() + "\n").encode("utf-8")
        return await self.send_packet(data_to_send, expect_reply=True)

# 实例化全局客户端
client = PersistentClient()

# ================= 后台监控任务 =================
async def monitor_task():
    print("Starting Persistent Monitor...")
    while True:
        try:
            # 发送心跳
            resp = await client.send_command("get_status")
            
            # 更新状态
            if resp and "device_id" in resp:
                device_state["online"] = True
                device_state["info"] = resp
                device_state["last_seen"] = time.time()
                # print("Heartbeat OK")
        except Exception:
            # print("Heartbeat Fail")
            device_state["online"] = False
        
        await asyncio.sleep(POLL_INTERVAL)

# ================= HTTP API =================

async def api_list_devices(request):
    # 适配前端格式
    if device_state["online"]:
        info = device_state["info"]
        return web.json_response({
            info["device_id"]: {
                "friendly_name": info.get("friendly_name", "Device"),
                "ip": client.ip,
                "tcp_port": TCP_PORT,
                "device_type": info.get("device_type", "SwitchBot"),
                "last_seen": device_state["last_seen"]
            }
        })
    else:
        return web.json_response({})

async def api_control(request):
    data = await request.json()
    cmd = data.get("command")
    if not cmd: return web.json_response({"error": "No command"}, status=400)

    try:
        # 复用同一个长连接发送
        resp = await client.send_command(cmd)
        return web.json_response(resp)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def api_list_macros(request):
    """返回当前宏脚本；可选从设备刷新（内存中的 macros_cache 已支持持久化到本地文件）"""
    refresh_flag = request.rel_url.query.get("refresh", "0") == "1"
    if refresh_flag and device_state["online"]:
        for macro_id in range(MAX_MACROS):
            try:
                await fetch_macro_from_device(macro_id)
            except Exception as e:
                print(f"Refresh macro {macro_id} failed: {e}")
                break
    return web.json_response({
        "max_macros": MAX_MACROS,
        "macros": macros_cache
    })


async def api_save_macro(request):
    """保存/下发指定 ID 的宏脚本"""
    try:
        macro_id = int(request.match_info.get("macro_id", "-1"))
    except ValueError:
        return web.json_response({"error": "macro_id 无效"}, status=400)

    if macro_id < 0 or macro_id >= MAX_MACROS:
        return web.json_response({"error": "macro_id 超出范围"}, status=400)

    try:
        payload = await request.json()
        steps_raw = payload.get("steps", [])
        steps_clean = sanitize_macro_steps(steps_raw)

        # 先基于当前缓存构造一个“假想新状态”，用于检查容量限制
        new_all_macros = list(macros_cache)
        new_all_macros[macro_id] = steps_clean
        _enforce_macro_storage_limits(new_all_macros)

        # 容量检查通过后再真正生成数据包并下发到设备
        packet = build_define_packet_from_steps(macro_id, steps_clean)
        device_resp = await client.send_packet(packet, expect_reply=True)
    except ValueError as ve:
        return web.json_response({"error": str(ve)}, status=400)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

    macros_cache[macro_id] = steps_clean
    # 更新内存缓存后同步写入本地存储文件
    try:
        _save_macros_to_disk()
    except Exception:
        # 写盘失败只打印日志，不影响前端保存结果
        pass
    return web.json_response({
        "status": "ok",
        "macro_id": macro_id,
        "steps": steps_clean,
        "device": device_resp
    })


async def api_run_macro(request):
    """运行指定的宏（如果已在设备端定义）"""
    try:
        macro_id = int(request.match_info.get("macro_id", "-1"))
    except ValueError:
        return web.json_response({"error": "macro_id 无效"}, status=400)

    if macro_id < 0 or macro_id >= MAX_MACROS:
        return web.json_response({"error": "macro_id 超出范围"}, status=400)

    try:
        packet = build_run_packet(macro_id)
        device_resp = await client.send_packet(packet, expect_reply=True)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

    return web.json_response({
        "status": "ok",
        "macro_id": macro_id,
        "device": device_resp
    })

# ================= Python 宏构建工具（控制台使用） =================

class MacroBuilder:
    """
    用于在 Python 端构建二进制宏脚本:
    - press(btn, duration_ms)
    - delay(duration_ms)
    """
    def __init__(self):
        self.byte_stream = bytearray()

    def press(self, btn_idx: int, duration_ms: int):
        """添加按键动作（btn_idx=1/2/3），自动选择 1B/2B 时长编码"""
        if btn_idx not in (1, 2, 3):
            return self

        # 选择对应的短/长指令
        short_op = {1: OP_BTN1_MS, 2: OP_BTN2_MS, 3: OP_BTN3_MS}[btn_idx]
        long_op  = {1: OP_BTN1_L,  2: OP_BTN2_L,  3: OP_BTN3_L}[btn_idx]

        duration_ms = max(0, int(duration_ms))
        if duration_ms <= 255:
            self.byte_stream.append(short_op)
            self.byte_stream.append(duration_ms & 0xFF)
        else:
            self.byte_stream.append(long_op)
            self.byte_stream.extend(duration_ms.to_bytes(2, "big"))
        return self

    def delay(self, duration_ms: int):
        """添加延时，智能选择 ms/长 ms/秒 指令"""
        duration_ms = max(0, int(duration_ms))

        # 优先用秒，节省空间（且 duration_ms 为整秒）
        if duration_ms % 1000 == 0 and duration_ms // 1000 <= 255:
            sec = duration_ms // 1000
            self.byte_stream.append(OP_DELAY_S)
            self.byte_stream.append(sec & 0xFF)
        elif duration_ms <= 255:
            self.byte_stream.append(OP_DELAY_MS)
            self.byte_stream.append(duration_ms & 0xFF)
        else:
            self.byte_stream.append(OP_DELAY_L)
            self.byte_stream.extend(duration_ms.to_bytes(2, "big"))
        return self

    def build_define_packet(self, macro_id: int) -> bytes:
        """生成完整的宏定义数据帧（CMD_DEFINE）"""
        macro_id = int(macro_id) & 0xFF
        payload = bytes(self.byte_stream)
        length = len(payload)
        header = bytes([CMD_DEFINE, macro_id]) + length.to_bytes(2, "big")
        return header + payload

    def estimate_payload_size(self) -> int:
        """仅返回当前脚本 payload 的字节长度（不含 4 字节协议头）"""
        return len(self.byte_stream)

def build_run_packet(macro_id: int) -> bytes:
    """生成运行宏的数据帧（CMD_RUN），通常没有 payload。"""
    macro_id = int(macro_id) & 0xFF
    header = bytes([CMD_RUN, macro_id]) + (0).to_bytes(2, "big")
    return header


def _estimate_macro_payload_size_from_steps(steps) -> int:
    """根据 steps 估算宏脚本的 payload 字节数"""
    builder = MacroBuilder()
    for step in steps:
        action = step["action"]
        duration = step["duration"]
        if action == "delay":
            builder.delay(duration)
        elif action == "btn1":
            builder.press(1, duration)
        elif action == "btn2":
            builder.press(2, duration)
        elif action == "btn3":
            builder.press(3, duration)
        else:
            raise ValueError(f"未知动作: {action}")
    return builder.estimate_payload_size()


def _enforce_macro_storage_limits(all_macros_steps):
    """
    对一组宏步骤列表检查存储限制：
    - 单个宏 payload 字节数不能超过 MAX_MACRO_PAYLOAD_BYTES
    - 所有宏 payload 总和不能超过 MAX_TOTAL_MACRO_PAYLOAD_BYTES
    """
    total = 0
    for idx, steps in enumerate(all_macros_steps):
        if not steps:
            continue
        size = _estimate_macro_payload_size_from_steps(steps)
        if size > MAX_MACRO_PAYLOAD_BYTES:
            raise ValueError(f"宏 #{idx} 的脚本字节数 {size} 超过单个宏上限 {MAX_MACRO_PAYLOAD_BYTES}")
        total += size
    if total > MAX_TOTAL_MACRO_PAYLOAD_BYTES:
        raise ValueError(f"所有宏脚本总字节数 {total} 超过上限 {MAX_TOTAL_MACRO_PAYLOAD_BYTES}")


def _load_macros_from_disk():
    """启动时从本地文件加载宏脚本到内存缓存"""
    global macros_cache
    if not os.path.exists(MACRO_STORAGE_PATH):
        return
    try:
        with open(MACRO_STORAGE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        stored_macros = data.get("macros", [])
        new_cache = [[] for _ in range(MAX_MACROS)]
        for i in range(min(MAX_MACROS, len(stored_macros))):
            steps = stored_macros[i]
            # 复用前端输入同样的校验逻辑
            try:
                cleaned = sanitize_macro_steps(steps)
            except Exception:
                cleaned = []
            new_cache[i] = cleaned
        # 加载完成后再整体检查容量限制，异常则丢弃加载结果
        _enforce_macro_storage_limits(new_cache)
        macros_cache = new_cache
        print(f"Loaded macros from disk: {MACRO_STORAGE_PATH}")
    except Exception as e:
        print(f"Failed to load macros from disk: {e}")


def _save_macros_to_disk():
    """将当前内存中的宏脚本写入本地文件"""
    # 写入前再检查一次限制，避免手动修改内存后写入非法数据
    _enforce_macro_storage_limits(macros_cache)
    data = {
        "version": 1,
        "max_macros": MAX_MACROS,
        "macros": macros_cache,
    }
    try:
        with open(MACRO_STORAGE_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"Saved macros to disk: {MACRO_STORAGE_PATH}")
    except Exception as e:
        print(f"Failed to save macros to disk: {e}")


# 模块加载完成后，尝试从磁盘恢复之前保存的宏（如果有的话）
_load_macros_from_disk()


def sanitize_macro_steps(raw_steps):
    """
    将来自前端的 steps 数据校验并标准化：
    raw_steps: [{"action": "btn1", "duration": 100}, ...]
    """
    if not isinstance(raw_steps, list):
        raise ValueError("steps 必须是数组")
    sanitized = []
    for idx, step in enumerate(raw_steps):
        if not isinstance(step, dict):
            raise ValueError(f"第 {idx+1} 个步骤不是对象")
        action = step.get("action")
        duration = step.get("duration")
        if action is None or duration is None:
            raise ValueError(f"第 {idx+1} 个步骤缺少 action 或 duration")
        if not isinstance(action, str):
            raise ValueError(f"第 {idx+1} 个步骤 action 必须为字符串")
        action_key = action.strip().lower()
        if action_key in ("btn1", "button1", "1"):
            action_key = "btn1"
            btn_idx = 1
        elif action_key in ("btn2", "button2", "2"):
            action_key = "btn2"
            btn_idx = 2
        elif action_key in ("btn3", "button3", "3"):
            action_key = "btn3"
            btn_idx = 3
        elif action_key in ("delay", "wait", "d"):
            action_key = "delay"
            btn_idx = 0
        else:
            raise ValueError(f"第 {idx+1} 个步骤 action 无效: {action}")

        try:
            duration_ms = int(duration)
        except Exception:
            raise ValueError(f"第 {idx+1} 个步骤 duration 不是整数")
        if duration_ms <= 0 or duration_ms > 600000:
            raise ValueError(f"第 {idx+1} 个步骤 duration 范围无效(1~600000)")

        sanitized.append({
            "action": "delay" if btn_idx == 0 else f"btn{btn_idx}",
            "duration": duration_ms
        })
    if not sanitized:
        raise ValueError("至少需要一个步骤")
    return sanitized


def build_define_packet_from_steps(macro_id: int, steps):
    """校验步骤并生成定义宏的数据包"""
    builder = MacroBuilder()
    for step in steps:
        action = step["action"]
        duration = step["duration"]
        if action == "delay":
            builder.delay(duration)
        elif action == "btn1":
            builder.press(1, duration)
        elif action == "btn2":
            builder.press(2, duration)
        elif action == "btn3":
            builder.press(3, duration)
        else:
            raise ValueError(f"未知动作: {action}")

    if len(builder.byte_stream) == 0:
        raise ValueError("宏脚本为空")

    return builder.build_define_packet(macro_id)


def decode_macro_payload(payload: bytes):
    """将设备端返回的字节码解析为步骤列表"""
    steps = []
    pc = 0
    total = len(payload)
    while pc < total:
        opcode = payload[pc]
        pc += 1
        if opcode in (OP_BTN1_MS, OP_BTN2_MS, OP_BTN3_MS, OP_DELAY_MS, OP_DELAY_S):
            if pc >= total:
                break
            val = payload[pc]
            pc += 1
            duration = val * 1000 if opcode == OP_DELAY_S else val
        elif opcode in (OP_BTN1_L, OP_BTN2_L, OP_BTN3_L, OP_DELAY_L):
            if pc + 1 >= total:
                break
            high = payload[pc]
            low = payload[pc + 1]
            pc += 2
            duration = (high << 8) | low
        else:
            # 未知指令，停止解析
            break

        if opcode in (OP_BTN1_MS, OP_BTN1_L):
            action = "btn1"
        elif opcode in (OP_BTN2_MS, OP_BTN2_L):
            action = "btn2"
        elif opcode in (OP_BTN3_MS, OP_BTN3_L):
            action = "btn3"
        else:
            action = "delay"

        steps.append({"action": action, "duration": int(duration)})

    return steps


async def fetch_macro_from_device(macro_id: int):
    """向设备查询指定宏，并更新本地缓存"""
    packet = bytes([CMD_QUERY, macro_id & 0xFF]) + (0).to_bytes(2, "big")
    resp = await client.send_packet(packet, expect_reply=True)
    if not isinstance(resp, dict):
        return
    if resp.get("status") != "ok":
        return
    hex_data = resp.get("payload_hex", "")
    defined = resp.get("defined", False)
    if defined and hex_data:
        try:
            payload = bytes.fromhex(hex_data)
            macros_cache[macro_id] = decode_macro_payload(payload)
        except Exception:
            pass
    else:
        macros_cache[macro_id] = []

# ================= 标准启动代码 =================
INDEX_HTML = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>ESP32 长连接控制台</title>
  <style>
    :root {
      --bg: #0f172a;
      --card-bg: #111827;
      --accent: #3b82f6;
      --accent-soft: rgba(59,130,246,0.15);
      --accent-muted: #6b7280;
      --success: #22c55e;
      --danger: #ef4444;
      --text-main: #e5e7eb;
      --text-sub: #9ca3af;
      --border-soft: #1f2937;
      --shadow: 0 18px 45px rgba(15,23,42,0.9);
    }
    * {
      box-sizing: border-box;
    }
    body {
      margin: 0;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      background: radial-gradient(circle at top, #1e293b 0, #020617 55%, #020617 100%);
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: var(--text-main);
    }
    .card {
      width: 100%;
      max-width: 640px;
      background: linear-gradient(135deg, rgba(15,23,42,0.98), rgba(17,24,39,0.98));
      border-radius: 18px;
      padding: 22px 22px 20px;
      box-shadow: var(--shadow);
      border: 1px solid var(--border-soft);
      backdrop-filter: blur(18px);
    }
    .header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 10px;
    }
    .title-group h1 {
      margin: 0;
      font-size: 20px;
      letter-spacing: 0.03em;
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .title-tag {
      font-size: 11px;
      padding: 2px 8px;
      border-radius: 999px;
      background: var(--accent-soft);
      color: var(--accent);
      border: 1px solid rgba(37,99,235,0.4);
      text-transform: uppercase;
    }
    .subtitle {
      margin: 4px 0 0;
      font-size: 12px;
      color: var(--text-sub);
    }
    .chip-row {
      display: flex;
      align-items: center;
      gap: 8px;
      font-size: 11px;
      color: var(--text-sub);
    }
    .chip {
      padding: 2px 8px;
      border-radius: 999px;
      border: 1px solid var(--border-soft);
      background: rgba(15,23,42,0.85);
    }
    .indicator {
      display:inline-block;
      width:9px;
      height:9px;
      border-radius:50%;
      background: var(--danger);
      box-shadow: 0 0 0 0 rgba(239,68,68,0.7);
      transition: all 0.18s ease-out;
    }
    .indicator.online {
      background: var(--success);
      box-shadow: 0 0 0 6px rgba(34,197,94,0.15);
    }
    .main {
      margin-top: 12px;
      display: grid;
      grid-template-columns: minmax(0, 1.1fr) minmax(0, 0.9fr);
      gap: 12px;
    }
    @media (max-width: 640px) {
      .card { margin: 8px; padding: 18px 16px 16px; }
      .main { grid-template-columns: 1fr; }
    }
    .controls {
      border-radius: 12px;
      border: 1px solid var(--border-soft);
      background: radial-gradient(circle at top left, rgba(30,64,175,0.14), transparent 55%);
      padding: 10px 12px 12px;
    }
    .controls-label {
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.09em;
      color: var(--text-sub);
      margin-bottom: 6px;
    }
    .btn-row {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }
    button {
      padding: 8px 16px;
      font-size: 14px;
      cursor: pointer;
      background: var(--accent);
      color: #f9fafb;
      border: none;
      border-radius: 999px;
      font-weight: 500;
      display:flex;
      align-items:center;
      gap:6px;
      box-shadow: 0 6px 16px rgba(37,99,235,0.45);
      transition: transform 0.12s ease-out, box-shadow 0.12s ease-out, background 0.12s;
    }
    button span.icon {
      font-size: 14px;
      opacity: 0.9;
    }
    button.secondary {
      background: #374151;
      box-shadow: none;
      color: var(--text-main);
    }
    button:active {
      transform: translateY(1px);
      box-shadow: 0 3px 10px rgba(37,99,235,0.3);
    }
    button:disabled {
      background: #4b5563;
      box-shadow: none;
      cursor: not-allowed;
      opacity: 0.7;
    }
    .status-panel {
      border-radius: 12px;
      border: 1px solid var(--border-soft);
      background: rgba(15,23,42,0.9);
      padding: 10px 12px 10px;
      display:flex;
      flex-direction:column;
      gap:6px;
    }
    .status-panel-header {
      display:flex;
      justify-content:space-between;
      align-items:center;
      font-size:11px;
      color: var(--text-sub);
    }
    .status-tag {
      padding:1px 8px;
      border-radius:999px;
      border:1px solid rgba(148,163,184,0.6);
      font-size:10px;
      text-transform:uppercase;
      letter-spacing:0.08em;
    }
    .macro-panel {
      margin-top: 16px;
      border-radius: 14px;
      border: 1px solid var(--border-soft);
      padding: 14px;
      background: rgba(15,23,42,0.92);
      display:flex;
      flex-direction:column;
      gap: 12px;
    }
    .macro-panel-header {
      display:flex;
      justify-content:space-between;
      align-items:flex-start;
      gap:10px;
    }
    .macro-panel-header h3 {
      margin:0;
      font-size:15px;
      letter-spacing:0.04em;
    }
    .macro-panel-header p {
      margin:0;
      font-size:12px;
      color: var(--text-sub);
    }
    .macro-list {
      display:grid;
      grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
      gap:12px;
    }
    .macro-card {
      border-radius:12px;
      border:1px solid var(--border-soft);
      padding:10px;
      background: rgba(2,6,23,0.65);
      display:flex;
      flex-direction:column;
      gap:8px;
      overflow: visible;
    }
    .macro-quick-row {
      display:flex;
      flex-wrap:wrap;
      gap:8px;
      align-items:center;
      font-size:12px;
      color: var(--text-sub);
    }
    .pill-btn {
      padding:4px 10px;
      border-radius:999px;
      border:1px solid var(--border-soft);
      background: rgba(59,130,246,0.12);
      color: var(--text-main);
      cursor:pointer;
      font-size:12px;
    }
    .pill-btn:hover { border-color: var(--accent); }
    .macro-steps {
      display:flex;
      flex-direction:column;
      gap:8px;
      margin-top:4px;
      overflow: visible;
    }
    .step-row {
      display:grid;
      grid-template-columns: 28px minmax(0, 1fr) 120px 36px;
      gap:8px;
      align-items:center;
      padding:8px;
      border:1px solid var(--border-soft);
      border-radius:8px;
      background: rgba(15,23,42,0.65);
    }
    .drag-handle {
      cursor:grab;
      color: var(--text-sub);
      text-align:center;
      user-select:none;
    }
    .step-row select, .step-row input {
      width:100%;
      padding:6px 8px;
      border-radius:6px;
      border:1px solid #1e293b;
      background:#0b1224;
      color: var(--text-main);
      font-size:12px;
      overflow: visible;
    }
    .step-row input[type="number"] {
      -moz-appearance:textfield;
    }
    .ghost-btn {
      background: #1f2937;
      border:1px solid #1f2937;
      color: var(--text-main);
      padding:6px 8px;
      border-radius:8px;
      cursor:pointer;
      font-size:12px;
    }
    .ghost-btn:hover { border-color: var(--accent); }
    .macro-card-header {
      display:flex;
      align-items:center;
      justify-content:space-between;
      font-size:13px;
      color: var(--text-sub);
    }
    .macro-text {
      width:100%;
      min-height:130px;
      border-radius:8px;
      border:1px solid #1e293b;
      background:#020617;
      color: var(--text-main);
      font-family: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
      font-size:12px;
      padding:8px;
      resize:vertical;
    }
    .macro-actions {
      display:flex;
      gap:8px;
    }
    .small-btn {
      padding:6px 12px;
      font-size:12px;
    }
    #macro-status {
      min-height:18px;
      font-size:12px;
      color: var(--text-sub);
    }
    #macro-status.active {
      color: var(--accent);
    }
    #macro-status.error {
      color: var(--danger);
    }
    .macro-hint {
      font-size:11px;
      color: var(--text-sub);
      border-top:1px dashed var(--border-soft);
      padding-top:6px;
    }
    #device-info {
      font-size: 13px;
      color: var(--text-main);
    }
    #status {
      margin-top: 4px;
      font-family: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
      font-size: 11px;
      color: #d1d5db;
      white-space: pre-wrap;
      background: #020617;
      border-radius: 8px;
      padding: 8px 9px;
      border: 1px solid #1e293b;
      max-height: 140px;
      overflow:auto;
    }
  </style>
</head>
<body>
  <div class="card">
    <div class="header">
      <div class="title-group">
        <h1><span id="ind" class="indicator"></span>ESP32 Switch Controller</h1>
        <div class="subtitle">通过 <code>esp32-control.local:{TCP_PORT}</code> 建立长连接控制</div>
      </div>
      <div class="chip-row">
        <div class="chip">模式：本地局域网</div>
        <div class="chip">连接：持久 TCP</div>
      </div>
    </div>
    <div class="main">
      <div class="controls">
        <div class="controls-label">开关控制</div>
        <div class="btn-row">
          <button id="btn1" onclick="cmd('btn1')">
            <span class="icon">⏺</span><span>开关 1</span>
          </button>
          <button id="btn2" onclick="cmd('btn2')">
            <span class="icon">⏺</span><span>开关 2</span>
          </button>
          <button id="btn3" onclick="cmd('btn3')">
            <span class="icon">⏺</span><span>开关 3</span>
          </button>
          <button id="btnStatus" class="secondary" onclick="cmd('get_status')">
            <span class="icon">↻</span><span>刷新状态</span>
          </button>
        </div>
      </div>
      <div class="status-panel">
        <div class="status-panel-header">
          <span>设备状态</span>
          <span class="status-tag">最近心跳</span>
        </div>
        <div id="device-info">连接中...</div>
        <div id="status">Ready</div>
      </div>
    </div>
    <div class="macro-panel">
      <div class="macro-panel-header">
        <div>
          <h3>脚本宏配置</h3>
          <p>使用 JSON 数组描述每个步骤，action 可为 btn1/btn2/btn3/delay，duration 单位毫秒。</p>
        </div>
        <button class="secondary small-btn" onclick="loadMacros(true)">从设备读取</button>
      </div>
      <div id="macro-status"></div>
      <div id="macro-list" class="macro-list"></div>
      <div class="macro-hint">示例：[{ "action": "btn1", "duration": 120 }, { "action": "delay", "duration": 500 }, { "action": "btn2", "duration": 200 }]</div>
    </div>
  </div>

  <script>
    function setButtonsEnabled(enabled) {
      ['btn1','btn2','btn3','btnStatus'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.disabled = !enabled;
      });
    }

    async function cmd(c) {
      const s = document.getElementById('status');
      const start = Date.now();
      s.innerText = "发送中: " + c + " ...";
      try {
        const r = await fetch('/control', {
            method: 'POST', 
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({command: c})
        });
        const d = await r.json();
        const time = Date.now() - start;
        s.innerText = `[${time}ms] ` + JSON.stringify(d, null, 2);
      } catch(e) { s.innerText = "Error: " + e; }
    }

    setButtonsEnabled(false);
    setInterval(async () => {
      try {
        const r = await fetch('/devices');
        const d = await r.json();
        const ids = Object.keys(d);
        const ind = document.getElementById('ind');
        const info = document.getElementById('device-info');
        
        if (ids.length > 0) {
          const dev = d[ids[0]];
          ind.classList.add('online');
          info.innerText = `${dev.friendly_name || 'Device'}  ·  ${dev.ip || 'esp32-control.local'}  ·  在线`;
          setButtonsEnabled(true);
        } else {
          ind.classList.remove('online');
          info.innerText = "设备离线 / 连接断开";
          setButtonsEnabled(false);
        }
      } catch {
        setButtonsEnabled(false);
      }
    }, 2000);

    function setMacroStatus(message = '', isError = false) {
      const el = document.getElementById('macro-status');
      if (!el) return;
      el.textContent = message;
      el.classList.remove('active', 'error');
      if (!message) return;
      el.classList.add(isError ? 'error' : 'active');
    }

    // ========== 可视化拖拽式宏编辑 ==========
    const macroState = [];
    let dragInfo = null;

    function createStepElement(macroId, step, index) {
      const row = document.createElement('div');
      row.className = 'step-row';
      row.draggable = true;
      row.dataset.index = index;

      const drag = document.createElement('span');
      drag.className = 'drag-handle';
      drag.textContent = '☰';
      drag.addEventListener('dragstart', (e) => {
        dragInfo = { macroId, from: index };
        e.dataTransfer.effectAllowed = 'move';
      });
      drag.addEventListener('dragend', () => { dragInfo = null; });

      const select = document.createElement('select');
      ['btn1','btn2','btn3','delay'].forEach(opt => {
        const o = document.createElement('option');
        o.value = opt;
        o.textContent = opt === 'delay' ? '延时' : `按钮 ${opt.slice(-1)}`;
        if (step.action === opt) o.selected = true;
        select.appendChild(o);
      });
      select.addEventListener('change', (e) => {
        macroState[macroId][index].action = e.target.value;
      });

      const input = document.createElement('input');
      input.type = 'number';
      input.min = '1';
      input.placeholder = '时长(ms)';
      input.value = step.duration || 100;
      input.addEventListener('input', (e) => {
        const v = parseInt(e.target.value || '0', 10);
        macroState[macroId][index].duration = isNaN(v) ? 0 : v;
      });

      const del = document.createElement('button');
      del.className = 'ghost-btn';
      del.textContent = '✕';
      del.addEventListener('click', () => {
        macroState[macroId].splice(index, 1);
        renderMacro(macroId);
      });

      row.addEventListener('dragover', (e) => {
        e.preventDefault();
      });
      row.addEventListener('drop', (e) => {
        e.preventDefault();
        if (!dragInfo || dragInfo.macroId !== macroId) return;
        const from = dragInfo.from;
        const to = index;
        if (from === to) return;
        const arr = macroState[macroId];
        const [item] = arr.splice(from, 1);
        arr.splice(to, 0, item);
        dragInfo = null;
        renderMacro(macroId);
      });

      row.appendChild(drag);
      row.appendChild(select);
      row.appendChild(input);
      row.appendChild(del);
      return row;
    }

    function renderMacro(macroId) {
      const list = document.getElementById(`step-list-${macroId}`);
      if (!list) return;
      list.innerHTML = '';
      const steps = macroState[macroId] || [];
      if (!steps.length) {
        const hint = document.createElement('div');
        hint.className = 'macro-hint';
        hint.textContent = '拖拽调整顺序，点击上方按钮快速添加步骤';
        list.appendChild(hint);
        return;
      }
      steps.forEach((step, idx) => list.appendChild(createStepElement(macroId, step, idx)));
    }

    function renderMacroCards(total) {
      const container = document.getElementById('macro-list');
      if (!container) return;
      container.innerHTML = '';
      for (let i = 0; i < total; i++) {
        const card = document.createElement('div');
        card.className = 'macro-card';
        card.id = `macro-card-${i}`;
        card.innerHTML = `
          <div class="macro-card-header">
            <span>宏 #${i}</span>
            <button class="secondary small-btn" data-run>运行</button>
          </div>
          <div class="macro-quick-row">
            <span>快速添加：</span>
            <button class="pill-btn" data-add="btn1">按钮1</button>
            <button class="pill-btn" data-add="btn2">按钮2</button>
            <button class="pill-btn" data-add="btn3">按钮3</button>
            <button class="pill-btn" data-add="delay">延时</button>
          </div>
          <div class="macro-steps" id="step-list-${i}"></div>
          <div class="macro-actions">
            <button class="small-btn" data-add-custom>新增步骤</button>
            <button class="small-btn" data-save>保存</button>
          </div>
        `;
        container.appendChild(card);

        // 绑定事件
        card.querySelector('[data-run]').addEventListener('click', () => runMacroVisual(i));
        card.querySelectorAll('[data-add]').forEach(btn => {
          btn.addEventListener('click', () => {
            const action = btn.getAttribute('data-add');
            const defaultDuration = action === 'delay' ? 500 : 120;
            macroState[i].push({ action, duration: defaultDuration });
            renderMacro(i);
          });
        });
        card.querySelector('[data-add-custom]').addEventListener('click', () => {
          macroState[i].push({ action: 'btn1', duration: 100 });
          renderMacro(i);
        });
        card.querySelector('[data-save]').addEventListener('click', () => saveMacroVisual(i));
      }
    }

    async function loadMacros(refresh=false) {
      const container = document.getElementById('macro-list');
      if (!container) return;
      try {
        const resp = await fetch('/macros' + (refresh ? '?refresh=1' : ''));
        const data = await resp.json();
        const max = data.max_macros || 0;
        const macros = data.macros || [];
        const total = max || macros.length;
        // 初始化 state
        macroState.length = total;
        for (let i = 0; i < total; i++) {
          macroState[i] = Array.isArray(macros[i]) ? macros[i].map(s => ({...s})) : [];
        }
        renderMacroCards(total);
        for (let i = 0; i < total; i++) renderMacro(i);
        setMacroStatus('宏配置已加载');
      } catch (err) {
        console.error(err);
        setMacroStatus('加载宏失败: ' + err, true);
      }
    }

    function validateSteps(steps) {
      if (!Array.isArray(steps) || !steps.length) {
        throw new Error('至少需要一个步骤');
      }
      return steps.map((s, idx) => {
        const action = s.action;
        const duration = parseInt(s.duration, 10);
        if (!['btn1','btn2','btn3','delay'].includes(action)) {
          throw new Error(`第 ${idx+1} 步动作无效`);
        }
        if (!duration || duration <= 0) {
          throw new Error(`第 ${idx+1} 步时长无效`);
        }
        return { action, duration };
      });
    }

    async function saveMacroVisual(id) {
      try {
        const steps = validateSteps(macroState[id] || []);
        const resp = await fetch(`/macros/${id}`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ steps })
        });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error || resp.statusText);
        setMacroStatus(`宏 #${id} 保存成功`);
        // 重新加载，以便和设备侧同步
        await loadMacros(true);
      } catch (err) {
        setMacroStatus(`宏 #${id} 保存失败: ${err.message || err}`, true);
      }
    }

    async function runMacroVisual(id) {
      try {
        const resp = await fetch(`/macros/${id}/run`, { method: 'POST' });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error || resp.statusText);
        setMacroStatus(`宏 #${id} 运行指令已发送`);
      } catch (err) {
        setMacroStatus(`宏 #${id} 运行失败: ${err.message || err}`, true);
      }
    }

    loadMacros(true);
  </script>
</body>
</html>
"""

async def handle_index(request):
    return web.Response(text=INDEX_HTML, content_type="text/html")

async def start_server():
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/devices", api_list_devices)
    app.router.add_post("/control", api_control)
    app.router.add_get("/macros", api_list_macros)
    app.router.add_post(r"/macros/{macro_id:\d+}", api_save_macro)
    app.router.add_post(r"/macros/{macro_id:\d+}/run", api_run_macro)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 5000)
    await site.start()
    print("Web Server: http://127.0.0.1:5000")
    await asyncio.Future()


# ========== 下面是一些控制台使用示例（不会自动调用，只作为参考） ==========
#
# async def example_define_and_run():
#     """
#     示例：定义一个 ID=0 的宏：
#       - 按 Btn1 100ms
#       - 延时 1s
#       - 按 Btn2 500ms
#     然后运行该宏。
#     在 Python REPL 或你自己的脚本里调用：
#         asyncio.run(example_define_and_run())
#     """
#     global client
#     builder = MacroBuilder()
#     builder.press(1, 100).delay(1000).press(2, 500)
#     packet_define = builder.build_define_packet(0)
#     resp1 = await client.send_packet(packet_define, expect_reply=True)
#     print("Define macro resp:", resp1)
#
#     packet_run = build_run_packet(0)
#     resp2 = await client.send_packet(packet_run, expect_reply=True)
#     print("Run macro resp:", resp2)

async def main():
    if hasattr(asyncio, "WindowsSelectorEventLoopPolicy"):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        
    await asyncio.gather(
        monitor_task(),
        start_server()
    )

if __name__ == "__main__":
    asyncio.run(main())