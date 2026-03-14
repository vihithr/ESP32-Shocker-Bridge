#!/usr/bin/env python3
"""
ESP32 Shocker Bridge - One-click Flash Tool
用法: python flash.py
不需要安装 PlatformIO，只需 Python 3 + esptool
"""

import subprocess
import sys
import os
import glob

FIRMWARE_PATH = os.path.join(os.path.dirname(__file__), "firmware", "firmware.bin")

BOOTLOADER_PATH = os.path.join(os.path.dirname(__file__), "firmware", "bootloader.bin")
PARTITIONS_PATH = os.path.join(os.path.dirname(__file__), "firmware", "partitions.bin")
BOOT_APP_PATH   = os.path.join(os.path.dirname(__file__), "firmware", "boot_app0.bin")

# 是否有完整的 bootloader 套件
HAS_FULL_SUITE = all(os.path.exists(p) for p in [
    BOOTLOADER_PATH, PARTITIONS_PATH, BOOT_APP_PATH
])

def find_esptool():
    """优先用已安装的 esptool，找不到则提示安装"""
    try:
        subprocess.run([sys.executable, "-m", "esptool", "version"],
                       capture_output=True, check=True)
        return [sys.executable, "-m", "esptool"]
    except Exception:
        pass
    try:
        subprocess.run(["esptool.py", "version"], capture_output=True, check=True)
        return ["esptool.py"]
    except Exception:
        pass
    return None

def find_esp32_port():
    """自动探测 ESP32-C3 串口"""
    import serial.tools.list_ports
    candidates = []
    for port in serial.tools.list_ports.comports():
        desc = (port.description or "").lower()
        hwid = (port.hwid or "").lower()
        # ESP32-C3 内置 USB-JTAG 的 VID:PID
        if "303a:1001" in hwid or "usb jtag" in desc or "esp32" in desc:
            candidates.append(port.device)
    if len(candidates) == 1:
        return candidates[0]
    if candidates:
        print("检测到多个候选端口：")
        for i, p in enumerate(candidates):
            print(f"  [{i}] {p}")
        idx = input("请输入编号：").strip()
        try:
            return candidates[int(idx)]
        except Exception:
            return candidates[0]
    return None

def main():
    print("="*50)
    print(" ESP32 Shocker Bridge - Flash Tool")
    print("="*50)

    # 1. 检查固件文件
    if not os.path.exists(FIRMWARE_PATH):
        print(f"[错误] 找不到固件文件: {FIRMWARE_PATH}")
        print("请先从 Releases 页面下载固件，放入 firmware/ 目录。")
        input("按回车退出...")
        sys.exit(1)

    # 2. 检查 esptool
    esptool = find_esptool()
    if esptool is None:
        print("[提示] 未检测到 esptool，正在安装...")
        subprocess.run([sys.executable, "-m", "pip", "install", "esptool"], check=True)
        esptool = [sys.executable, "-m", "esptool"]

    # 3. 自动检测端口
    print("\n[*] 正在检测 ESP32 设备...")
    port = find_esp32_port()
    if port is None:
        port = input("未自动检测到设备，请手动输入串口号（如 COM3）：").strip()

    print(f"[*] 使用端口: {port}")
    print(f"[*] 固件: {FIRMWARE_PATH}")
    input("\n按回车开始烧录（确保 ESP32 已通过 USB 连接）...")

    # 4. 构建烧录命令
    base_args = [
        "--chip", "esp32c3",
        "--port", port,
        "--baud", "460800",
        "--before", "default_reset",
        "--after",  "hard_reset",
        "write_flash",
        "--flash_mode", "dio",
        "--flash_freq", "80m",
        "--flash_size", "4MB",
    ]

    if HAS_FULL_SUITE:
        # 完整烧录（含 bootloader，全新设备推荐）
        addr_args = [
            "0x0000",  BOOTLOADER_PATH,
            "0x8000",  PARTITIONS_PATH,
            "0xe000",  BOOT_APP_PATH,
            "0x10000", FIRMWARE_PATH,
        ]
        print("[*] 模式：完整烧录（bootloader + firmware）")
    else:
        # 仅烧录应用固件（已有 bootloader 的设备）
        addr_args = ["0x10000", FIRMWARE_PATH]
        print("[*] 模式：仅烧录应用固件")

    cmd = esptool + base_args + addr_args

    # 5. 执行烧录
    print("\n" + "-"*50)
    result = subprocess.run(cmd)
    print("-"*50)

    if result.returncode == 0:
        print("\n[完成] 烧录成功！设备将自动重启。")
        print("如果是首次使用，请连接热点 'ESP32_Config'（密码 12345678）完成 WiFi 配置。")
    else:
        print("\n[失败] 烧录出错，请检查：")
        print("  1. ESP32 是否已通过 USB 连接")
        print("  2. 串口号是否正确")
        print("  3. 尝试按住 BOOT 键再重新运行本脚本")

    input("\n按回车退出...")

if __name__ == "__main__":
    main()

