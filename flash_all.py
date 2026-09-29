#!/usr/bin/env python3
"""
flash_all.py — SG2002 USB 一键烧录脚本

自动化以下步骤：
  Step 2: 等待板子进入 ROM 模式 (3346:1000)
  Step 3: ROM 阶段发送 fip.bin，启动 U-Boot
  Step 4: 确保进入 U-Boot USB 下载模式 (3346:1001)
          - 若 ROM 工具已自动进入 utask，直接跳过 UART 交互
          - 否则发回车 → 读 soph# → 发 cvi_utask
  Step 5: 烧录 boot.sd 和 rootfs.sd 到 SDNAND
  Step 6: 完成后自动重启

用法：
  python3 flash_all.py --uart-port /dev/ttyACM0 --flash-dir ./tmp/upgrade

可选参数：
  --rom-timeout N     等待 ROM 设备超时秒数 (默认 120)
  --uboot-timeout N   等待 U-Boot 提示符超时秒数 (默认 30)
  --utask-timeout N   等待 U-Boot USB 下载模式超时秒数 (默认 30)
  --no-reboot         烧录完成后不重启
  --skip-rom          跳过 Step 2/3 (假设 U-Boot 已在运行)
"""

import argparse
import os
import subprocess
import sys
import time

try:
    import serial
except ImportError:
    print("错误: 未安装 pyserial，请执行: pip3 install pyserial")
    sys.exit(1)

CVITEK_VID = "3346"
ROM_PID = "1000"
UBOOT_PID = "1001"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def check_usb(pid):
    """通过 lsusb 检查 VID:PID = 3346:xxxx 是否存在"""
    try:
        r = subprocess.run(["lsusb"], capture_output=True, text=True, timeout=5)
    except Exception:
        return False
    target = f"{CVITEK_VID}:{pid}".lower()
    return target in r.stdout.lower()


