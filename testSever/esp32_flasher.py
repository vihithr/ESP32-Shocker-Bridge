"""
ESP32 Flasher Module (预编译固件模式)
======================================
提供 ESP32 固件的烧录功能，支持两种模式：
1. 【推荐】预编译模式：使用打包的 .bin 文件，无需 PlatformIO
2. 开发模式：使用 PlatformIO 编译（需要安装 PlatformIO）

打包 EXE 给用户时：
- 将编译好的固件放在 firmware/ 目录下
- 包含 esptool.py 或让用户安装 pip install esptool

使用方法：
    from esp32_flasher import ESP32Flasher

    # 初始化（自动检测固件目录）
    flasher = ESP32Flasher(log_callback=print)

    # 列出串口
    flasher.list_ports()

    # 烧录固件
    flasher.flash(port="COM3", baud=460800)
"""

import os
import sys
import subprocess
import threading
import time
import re
import glob
import shutil
import json
import hashlib
import tempfile
import zipfile
import urllib.request
from typing import Callable, List, Optional, Dict, Any


class ESP32FlasherError(Exception):
    """烧录器错误异常"""
    pass


class ESP32Flasher:
    """
    ESP32 固件烧录工具（预编译模式）

    Args:
        project_path: 项目根目录路径（用于查找固件）
        firmware_dir: 固件目录，默认 "firmware"
        log_callback: 日志回调函数，签名为 (level: str, msg: str) -> None
                     level: "INFO" | "OK" | "WARN" | "ERROR"
    """

    # 默认固件目录名
    DEFAULT_FIRMWARE_DIR = "firmware"

    # 默认固件文件名
    DEFAULT_FIRMWARE_NAME = "firmware.bin"

    # esptool.py 参数
    DEFAULT_FLASH_SIZE = "4MB"
    DEFAULT_FLASH_FREQ = "80m"
    DEFAULT_FLASH_MODE = "dio"
    DEFAULT_CHIP = "esp32c3"
    DEFAULT_FLASH_ADDR = "0x0"

    # ESP32-C3 标准分区烧录映射（与 PlatformIO 一致）
    FLASH_LAYOUT: Dict[str, tuple] = {
        # (相对文件名, 烧录地址)
        "bootloader.bin": "0x0",
        "partitions.bin": "0x8000",
        "boot_app0.bin": "0xe000",
        "firmware.bin":   "0x10000",  # app image（PlatformIO 编译产物）
    }

    def __init__(
        self,
        project_path: Optional[str] = None,
        firmware_dir: Optional[str] = None,
        log_callback: Optional[Callable[[str, str], None]] = None,
    ):
        # 确定路径
        if project_path is None:
            self.project_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        else:
            self.project_path = project_path

        self.firmware_dir = firmware_dir or self.DEFAULT_FIRMWARE_DIR
        self.log_callback = log_callback or self._default_log

        self._flash_thread: Optional[threading.Thread] = None
        self._cancel_event = threading.Event()

        # 缓存串口列表
        self._cached_ports: List[Dict[str, str]] = []
        self._last_port_scan = 0.0

        # 固件信息
        self._firmware_path: Optional[str] = None
        self._esptool_path: Optional[str] = None

    def _default_log(self, level: str, msg: str):
        """默认日志输出"""
        print(f"[{level}] {msg}", file=sys.stderr if level == "ERROR" else sys.stdout)

    def _log(self, level: str, msg: str):
        """发送日志"""
        self.log_callback(level, msg)

    # ============================================================
    # 固件相关
    # ============================================================

    def find_firmware(self) -> Optional[Dict[str, str]]:
        """
        查找固件文件（多段模式：bootloader/partitions/boot_app0/app）

        Returns:
            {烧录地址: 文件路径} 字典，未找到返回 None

        兼容以下固件目录结构：
        A) 已分段的（推荐）:
           firmware/
             ├── bootloader.bin   (0x0)
             ├── partitions.bin   (0x8000)
             ├── boot_app0.bin    (0xe000)  ← 可选，OTA 选择器
             └── firmware.bin     (0x10000, 纯 app image)

        B) 旧的单文件模式（向后兼容）:
           firmware/
             └── firmware.bin     (从 0x0 烧录，会覆盖 bootloader)
        """
        meipass = getattr(sys, '_MEIPASS', None)

        # 候选搜索根目录
        root_candidates = [
            os.path.join(self.project_path, self.firmware_dir),
            os.path.join(self.project_path),
            os.path.join(os.path.dirname(os.path.abspath(__file__)), self.firmware_dir),
            os.path.join(os.path.dirname(os.path.abspath(__file__))),
            os.path.join(os.path.dirname(sys.executable), self.firmware_dir),
            os.path.join(os.path.dirname(sys.executable)),
        ]
        if meipass:
            root_candidates += [
                os.path.join(meipass, self.firmware_dir),
                os.path.join(meipass),
            ]

        # 第一优先级：分段固件目录
        for root in root_candidates:
            result = self._try_find_segmented(root)
            if result:
                self._log("INFO", f"使用分段固件目录: {root}")
                return result

        # 第二优先级：单文件 firmware.bin（向后兼容 0x0 烧录）
        for root in root_candidates:
            cand = os.path.join(root, self.DEFAULT_FIRMWARE_NAME)
            if cand and os.path.exists(cand):
                self._log("WARN",
                          f"只找到单文件固件，将从 0x0 整块烧录（会覆盖 bootloader 区域）。"
                          f"如果出现 SHA-256 校验失败或反复重启，"
                          f"请使用 PlatformIO 编译后把 bootloader.bin/partitions.bin/boot_app0.bin 一起放到 firmware/ 目录。")
                return {self.DEFAULT_FLASH_ADDR: cand}

        return None

    def _try_find_segmented(self, root: str) -> Optional[Dict[str, str]]:
        """尝试在指定目录找分段固件"""
        if not root or not os.path.isdir(root):
            return None
        bootloader = os.path.join(root, "bootloader.bin")
        partitions = os.path.join(root, "partitions.bin")
        boot_app0 = os.path.join(root, "boot_app0.bin")
        app = os.path.join(root, "firmware.bin")

        # 必须至少 bootloader + partitions + app 三个文件
        if not (os.path.exists(bootloader) and os.path.exists(partitions) and os.path.exists(app)):
            return None

        layout: Dict[str, str] = {
            "0x0":     bootloader,
            "0x8000":  partitions,
            "0x10000": app,
        }
        if os.path.exists(boot_app0):
            layout["0xe000"] = boot_app0

        return layout

    def firmware_info(self, layout: Dict[str, str]) -> str:
        """格式化显示固件布局"""
        lines = []
        for addr, path in layout.items():
            name = os.path.basename(path)
            size = os.path.getsize(path) if os.path.exists(path) else 0
            lines.append(f"  {addr}: {name} ({size} bytes)")
        return "\n".join(lines)

    def get_firmware_info(self) -> Optional[Dict[str, Any]]:
        """获取固件信息（取 app 段为代表）"""
        layout = self.find_firmware()
        if not layout:
            return None

        # 取最大（通常是 app 段）
        main_path = max(layout.values(), key=os.path.getsize)
        try:
            stat = os.stat(main_path)
            return {
                "path": main_path,
                "size": stat.st_size,
                "size_str": self._format_size(stat.st_size),
                "modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)),
                "md5": self._calc_md5(main_path),
                "name": os.path.basename(main_path),
                "layout": layout,            # 多段布局
                "segmented": len(layout) > 1,
            }
        except Exception:
            return None

    @staticmethod
    def _format_size(size: int) -> str:
        """格式化文件大小"""
        for unit in ["B", "KB", "MB", "GB"]:
            if size < 1024:
                return f"{size:.2f} {unit}"
            size /= 1024
        return f"{size:.2f} TB"

    @staticmethod
    def _calc_md5(filepath: str) -> str:
        """计算文件 MD5"""
        hash_md5 = hashlib.md5()
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hash_md5.update(chunk)
        return hash_md5.hexdigest()

    # ============================================================
    # 串口相关
    # ============================================================

    def list_ports(self, force_refresh: bool = False) -> List[Dict[str, str]]:
        """
        列出所有可用的串口

        Args:
            force_refresh: 强制刷新缓存

        Returns:
            串口列表，每个元素为 {"port": "COM3", "description": "...", "hwid": "..."}
        """
        # 缓存 5 秒
        if not force_refresh and time.time() - self._last_port_scan < 5.0:
            return self._cached_ports

        ports = []

        # 方法 1: 使用 serial.tools.list_ports (如果可用)
        try:
            import serial.tools.list_ports
            for info in serial.tools.list_ports.comports():
                ports.append({
                    "port": info.device,
                    "description": info.description or "Unknown",
                    "hwid": info.hwid or "",
                })
        except ImportError:
            pass

        # 方法 2: Windows 注册表
        if not ports and sys.platform == "win32":
            try:
                import winreg
                key = winreg.OpenKey(
                    winreg.HKEY_LOCAL_MACHINE,
                    r"HARDWARE\DEVICEMAP\SERIALCOMM"
                )
                try:
                    i = 0
                    while True:
                        name, value, _ = winreg.EnumValue(key, i)
                        ports.append({
                            "port": value,
                            "description": name,
                            "hwid": "",
                        })
                        i += 1
                except WindowsError:
                    pass
                finally:
                    winreg.CloseKey(key)
            except Exception:
                pass

        # 方法 3: 尝试直接列举 COM 口
        if not ports:
            for i in range(1, 50):
                port_name = f"COM{i}"
                if os.path.exists(f"\\\\.\\{port_name}") or os.path.exists(port_name):
                    ports.append({
                        "port": port_name,
                        "description": "Serial Port",
                        "hwid": "",
                    })

        self._cached_ports = ports
        self._last_port_scan = time.time()
        return ports

    # ============================================================
    # esptool 相关
    # ============================================================

    def _is_frozen(self) -> bool:
        """是否运行在 PyInstaller frozen EXE 中"""
        return getattr(sys, 'frozen', False) or hasattr(sys, '_MEIPASS')

    def _esptool_callable(self) -> bool:
        """当前是否可以直接 import esptool（不需要子进程）"""
        # 任何能成功 import esptool 的环境都直接调用，避免反复启动新进程
        try:
            import esptool  # noqa: F401
            return True
        except ImportError:
            return False

    def _get_esptool(self) -> str:
        """
        获取 esptool 调用方式

        返回:
          - "INTERNAL" 表示应使用 _run_esptool_internal 直接调用 esptool.main()
          - 其他字符串是命令行，会被 _run_command_sync 当作子进程执行
        """
        if self._esptool_path:
            return self._esptool_path

        # 0. EXE / 打包环境：直接 import esptool（避免 sys.executable == 当前 EXE 导致再次启动自己）
        if self._esptool_callable():
            self._esptool_path = "INTERNAL"
            return self._esptool_path

        # 1. 检查打包的 esptool 脚本
        script_dir = os.path.dirname(os.path.abspath(__file__))
        bundled_esptool = os.path.join(script_dir, "tools", "esptool.py")
        if os.path.exists(bundled_esptool):
            # 注意：frozen EXE 下 sys.executable 就是 EXE 自己，不能用！
            if self._is_frozen():
                # 用基础 python.exe（如果存在），否则回退到内部调用
                pass
            else:
                self._esptool_path = f"{sys.executable} \"{bundled_esptool}\""
                return self._esptool_path

        # 2. 检查系统 PATH 中的 esptool
        try:
            result = subprocess.run(
                ["esptool.py", "--version"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                self._esptool_path = "esptool.py"
                return self._esptool_path
        except (subprocess.SubprocessError, FileNotFoundError):
            pass

        # 3. pip 安装的 esptool（仅开发模式使用）
        if not self._is_frozen():
            try:
                result = subprocess.run(
                    [sys.executable, "-m", "esptool", "--version"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if result.returncode == 0:
                    self._esptool_path = f"{sys.executable} -m esptool"
                    return self._esptool_path
            except (subprocess.SubprocessError, FileNotFoundError):
                pass

        raise ESP32FlasherError(
            "未找到 esptool.py\n"
            "请选择以下方式之一：\n"
            "1. pip install esptool\n"
            "2. 将 esptool.py 放在 tools/ 目录下\n"
            "3. 确保 esptool.py 在系统 PATH 中"
        )

    def install_esptool(self) -> bool:
        """自动安装 esptool"""
        self._log("INFO", "正在安装 esptool...")

        # EXE 模式下 esptool 已经被 collect-all 打进来，无需安装
        if self._esptool_callable():
            self._log("OK", "esptool 已包含在程序中，无需安装")
            self._esptool_path = "INTERNAL"
            return True

        try:
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", "esptool"],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode == 0:
                self._log("OK", "esptool 安装成功")
                self._esptool_path = None  # 重置，下次自动检测
                return True
            else:
                self._log("ERROR", f"esptool 安装失败: {result.stderr}")
                return False
        except Exception as e:
            self._log("ERROR", f"安装 esptool 失败: {e}")
            return False

    # ============================================================
    # 烧录功能
    # ============================================================

    def flash(
        self,
        port: str,
        baud: int = 460800,
        flash_size: Optional[str] = None,
        flash_freq: Optional[str] = None,
        flash_mode: Optional[str] = None,
        chip: Optional[str] = None,
        flash_addr: Optional[str] = None,
        background: bool = True,
        verify: bool = True,
        erase_before: bool = False,
    ) -> bool:
        """
        烧录固件到 ESP32

        Args:
            port: 串口名称，如 "COM3" 或 "/dev/ttyUSB0"
            baud: 烧录波特率，默认 460800
            flash_size: Flash 大小，如 "4MB"
            flash_freq: Flash 频率，如 "80m"
            flash_mode: Flash 模式，如 "dio"
            chip: 芯片类型，默认 "esp32c3"
            flash_addr: 烧录地址，默认 "0x0"
            background: 是否在后台执行
            verify: 是否验证烧录
            erase_before: 烧录前先擦除整个 Flash（解决旧 bootloader / partition table 残留导致的反复重启）

        Returns:
            烧录是否成功（仅在 background=False 时有效）
        """
        # 查找固件（多段布局）
        firmware_layout = self.find_firmware()
        if not firmware_layout:
            raise ESP32FlasherError(
                f"未找到固件文件！\n"
                f"请将以下文件放在 firmware/ 目录：\n"
                f"  bootloader.bin    (0x0)\n"
                f"  partitions.bin    (0x8000)\n"
                f"  boot_app0.bin     (0xe000，可选)\n"
                f"  firmware.bin      (0x10000)\n\n"
                f"或使用 PlatformIO 编译后从 .pio/build/esp32-c3-supermini/ 复制这些文件。"
            )

        # 获取 esptool
        try:
            esptool_cmd = self._get_esptool()
        except ESP32FlasherError as e:
            raise e

        # 构建 esptool 命令（多段：地址+文件 交替）
        # esptool v5+ 用 write-flash, --flash-size, --flash-freq, --flash-mode
        if esptool_cmd == "INTERNAL":
            argv = []
        else:
            cmd_parts = esptool_cmd.split()
            if len(cmd_parts) > 1 and cmd_parts[0] == sys.executable:
                argv = [cmd_parts[0], cmd_parts[1]]
            else:
                argv = [esptool_cmd]

        argv.extend([
            "--chip", chip or self.DEFAULT_CHIP,
            "--port", port,
            "--baud", str(baud),
            "write-flash",
        ])

        # Flash 参数（esptool v5 使用 hyphen）
        argv.extend(["--flash-size", flash_size or self.DEFAULT_FLASH_SIZE])
        if flash_freq:
            argv.extend(["--flash-freq", flash_freq])
        if flash_mode:
            argv.extend(["--flash-mode", flash_mode])
        # 注：esptool v5 默认自动 verify，不再需要 --verify

        # 添加固件地址和文件（多段）
        for addr, path in sorted(firmware_layout.items()):
            argv.extend([addr, path])

        self._log("INFO", f"开始烧录固件（共 {len(firmware_layout)} 段）...")
        for addr, path in sorted(firmware_layout.items()):
            self._log("INFO", f"  {addr}: {os.path.basename(path)} ({os.path.getsize(path)} bytes)")
        self._log("INFO", f"串口: {port}, 波特率: {baud}")

        if background:
            self._cancel_event.clear()
            self._flash_thread = threading.Thread(
                target=self._flash_worker,
                args=(argv, erase_before),
                daemon=True,
            )
            self._flash_thread.start()
            return True
        else:
            # 非后台模式直接串行执行：erase -> flash
            if erase_before:
                self._log("INFO", "===== 第 1/2 步：擦除 Flash =====")
                erase_argv = argv[:6] + ["erase-flash"]
                if self._run_esptool(erase_argv, "擦除") != 0:
                    return False
                time.sleep(1.0)
                self._log("INFO", "===== 第 2/2 步：烧录固件 =====")
            return self._run_esptool(argv, "烧录") == 0

    def _flash_worker(self, argv: List[str], erase_first: bool = False):
        """烧录工作线程"""
        if erase_first:
            self._log("INFO", "===== 第 1/2 步：擦除 Flash =====")
            erase_argv = argv[:6] + ["erase-flash"]
            rc_erase = self._run_esptool(erase_argv, "擦除")
            if rc_erase != 0:
                self._log("ERROR", f"擦除失败，退出码: {rc_erase}，跳过烧录")
                self._flash_thread = None
                return

            # 擦除后设备会重启，需要稍等一下避免串口未就绪
            self._log("INFO", "擦除完成，等待设备重新进入下载模式...")
            time.sleep(1.0)

            self._log("INFO", "===== 第 2/2 步：烧录固件 =====")

        result = self._run_esptool(argv, "烧录")
        if result == 0:
            self._log("OK", "烧录成功！")
        else:
            self._log("ERROR", f"烧录失败，退出码: {result}")
        self._flash_thread = None

    def wait_flash(self, timeout: Optional[float] = None) -> int:
        """等待烧录完成"""
        if self._flash_thread is None:
            return 0
        self._flash_thread.join(timeout=timeout)
        return 0 if self._flash_thread is None else -1

    # ============================================================
    # 擦除 Flash
    # ============================================================

    def erase_flash(
        self,
        port: str,
        baud: int = 460800,
        chip: Optional[str] = None,
        background: bool = True,
    ) -> bool:
        """
        擦除整个 Flash

        Args:
            port: 串口
            baud: 波特率
            chip: 芯片类型，默认 esp32c3
            background: 是否后台执行

        Returns:
            是否启动成功（仅指示是否开始执行，不反映擦除结果）
        """
        # 确保 esptool 可用
        try:
            self._get_esptool()
        except ESP32FlasherError as e:
            self._log("ERROR", str(e))
            raise

        argv = [
            "--chip", chip or self.DEFAULT_CHIP,
            "--port", port,
            "--baud", str(baud),
            "erase-flash",
        ]

        self._log("INFO", f"开始擦除 Flash...")
        self._log("INFO", f"串口: {port}, 波特率: {baud}")
        self._log("WARN", "擦除将移除 Flash 上所有数据，包括 bootloader / partition table / NVS")

        if background:
            self._cancel_event.clear()
            self._flash_thread = threading.Thread(
                target=self._erase_worker,
                args=(argv,),
                daemon=True,
            )
            self._flash_thread.start()
            return True
        else:
            return self._run_esptool(argv, "擦除") == 0

    def _erase_worker(self, argv: List[str]):
        """擦除工作线程"""
        result = self._run_esptool(argv, "擦除")
        if result == 0:
            self._log("OK", "擦除完成！Flash 已清空，可以烧录新固件")
        else:
            self._log("ERROR", f"擦除失败，退出码: {result}")
        self._flash_thread = None

    @property
    def is_flashing(self) -> bool:
        """是否正在烧录"""
        return self._flash_thread is not None and self._flash_thread.is_alive()

    def cancel(self):
        """取消当前操作"""
        self._cancel_event.set()
        self._log("WARN", "已请求取消烧录")

    # ============================================================
    # 串口监视器
    # ============================================================

    def open_serial_monitor(self, port: str, baud: int = 115200):
        """打开串口监视器"""
        # 尝试使用 pyserial
        try:
            import serial
            self._log("INFO", f"串口监视器已打开: {port} @ {baud}")
            self._log("INFO", "提示: 使用 Ctrl+C 退出监视器")
            ser = serial.Serial(port, baud, timeout=0.1)
            try:
                while True:
                    if ser.in_waiting:
                        data = ser.read(ser.in_waiting)
                        try:
                            print(data.decode('utf-8', errors='replace'), end='', flush=True)
                        except Exception:
                            pass
                    time.sleep(0.01)
            except KeyboardInterrupt:
                pass
            finally:
                ser.close()
                self._log("INFO", "串口监视器已关闭")
        except ImportError:
            self._log("ERROR", "需要安装 pyserial: pip install pyserial")
        except Exception as e:
            self._log("ERROR", f"无法打开串口: {e}")

    def open_download_mode(self, port: str):
        """发送 ESP32 进入下载模式的信号"""
        try:
            import serial
            self._log("INFO", "正在触发下载模式...")

            # ESP32 进入下载模式的方法：
            # 1. 切换到低波特率连接
            # 2. RTS/DTR 信号控制

            # 尝试使用 1200 波特率连接（会触发下载模式）
            try:
                ser = serial.Serial(port, 1200, timeout=0.5)
                ser.close()
                self._log("OK", "已发送下载模式触发信号")
            except Exception:
                pass

            # 如果上面的方法不行，尝试直接操作 DTR/RTS
            try:
                ser = serial.Serial(port)
                ser.dtr = False  # 通常拉低 DTR 进入下载模式
                ser.rts = True
                time.sleep(0.1)
                ser.rts = False
                ser.close()
                self._log("OK", "已通过 DTR/RTS 触发下载模式")
            except Exception:
                pass

            self._log("INFO", "请在 3 秒内断开并重新连接 USB（如果设备未进入下载模式）")

        except ImportError:
            self._log("ERROR", "需要安装 pyserial: pip install pyserial")
        except Exception as e:
            self._log("ERROR", f"无法触发下载模式: {e}")

    # ============================================================
    # 工具方法
    # ============================================================

    def _run_command_sync(
        self,
        cmd: List[str],
        operation: str,
        cwd: Optional[str] = None,
    ) -> int:
        """同步执行命令"""
        self._log("INFO", f"执行: {' '.join(cmd)}")

        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=cwd,
                text=True,
                bufsize=1,
            )

            for line in iter(process.stdout.readline, ""):
                if not line:
                    break
                if self._cancel_event.is_set():
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    self._log("WARN", f"{operation}已取消")
                    return -1

                line = line.rstrip()
                self._parse_progress(line, operation)

            process.wait()
            return process.returncode

        except FileNotFoundError as e:
            self._log("ERROR", f"命令不存在: {cmd[0]}")
            raise ESP32FlasherError(f"命令不存在: {cmd[0]}")
        except Exception as e:
            self._log("ERROR", f"{operation}失败: {e}")
            raise

    def _run_esptool(self, argv: List[str], operation: str) -> int:
        """
        运行 esptool：
          - EXE 模式下直接调用 esptool.main(argv)，不创建子进程（避免反复启动新窗口）
          - 否则走 _run_command_sync 子进程方式
        """
        self._log("INFO", f"执行: esptool {' '.join(argv)}")

        # 安全检查：如果 argv[0] 是 sys.executable 且我们处于 frozen EXE，绝对不能执行
        if getattr(sys, 'frozen', False) and argv and argv[0] == sys.executable:
            self._log("ERROR", "拒绝在 EXE 中递归启动自身")
            return -1

        if self._esptool_callable():
            # 直接调用 esptool.main()，接管 stdout/stderr 解析进度
            try:
                import esptool
                import io
                import contextlib

                buf = io.StringIO()
                try:
                    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                        rc = esptool.main(argv)
                except SystemExit as e:
                    rc = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
                except Exception as e:
                    self._log("ERROR", f"{operation}失败: {e}")
                    return 1

                # 把 esptool 的输出逐行喂给 _parse_progress
                for line in buf.getvalue().splitlines():
                    if self._cancel_event.is_set():
                        self._log("WARN", f"{operation}已取消")
                        return -1
                    self._parse_progress(line.rstrip(), operation)

                return rc if isinstance(rc, int) else 0
            except Exception as e:
                self._log("ERROR", f"{operation}异常: {e}")
                raise ESP32FlasherError(f"{operation}异常: {e}")

        return self._run_command_sync(argv, operation)

    def _parse_progress(self, line: str, operation: str):
        """解析烧录进度"""
        if "Writing at" in line or "Reading" in line:
            if "(" in line and ")" in line:
                percent = line[line.find("(")+1:line.find(")")]
                self._log("INFO", f"{operation}进度: {percent}")
            else:
                self._log("INFO", line)
        elif "Hash of data validated" in line or "CRC" in line:
            self._log("OK", line)
        elif "Leaving" in line or "Hard resetting" in line:
            self._log("INFO", line)
        elif any(x in line for x in ["ERROR", "Error", "error", "FAILED", "Failed"]):
            self._log("ERROR", line)
        elif "warning" in line.lower() or "Warning" in line:
            self._log("WARN", line)
        else:
            if line.strip():
                self._log("INFO", line)

    def is_esptool_available(self) -> bool:
        """检查 esptool 是否可用"""
        try:
            self._get_esptool()
            return True
        except ESP32FlasherError:
            return False

    def check_prerequisites(self) -> Dict[str, bool]:
        """检查前置条件"""
        return {
            "firmware_found": self.find_firmware() is not None,
            "esptool_available": self.is_esptool_available(),
        }


# ============================================================
# 便捷函数
# ============================================================

def quick_flash(
    port: str,
    project_path: Optional[str] = None,
    log_callback: Optional[Callable[[str, str], None]] = None,
    baud: int = 460800,
) -> bool:
    """
    一键烧录

    Args:
        port: 串口
        project_path: 项目路径
        log_callback: 日志回调
        baud: 烧录波特率

    Returns:
        是否成功
    """
    def default_log(level, msg):
        if log_callback:
            log_callback(level, msg)
        else:
            print(f"[{level}] {msg}")

    flasher = ESP32Flasher(project_path, log_callback=default_log)
    return flasher.flash(port, baud, background=False)


def create_firmware_package(
    source_dir: str,
    output_path: str,
    firmware_file: Optional[str] = None,
) -> str:
    """
    创建固件包（包含固件 + esptool）

    Args:
        source_dir: 源目录（包含 firmware.bin）
        output_path: 输出路径（.zip）
        firmware_file: 固件文件名，默认 firmware.bin

    Returns:
        生成的 zip 文件路径
    """
    import tempfile
    import shutil

    with tempfile.TemporaryDirectory() as tmpdir:
        pkg_dir = os.path.join(tmpdir, "ESP32_Firmware_Package")
        os.makedirs(pkg_dir)

        # 复制固件
        src_firmware = os.path.join(source_dir, firmware_file or "firmware.bin")
        if not os.path.exists(src_firmware):
            # 尝试在 .pio/build 中查找
            build_dir = os.path.join(source_dir, ".pio", "build")
            if os.path.exists(build_dir):
                for env_dir in os.listdir(build_dir):
                    env_path = os.path.join(build_dir, env_dir)
                    if os.path.isdir(env_path):
                        bin_path = os.path.join(env_path, "firmware.bin")
                        if os.path.exists(bin_path):
                            src_firmware = bin_path
                            break

        if os.path.exists(src_firmware):
            shutil.copy(src_firmware, os.path.join(pkg_dir, "firmware.bin"))

        # 创建烧录脚本
        script_content = '''@echo off
chcp 65001 >nul
echo ========================================
echo   ESP32 固件烧录工具
echo ========================================
echo.

REM 检查 Python
python --version >nul 2>&1
if errorlevel 1 (
    echo 错误: 未安装 Python
    echo 请访问 https://www.python.org 下载安装
    pause
    exit /b 1
)

REM 安装 esptool
echo 正在检查 esptool...
pip show esptool >nul 2>&1
if errorlevel 1 (
    echo 正在安装 esptool...
    pip install esptool
)

REM 列出可用串口
echo.
echo 可用串口:
python -m esptool --help-port
echo.

set /p PORT="请输入串口名称 (如 COM3): "
set /p BAUD="请输入波特率 (默认 460800): "

if "%BAUD%"=="" set BAUD=460800

echo.
echo 开始烧录...
python -m esptool --chip esp32c3 --port %PORT% --baud %BAUD% write_flash --flash_size 4MB --flash_freq 80m 0x0 firmware.bin

echo.
echo 烧录完成！
pause
'''

        with open(os.path.join(pkg_dir, "烧录固件.bat"), "w", encoding="utf-8") as f:
            f.write(script_content)

        # 复制 esptool.py
        try:
            result = subprocess.run(
                [sys.executable, "-c", "import esptool; import os; print(os.path.dirname(esptool.__file__))"],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                esptool_dir = result.stdout.strip()
                # 复制整个 esptool 目录
                dest_esptool = os.path.join(pkg_dir, "tools")
                shutil.copytree(esptool_dir, dest_esptool)
        except Exception:
            pass

        # 创建 README
        readme = '''# ESP32 固件烧录包

## 文件说明
- `firmware.bin` - ESP32 固件文件
- `烧录固件.bat` - Windows 烧录脚本
- `tools/` - esptool 工具（如果有）

## 使用方法

### 方法一：使用脚本（推荐）
1. 双击运行 `烧录固件.bat`
2. 输入串口名称（如 COM3）
3. 输入波特率（默认 460800）
4. 等待烧录完成

### 方法二：手动烧录
```bash
pip install esptool
python -m esptool --chip esp32c3 --port COM3 --baud 460800 write_flash --flash_size 4MB 0x0 firmware.bin
```

## 注意事项
- 请确保 ESP32 已通过 USB 连接到电脑
- 如果无法识别串口，请安装 CH340/CP2102 驱动
- ESP32-C3 使用 USB 直接连接，无需额外串口芯片
'''
        with open(os.path.join(pkg_dir, "README.md"), "w", encoding="utf-8") as f:
            f.write(readme)

        # 打包
        shutil.make_archive(output_path.replace('.zip', ''), 'zip', tmpdir, "ESP32_Firmware_Package")

    return output_path
