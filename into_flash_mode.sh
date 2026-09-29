#!/bin/bash
# 1. 烧录fip.bin作为启动引导
./usb_dl/rom_usb_dl/cv181x_rom_usb_download.py --image_dir ./tmp/upgrade

sleep 3

# 2. UART0 波特率115200，输入命令，进入USB烧录模式
sudo stty -F /dev/ttyACM0 115200 cs8
sudo sh -c 'printf "cvi_utask vid 0x3346 pid 0x1001\r" > /dev/ttyACM0'

sleep 3

# 3. 检查USB设备是否进入烧录模式
lsusb | grep CVITEK

# 这一步先烧录 boot.sd，再烧录 rootfs.sd
sudo python3 flash_custom.py --uart-port /dev/ttyACM0 --flash-dir ./tmp/upgrade --only boot
# sudo python3 flash_custom.py --uart-port /dev/ttyACM0 --flash-dir ./tmp/upgrade --only rootfs