def wait_usb(pid, timeout, desc):
    """等待 USB 设备出现"""
    log(f"等待 {desc} (3346:{pid})，最长 {timeout}s ...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if check_usb(pid):
            log(f"检测到 {desc} (3346:{pid})")
            return True
        time.sleep(0.5)
    log(f"超时: 未检测到 {desc} (3346:{pid})")
    return False


def wait_uboot_prompt(uart, timeout):
    """主动发送回车，然后监听 UART 直到出现 U-Boot 提示符 'soph#'"""
    log(f"发送回车并等待 U-Boot 提示符 'soph#' (超时 {timeout}s) ...")

    # 先清缓冲，再主动发一个回车，触发 U-Boot 打印提示符
    try:
        uart.reset_input_buffer()
    except Exception:
        pass
    time.sleep(0.2)
    try:
        uart.write(b"\r")
        uart.flush()
    except Exception:
        pass

    deadline = time.time() + timeout
    buf = b""
    while time.time() < deadline:
        try:
            data = uart.read(4096)
        except Exception:
            data = b""
        if data:
            buf += data
            try:
                sys.stdout.write(data.decode("utf-8", "replace"))
                sys.stdout.flush()
            except Exception:
                pass
            if b"soph#" in buf:
                log("检测到 'soph#'")
                return True
        else:
            time.sleep(0.1)
    log("超时: 未检测到 'soph#'")
    return False


def send_cvi_utask(uart):
    """通过 UART 发送 cvi_utask 命令，让 U-Boot 切到 USB 下载模式"""
    log("发送: cvi_utask vid 0x3346 pid 0x1001")
    try:
        uart.reset_input_buffer()
    except Exception:
        pass
    time.sleep(0.3)
    uart.write(b"\r")
    time.sleep(0.5)
    try:
        uart.reset_input_buffer()
    except Exception:
        pass
    uart.write(b"cvi_utask vid 0x3346 pid 0x1001\r")
    try:
        uart.flush()
    except Exception:
        pass
    time.sleep(1.0)


def run_step(cmd, desc, timeout=600):
    """执行子命令，返回是否成功"""
    log(f"执行: {desc}")
    log(f"  $ {' '.join(cmd)}")
    try:
        r = subprocess.run(cmd, timeout=timeout)
    except subprocess.TimeoutExpired:
        log(f"命令超时 ({timeout}s)")
        return False
    if r.returncode != 0:
        log(f"命令失败: 退出码 {r.returncode}")
        return False
    return True


def main():
    ap = argparse.ArgumentParser(
        description="SG2002 USB 一键烧录 (ROM 阶段 + U-Boot 阶段)")
    ap.add_argument("--uart-port", required=True,
                    help="UART 控制台设备，如 /dev/ttyACM0 (CH340)")
    ap.add_argument("--flash-dir", required=True,
                    help="包含 fip.bin, boot.sd, rootfs.sd 的目录")
    ap.add_argument("--rom-timeout", type=int, default=120,
                    help="等待 ROM 设备超时秒数 (默认 120)")
    ap.add_argument("--uboot-timeout", type=int, default=30,
                    help="等待 U-Boot 提示符超时秒数 (默认 30)")
    ap.add_argument("--utask-timeout", type=int, default=30,
                    help="等待 U-Boot USB 下载模式超时秒数 (默认 30)")
    ap.add_argument("--no-reboot", action="store_true",
                    help="烧录完成后不重启")
    ap.add_argument("--skip-rom", action="store_true",
                    help="跳过 Step 2/3 (假设 U-Boot 已在运行)")
    args = ap.parse_args()

    # ---------- 前置检查 ----------
    flash_dir = os.path.abspath(args.flash_dir)
    if not os.path.isdir(flash_dir):
        log(f"错误: flash-dir 不存在: {flash_dir}")
        sys.exit(1)
    for f in ("fip.bin", "boot.sd", "rootfs.sd"):
        p = os.path.join(flash_dir, f)
        if not os.path.exists(p):
            log(f"错误: 缺少文件: {p}")
            sys.exit(1)
    if not os.path.exists(args.uart_port):
        log(f"错误: UART 设备不存在: {args.uart_port}")
        sys.exit(1)

    rom_tool = os.path.join(SCRIPT_DIR, "usb_dl", "rom_usb_dl",
                            "cv181x_rom_usb_download.py")
    flash_tool = os.path.join(SCRIPT_DIR, "flash_custom.py")
    if not os.path.exists(rom_tool):
        log(f"错误: 找不到 {rom_tool}")
        sys.exit(1)
    if not os.path.exists(flash_tool):
        log(f"错误: 找不到 {flash_tool}")
        sys.exit(1)

    # ============================================================
    # Step 2: 等待 ROM 模式
    # ============================================================
    if not args.skip_rom:
        print()
        log("=" * 60)
        log("Step 2: 等待板子进入 ROM 模式 (3346:1000)")
        log("=" * 60)
        log("请断开板子电源 → 重新上电，进入 ROM USB 下载模式")
        if not wait_usb(ROM_PID, args.rom_timeout, "ROM 下载模式"):
            log("失败: 板子未进入 ROM 模式")
            sys.exit(1)

        # ========================================================
        # Step 3: ROM 阶段发 fip.bin
        # ========================================================
        print()
        log("=" * 60)
        log("Step 3: ROM 阶段发送 fip.bin，启动 U-Boot")
        log("=" * 60)
        cmd = [sys.executable, rom_tool, "--image_dir", flash_dir]
        if not run_step(cmd, "cv181x_rom_usb_download.py", timeout=180):
            sys.exit(1)
        # ROM 工具跑完后，给它一点时间让 USB 状态稳定
        time.sleep(2)
    else:
        log("已指定 --skip-rom，跳过 Step 2/3")

    # ============================================================
    # Step 4: 确保进入 U-Boot USB 下载模式 (3346:1001)
    # ============================================================
    print()
    log("=" * 60)
    log("Step 4: 确保进入 U-Boot USB 下载模式 (3346:1001)")
    log("=" * 60)

    if check_usb(UBOOT_PID):
        # ROM 工具已经自动连接 cvi_utask，无需 UART 交互
        log(f"已检测到 3346:{UBOOT_PID}，跳过 UART 交互")
    else:
        log(f"当前未检测到 3346:{UBOOT_PID}，通过 UART 尝试进入")
        try:
            uart = serial.Serial(args.uart_port, 115200, timeout=1)
        except Exception as e:
            log(f"错误: 无法打开 UART {args.uart_port}: {e}")
            sys.exit(1)

        try:
            if not wait_uboot_prompt(uart, args.uboot_timeout):
                log("警告: 未检测到 U-Boot 提示符，仍尝试发送 cvi_utask")
            send_cvi_utask(uart)
        finally:
            try:
                uart.close()
            except Exception:
                pass

        if not wait_usb(UBOOT_PID, args.utask_timeout, "U-Boot USB 下载模式"):
            log("失败: 未进入 U-Boot USB 下载模式")
            sys.exit(1)

    # ============================================================
    # Step 5: 烧录分区
    # ============================================================
    print()
    log("=" * 60)
    log("Step 5: 烧录 boot.sd 和 rootfs.sd 到 SDNAND")
    log("=" * 60)
    cmd = [sys.executable, flash_tool,
           "--flash-dir", flash_dir,
           "--uart-port", args.uart_port]
    if args.no_reboot:
        cmd.append("--no-reboot")
    if not run_step(cmd, "flash_custom.py", timeout=3600):
        sys.exit(1)

    # ============================================================
    # Step 6: 完成
    # ============================================================
    print()
    log("=" * 60)
    log("Step 6: 烧录完成")
    log("=" * 60)
    if not args.no_reboot:
        log("设备已重启，从 SDNAND 启动")
    else:
        log("请手动重启设备 (--no-reboot)")


if __name__ == "__main__":
    main()