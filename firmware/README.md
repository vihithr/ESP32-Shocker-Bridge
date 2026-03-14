# firmware/

此目录存放预编译的 ESP32 固件文件，供不想搭建编译环境的用户直接烧录使用。

## 文件说明

| 文件 | 烧录地址 | 说明 |
|------|----------|------|
| `bootloader.bin` | `0x0000` | Bootloader（全新设备需要） |
| `partitions.bin` | `0x8000` | 分区表 |
| `boot_app0.bin` | `0xe000` | OTA 引导 |
| `firmware.bin` | `0x10000` | 主应用固件 |

## 获取方式

从本仓库 [Releases](../../releases) 页面下载最新版本的固件压缩包，解压到此目录。

## 编译固件（开发者）

使用 VS Code + PlatformIO 插件打开项目根目录，点击底部工具栏 ✓ Build 按钮编译。
编译完成后产物位于 `.pio/build/esp32-c3-supermini/`，复制以下文件到此目录：

```
.pio/build/esp32-c3-supermini/firmware.bin         → firmware/firmware.bin
.pio/build/esp32-c3-supermini/bootloader.bin       → firmware/bootloader.bin
.pio/build/esp32-c3-supermini/partitions.bin       → firmware/partitions.bin
~/.platformio/packages/framework-arduinoespressif32/
    tools/partitions/boot_app0.bin                 → firmware/boot_app0.bin
```

