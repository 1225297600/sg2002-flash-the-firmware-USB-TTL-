#!/usr/bin/env python3
"""自定义 SG2002 SDNAND 烧写器

工作原理:
  - UART (CH340, /dev/ttyACM0) 作为控制台，发送 cvi_utask 进入 USB 下载模式
  - OTG (CVITEK, VID:PID=3346:1001) 传输 CIMG 分块数据到 DDR
  - CVI_USB_PROGRAM 让 U-Boot 把数据写入 SDNAND
  - 通过 UART 等待 "CVI_USB_PROGRAM done" 确认写入完成

用法:
  python3 flash_custom.py [--flash-dir DIR] [--uart-port PORT] [--no-reboot]

前置条件:
  1. 板子已通过官方 cv181x_rom_usb_download.py 发送 fip.bin，U-Boot 已启动
  2. UART 控制台可用 (/dev/ttyACM0, CH340)
  3. flash-dir 目录下有 boot.sd 和 rootfs.sd (CIMG 格式)

修订记录 (v2):
  - ensure_utask 先等 OTG 重新枚举，不立即重发 cvi_utask
  - chunk 循环改为基于 dev.is_open，避免 USB 枚举抖动误判
  - send_reboot 前不再调用 ensure_utask
  - chunk 发送失败自动重试 3 次
  - PROGRAM 未收到 UART ACK 时，通过探测 OTG 设备是否在线做二次确认
"""
import serial
import serial.tools.list_ports
import time
import sys
import os
import argparse
from array import array

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
import cv_usb_util.cv_usb_pkt as pkt
from cv_usb_util.cv_usb_pyserial import cv_usb_pyserial

MAX_CHUNK = 16 * 1024 * 1024   # 16MB per chunk
CHUNK_HDR = 64
CVITEK_VID = 0x3346
OTG_PID = 0x1001


def find_cvitek(pid):
    """查找指定 PID 的 CVITEK USB 串口设备"""
    for p in serial.tools.list_ports.comports():
        if p.vid == CVITEK_VID and p.pid == pid:
            return p.device
    return None


def uart_open(port):
    return serial.Serial(port, 115200, timeout=1)


def drain_uart(uart, settle=0.5):
    """清空 UART 接收缓冲"""
    time.sleep(settle)
    try:
        uart.reset_input_buffer()
    except Exception:
        pass


def enter_utask(uart, timeout=20):
    """通过 UART 发 cvi_utask，等待 OTG 枚举 3346:1001"""
    drain_uart(uart, 0.3)
    uart.write(b'\r')
    time.sleep(0.3)
    drain_uart(uart, 0.2)
    uart.write(b'cvi_utask vid 0x3346 pid 0x1001\r')

    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.5)
        if find_cvitek(OTG_PID):
            time.sleep(0.5)   # 等设备完成 USB 初始化
            return True
    return False


def ensure_utask(uart, wait=10.0):
    """确保 utask 在线。

    关键: 先等 OTG 重新枚举，不要立即重发 cvi_utask。
    写完 boot.sd 后 OTG 可能短暂掉线再重连，立即重发会干扰 U-Boot。
    """
    if find_cvitek(OTG_PID):
        return True

    deadline = time.time() + wait
    while time.time() < deadline:
        if find_cvitek(OTG_PID):
            return True
        time.sleep(0.5)

    print("  [warn] OTG offline after waiting, re-entering utask...")
    return enter_utask(uart)


def wait_program_done(uart, timeout=60):
    """从 UART 等待 'CVI_USB_PROGRAM done'"""
    deadline = time.time() + timeout
    buf = b''
    while time.time() < deadline:
        try:
            d = uart.read(4096)
        except Exception:
            d = b''
        if d:
            buf += d
            if b'CVI_USB_PROGRAM done' in buf:
                return True
            if b'PROGRAM' in buf and (b'fail' in buf.lower() or b'error' in buf.lower()):
                print(f"  [UART] program error: {buf[-200:].decode('utf-8', 'replace')}")
                return False
        else:
            time.sleep(0.1)
    print("  [UART] wait PROGRAM done timeout")
    return False


