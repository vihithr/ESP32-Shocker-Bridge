import frida
import sys
import time
import json
import os
# 替换 requests 为标准库 urllib
import urllib.request
import urllib.error

# ================= 工具函数 =================
def load_config():
    """读取同级目录下的 config.json"""
    if getattr(sys, 'frozen', False):
        application_path = os.path.dirname(sys.executable)
    else:
        application_path = os.path.dirname(os.path.abspath(__file__))

    config_path = os.path.join(application_path, 'Universal_STGB_HP_Hook_config.json')

    if not os.path.exists(config_path):
        print(f"[Error] 配置文件未找到: {config_path}")
        sys.exit(1)

    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f"[Error] 配置错误: {e}")
        sys.exit(1)

def replace_placeholders(data, val):
    """递归替换 payload 中的占位符"""
    if isinstance(data, dict):
        return {k: replace_placeholders(v, val) for k, v in data.items()}
    elif isinstance(data, list):
        return [replace_placeholders(i, val) for i in data]
    elif isinstance(data, str):
        if data == "$VAL": return val
        if data == "$TIME": return int(time.time())
        return data
    else:
        return data

def send_request_urllib(url, payload, headers, method, timeout):
    """使用原生 urllib 发送请求 (不依赖 requests)"""
    try:
        # 1. 处理数据: JSON 转 bytes
        json_data = json.dumps(payload).encode('utf-8')
        
        # 2. 构建 Request 对象
        req = urllib.request.Request(url, data=json_data, method=method.upper())
        
        # 3. 添加 Headers
        for k, v in headers.items():
            req.add_header(k, v)
        
        # 4. 发送请求
        with urllib.request.urlopen(req, timeout=timeout) as response:
            # 只要没抛出异常，就是成功 (200 OK)
            return True
            
    except urllib.error.URLError as e:
        print(f"   [X] 连接失败: {e}")
    except Exception as e:
        print(f"   [X] 发送错误: {e}")
    return False

# ================= 初始化 =================
print("[*] 读取配置...")
CONFIG = load_config()

PROCESS_NAME = CONFIG['settings']['process_name']
TARGET_URL = CONFIG['settings']['target_url']
DEBOUNCE = CONFIG['settings']['debounce_seconds']
PATTERN = CONFIG['scan']['pattern']
OFFSET = CONFIG['scan']['offset_bytes']
METHOD = CONFIG['settings'].get('method', 'POST')
TIMEOUT = CONFIG['settings'].get('timeout', 2)

# Frida JS (保持不变)
jscode = f"""
rpc.exports = {{}};
function scanAndHook() {{
    var module = Process.enumerateModules()[0];
    var pattern = "{PATTERN}";
    Memory.scan(module.base, module.size, pattern, {{
        onMatch: function(address, size) {{
            var hookAddr = address.add({OFFSET});
            console.log("[+] 特征匹配成功: " + address);
            console.log("[*] Hook 注入点: " + hookAddr);
            try {{
                var ins = Instruction.parse(hookAddr);
                var targetReg = ins.operands[0].value;
                attachHook(hookAddr, targetReg);
                return "stop"; 
            }} catch (e) {{}}
        }},
        onComplete: function() {{ }}
    }});
}}
function attachHook(targetAddr, regName) {{
    Interceptor.attach(targetAddr, {{
        onEnter: function(args) {{
            try {{
                var val = this.context[regName].toInt32();
                send({{ type: "trigger", val: val }});
            }} catch (e) {{}}
        }}
    }});
    console.log("[+] Hook Ready (" + regName + ")");
}}
setTimeout(scanAndHook, 1000);
"""

last_trigger = 0

def on_message(message, data):
    global last_trigger
    if message['type'] == 'send':
        payload = message['payload']
        val = payload['val']
        # 这里可以做简单的数值处理，比如减1
        val = val - 1
        
        print(f"[Event] Val: {val}")
        
        now = time.time()
        if now - last_trigger > DEBOUNCE:
            custom_payload = replace_placeholders(CONFIG['payload'], val)
            headers = CONFIG.get('headers', {})
            
            print(f"   >>> Post to {TARGET_URL}")
            
            # 调用 urllib 发送
            send_request_urllib(TARGET_URL, custom_payload, headers, METHOD, TIMEOUT)
            
            last_trigger = now

def main():
    print(f"--- Universal Hook Lite ---")
    try:
        session = frida.attach(PROCESS_NAME)
    except Exception as e:
        print(f"[!] 未找到进程: {PROCESS_NAME}")
        time.sleep(3)
        sys.exit(1)

    script = session.create_script(jscode)
    script.on('message', on_message)
    script.load()
    print("[*] Running...")
    sys.stdin.read()

if __name__ == '__main__':
    main()