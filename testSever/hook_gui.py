"""
Universal Hook GUI (with embedded testSever)
============================================
Tkinter GUI for HookEngine + 嵌入式 testSever:
- 配置类型动态切换（特征码扫描 / 固定地址偏移）
- 进程列表选择器（psutil）
- 实时日志面板
- 启动 / 停止 / 测试发送 / 加载 / 保存配置
- 【新增】在子线程中启动 testSever (aiohttp + monitor_task)
- 【新增】宏配方编辑 + 下发到 ESP32

运行:
    python hook_gui.py
"""

import os
import sys
import json
import threading
import queue
import time
import asyncio
import webbrowser
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

try:
    import psutil
except ImportError:
    psutil = None

try:
    import requests
except ImportError:
    requests = None

from hook_engine import HookEngine, ConfigError, send_request, replace_placeholders

try:
    from esp32_flasher import ESP32Flasher, ESP32FlasherError
except ImportError:
    ESP32Flasher = None
    ESP32FlasherError = Exception


APP_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG_PATH = os.path.join(APP_DIR, "Universal_STGB_HP_Hook_config.json")
DEFAULT_PORT = 5000

# ============================================================
# 日志面板：跨线程安全
# ============================================================
class LogPanel:
    LEVEL_COLOR = {
        "INFO":  "#222222",
        "OK":    "#0a7a2f",
        "WARN":  "#b07d00",
        "ERROR": "#c0392b",
        "EVENT": "#1f4e9d",
        "NET":   "#6c3483",
        "SERVER": "#0b6e8a",
    }

    def __init__(self, text_widget: scrolledtext.ScrolledText):
        self.text = text_widget
        for lvl, color in self.LEVEL_COLOR.items():
            self.text.tag_configure(lvl, foreground=color)
        self.text.configure(state=tk.DISABLED)

    def post(self, level: str, msg: str):
        self.text.configure(state=tk.NORMAL)
        ts = time.strftime("%H:%M:%S")
        self.text.insert(tk.END, f"[{ts}] ", "INFO")
        self.text.insert(tk.END, f"[{level}] ", level)
        self.text.insert(tk.END, msg + "\n", level)
        self.text.see(tk.END)
        self.text.configure(state=tk.DISABLED)

    def clear(self):
        self.text.configure(state=tk.NORMAL)
        self.text.delete("1.0", tk.END)
        self.text.configure(state=tk.DISABLED)


# ============================================================
# 进程选择子窗口
# ============================================================
class ProcessPicker(tk.Toplevel):
    def __init__(self, master, on_pick):
        super().__init__(master)
        self.title("选择进程")
        self.geometry("520x440")
        self.transient(master)
        self.grab_set()
        self.on_pick = on_pick

        ttk.Label(self, text="双击行选择（按 PID / 名称过滤）").pack(anchor=tk.W, padx=8, pady=(8, 0))
        filt = ttk.Frame(self)
        filt.pack(fill=tk.X, padx=8)
        ttk.Label(filt, text="过滤:").pack(side=tk.LEFT)
        self.filter_var = tk.StringVar()
        entry = ttk.Entry(filt, textvariable=self.filter_var)
        entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 0))
        entry.bind("<KeyRelease>", lambda e: self.refresh())
        ttk.Button(filt, text="刷新", command=self.refresh).pack(side=tk.LEFT, padx=4)

        cols = ("pid", "name")
        self.tree = ttk.Treeview(self, columns=cols, show="headings", selectmode="browse")
        self.tree.heading("pid", text="PID")
        self.tree.heading("name", text="进程名")
        self.tree.column("pid", width=80, anchor=tk.W)
        self.tree.column("name", width=400, anchor=tk.W)
        self.tree.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        self.tree.bind("<Double-1>", self._on_dbl)
        self.tree.bind("<Return>", self._on_dbl)

        ttk.Button(self, text="取消", command=self.destroy).pack(side=tk.RIGHT, padx=8, pady=(0, 8))
        self.refresh()

    def refresh(self):
        if psutil is None:
            messagebox.showerror("缺少依赖", "请先 pip install psutil", parent=self)
            return
        self.tree.delete(*self.tree.get_children())
        kw = self.filter_var.get().strip().lower()
        procs = []
        for p in psutil.process_iter(["pid", "name"]):
            try:
                pinfo = p.info
                name = pinfo.get("name") or ""
                if kw and kw not in name.lower() and kw not in str(pinfo.get("pid")):
                    continue
                procs.append((pinfo.get("pid"), name))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        procs.sort(key=lambda x: (x[1] or "").lower())
        for pid, name in procs[:2000]:
            self.tree.insert("", tk.END, values=(pid, name))

    def _on_dbl(self, _evt=None):
        sel = self.tree.selection()
        if not sel:
            return
        pid, name = self.tree.item(sel[0], "values")
        self.on_pick(str(name))
        self.destroy()