def connect_otg():
    """连接 OTG 数据口"""
    port = find_cvitek(OTG_PID)
    if not port:
        return None
    try:
        dev = serial.Serial(port, 115200, timeout=30)
    except Exception as e:
        print(f"  [OTG] cannot open {port}: {e}")
        return None
    time.sleep(0.3)
    try:
        dev.reset_input_buffer()
    except Exception:
        pass
    return dev


def send_reboot(uart):
    """通过 OTG 发送 CVI_USB_REBOOT 让设备重启"""
    dev = connect_otg()
    if not dev:
        print("[REBOOT] cannot open OTG, try power cycle")
        return False
    try:
        usb = cv_usb_pyserial()
        usb.device = dev
        usb.filesize = 128
        reboot_cmd = array('B', [pkt.CVI_USB_REBOOT, 0x00, 0x00,
                                  0x00, 0x00, 0x00, 0x00, 0x00])
        usb.serial_write(reboot_cmd, 0, 0)
        print("[REBOOT] CVI_USB_REBOOT sent")
        return True
    except Exception as e:
        print(f"[REBOOT] error: {e}")
        return False
    finally:
        try:
            dev.close()
        except Exception:
            pass


def read_img_addr(dev):
    """D2S 请求 IMG_ADDR"""
    msg = bytes([pkt.CV_USB_D2S, 0x00, pkt.USB_MSG_D2S_SIZE, 0, 0, 0, 0, 0])
    dev.write(msg)
    time.sleep(0.4)
    recv = dev.read(8)
    if len(recv) != 8:
        raise IOError(f"short D2S response: {len(recv)} bytes")
    hex_arr = [hex(x)[2:].rjust(2, '0') for x in recv]
    hex_arr.reverse()
    return int("0x" + "".join(hex_arr), 16)


def send_one_chunk(usb, fd, send_size, img_addr, max_retry=3):
    """发送一个 chunk，失败回退文件指针并重试"""
    last_err = None
    for attempt in range(1, max_retry + 1):
        pos = fd.tell()
        try:
            usb.header = array('B'); usb.data = array('B')
            usb.bulk_command = array('B'); usb.ser_cmd = array('B')
            rc = usb.usb_send_chunk(fd, send_size, img_addr, 0)
            if rc != pkt.FAIL:
                return True
            last_err = f"send_chunk returned {rc}"
        except Exception as e:
            last_err = str(e)
        print(f"  [retry {attempt}/{max_retry}] send chunk failed: {last_err}")
        try:
            fd.seek(pos)   # 回退到本 chunk 起点
        except Exception:
            pass
        time.sleep(1)
    return False


