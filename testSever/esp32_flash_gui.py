"""
ESP32 Firmware Flasher GUI
==========================
独立的 ESP32 烧录工具，可以打包成 EXE 分发给用户。

功能：
- 自动检测可用串口
- 烧录预编译的固件
- 内置 esptool，无需额外安装
- 串口监视器

打包方法：
    pip install pyinstaller
    pyinstaller --onefile --windowed esp32_flash_gui.py

Author: Your Name
"""

import os
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext

# 导入烧录模块
try:
    from esp32_flasher import ESP32Flasher, ESP32FlasherError
except ImportError:
    from testSever.esp32_flasher import ESP32Flasher, ESP32FlasherError


class LogPanel:
    """日志面板"""
    COLORS = {
        "INFO": "#333333",
        "OK": "#0a7a2f",
        "WARN": "#b07d00",
        "ERROR": "#c0392b",
    }

    def __init__(self, text_widget: scrolledtext.ScrolledText):
        self.text = text_widget
        for level, color in self.COLORS.items():
            self.text.tag_configure(level, foreground=color)
        self.text.tag_configure("TIMESTAMP", foreground="#888888")
        self.text.configure(state=tk.DISABLED)

    def log(self, level: str, msg: str):
        self.text.configure(state=tk.NORMAL)
        ts = time.strftime("%H:%M:%S")
        self.text.insert(tk.END, f"[{ts}] ", "TIMESTAMP")
        self.text.insert(tk.END, f"[{level}] ", level)
        self.text.insert(tk.END, msg + "\n", level)
        self.text.see(tk.END)
        self.text.configure(state=tk.DISABLED)

    def clear(self):
        self.text.configure(state=tk.NORMAL)
        self.text.delete("1.0", tk.END)
        self.text.configure(state=tk.DISABLED)