# ============================================================
# 嵌入式 testSever 线程 (aiohttp + monitor_task)
# ============================================================
class TestSeverRunner:
    """
    在子线程中运行 testSever 的 asyncio 主入口。
    - log_cb(level, msg) 接收子线程日志
    - start() / stop()
    """

    def __init__(self, port: int, log_cb):
        self.port = port
        self.log_cb = log_cb
        self._thread = None
        self._loop = None
        self._stop_event = None  # type: asyncio.Event | None
        self._ready = threading.Event()
        self._failed = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        if self.is_running:
            self.log_cb("WARN", "testSever 已在运行")
            return
        self._ready.clear()
        self._failed = None
        self._thread = threading.Thread(target=self._thread_main, daemon=True)
        self._thread.start()
        # 等待子线程启动完成 / 失败 (最多 5 秒)
        if not self._ready.wait(timeout=5.0):
            self.log_cb("ERROR", "testSever 启动超时")
            return
        if self._failed:
            self.log_cb("ERROR", f"testSever 启动失败: {self._failed}")

    def stop(self, timeout: float = 3.0):
        if not self.is_running or self._loop is None:
            return
        loop = self._loop

        async def _shutdown():
            if self._stop_event is not None:
                self._stop_event.set()

        try:
            fut = asyncio.run_coroutine_threadsafe(_shutdown(), loop)
            fut.result(timeout=timeout)
        except Exception as e:
            self.log_cb("WARN", f"shutdown 调度失败: {e}")
        self._thread.join(timeout=timeout)
        self._thread = None
        self._loop = None
        self.log_cb("INFO", "testSever 已停止")

    def _thread_main(self):
        # Windows 兼容：使用 Selector 策略
        if hasattr(asyncio, "WindowsSelectorEventLoopPolicy"):
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._async_main())
        except Exception as e:
            self._failed = repr(e)
            self.log_cb("ERROR", f"testSever 主循环异常: {e}")
        finally:
            try:
                self._loop.close()
            except Exception:
                pass

    async def _async_main(self):
        # 延迟 import，避免主线程 import 阶段就被卡住
        try:
            import testSever
        except Exception as e:
            self._failed = f"无法 import testSever: {e}"
            self.log_cb("ERROR", self._failed)
            self._ready.set()
            return

        # 改写 testSever 的 stdout 到 GUI 日志
        self._install_log_bridge(testSever)

        self._stop_event = asyncio.Event()
        monitor_task = asyncio.create_task(testSever.monitor_task())
        server_task = asyncio.create_task(testSever.start_server(self.port))

        # 等待 server_task 完成 / 失败
        async def _wait_any():
            done, pending = await asyncio.wait(
                {monitor_task, server_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in done:
                if t is server_task and t.exception():
                    self._failed = repr(t.exception())
                    self.log_cb("ERROR", f"testSever start_server 异常: {self._failed}")
            # 让另一方也能干净退出
            for t in pending:
                t.cancel()

        try:
            self.log_cb("OK", f"testSever 已启动: http://127.0.0.1:{self.port}")
            self._ready.set()
            await self._stop_event.wait()
            await _wait_any()
        except Exception as e:
            self._failed = repr(e)
            self.log_cb("ERROR", f"testSever 主循环异常: {e}")
            self._ready.set()
            return
        finally:
            for t in (monitor_task, server_task):
                if t is not None and not t.done():
                    t.cancel()
                    try:
                        await t
                    except (asyncio.CancelledError, Exception):
                        pass

    def _install_log_bridge(self, testSever_mod):
        """
        预留钩子：在 testSever 模块装上日志桥 (未来需要时实现)。
        当前版本不替换 print, 避免影响 aiohttp 内部输出。
        """
        return


# ============================================================
# ESP32 烧录页签
# ============================================================
class ESP32FlasherTab(ttk.Frame):
    """
    ESP32 固件编译和烧录界面
    """

    def __init__(self, master, log_panel: LogPanel):
        super().__init__(master, padding=10)
        self.logger = log_panel

        if ESP32Flasher is None:
            self._build_disabled_ui()
            return

        self.flasher = ESP32Flasher(project_path=None, log_callback=self._on_log)
        self._build_ui()

    def _build_disabled_ui(self):
        """ESP32 Flasher 不可用时的界面"""
        msg = ttk.Label(
            self,
            text="ESP32 Flasher 不可用\n请确保 esp32_flasher.py 在同一目录下",
            font=("", 12),
            foreground="#c0392b",
        )
        msg.pack(pady=40)

    def _build_ui(self):
        """构建烧录界面"""
        # 顶部状态栏
        status_bar = ttk.Frame(self)
        status_bar.pack(fill=tk.X, pady=(0, 10))

        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(status_bar, text="状态:").pack(side=tk.LEFT)
        ttk.Label(status_bar, textvariable=self.status_var, foreground="#0b6e8a").pack(side=tk.LEFT, padx=4)

        ttk.Button(status_bar, text="刷新串口", command=self._refresh_ports).pack(side=tk.RIGHT)

        # 左侧：编译区域
        left_frame = ttk.LabelFrame(self, text="编译固件", padding=10)
        left_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 5))

        # Firmware info
        self.firmware_info_var = tk.StringVar(value="未编译")
        ttk.Label(left_frame, text="固件:").pack(anchor=tk.W)
        ttk.Label(left_frame, textvariable=self.firmware_info_var, foreground="#666666").pack(anchor=tk.W)

        # 编译按钮
        btn_frame = ttk.Frame(left_frame)
        btn_frame.pack(fill=tk.X, pady=10)

        self.compile_btn = ttk.Button(btn_frame, text="编译固件", command=self._do_compile)
        self.compile_btn.pack(side=tk.LEFT, padx=(0, 4))

        self.compile_clean_btn = ttk.Button(btn_frame, text="清理并编译", command=self._do_compile_clean)
        self.compile_clean_btn.pack(side=tk.LEFT, padx=4)

        self.compile_status = tk.StringVar(value="")
        ttk.Label(btn_frame, textvariable=self.compile_status, foreground="#666666").pack(side=tk.LEFT, padx=8)

        # PlatformIO 检查
        self.pio_available = self.flasher.is_pio_available()
        if not self.pio_available:
            ttk.Label(
                left_frame,
                text="警告: PlatformIO 不可用\n请确保已安装: pip install platformio",
                foreground="#c0392b",
            ).pack(pady=5)

        # 右侧：烧录区域
        right_frame = ttk.LabelFrame(self, text="烧录到 ESP32", padding=10)
        right_frame.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=(5, 0))

        # 串口选择
        ttk.Label(right_frame, text="串口:").pack(anchor=tk.W)
        port_frame = ttk.Frame(right_frame)
        port_frame.pack(fill=tk.X, pady=4)

        self.port_var = tk.StringVar()
        self.port_combo = ttk.Combobox(port_frame, textvariable=self.port_var, state="readonly", width=15)
        self.port_combo.pack(side=tk.LEFT, fill=tk.X, expand=True)

        ttk.Button(port_frame, text="刷新", command=self._refresh_ports).pack(side=tk.LEFT, padx=(4, 0))

        # 波特率
        ttk.Label(right_frame, text="波特率:").pack(anchor=tk.W, pady=(8, 0))
        self.baud_var = tk.StringVar(value="460800")
        baud_frame = ttk.Frame(right_frame)
        baud_frame.pack(fill=tk.X, pady=4)

        for baud in ["115200", "256000", "460800", "921600"]:
            rb = ttk.Radiobutton(baud_frame, text=baud, variable=self.baud_var, value=baud)
            rb.pack(side=tk.LEFT, padx=(0, 8))

        # Flash 参数
        ttk.Label(right_frame, text="Flash 参数:").pack(anchor=tk.W, pady=(8, 0))
        flash_frame = ttk.Frame(right_frame)
        flash_frame.pack(fill=tk.X, pady=4)

        ttk.Label(flash_frame, text="大小:").pack(side=tk.LEFT)
        self.flash_size_var = tk.StringVar(value="4MB")
        size_combo = ttk.Combobox(
            flash_frame, textvariable=self.flash_size_var,
            values=["1MB", "2MB", "4MB", "8MB", "16MB"], state="readonly", width=6
        )
        size_combo.pack(side=tk.LEFT, padx=(4, 12))

        ttk.Label(flash_frame, text="频率:").pack(side=tk.LEFT)
        self.flash_freq_var = tk.StringVar(value="80m")
        freq_combo = ttk.Combobox(
            flash_frame, textvariable=self.flash_freq_var,
            values=["40m", "80m"], state="readonly", width=5
        )
        freq_combo.pack(side=tk.LEFT, padx=(4, 0))

        # 烧录按钮
        flash_btn_frame = ttk.Frame(right_frame)
        flash_btn_frame.pack(fill=tk.X, pady=10)

        self.flash_btn = ttk.Button(flash_btn_frame, text="烧录固件", command=self._do_flash)
        self.flash_btn.pack(fill=tk.X)

        self.flash_status = tk.StringVar(value="")
        ttk.Label(flash_btn_frame, textvariable=self.flash_status, foreground="#666666").pack(pady=4)

        # 串口监视器按钮
        monitor_frame = ttk.Frame(right_frame)
        monitor_frame.pack(fill=tk.X, pady=(8, 0))

        self.monitor_btn = ttk.Button(
            monitor_frame, text="打开串口监视器", command=self._open_monitor
        )
        self.monitor_btn.pack(fill=tk.X)

        # 底部快捷操作
        quick_frame = ttk.LabelFrame(self, text="一键操作", padding=10)
        quick_frame.pack(fill=tk.X, pady=(10, 0))

        self.quick_flash_btn = ttk.Button(
            quick_frame,
            text="编译 + 烧录",
            command=self._do_quick_flash,
        )
        self.quick_flash_btn.pack(side=tk.LEFT, padx=(0, 8))

        self.quick_flash_clean_btn = ttk.Button(
            quick_frame,
            text="清理 + 编译 + 烧录",
            command=self._do_quick_flash_clean,
        )
        self.quick_flash_clean_btn.pack(side=tk.LEFT)

        # 初始刷新串口
        self._refresh_ports()

    def _on_log(self, level: str, msg: str):
        """日志回调"""
        self.logger.post(level, msg)

    def _refresh_ports(self):
        """刷新串口列表"""
        try:
            ports = self.flasher.list_ports(force_refresh=True)
            port_names = [p["port"] for p in ports]
            self.port_combo["values"] = port_names
            if port_names:
                self.port_combo.set(port_names[0])
            else:
                self.port_combo.set("")
        except Exception as e:
            self.logger.post("WARN", f"刷新串口失败: {e}")

    def _update_firmware_info(self):
        """更新固件信息"""
        info = self.flasher.get_firmware_info()
        if info:
            self.firmware_info_var.set(f"{info['path']}\n大小: {info['size_str']}")
        else:
            self.firmware_info_var.set("未编译")

    def _do_compile(self):
        """执行编译"""
        if not self.flasher.is_pio_available():
            messagebox.showerror("错误", "PlatformIO 不可用，请先安装", parent=self)
            return

        self.compile_btn.configure(state=tk.DISABLED)
        self.compile_clean_btn.configure(state=tk.DISABLED)
        self.compile_status.set("编译中...")
        self.status_var.set("正在编译...")

        def worker():
            try:
                self.flasher.compile(background=False)
                self.after(0, self._on_compile_done, True)
            except Exception as e:
                self.after(0, self._on_compile_done, False, str(e))

        threading.Thread(target=worker, daemon=True).start()

    def _do_compile_clean(self):
        """执行清理并编译"""
        if not self.flasher.is_pio_available():
            messagebox.showerror("错误", "PlatformIO 不可用，请先安装", parent=self)
            return

        if not messagebox.askyesno("确认", "清理编译会删除所有已编译文件，确定继续？", parent=self):
            return

        self.compile_btn.configure(state=tk.DISABLED)
        self.compile_clean_btn.configure(state=tk.DISABLED)
        self.compile_status.set("清理并编译中...")
        self.status_var.set("正在清理并编译...")

        def worker():
            try:
                self.flasher.compile(clean=True, background=False)
                self.after(0, self._on_compile_done, True)
            except Exception as e:
                self.after(0, self._on_compile_done, False, str(e))

        threading.Thread(target=worker, daemon=True).start()

    def _on_compile_done(self, success: bool, error: str = ""):
        """编译完成回调"""
        self.compile_btn.configure(state=tk.NORMAL)
        self.compile_clean_btn.configure(state=tk.NORMAL)

        if success:
            self.compile_status.set("编译完成")
            self.status_var.set("编译完成")
            self._update_firmware_info()
        else:
            self.compile_status.set("编译失败")
            self.status_var.set("编译失败")
            if error:
                self.logger.post("ERROR", f"编译异常: {error}")

    def _do_flash(self):
        """执行烧录"""
        port = self.port_var.get()
        if not port:
            messagebox.showwarning("请选择串口", "请先选择一个串口", parent=self)
            return

        try:
            baud = int(self.baud_var.get())
        except ValueError:
            baud = 460800

        self.flash_btn.configure(state=tk.DISABLED, text="烧录中...")
        self.status_var.set("正在烧录...")

        def worker():
            try:
                success = self.flasher.flash(
                    port=port,
                    baud=baud,
                    flash_size=self.flash_size_var.get(),
                    flash_freq=self.flash_freq_var.get(),
                    background=False,
                )
                self.after(0, self._on_flash_done, success)
            except Exception as e:
                self.after(0, self._on_flash_done, False, str(e))

        threading.Thread(target=worker, daemon=True).start()

    def _on_flash_done(self, success: bool, error: str = ""):
        """烧录完成回调"""
        self.flash_btn.configure(state=tk.NORMAL, text="烧录固件")

        if success:
            self.flash_status.set("烧录成功")
            self.status_var.set("烧录完成")
        else:
            self.flash_status.set("烧录失败")
            self.status_var.set("烧录失败")
            if error:
                self.logger.post("ERROR", f"烧录异常: {error}")

    def _do_quick_flash(self):
        """一键编译并烧录"""
        port = self.port_var.get()
        if not port:
            messagebox.showwarning("请选择串口", "请先选择一个串口", parent=self)
            return

        if not self.flasher.is_pio_available():
            messagebox.showerror("错误", "PlatformIO 不可用", parent=self)
            return

        self.quick_flash_btn.configure(state=tk.DISABLED)
        self.quick_flash_clean_btn.configure(state=tk.DISABLED)
        self.compile_btn.configure(state=tk.DISABLED)
        self.compile_clean_btn.configure(state=tk.DISABLED)
        self.flash_btn.configure(state=tk.DISABLED)
        self.status_var.set("一键烧录中...")

        try:
            baud = int(self.baud_var.get())
        except ValueError:
            baud = 460800

        def worker():
            try:
                # 编译
                self.after(0, lambda: self.logger.post("INFO", "=== 开始一键烧录 ==="))
                self.after(0, lambda: self.logger.post("INFO", "步骤 1/2: 编译固件..."))
                self.after(0, lambda: self.compile_status.set("编译中..."))

                compile_ok = self.flasher.compile(clean=False, background=False)
                if not compile_ok:
                    self.after(0, self._on_quick_flash_fail, "编译失败")
                    return

                self.after(0, lambda: self.compile_status.set("编译完成"))
                self.after(0, lambda: self.logger.post("OK", "编译完成"))

                # 烧录
                self.after(0, lambda: self.logger.post("INFO", f"步骤 2/2: 烧录到 {port}..."))
                self.after(0, lambda: self.flash_status.set("烧录中..."))

                flash_ok = self.flasher.flash(
                    port=port,
                    baud=baud,
                    flash_size=self.flash_size_var.get(),
                    flash_freq=self.flash_freq_var.get(),
                    background=False,
                )

                if flash_ok:
                    self.after(0, self._on_quick_flash_done)
                else:
                    self.after(0, self._on_quick_flash_fail, "烧录失败")

            except Exception as e:
                self.after(0, self._on_quick_flash_fail, str(e))

        threading.Thread(target=worker, daemon=True).start()

    def _do_quick_flash_clean(self):
        """清理并一键编译烧录"""
        if not messagebox.askyesno("确认", "清理编译会删除所有已编译文件，确定继续？", parent=self):
            return
        self._do_quick_flash()

    def _on_quick_flash_done(self):
        """一键烧录完成"""
        self.logger.post("OK", "=== 一键烧录完成 ===")
        self.status_var.set("一键烧录完成")
        self._update_buttons()

    def _on_quick_flash_fail(self, error: str):
        """一键烧录失败"""
        self.logger.post("ERROR", f"一键烧录失败: {error}")
        self.status_var.set("一键烧录失败")
        self._update_buttons()

    def _update_buttons(self):
        """更新按钮状态"""
        self.quick_flash_btn.configure(state=tk.NORMAL)
        self.quick_flash_clean_btn.configure(state=tk.NORMAL)
        self.compile_btn.configure(state=tk.NORMAL)
        self.compile_clean_btn.configure(state=tk.NORMAL)
        self.flash_btn.configure(state=tk.NORMAL)
        self.compile_status.set("")
        self.flash_status.set("")

    def _open_monitor(self):
        """打开串口监视器"""
        port = self.port_var.get()
        if not port:
            messagebox.showwarning("请选择串口", "请先选择一个串口", parent=self)
            return

        if not self.flasher.is_pio_available():
            messagebox.showerror("错误", "PlatformIO 不可用，请先安装", parent=self)
            return

        try:
            self.flasher.open_serial_monitor(port)
            self.logger.post("INFO", f"已打开串口监视器: {port}")
        except Exception as e:
            messagebox.showerror("错误", f"无法打开串口监视器: {e}", parent=self)


