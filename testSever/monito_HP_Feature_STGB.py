import frida
import sys
import requests
import time

# ================= 配置 =================
PROCESS_NAME = "Cambria Sword 1.0.EXE"  # 换游戏时改这里
TARGET_URL = "http://127.0.0.1:5000/macros/0/run"
# =======================================

jscode = """
rpc.exports = {};

function scanAndHook() {
    console.log("[*] 正在智能扫描同引擎特征...");
    
    var module = Process.enumerateModules()[0];
    
    // ================= 超级特征码解释 =================
    // 原始: 8B 4E 08 8B 96 64 02 00 00 83 C0 FF 89 86 70 02 00 00 89 91 54 03 00 00 D9 05
    //
    // 8B ?? 08          -> mov r1, [r2 + 08]      (读取结构体头)
    // 8B ?? ?? ?? ?? ?? -> mov r3, [r2 + Offset]  (读取当前数值)
    // 83 ?? FF          -> add r3, -1             (核心：数值 -1) <--- 我们要Hook这里
    // 89 ?? ?? ?? ?? ?? -> mov [r2 + Offset], r3  (写回数值)
    // 89 ?? ?? ?? ?? ?? -> mov [r1 + Offset], r4  (写回关联数据)
    // D9 05             -> fld dword ptr [...]    (浮点数操作，极强的定位锚点)
    // ================================================
    
    var pattern = "8B ?? 08 8B ?? ?? ?? ?? ?? 83 ?? FF 89 ?? ?? ?? ?? ?? 89 ?? ?? ?? ?? ?? D9 05";
    
    Memory.scan(module.base, module.size, pattern, {
        onMatch: function(address, size) {
            // 计算 'add r3, -1' 的位置
            // 8B ?? 08 (3字节) + 8B ??..?? (6字节) = 9字节偏移
            var hookAddr = address.add(9);
            
            console.log("[+] 捕获到特征地址: " + address);
            console.log("[*] 定位核心指令: " + hookAddr);
            
            // 动态解析指令，判断是哪个寄存器
            try {
                var ins = Instruction.parse(hookAddr);
                console.log("[?] 解析指令: " + ins.toString());
                
                // ins.operands[0].value 应该是 'eax', 'ebx' 等
                var targetReg = ins.operands[0].value;
                console.log("[!] 自动识别目标寄存器: " + targetReg);
                
                attachHook(hookAddr, targetReg);
                return "stop"; // 找到一个就停，避免重复
            } catch (e) {
                console.log("[X] 指令解析失败，跳过: " + e);
            }
        },
        onError: function(reason) { console.log("[!] 扫描错误: " + reason); },
        onComplete: function() { console.log("[*] 扫描结束"); }
    });
}

function attachHook(targetAddr, regName) {
    Interceptor.attach(targetAddr, {
        onEnter: function(args) {
            // 使用 context[regName] 动态读取，不再死板地读 eax
            var val = this.context[regName].toInt32();
            
            // 过滤：只有当数值确实减少时才触发（可选）
            // 这里直接透传给 Python
            send({ type: "trigger", val: val });
        }
    });
    console.log("[+] 动态 Hook 已启动 (" + regName + ")");
}

setTimeout(scanAndHook, 1000);
"""

# 简单的防抖逻辑
last_trigger = 0

def on_message(message, data):
    global last_trigger
    if message['type'] == 'send':
        payload = message['payload']
        val = payload['val']-1
        print(f"[!] 游戏内数值变动 -> {val}")
        
        # 触发 HTTP
        now = time.time()
        if now - last_trigger > 0.5: # 0.5秒冷却
            try:
                print(f"   >>> 发送电击指令 ({TARGET_URL})")
                requests.post(TARGET_URL)
                last_trigger = now
            except:
                pass

def main():
    print(f"[*] 附加进程: {PROCESS_NAME}")
    try:
        session = frida.attach(PROCESS_NAME)
        script = session.create_script(jscode)
        script.on('message', on_message)
        script.load()
        sys.stdin.read()
    except Exception as e:
        print(e)

if __name__ == '__main__':
    main()