class ESP32FlashGUI(tk.Tk):
    def __init__(self):
        super().__init__()

        # 窗口设置
        self.title("ESP32 固件烧录工具 v1.0")
        self.geometry("700x600")
        self.minsize(600, 500)

        # 居中显示
        self.center_window()

        # 初始化烧录器
        self.flasher = ESP32Flasher(log_callback=self._log)
        self._is_flashing = False

        # 构建界面
        self._build_ui()

        # 检查前置条件
        self.after(500, self._check_prerequisites)

    def center_window(self):
        """窗口居中"""
        self.update_idletasks()
        w = self.winfo_width()
        h = self.winfo_height()
        x = (self.winfo_screenwidth() // 2) - (w // 2)
        y = (self.winfo_screenheight() // 2) - (h // 2)
        self.geometry(f"{w}x{h}+{x}+{y}")

    def _build_ui(self):
        """构建界面"""
        # 主容器
        main_frame = ttk.Frame(self, padding=12)
        main_frame.pack(fill=tk.BOTH, expand=True)

        # 标题
        title_frame = ttk.Frame(main_frame)
        title_frame.pack(fill=tk.X, pady=(0, 12))

        ttk.Label(
            title_frame,
            text="ESP32 固件烧录工具",
            font=("Microsoft YaHei", 16, "bold")
        ).pack(side=tk.LEFT)

        ttk.Label(
            title_frame,
            text="ESP32-C3 / ESP32-S3",
            font=("Microsoft YaHei", 10),
            foreground="#666666"
        ).pack(side=tk.RIGHT, pady=8)

        # 上部区域：固件信息和串口设置
        top_frame = ttk.LabelFrame(main_frame, text="烧录设置", padding=10)
        top_frame.pack(fill=tk.X, pady=(0, 10))

        # 固件信息
        firmware_frame = ttk.Frame(top_frame)
        firmware_frame.pack(fill=tk.X, pady=(0, 10))

        ttk.Label(firmware_frame, text="固件:").pack(side=tk.LEFT)
        self.firmware_label = ttk.Label(
            firmware_frame,
            text="正在检测...",
            foreground="#666666"
        )
        self.firmware_label.pack(side=tk.LEFT, padx=(8, 0))

        self.firmware_status_icon = ttk.Label(firmware_frame, text="")
        self.firmware_status_icon.pack(side=tk.LEFT, padx=(8, 0))

        # 刷新固件按钮
        ttk.Button(
            firmware_frame,
            text="刷新",
            command=self._refresh_firmware,
            width=6
        ).pack(side=tk.RIGHT)

        # 串口选择
        port_frame = ttk.Frame(top_frame)
        port_frame.pack(fill=tk.X, pady=(0, 8))

        ttk.Label(port_frame, text="串口:").pack(side=tk.LEFT)

        self.port_var = tk.StringVar()
        self.port_combo = ttk.Combobox(
            port_frame,
            textvariable=self.port_var,
            state="readonly",
            width=15
        )
        self.port_combo.pack(side=tk.LEFT, padx=(8, 4))

        ttk.Button(
            port_frame,
            text="刷新",
            command=self._refresh_ports,
            width=6
        ).pack(side=tk.LEFT)

        # 波特率
        ttk.Label(port_frame, text="波特率:").pack(side=tk.LEFT, padx=(20, 4))

        self.baud_var = tk.StringVar(value="460800")
        baud_combo = ttk.Combobox(
            port_frame,
            textvariable=self.baud_var,
            values=["115200", "256000", "460800", "921600"],
            state="readonly",
            width=8
        )
        baud_combo.pack(side=tk.LEFT)

        # Flash 参数
        flash_frame = ttk.Frame(top_frame)
        flash_frame.pack(fill=tk.X)

        ttk.Label(flash_frame, text="Flash:").pack(side=tk.LEFT)

        ttk.Label(flash_frame, text="大小").pack(side=tk.LEFT, padx=(8, 2))

        self.size_var = tk.StringVar(value="4MB")
        size_combo = ttk.Combobox(
            flash_frame,
            textvariable=self.size_var,
            values=["1MB", "2MB", "4MB", "8MB", "16MB"],
            state="readonly",
            width=5
        )
        size_combo.pack(side=tk.LEFT)

        ttk.Label(flash_frame, text="频率").pack(side=tk.LEFT, padx=(12, 2))

        self.freq_var = tk.StringVar(value="80m")
        freq_combo = ttk.Combobox(
            flash_frame,
            textvariable=self.freq_var,
            values=["40m", "80m"],
            state="readonly",
            width=5
        )
        freq_combo.pack(side=tk.LEFT)

        # 烧录前擦除复选框
        self.erase_before_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            flash_frame,
            text="烧录前先擦除 Flash (推荐)",
            variable=self.erase_before_var,
        ).pack(side=tk.LEFT, padx=(16, 0))

        # 烧录按钮区域
        button_frame = ttk.Frame(main_frame)
        button_frame.pack(fill=tk.X, pady=(0, 10))

        self.flash_btn = ttk.Button(
            button_frame,
            text="开始烧录",
            command=self._do_flash,
            style="Accent.TButton"
        )
        self.flash_btn.pack(side=tk.LEFT, padx=(0, 8))

        self.erase_btn = ttk.Button(
            button_frame,
            text="仅擦除 Flash",
            command=self._do_erase_only
        )
        self.erase_btn.pack(side=tk.LEFT, padx=(0, 8))

        self.monitor_btn = ttk.Button(
            button_frame,
            text="串口监视器",
            command=self._open_monitor
        )
        self.monitor_btn.pack(side=tk.LEFT, padx=(0, 8))

        self.install_btn = ttk.Button(
            button_frame,
            text="安装 esptool",
            command=self._install_esptool
        )
        self.install_btn.pack(side=tk.LEFT)

        # 进度条
        self.progress = ttk.Progressbar(
            main_frame,
            mode="indeterminate",
            length=200
        )

        # 日志区域
        log_frame = ttk.LabelFrame(main_frame, text="日志", padding=8)
        log_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 8))

        self.log_text = scrolledtext.ScrolledText(
            log_frame,
            wrap=tk.WORD,
            height=15,
            font=("Consolas", 10)
        )
        self.log_text.pack(fill=tk.BOTH, expand=True)

        self.logger = LogPanel(self.log_text)

        # 底部状态栏
        status_frame = ttk.Frame(main_frame)
        status_frame.pack(fill=tk.X)

        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(
            status_frame,
            textvariable=self.status_var,
            foreground="#666666"
        ).pack(side=tk.LEFT)

        ttk.Button(
            status_frame,
            text="清空日志",
            command=lambda: self.logger.clear()
        ).pack(side=tk.RIGHT)

    def _log(self, level: str, msg: str):
        """日志回调"""
        self.after(0, lambda: self.logger.log(level, msg))

    def _refresh_firmware(self):
        """刷新固件信息"""
        info = self.flasher.get_firmware_info()
        if info:
            if info.get('segmented'):
                seg_count = len(info['layout'])
                self.firmware_label.configure(
                    text=f"{info['name']} ({info['size_str']}) · {seg_count} 段分段烧录",
                    foreground="#0a7a2f"
                )
                self.firmware_status_icon.configure(text="✓", foreground="#0a7a2f")
            else:
                self.firmware_label.configure(
                    text=f"{info['name']} ({info['size_str']}) · 单文件",
                    foreground="#b07a00"
                )
                self.firmware_status_icon.configure(text="⚠", foreground="#b07a00")
        else:
            self.firmware_label.configure(
                text="未找到固件",
                foreground="#c0392b"
            )
            self.firmware_status_icon.configure(text="✗", foreground="#c0392b")

    def _refresh_ports(self):
        """刷新串口列表"""
        ports = self.flasher.list_ports(force_refresh=True)
        port_names = [f"{p['port']} - {p['description'][:20]}" for p in ports]
        self.port_combo["values"] = port_names
        if port_names:
            self.port_combo.set(port_names[0])
        else:
            self.port_combo.set("")

    def _check_prerequisites(self):
        """检查前置条件"""
        # 刷新固件
        self._refresh_firmware()

        # 刷新串口
        self._refresh_ports()

        # 检查 esptool
        if not self.flasher.is_esptool_available():
            self._log("WARN", "esptool 未找到，点击【安装 esptool】按钮进行安装")

    def _get_selected_port(self) -> str:
        """获取选中的串口"""
        port_str = self.port_var.get()
        if not port_str:
            return ""
        # 提取串口名（去掉描述部分）
        if " - " in port_str:
            return port_str.split(" - ")[0].strip()
        return port_str.strip()

    def _do_flash(self):
        """执行烧录"""
        if self._is_flashing:
            return

        port = self._get_selected_port()
        if not port:
            messagebox.showwarning("请选择串口", "请先选择一个串口")
            return

        # 检查固件
        if not self.flasher.find_firmware():
            messagebox.showerror(
                "未找到固件",
                f"请将固件文件放在以下位置:\n"
                f"{self.flasher.project_path}\\firmware\\firmware.bin"
            )
            return

        # 检查 esptool
        if not self.flasher.is_esptool_available():
            result = messagebox.askyesno(
                "esptool 未安装",
                "esptool 未安装，是否现在安装？\n\n"
                "提示：需要网络连接"
            )
            if result:
                self._install_esptool()
            return

        # 确认烧录
        erase = self.erase_before_var.get()
        if erase:
            confirm_msg = (
                f"即将执行：\n"
                f"1. 擦除整个 Flash\n"
                f"2. 烧录固件到 {port}\n\n"
                f"⚠ 擦除会清除所有数据（包括 NVS、saved Wi-Fi 等）\n"
                f"请确保 ESP32 已正确连接！"
            )
        else:
            confirm_msg = (
                f"即将烧录固件到 {port}\n\n"
                f"请确保 ESP32 已正确连接！"
            )
        result = messagebox.askyesno("确认烧录", confirm_msg)
        if not result:
            return

        # 开始烧录
        self._is_flashing = True
        self.flash_btn.configure(text="烧录中...", state=tk.DISABLED)
        self.erase_btn.configure(state=tk.DISABLED)
        self.progress.pack(fill=tk.X, pady=(0, 8))
        self.progress.start()
        self.status_var.set("正在擦除+烧录..." if erase else "正在烧录...")

        try:
            baud = int(self.baud_var.get())
        except ValueError:
            baud = 460800

        def worker():
            try:
                success = self.flasher.flash(
                    port=port,
                    baud=baud,
                    flash_size=self.size_var.get(),
                    flash_freq=self.freq_var.get(),
                    background=False,
                    erase_before=erase,
                )
                self.after(0, self._on_flash_done, success)
            except Exception as e:
                self.after(0, self._on_flash_done, False, str(e))

        threading.Thread(target=worker, daemon=True).start()

    def _do_erase_only(self):
        """仅擦除 Flash（不烧录）"""
        port = self._get_selected_port()
        if not port:
            messagebox.showwarning("请选择串口", "请先选择一个串口")
            return

        if not self.flasher.is_esptool_available():
            messagebox.showerror(
                "esptool 未安装",
                "esptool 未安装，请先点击【安装 esptool】按钮。"
            )
            return

        result = messagebox.askyesno(
            "确认擦除",
            f"即将擦除 {port} 上的整个 Flash！\n\n"
            f"⚠ 所有数据（包括 bootloader / NVS / 已烧录固件）将被清除。\n"
            f"确定继续吗？"
        )
        if not result:
            return

        self.erase_btn.configure(state=tk.DISABLED, text="擦除中...")
        self.flash_btn.configure(state=tk.DISABLED)
        self.progress.pack(fill=tk.X, pady=(0, 8))
        self.progress.start()
        self.status_var.set("正在擦除 Flash...")

        try:
            baud = int(self.baud_var.get())
        except ValueError:
            baud = 460800

        def worker():
            try:
                success = self.flasher.erase_flash(
                    port=port,
                    baud=baud,
                    background=False,
                )
                self.after(0, self._on_erase_done, success)
            except Exception as e:
                self.after(0, self._on_erase_done, False, str(e))

        threading.Thread(target=worker, daemon=True).start()

    def _on_erase_done(self, success: bool, error: str = ""):
        """擦除完成回调"""
        self.erase_btn.configure(state=tk.NORMAL, text="仅擦除 Flash")
        self.flash_btn.configure(state=tk.NORMAL)
        self.progress.stop()
        self.progress.pack_forget()

        if success:
            self.status_var.set("擦除完成")
            messagebox.showinfo("擦除成功", "Flash 擦除完成！\n\n可以点击【开始烧录】上传新固件。")
        else:
            self.status_var.set("擦除失败")
            if error:
                self._log("ERROR", error)
            messagebox.showerror("擦除失败", f"擦除失败！\n\n{error}")

    def _on_flash_done(self, success: bool, error: str = ""):
        """烧录完成回调"""
        self._is_flashing = False
        self.flash_btn.configure(text="开始烧录", state=tk.NORMAL)
        self.erase_btn.configure(state=tk.NORMAL)
        self.progress.stop()
        self.progress.pack_forget()

        if success:
            self.status_var.set("烧录完成")
            if self.erase_before_var.get():
                messagebox.showinfo(
                    "烧录成功",
                    "擦除 + 烧录全部完成！\n\nESP32 即将重启。\n提示：因为已擦除，下次启动将进入下载模式（需按 RESET 退出）"
                )
            else:
                messagebox.showinfo("烧录成功", "固件烧录成功！\n\nESP32 即将重启。")
        else:
            self.status_var.set("烧录失败")
            if error:
                self._log("ERROR", error)
            messagebox.showerror("烧录失败", f"烧录失败！\n\n{error}")

    def _install_esptool(self):
        """安装 esptool"""
        self.install_btn.configure(state=tk.DISABLED, text="安装中...")
        self.status_var.set("正在安装 esptool...")

        def worker():
            success = self.flasher.install_esptool()
            self.after(0, self._on_install_done, success)

        threading.Thread(target=worker, daemon=True).start()

    def _on_install_done(self, success: bool):
        """安装完成回调"""
        self.install_btn.configure(state=tk.NORMAL, text="安装 esptool")

        if success:
            self.status_var.set("esptool 安装成功")
            self._log("OK", "esptool 安装成功！")
        else:
            self.status_var.set("esptool 安装失败")
            self._log("ERROR", "esptool 安装失败，请检查网络连接")

    def _open_monitor(self):
        """打开串口监视器"""
        port = self._get_selected_port()
        if not port:
            messagebox.showwarning("请选择串口", "请先选择一个串口")
            return

        try:
            baud = int(self.baud_var.get())
        except ValueError:
            baud = 115200

        self._log("INFO", f"打开串口监视器: {port} @ {baud}")
        self._log("INFO", "提示: 在弹出的窗口中使用 Ctrl+C 退出")

        # 在新窗口中打开监视器
        MonitorWindow(self, port, baud)