# ============================================================
# 宏编辑页签
# ============================================================
class MacroEditor(ttk.Frame):
    """
    复用 testSever 的 MacroBuilder (构造二进制) + 直接打设备.
    注意: testSever 的宏定义接口是 POST /macros/{id} (JSON steps),
          GUI 也可以走 HTTP 到 testSever 自己的 API。
    """

    def __init__(self, master, log_panel: LogPanel, get_server_url):
        super().__init__(master, padding=8)
        self.logger = log_panel
        self.get_server_url = get_server_url  # callable: () -> str | None

        top = ttk.Frame(self)
        top.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(top, text="宏 ID:").pack(side=tk.LEFT)
        self.macro_id_var = tk.IntVar(value=0)
        ttk.Spinbox(top, from_=0, to=9, width=6, textvariable=self.macro_id_var).pack(side=tk.LEFT, padx=4)
        ttk.Button(top, text="发送到设备", command=self.action_send).pack(side=tk.LEFT, padx=8)
        ttk.Button(top, text="运行一次", command=self.action_run).pack(side=tk.LEFT)

        # 步骤列表
        cols = ("action", "duration")
        self.tree = ttk.Treeview(self, columns=cols, show="headings", height=10)
        self.tree.heading("action", text="动作")
        self.tree.heading("duration", text="时长 (ms)")
        self.tree.column("action", width=120, anchor=tk.W)
        self.tree.column("duration", width=120, anchor=tk.W)
        self.tree.pack(fill=tk.BOTH, expand=True)

        # 添加按钮
        bar = ttk.Frame(self)
        bar.pack(fill=tk.X, pady=8)
        for act in ("btn1", "btn2", "btn3", "delay"):
            ttk.Button(bar, text=f"+ {act}", command=lambda a=act: self._add_step(a)).pack(side=tk.LEFT, padx=2)
        ttk.Button(bar, text="- 删除选中", command=self._del_step).pack(side=tk.LEFT, padx=8)
        ttk.Button(bar, text="清空", command=self._clear).pack(side=tk.LEFT)

        # 初始化一个示例
        self._steps = [
            {"action": "btn1", "duration": 100},
            {"action": "delay", "duration": 500},
            {"action": "btn2", "duration": 200},
        ]
        self._refresh_tree()

    def _refresh_tree(self):
        self.tree.delete(*self.tree.get_children())
        for s in self._steps:
            self.tree.insert("", tk.END, values=(s["action"], s["duration"]))

    def _add_step(self, action: str):
        default = 500 if action == "delay" else 120
        self._steps.append({"action": action, "duration": default})
        self._refresh_tree()

    def _del_step(self):
        sel = self.tree.selection()
        if not sel:
            return
        idx = self.tree.index(sel[0])
        del self._steps[idx]
        self._refresh_tree()

    def _clear(self):
        self._steps.clear()
        self._refresh_tree()

    def _validate(self) -> bool:
        if not self._steps:
            messagebox.showwarning("空", "请先添加至少一个步骤", parent=self)
            return False
        for s in self._steps:
            if s["action"] not in ("btn1", "btn2", "btn3", "delay"):
                messagebox.showerror("错误", f"非法 action: {s['action']}", parent=self)
                return False
        return True

    def _server_url(self):
        u = self.get_server_url()
        if not u:
            messagebox.showerror("未启动", "请先在【服务器】标签页启动 testSever", parent=self)
            return None
        return u

    def action_send(self):
        if not self._validate():
            return
        url = self._server_url()
        if not url:
            return
        mid = int(self.macro_id_var.get())
        if requests is None:
            messagebox.showerror("依赖缺失", "请 pip install requests", parent=self)
            return
        try:
            r = requests.post(
                f"{url}/macros/{mid}",
                json={"steps": self._steps},
                timeout=5,
            )
            self.logger.post("INFO", f"定义宏 {mid} -> HTTP {r.status_code}: {r.text[:200]}")
        except Exception as e:
            self.logger.post("ERROR", f"定义宏失败: {e}")

    def action_run(self):
        url = self._server_url()
        if not url:
            return
        mid = int(self.macro_id_var.get())
        if requests is None:
            messagebox.showerror("依赖缺失", "请 pip install requests", parent=self)
            return
        try:
            r = requests.post(f"{url}/macros/{mid}/run", timeout=5)
            self.logger.post("INFO", f"运行宏 {mid} -> HTTP {r.status_code}: {r.text[:200]}")
        except Exception as e:
            self.logger.post("ERROR", f"运行宏失败: {e}")


