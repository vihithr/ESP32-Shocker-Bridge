import frida
import sys
import requests
import time
import json
import os

# ================= 工具函数 =================
def load_config():
    """读取同级目录下的 config.json"""
    # 获取当前可执行文件所在的路径（兼容 PyInstaller 打包后的情况）
    if getattr(sys, 'frozen', False):
        application_path = os.path.dirname(sys.executable)
    else:
        application_path = os.path.dirname(os.path.abspath(__file__))

    config_path = os.path.join(application_path, 'Universal_STGB_HP_Hook_config.json')

    if not os.path.exists(config_path):
        print(f"[Error] 配置文件未找到: {config_path}")
        print("请确保 config.json 与本程序在同一目录下。")
        input("按任意键退出...")
        sys.exit(1)

    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f"[Error] 配置文件格式错误: {e}")
        input("按任意键退出...")
        sys.exit(1)

def replace_placeholders(data, val):
    """递归替换 payload 中的占位符 ($VAL, $TIME)"""
    if isinstance(data, dict):
        return {k: replace_placeholders(v, val) for k, v in data.items()}
    elif isinstance(data, list):
        return [replace_placeholders(i, val) for i in data]
    elif isinstance(data, str):
        if data == "$VAL":
            return val
        if data == "$TIME":
            return int(time.time())
        return data
    else:
        return data

# ================= 初始化 =================
print("[*] 正在读取配置...")
CONFIG = load_config()

PROCESS_NAME = CONFIG['settings']['process_name']
TARGET_URL = CONFIG['settings']['target_url']
DEBOUNCE = CONFIG['settings']['debounce_seconds']
PATTERN = CONFIG['scan']['pattern']
OFFSET = CONFIG['scan']['offset_bytes']

# Frida JS 模板
jscode = f"""
rpc.exports = {{}};

function scanAndHook() {{
    console.log("[*] 扫描模块特征码...");
    
    // 获取主模块
    var module = Process.enumerateModules()[0];
    var pattern = "{PATTERN}";
    
    Memory.scan(module.base, module.size, pattern, {{
        onMatch: function(address, size) {{
            // 动态计算偏移
            var hookAddr = address.add({OFFSET});
            
            console.log("[+] 特征匹配成功: " + address);
            console.log("[*] Hook 注入点: " + hookAddr);
            
            try {{
                var ins = Instruction.parse(hookAddr);
                // 自动识别操作数寄存器 (eax, ebx, etc.)
                var targetReg = ins.operands[0].value;
                console.log("[!] 自动识别寄存器: " + targetReg);
                
                attachHook(hookAddr, targetReg);
                return "stop"; 
            }} catch (e) {{
                console.log("[X] 指令解析失败: " + e);
            }}
        }},
        onError: function(reason) {{ console.log("[!] 扫描错误: " + reason); }},
        onComplete: function() {{ console.log("[*] 扫描结束"); }}
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
    console.log("[+] 监听服务已就绪 (" + regName + ")");
}}

setTimeout(scanAndHook, 1000);
"""

last_trigger = 0

def on_message(message, data):
    global last_trigger
    
    if message['type'] == 'send':
        payload = message['payload']
        # 原始值
        raw_val = payload['val']
        # 可以在这里做简单的预处理，比如 -1
        processed_val = raw_val - 1 

        print(f"[Event] Capture Value: {processed_val}")
        
        now = time.time()
        if now - last_trigger > DEBOUNCE:
            try:
                # 构建自定义 Payload
                custom_payload = replace_placeholders(CONFIG['payload'], processed_val)
                headers = CONFIG.get('headers', {})
                
                print(f"   >>> Triggering Webhook: {TARGET_URL}")
                # print(f"   >>> Payload: {custom_payload}") # 调试用

                if CONFIG['settings']['method'].upper() == "POST":
                    requests.post(TARGET_URL, json=custom_payload, headers=headers, timeout=CONFIG['settings']['timeout'])
                else:
                    requests.get(TARGET_URL, params=custom_payload, headers=headers, timeout=CONFIG['settings']['timeout'])
                
                last_trigger = now
            except Exception as e:
                print(f"   [X] Network Error: {e}")

def main():
    print(f"==========================================")
    print(f" Universal Game Trigger v1.0")
    print(f" Target: {PROCESS_NAME}")
    print(f"==========================================\n")
    
    try:
        session = frida.attach(PROCESS_NAME)
        print(f"[*] 进程挂载成功")
    except Exception as e:
        print(f"[!] 无法找到进程: {PROCESS_NAME}")
        print("请确保游戏已经运行。")
        input("按任意键退出...")
        sys.exit(1)

    script = session.create_script(jscode)
    script.on('message', on_message)
    script.load()
    
    print("[*] 引擎运行中... (Ctrl+C 停止)")
    try:
        sys.stdin.read()
    except KeyboardInterrupt:
        print("\n[*] 正在退出...")

if __name__ == '__main__':
    main()