class MonitorWindow(tk.Toplevel):
    """串口监视器窗口"""

    def __init__(self, parent, port: str, baud: int):
        super().__init__(parent)

        self.title(f"串口监视器 - {port}")
        self.geometry("700x400")

        self.serial = None
        self.running = False

        # 构建界面
        frame = ttk.Frame(self, padding=8)
        frame.pack(fill=tk.BOTH, expand=True)

        # 工具栏
        toolbar = ttk.Frame(frame)
        toolbar.pack(fill=tk.X, pady=(0, 8))

        self.baud_combo = ttk.Combobox(
            toolbar,
            values=["9600", "115200", "256000", "460800"],
            width=8
        )
        self.baud_combo.set(str(baud))
        self.baud_combo.pack(side=tk.LEFT, padx=(0, 8))

        ttk.Button(
            toolbar,
            text="重新连接",
            command=lambda: self._connect(port, int(self.baud_combo.get()))
        ).pack(side=tk.LEFT)

        self.status_label = ttk.Label(toolbar, text="未连接", foreground="#c0392b")
        self.status_label.pack(side=tk.RIGHT)

        # 文本区域
        self.text = scrolledtext.ScrolledText(frame, wrap=tk.WORD, font=("Consolas", 10))
        self.text.pack(fill=tk.BOTH, expand=True)

        # 底部输入
        input_frame = ttk.Frame(frame)
        input_frame.pack(fill=tk.X, pady=(8, 0))

        self.input_var = tk.StringVar()
        input_entry = ttk.Entry(input_frame, textvariable=self.input_var)
        input_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        input_entry.bind("<Return>", lambda e: self._send_input())

        ttk.Button(input_frame, text="发送", command=self._send_input).pack(side=tk.LEFT, padx=(8, 0))

        # 自动连接
        self._connect(port, baud)

        # 绑定关闭事件
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _connect(self, port: str, baud: int):
        """连接串口"""
        self._disconnect()

        try:
            import serial
            self.serial = serial.Serial(port, baud, timeout=0.1)
            self.running = True
            self.status_label.configure(text=f"已连接 {port} @ {baud}", foreground="#0a7a2f")
            self._log(f"串口已打开: {port} @ {baud}")

            # 启动读取线程
            threading.Thread(target=self._read_loop, daemon=True).start()

        except Exception as e:
            self._log(f"连接失败: {e}")

    def _disconnect(self):
        """断开连接"""
        self.running = False
        if self.serial and self.serial.is_open:
            self.serial.close()
        self.serial = None

    def _read_loop(self):
        """读取数据循环"""
        while self.running and self.serial and self.serial.is_open:
            try:
                if self.serial.in_waiting:
                    data = self.serial.read(self.serial.in_waiting)
                    try:
                        text = data.decode('utf-8', errors='replace')
                        self.after(0, lambda t=text: self._log(t, newline=False))
                    except Exception:
                        pass
            except Exception:
                break
            time.sleep(0.01)

    def _log(self, msg: str, newline: bool = True):
        """添加日志"""
        suffix = "\n" if newline else ""
        self.text.configure(state=tk.NORMAL)
        self.text.insert(tk.END, msg + suffix)
        self.text.see(tk.END)
        self.text.configure(state=tk.DISABLED)

    def _send_input(self):
        """发送输入"""
        if not self.serial or not self.serial.is_open:
            return

        text = self.input_var.get()
        if text:
            try:
                self.serial.write((text + "\r\n").encode())
                self._log(f">>> {text}")
                self.input_var.set("")
            except Exception as e:
                self._log(f"发送失败: {e}")

    def _on_close(self):
        """关闭窗口"""
        self._disconnect()
        self.destroy()


def main():
    app = ESP32FlashGUI()
    app.mainloop()


if __name__ == "__main__":
    main()