# ============================================================
# 主应用
# ============================================================
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Universal Hook Tool - GUI (with embedded testSever)")
        self.geometry("1040x800")
        self.minsize(880, 620)

        self.engine: HookEngine | None = None
        self.server: TestSeverRunner | None = None

        self._build_widgets()
        self._load_default_config()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- UI 构建 ----------
    def _build_widgets(self):
        nb = ttk.Notebook(self)
        nb.pack(fill=tk.BOTH, expand=True)

        self.tab_hook = ttk.Frame(nb)
        self.tab_server = ttk.Frame(nb)
        self.tab_macro = ttk.Frame(nb)
        nb.add(self.tab_hook, text="Hook 配置")
        nb.add(self.tab_server, text="服务器")
        nb.add(self.tab_macro, text="宏编辑")

        self._build_hook_tab(self.tab_hook)
        self._build_server_tab(self.tab_server)
        self._build_macro_tab(self.tab_macro)

        # 底部状态栏
        self.status_var = tk.StringVar(value="就绪")
        bar = ttk.Frame(self, padding=6)
        bar.pack(fill=tk.X)
        ttk.Label(bar, textvariable=self.status_var).pack(side=tk.LEFT)
        ttk.Button(bar, text="清空日志", command=self.action_clear_log).pack(side=tk.RIGHT)

    # ---------- Hook 配置页签 ----------
    def _build_hook_tab(self, parent):
        # 顶部工具条
        bar = ttk.Frame(parent, padding=6)
        bar.pack(fill=tk.X)
        ttk.Button(bar, text="加载配置", command=self.action_load).pack(side=tk.LEFT)
        ttk.Button(bar, text="保存配置", command=self.action_save).pack(side=tk.LEFT, padx=4)
        ttk.Button(bar, text="另存为", command=self.action_save_as).pack(side=tk.LEFT, padx=4)
        ttk.Separator(bar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=8)
        ttk.Button(bar, text="测试发送", command=self.action_test_send).pack(side=tk.LEFT)
        ttk.Button(bar, text="绑定到服务器 URL", command=self.action_bind_server_url).pack(side=tk.LEFT, padx=4)

        # 主体：左 配置 / 右 日志
        body = ttk.Panedwindow(parent, orient=tk.HORIZONTAL)
        body.pack(fill=tk.BOTH, expand=True, padx=6, pady=(0, 6))

        left = ttk.Frame(body)
        body.add(left, weight=3)

        right = ttk.Frame(body)
        body.add(right, weight=2)

        # 配置类型
        type_box = ttk.LabelFrame(left, text="配置类型", padding=8)
        type_box.pack(fill=tk.X, pady=(0, 6))
        self.mode_var = tk.StringVar(value="pattern")
        ttk.Radiobutton(type_box, text="特征码扫描 (Pattern)",
                        variable=self.mode_var, value="pattern",
                        command=self._on_mode_change).pack(anchor=tk.W)
        ttk.Radiobutton(type_box, text="固定地址偏移 (Address Offset)",
                        variable=self.mode_var, value="offset",
                        command=self._on_mode_change).pack(anchor=tk.W)

        # 基础设置
        base = ttk.LabelFrame(left, text="基础设置", padding=8)
        base.pack(fill=tk.X, pady=(0, 6))
        self._mk_entry(base, "进程名", "process_name", row=0)
        ttk.Button(base, text="选择...", command=self.action_pick_process).grid(row=0, column=2, padx=4)
        self._mk_entry(base, "目标 URL", "target_url", row=1)
        self._mk_combobox(base, "方法", "method", ["POST", "GET"], row=2)
        self._mk_entry(base, "超时 (秒)", "timeout", row=3)
        self._mk_entry(base, "防抖 (秒)", "debounce_seconds", row=4)

        # Hook 定位
        hook_box = ttk.LabelFrame(left, text="Hook 定位", padding=8)
        hook_box.pack(fill=tk.X, pady=(0, 6))
        self.pattern_var = tk.StringVar()
        self.offset_var = tk.StringVar(value="0")
        self.func_entry_offset_var = tk.StringVar(value="-34")
        self.read_type_var = tk.StringVar(value="int")
        self.addr_var = tk.StringVar()
        ttk.Label(hook_box, text="Pattern:").grid(row=0, column=0, sticky=tk.W, pady=2)
        self.pattern_entry = ttk.Entry(hook_box, textvariable=self.pattern_var)
        self.pattern_entry.grid(row=0, column=1, sticky=tk.EW, padx=4)
        ttk.Label(hook_box, text="Offset (字节):").grid(row=1, column=0, sticky=tk.W, pady=2)
        ttk.Entry(hook_box, textvariable=self.offset_var).grid(row=1, column=1, sticky=tk.EW, padx=4)
        ttk.Label(hook_box, text="函数入口回溯偏移:").grid(row=2, column=0, sticky=tk.W, pady=2)
        ttk.Entry(hook_box, textvariable=self.func_entry_offset_var).grid(row=2, column=1, sticky=tk.EW, padx=4)
        ttk.Label(hook_box, text="读取类型:").grid(row=3, column=0, sticky=tk.W, pady=2)
        ttk.Combobox(hook_box, textvariable=self.read_type_var,
                     values=["int", "byte", "short", "sbyte"], state="readonly").grid(row=3, column=1, sticky=tk.EW, padx=4)
        ttk.Label(hook_box, text="Address Offset (hex):").grid(row=4, column=0, sticky=tk.W, pady=2)
        self.addr_entry = ttk.Entry(hook_box, textvariable=self.addr_var)
        self.addr_entry.grid(row=4, column=1, sticky=tk.EW, padx=4)
        hook_box.columnconfigure(1, weight=1)

        # Headers
        head_box = ttk.LabelFrame(left, text="HTTP Headers (JSON)", padding=8)
        head_box.pack(fill=tk.X, pady=(0, 6))
        self.headers_text = tk.Text(head_box, height=4)
        self.headers_text.pack(fill=tk.X)

        # Payload
        pay_box = ttk.LabelFrame(left, text="Payload (JSON, 支持 $VAL / $TIME)", padding=8)
        pay_box.pack(fill=tk.BOTH, expand=True)
        self.payload_text = tk.Text(pay_box, height=8)
        self.payload_text.pack(fill=tk.BOTH, expand=True)

        # 底部按钮
        btn_bar = ttk.Frame(parent, padding=8)
        btn_bar.pack(fill=tk.X)
        self.start_btn = ttk.Button(btn_bar, text="▶ 启动 Hook", command=self.action_start)
        self.start_btn.pack(side=tk.LEFT)
        self.stop_btn = ttk.Button(btn_bar, text="■ 停止", command=self.action_stop, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=6)

        # 日志面板 (放在 right)
        log_box = ttk.LabelFrame(right, text="实时日志", padding=4)
        log_box.pack(fill=tk.BOTH, expand=True)
        self.log_text = scrolledtext.ScrolledText(log_box, wrap=tk.NONE, height=20, font=("Consolas", 10))
        self.log_text.pack(fill=tk.BOTH, expand=True)
        self.logger = LogPanel(self.log_text)

    # ---------- 服务器页签 ----------
    def _build_server_tab(self, parent):
        frm = ttk.Frame(parent, padding=16)
        frm.pack(fill=tk.X)

        ttk.Label(frm, text="嵌入式 testSever (aiohttp + monitor_task)", font=("", 11, "bold")).grid(row=0, column=0, columnspan=4, sticky=tk.W, pady=(0, 12))

        ttk.Label(frm, text="端口:").grid(row=1, column=0, sticky=tk.W, pady=4)
        self.port_var = tk.IntVar(value=DEFAULT_PORT)
        ttk.Entry(frm, textvariable=self.port_var, width=8).grid(row=1, column=1, sticky=tk.W, pady=4)

        self.server_btn = ttk.Button(frm, text="▶ 启动服务器", command=self.action_toggle_server)
        self.server_btn.grid(row=1, column=2, padx=8, pady=4)

        ttk.Button(frm, text="打开网页", command=self.action_open_web).grid(row=1, column=3, padx=4, pady=4)

        ttk.Separator(frm, orient=tk.HORIZONTAL).grid(row=2, column=0, columnspan=4, sticky=tk.EW, pady=12)

        self.server_status_var = tk.StringVar(value="未运行")
        ttk.Label(frm, text="状态:").grid(row=3, column=0, sticky=tk.W, pady=4)
        self.server_status_lbl = ttk.Label(frm, textvariable=self.server_status_var, foreground="#a04040")
        self.server_status_lbl.grid(row=3, column=1, columnspan=3, sticky=tk.W, pady=4)

        self.server_url_var = tk.StringVar(value="")
        ttk.Label(frm, text="URL:").grid(row=4, column=0, sticky=tk.W, pady=4)
        ttk.Label(frm, textvariable=self.server_url_var, foreground="#0b6e8a").grid(row=4, column=1, columnspan=3, sticky=tk.W, pady=4)

        # 说明
        info = ttk.LabelFrame(parent, text="使用说明", padding=10)
        info.pack(fill=tk.BOTH, expand=True, padx=16, pady=(0, 16))
        text = (
            "1. 点击【▶ 启动服务器】在子线程中启动 testSever (aiohttp :5000)\n"
            "2. 服务器状态显示在【服务器】页签\n"
            "3. Hook 推送目标可以是 http://127.0.0.1:5000/macros/0/run\n"
            "   也可以在【Hook 配置】页点【绑定到服务器 URL】自动填充\n"
            "4. 关闭主窗口会一并停止服务器与 Hook"
        )
        ttk.Label(info, text=text, justify=tk.LEFT).pack(anchor=tk.W)

    # ---------- 宏编辑页签 ----------
    def _build_macro_tab(self, parent):
        self.macro_editor = MacroEditor(
            parent,
            log_panel=self.logger,
            get_server_url=self._current_server_url,
        )
        self.macro_editor.pack(fill=tk.BOTH, expand=True)

    # ---------- helpers ----------
    def _mk_entry(self, parent, label, key, row):
        ttk.Label(parent, text=label + ":").grid(row=row, column=0, sticky=tk.W, pady=2)
        var = tk.StringVar()
        e = ttk.Entry(parent, textvariable=var)
        e.grid(row=row, column=1, sticky=tk.EW, padx=4)
        parent.columnconfigure(1, weight=1)
        setattr(self, f"_{key}_var", var)

    def _mk_combobox(self, parent, label, key, values, row):
        ttk.Label(parent, text=label + ":").grid(row=row, column=0, sticky=tk.W, pady=2)
        var = tk.StringVar()
        cb = ttk.Combobox(parent, textvariable=var, values=values, state="readonly", width=10)
        cb.grid(row=row, column=1, sticky=tk.W, padx=4)
        setattr(self, f"_{key}_var", var)

    def _on_mode_change(self):
        mode = self.mode_var.get()
        if mode == "pattern":
            self.pattern_entry.configure(state="normal")
            self.addr_entry.configure(state="disabled")
        else:
            self.pattern_entry.configure(state="disabled")
            self.addr_entry.configure(state="normal")

    def _current_server_url(self) -> str | None:
        if self.server is None or not self.server.is_running:
            return None
        return f"http://127.0.0.1:{int(self.port_var.get())}"

    # ---------- 配置读写 ----------
    def _load_default_config(self):
        if os.path.exists(DEFAULT_CONFIG_PATH):
            try:
                self.action_load(path=DEFAULT_CONFIG_PATH)
                return
            except Exception as e:
                self.logger.post("WARN", f"加载默认配置失败: {e}")
        self._apply_config({
            "settings": {
                "process_name": "th15.exe",
                "target_url": "http://127.0.0.1:5000/macros/0/run",
                "method": "POST",
                "timeout": 2,
                "debounce_seconds": 1.0,
            },
            "scan": {"address_offset": "", "pattern": "", "offset_bytes": 0, "func_entry_offset": -34, "read_type": "int"},
            "headers": {"Content-Type": "application/json"},
            "payload": {"event": "miss", "hp": "$VAL"},
        })

    def action_load(self, path: str | None = None):
        if path is None:
            path = filedialog.askopenfilename(
                title="选择配置文件",
                filetypes=[("JSON", "*.json"), ("All", "*.*")],
                initialdir=APP_DIR,
            )
            if not path:
                return
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        self._apply_config(cfg)
        self.logger.post("INFO", f"已加载配置: {path}")

    def _apply_config(self, cfg: dict):
        s = cfg.get("settings", {})
        self._process_name_var.set(s.get("process_name", ""))
        self._target_url_var.set(s.get("target_url", ""))
        self._method_var.set(str(s.get("method", "POST")).upper())
        self._timeout_var.set(str(s.get("timeout", 2)))
        self._debounce_seconds_var.set(str(s.get("debounce_seconds", 0)))

        scan = cfg.get("scan", {})
        addr = str(scan.get("address_offset", "") or "")
        pattern = str(scan.get("pattern", "") or "")
        if addr and addr != "0x0":
            self.mode_var.set("offset")
        else:
            self.mode_var.set("pattern")
        self.addr_var.set(addr)
        self.pattern_var.set(pattern)
        self.offset_var.set(str(scan.get("offset_bytes", 0)))
        self.func_entry_offset_var.set(str(scan.get("func_entry_offset", -34)))
        self.read_type_var.set(str(scan.get("read_type", "int")))
        self._on_mode_change()

        self.headers_text.delete("1.0", tk.END)
        self.headers_text.insert("1.0", json.dumps(cfg.get("headers", {}), ensure_ascii=False, indent=2))
        self.payload_text.delete("1.0", tk.END)
        self.payload_text.insert("1.0", json.dumps(cfg.get("payload", {}), ensure_ascii=False, indent=2))

    def action_save(self):
        self.action_save_as(path=DEFAULT_CONFIG_PATH)

    def action_save_as(self, path: str | None = None):
        try:
            cfg = self._collect_config()
        except Exception as e:
            messagebox.showerror("配置错误", str(e), parent=self)
            return
        if path is None:
            path = filedialog.asksaveasfilename(
                title="保存配置",
                defaultextension=".json",
                filetypes=[("JSON", "*.json")],
                initialdir=APP_DIR,
            )
            if not path:
                return
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        self.logger.post("OK", f"已保存配置: {path}")

    def _collect_config(self) -> dict:
        headers_raw = self.headers_text.get("1.0", tk.END).strip() or "{}"
        payload_raw = self.payload_text.get("1.0", tk.END).strip() or "{}"
        try:
            headers = json.loads(headers_raw)
        except json.JSONDecodeError as e:
            raise ValueError(f"Headers 不是合法 JSON: {e}")
        try:
            payload = json.loads(payload_raw)
        except json.JSONDecodeError as e:
            raise ValueError(f"Payload 不是合法 JSON: {e}")

        scan = {
            "address_offset": self.addr_var.get().strip(),
            "pattern": self.pattern_var.get().strip(),
            "offset_bytes": int(self.offset_var.get() or 0),
            "func_entry_offset": int(self.func_entry_offset_var.get() or -34),
            "read_type": self.read_type_var.get().strip() or "int",
        }
        cfg = {
            "settings": {
                "process_name": self._process_name_var.get().strip(),
                "target_url": self._target_url_var.get().strip(),
                "method": self._method_var.get().strip().upper() or "POST",
                "timeout": int(float(self._timeout_var.get() or 2)),
                "debounce_seconds": float(self._debounce_seconds_var.get() or 0),
            },
            "scan": scan,
            "headers": headers,
            "payload": payload,
        }
        from hook_engine import validate_config
        validate_config(cfg)
        return cfg

    # ---------- 进程选择 ----------
    def action_pick_process(self):
        ProcessPicker(self, on_pick=lambda name: self._process_name_var.set(name))

    # ---------- 服务器控制 ----------
    def action_toggle_server(self):
        if self.server is None or not self.server.is_running:
            self._start_server()
        else:
            self._stop_server()

    def _start_server(self):
        port = int(self.port_var.get() or DEFAULT_PORT)
        self.server = TestSeverRunner(port=port, log_cb=self._server_log)
        self.server.start()
        if self.server.is_running and self.server._failed is None:
            self.server_btn.configure(text="■ 停止服务器")
            self.server_status_var.set("运行中")
            self.server_status_lbl.configure(foreground="#0a7a2f")
            url = self._current_server_url()
            self.server_url_var.set(url or "")
            self.logger.post("SERVER", f"testSever 已启动于 {url}")
        else:
            self.server = None
            self.server_btn.configure(text="▶ 启动服务器")
            self.server_status_var.set("启动失败")
            self.server_status_lbl.configure(foreground="#c0392b")

    def _stop_server(self):
        if self.server is None:
            return
        self.logger.post("SERVER", "正在停止 testSever...")
        self.server.stop()
        self.server = None
        self.server_btn.configure(text="▶ 启动服务器")
        self.server_status_var.set("未运行")
        self.server_status_lbl.configure(foreground="#a04040")
        self.server_url_var.set("")

    def action_open_web(self):
        url = self._current_server_url() or f"http://127.0.0.1:{int(self.port_var.get())}"
        try:
            webbrowser.open(url)
            self.logger.post("INFO", f"打开网页: {url}")
        except Exception as e:
            self.logger.post("ERROR", f"打开网页失败: {e}")

    def action_bind_server_url(self):
        url = self._current_server_url()
        if not url:
            messagebox.showwarning("未启动", "请先在【服务器】页签启动 testSever", parent=self)
            return
        # 拼出默认的 /macros/0/run 路径
        macro_id = 0
        new_url = f"{url}/macros/{macro_id}/run"
        self._target_url_var.set(new_url)
        self.logger.post("OK", f"已绑定目标 URL: {new_url}")

    # ---------- Hook 控制 ----------
    def action_start(self):
        try:
            cfg = self._collect_config()
        except Exception as e:
            messagebox.showerror("配置错误", str(e), parent=self)
            return

        self.logger.post("INFO", "=" * 40)
        self.logger.post("INFO", f"启动 Hook: {cfg['settings']['process_name']}")
        self.logger.post("INFO", f"模式: {self.mode_var.get()}")
        self.logger.post("INFO", "=" * 40)

        self.engine = HookEngine(cfg, log_cb=self._engine_log)
        try:
            self.engine.start(in_thread=True)
        except Exception as e:
            messagebox.showerror("启动失败", str(e), parent=self)
            self.engine = None
            return

        self.start_btn.configure(state=tk.DISABLED)
        self.stop_btn.configure(state=tk.NORMAL)
        self.status_var.set("Hook 运行中")

    def action_stop(self):
        if self.engine is None:
            return
        self.engine.stop()
        self.engine = None
        self.start_btn.configure(state=tk.NORMAL)
        self.stop_btn.configure(state=tk.DISABLED)
        self.status_var.set("Hook 已停止")

    def action_clear_log(self):
        self.logger.clear()

    def action_test_send(self):
        try:
            cfg = self._collect_config()
        except Exception as e:
            messagebox.showerror("配置错误", str(e), parent=self)
            return
        sample_val = 12345
        payload = replace_placeholders(cfg.get("payload", {}), sample_val)
        url = cfg["settings"]["target_url"]
        method = cfg["settings"]["method"]
        headers = cfg.get("headers", {})
        timeout = cfg["settings"]["timeout"]
        self.logger.post("INFO", f"测试发送 -> {url}  payload={payload}")
        if requests is not None and method == "POST":
            try:
                r = requests.post(url, json=payload, headers=headers, timeout=timeout)
                self.logger.post("OK", f"HTTP {r.status_code}")
                return
            except Exception as e:
                self.logger.post("WARN", f"requests 失败, 回退 urllib: {e}")
        ok, err = send_request(url, payload, headers, method, timeout)
        if ok:
            self.logger.post("OK", "测试发送成功")
        else:
            self.logger.post("ERROR", f"测试发送失败: {err}")

    # ---------- 引擎 / 服务器日志回调 (跨线程) ----------
    def _engine_log(self, level: str, msg: str):
        self.after(0, lambda: self.logger.post(level, msg))
        if level == "ERROR" and self.engine is not None and "无法附加" in msg:
            self.after(0, self.action_stop)

    def _server_log(self, level: str, msg: str):
        self.after(0, lambda: self.logger.post(level, msg))

    # ---------- 关闭 ----------
    def _on_close(self):
        try:
            if self.engine is not None and self.engine.is_running():
                self.engine.stop()
        except Exception:
            pass
        try:
            if self.server is not None and self.server.is_running:
                self.server.stop()
        except Exception:
            pass
        self.destroy()


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()