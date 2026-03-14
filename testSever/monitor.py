import frida
import sys
import requests
import time

# ================= 配置区域 =================
# 游戏进程名 (任务管理器里看到的名字)
PROCESS_NAME = "Cambria Sword 1.0.EXE" 

# 模块名称 (通常与进程名一致)
MODULE_NAME = "Cambria Sword 1.0.EXE"

# 图片中的偏移量: Cambria Sword 1.0.EXE + 1D26C
OFFSET = 0x1D26C 

# 目标 URL
TARGET_URL = "http://127.0.0.1:5000/macros/0/run"
# ===========================================

# 注入的 JavaScript 代码
# 修复点：使用 Process.findModuleByName 替代 Module.findBaseAddress
jscode = f"""
rpc.exports = {{}};

function startHook() {{
    console.log("[*] 开始查找模块: {MODULE_NAME}");

    // --- 修复开始 ---
    // 使用 Process.findModuleByName 获取模块对象
    var module = Process.findModuleByName("{MODULE_NAME}");
    
    if (!module) {{
        console.log("[-] 找不到模块: {MODULE_NAME}");
        console.log("[-] 当前加载的模块列表 (前10个):");
        Process.enumerateModules().slice(0, 10).forEach(function(m) {{
            console.log("    - " + m.name);
        }});
        return;
    }}
    
    var baseAddr = module.base;
    // --- 修复结束 ---

    // 2. 计算绝对地址 (基址 + 偏移)
    var targetAddr = baseAddr.add({OFFSET});
    
    console.log("[*] 模块基址: " + baseAddr);
    console.log("[*] 目标地址: " + targetAddr + " (Offset: 0x{OFFSET:X})");

    try {{
        // 3. 挂载拦截器
        Interceptor.attach(targetAddr, {{
            onEnter: function(args) {{
                // 图片中的指令是: mov [esi+00000270], eax
                // 此时 EAX 保存着要写入的值
                
                try {{
                    var val = this.context.eax.toInt32(); 
                    var ptr = this.context.esi;
                    
                    // 发送消息给 Python
                    send({{
                        type: "trigger_event",
                        value: val,
                        pointer: ptr.toString()
                    }});
                }} catch (e) {{
                    console.log("[!] 读取寄存器出错: " + e);
                }}
            }},
            onLeave: function(retval) {{
            }}
        }});
        console.log("[+] Hook 已成功启动！等待触发...");
    }} catch (e) {{
        console.log("[!] Interceptor.attach 失败: " + e);
    }}
}}

// 延迟 1 秒执行，确保模块已加载
setTimeout(startHook, 1000);
"""

# 防止请求过于频繁的简单限流
last_trigger_time = 0

def on_message(message, data):
    global last_trigger_time
    
    if message['type'] == 'send':
        payload = message['payload']
        
        if payload.get('type') == 'trigger_event':
            val = payload['value']
            print(f"[!] 监测到指令执行! EAX值: {val}")
            
            # --- 发送 HTTP POST 请求 ---
            current_time = time.time()
            if current_time - last_trigger_time > 0.5: # 0.5秒冷却
                try:
                    print(f"    -> 正在 POST 到 {TARGET_URL} ...")
                    requests.post(TARGET_URL)
                    last_trigger_time = current_time
                except Exception as e:
                    print(f"    [X] 请求失败: {e}")
            else:
                pass # print("    [~] 触发太快，已忽略")
                
    elif message['type'] == 'error':
        print(f"[Error] JS 错误: {message['stack']}")

def main():
    print(f"[*] 正在附加到进程: {PROCESS_NAME} ...")
    try:
        session = frida.attach(PROCESS_NAME)
    except Exception as e:
        print(f"[X] 无法附加: {e}")
        print("请确保游戏已运行，且脚本以管理员权限运行。")
        return

    script = session.create_script(jscode)
    script.on('message', on_message)
    script.load()
    
    print("[*] 脚本已加载，按 Ctrl+C 停止")
    sys.stdin.read()

if __name__ == '__main__':
    main()