def flash_file(uart, filepath, label):
    """发送一个 CIMG 文件并分块编程"""
    print(f"\n=== Flashing {label}: {os.path.basename(filepath)} ===")

    if not ensure_utask(uart):
        print(f"  ERROR: cannot enter utask for {label}")
        return False

    filesize = os.path.getsize(filepath)
    fd = open(filepath, 'rb')

    try:
        # 读 CIMG 全局头 (64 字节)
        hdr_data = fd.read(64)
        if len(hdr_data) < 64:
            print("  ERROR: truncated CIMG header")
            return False
        header = array('I')
        header.frombytes(hdr_data)
        chunk_header_sz = header[2]
        cnt = header[3]
        file_size = header[4]
        print(f"  CIMG: chunks={cnt}, file_size={file_size}, chunk_hdr={chunk_header_sz}")
        fd.seek(0)

        dev = connect_otg()
        if not dev:
            print("  ERROR: cannot open OTG")
            return False

        usb = cv_usb_pyserial()
        usb.device = dev
        usb.filesize = filesize

        try:
            img_addr = read_img_addr(dev)
        except Exception as e:
            print(f"  ERROR: read IMG_ADDR failed: {e}")
            dev.close()
            return False
        print(f"  IMG_ADDR = 0x{img_addr:x}")

        # 1) 发 64B CIMG 全局头
        try:
            usb.header = array('B'); usb.data = array('B')
            usb.bulk_command = array('B'); usb.ser_cmd = array('B')
            usb.usb_send_chunk(fd, 64, img_addr, 0)
        except Exception as e:
            print(f"  ERROR: send global header failed: {e}")
            dev.close()
            return False
        print("  Sent CIMG global header")

        # 2) 分块发送 + PROGRAM
        remain = file_size
        for j in range(cnt):
            if not dev.is_open:
                dev = connect_otg()
                if not dev:
                    print(f"  ERROR: OTG lost at chunk {j}, cannot reconnect")
                    return False
                usb.device = dev

            send_size = min(remain, MAX_CHUNK + chunk_header_sz)
            if not send_one_chunk(usb, fd, send_size, img_addr):
                print(f"  ERROR: send_chunk {j} failed after retries")
                dev.close()
                return False

            # CVI_USB_PROGRAM (mmc 写入期间设备不响应 bulk，不等 ACK)
            prog_cmd = array('B', [
                pkt.CVI_USB_PROGRAM, 0x00, 0x00,
                0x00, 0x04, 0x00, 0x30, 0x00,
            ])
            try:
                usb.serial_write(prog_cmd, 0, 0)
            except Exception as e:
                print(f"  ERROR: send PROGRAM cmd failed: {e}")
                dev.close()
                return False

            try:
                uart.reset_input_buffer()
            except Exception:
                pass

            if not wait_program_done(uart):
                # UART 未确认，用 OTG 是否在线做二次判断
                time.sleep(2)
                if find_cvitek(OTG_PID):
                    print(f"  [warn] no UART ack for chunk {j}, device alive — assuming OK")
                else:
                    print(f"  ERROR: chunk {j} program failed, device offline")
                    dev.close()
                    return False

            try:
                dev.reset_input_buffer()
            except Exception:
                pass

            remain -= send_size
            print(f"  chunk {j+1}/{cnt}: {send_size // 1024}KB written, "
                  f"remain={max(0, remain) // 1024}KB")
            if remain <= 0:
                break

        try:
            dev.close()
        except Exception:
            pass
        print(f"  {label} DONE")
        return True

    finally:
        try:
            fd.close()
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser(
        description="SG2002 SDNAND USB flasher (custom)")
    ap.add_argument('--flash-dir', default='.',
                    help='directory containing boot.sd, rootfs.sd (default: cwd)')
    ap.add_argument('--uart-port', default='/dev/ttyACM0',
                    help='UART console port (default: /dev/ttyACM0)')
    ap.add_argument('--no-reboot', action='store_true',
                    help='do not send reboot command after flashing')
    ap.add_argument('--only', choices=['boot', 'rootfs'],
                help='only flash the specified partition (default: both)')
    args = ap.parse_args()

    flash_dir = os.path.abspath(args.flash_dir)

    # boot.sd 先, rootfs.sd 后 (与 partition_sd.xml 顺序一致)
    all_targets = [
        (os.path.join(flash_dir, "boot.sd"), "BOOT"),
        (os.path.join(flash_dir, "rootfs.sd"), "ROOTFS"),
    ]

    if args.only:
        targets = [t for t in all_targets if t[1].lower() == args.only]
    else:
        targets = all_targets
        
    for path, label in targets:
        if not os.path.exists(path):
            print(f"Missing {path}")
            sys.exit(1)

    uart = uart_open(args.uart_port)
    time.sleep(0.3)

    for i, (path, label) in enumerate(targets):
        if not flash_file(uart, path, label):
            print(f"\nFAILED at {label}")
            uart.close()
            sys.exit(1)
        # 下一个分区前留点时间，让 OTG 稳定
        if i + 1 < len(targets):
            time.sleep(2)

    print("\n=== ALL FLASHING COMPLETE ===")

    if not args.no_reboot:
        time.sleep(1)
        # 不再调用 ensure_utask，直接 reboot
        send_reboot(uart)
        print("Device rebooting from SDNAND...")

    uart.close()


if __name__ == '__main__':
    main()