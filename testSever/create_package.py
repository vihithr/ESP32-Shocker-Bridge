"""
创建 ESP32 固件分发包
====================
这个脚本会：
1. 编译固件（如果需要）
2. 复制固件到 firmware 目录
3. 创建可分发的压缩包

使用方法：
    python create_package.py

Author: Your Name
"""

import os
import sys
import shutil
import zipfile
import subprocess
import time

# 项目路径
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTSEVER_DIR = os.path.dirname(os.path.abspath(__file__))
FIRMWARE_DIR = os.path.join(TESTSEVER_DIR, "firmware")
BUILD_DIR = os.path.join(PROJECT_ROOT, ".pio", "build", "esp32-c3-supermini")


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}")


def check_pio():
    """检查 PlatformIO 是否可用"""
    try:
        result = subprocess.run(
            ["pio", "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0
    except (subprocess.SubprocessError, FileNotFoundError):
        return False


def compile_firmware():
    """编译固件"""
    log("=" * 50)
    log("开始编译固件...")

    try:
        result = subprocess.run(
            ["pio", "run"],
            cwd=PROJECT_ROOT,
            capture_output=False,  # 实时输出
            text=True,
        )

        if result.returncode == 0:
            log("编译成功！")
            return True
        else:
            log(f"编译失败，退出码: {result.returncode}")
            return False

    except Exception as e:
        log(f"编译异常: {e}")
        return False


def find_firmware_bin():
    """查找编译输出的固件文件"""
    # 可能的固件路径
    paths = [
        os.path.join(BUILD_DIR, "firmware.bin"),
        os.path.join(BUILD_DIR, "bootloader.bin"),
    ]

    # 查找第一个 .bin 文件
    if os.path.exists(BUILD_DIR):
        for f in os.listdir(BUILD_DIR):
            if f.endswith(".bin"):
                return os.path.join(BUILD_DIR, f)

    return None


def copy_firmware():
    """复制固件到 firmware 目录"""
    firmware_path = find_firmware_bin()

    if not firmware_path:
        log("未找到编译输出文件，请先编译！")
        return False

    dest_path = os.path.join(FIRMWARE_DIR, "firmware.bin")

    # 确保目录存在
    os.makedirs(FIRMWARE_DIR, exist_ok=True)

    # 复制文件
    shutil.copy2(firmware_path, dest_path)
    size = os.path.getsize(dest_path)
    log(f"固件已复制到: {dest_path} ({size / 1024:.1f} KB)")
    return True


def create_package():
    """创建分发包"""
    log("=" * 50)
    log("创建分发包...")

    # 确保固件存在
    firmware_path = os.path.join(FIRMWARE_DIR, "firmware.bin")
    if not os.path.exists(firmware_path):
        log("错误: firmware/firmware.bin 不存在！")
        return False

    # 包名
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    package_name = f"ESP32_Firmware_Package_{timestamp}"
    zip_path = os.path.join(TESTSEVER_DIR, f"{package_name}.zip")

    # 创建 ZIP 包
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        # 添加固件
        zf.write(firmware_path, "firmware/firmware.bin")
        log(f"添加: firmware/firmware.bin")

        # 添加烧录工具
        flasher_path = os.path.join(TESTSEVER_DIR, "esp32_flash_gui.py")
        if os.path.exists(flasher_path):
            zf.write(flasher_path, "esp32_flash_gui.py")
            log(f"添加: esp32_flash_gui.py")

        # 添加烧录模块
        flasher_module = os.path.join(TESTSEVER_DIR, "esp32_flasher.py")
        if os.path.exists(flasher_module):
            zf.write(flasher_module, "esp32_flasher.py")
            log(f"添加: esp32_flasher.py")

        # 添加批处理脚本
        batch_path = os.path.join(TESTSEVER_DIR, "烧录固件.bat")
        if os.path.exists(batch_path):
            zf.write(batch_path, "烧录固件.bat")
            log(f"添加: 烧录固件.bat")

        # 添加 README
        readme_path = os.path.join(FIRMWARE_DIR, "README.md")
        if os.path.exists(readme_path):
            zf.write(readme_path, "README.txt")
            log(f"添加: README.txt")

    log(f"分发包已创建: {zip_path}")
    return zip_path


def create_batch_script():
    """创建 Windows 批处理脚本"""
    batch_content = '''@echo off
chcp 65001 >nul
title ESP32 固件烧录工具
color 0A

echo ================================================
echo    ESP32 固件烧录工具
echo    支持: ESP32-C3 / ESP32-S3
echo ================================================
echo.

REM 检查 Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未安装 Python
    echo.
    echo 请先安装 Python 3.8 或更高版本
    echo 下载地址: https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)

REM 检查依赖
pip show pyserial >nul 2>&1
if errorlevel 1 (
    echo [安装] 正在安装 pyserial...
    pip install pyserial
)

REM 检查 esptool
pip show esptool >nul 2>&1
if errorlevel 1 (
    echo [安装] 正在安装 esptool...
    pip install esptool
)

REM 列出可用串口
echo.
echo [提示] 可用串口:
python -c "import serial.tools.list_ports; [print(f'  {p.device}') for p in serial.tools.list_ports.comports()]" 2>nul || echo "  无法检测到串口"
echo.

set /p PORT="请输入串口名称 (如 COM3): "
if "%PORT%"=="" (
    echo [错误] 串口不能为空
    pause
    exit /b 1
)

echo.
echo [信息] 波特率: 460800
echo [信息] Flash: 4MB
echo.
echo 开始烧录固件...
echo.

python -m esptool --chip esp32c3 --port %PORT% --baud 460800 write_flash --flash_size 4MB --flash_freq 80m 0x0 firmware/firmware.bin

echo.
echo.
if errorlevel 1 (
    echo [错误] 烧录失败！
    echo.
    echo 常见问题:
    echo   1. 串口被占用 - 关闭其他串口程序
    echo   2. 驱动未安装 - 安装 CH340/CP2102 驱动
    echo   3. USB 线不支持数据传输 - 更换数据线
) else (
    echo [成功] 烧录完成！
    echo.
    echo ESP32 即将重启...
)
echo.
pause
'''

    batch_path = os.path.join(TESTSEVER_DIR, "烧录固件.bat")
    with open(batch_path, 'w', encoding='utf-8') as f:
        f.write(batch_content)

    print(f"批处理脚本已创建: {batch_path}")


def main():
    print("=" * 50)
    print("ESP32 固件打包工具")
    print("=" * 50)
    print()

    action = input("请选择操作:\n"
                   "  1. 仅编译固件\n"
                   "  2. 编译并复制固件到 firmware 目录\n"
                   "  3. 创建分发包（包含固件和烧录工具）\n"
                   "  4. 完整流程：编译 + 创建分发包\n"
                   "\n请输入 (1-4)，直接回车选择 4: ").strip()

    if action == "1":
        if not check_pio():
            print("错误: PlatformIO 不可用！")
            return
        compile_firmware()

    elif action == "2":
        if not check_pio():
            print("错误: PlatformIO 不可用！")
            return
        if compile_firmware():
            copy_firmware()

    elif action == "3":
        create_batch_script()
        create_package()

    else:  # 4 或默认
        # 检查 PlatformIO
        if not check_pio():
            print("错误: PlatformIO 不可用！")
            print("请安装 PlatformIO: pip install platformio")
            return

        # 编译
        if not compile_firmware():
            return

        # 复制固件
        copy_firmware()

        # 创建批处理脚本
        create_batch_script()

        # 创建分发包
        create_package()

    print()
    print("=" * 50)
    print("完成！")
    print("=" * 50)


if __name__ == "__main__":
    main()
