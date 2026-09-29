# SG2002 USB 烧录 SD 操作说明

## 文件夹目录

    sg2002_flash_toolkit/
    ├── README.md                     说明文档
    ├── img_to_upgrade.py             img → CIMG 分区包
    ├── flash_custom.py               SD 烧写器
    ├── cv_usb_util/                  补丁版 USB 协议库
    ├── usb_dl/                       官方烧录工具
    │   ├── cv181x_dl.py
    │   └── rom_usb_dl/
    │       ├── cv181x_rom_usb_download.py     ROM 阶段
    │       ├── cv181x_uboot_usb_download.py   U-Boot 阶段
    │       └── XmlParser.py
    └── tmp/
        ├── 2026-01-14-16-03-d4003f.img
        └── upgrade/                  fip.bin, boot.sd, rootfs.sd, partition_sd.xml

## 接线

- SG2002-UART0 → 主机串口/USB转串口，115200
- SG2002-USB → 主机USB

## 1.生成烧录包

    cd sg2002_flash_toolkit
    mkdir -p ./tmp
    python3 img_to_upgrade.py <你的 img 文件路径> -o ./tmp/upgrade.zip --unzip

## 2.自动烧录步骤

**2.1 执行 flash_all.py 脚本**

    python3 flash_all.py --uart-port <UART0对应的/dev/tty*> --flash-dir ./tmp/upgrade
    
**2.2 完成后重启，从 SD 启动。**


# 3.烧录流程说明

**3.1 烧录fip.bin作为启动引导**

    ./usb_dl/rom_usb_dl/cv181x_rom_usb_download.py --image_dir ./tmp/upgrade
    
**3.2 UART0 波特率115200，输入命令，进入USB烧录模式**

    sudo stty -F /dev/ttyACM0 115200 cs8
    sudo sh -c 'printf "cvi_utask vid 0x3346 pid 0x1001\r" > /dev/ttyACM0'

**3.3 检查USB设备是否进入烧录模式**
    
    lsusb | grep CVITEK

**3.4 执行 flash_custom.py 脚本**
这一步先烧录 boot.sd，再烧录 rootfs.sd

    sudo python3 flash_custom.py --uart-port /dev/ttyACM0 --flash-dir ./tmp/upgrade --only boot
    sudo python3 flash_custom.py --uart-port /dev/ttyACM0 --flash-dir ./tmp/upgrade --only rootfs

**3.5 完成后重启，从 SD 启动。**
