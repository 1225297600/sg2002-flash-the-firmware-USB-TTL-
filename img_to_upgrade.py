#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
img_to_upgrade.py — 从 LicheeRV-Nano / CVITEK 启动镜像生成 OTA upgrade.zip

用法:
    python3 img_to_upgrade.py <input.img> [-o upgrade.zip]
        [--build-tree <LicheeRV-Nano-Build-20260114>]
        [--partition-xml <partition_sd.xml>]
        [--unzip]

输入一张含 MBR 分区表的 SD/eMMC 启动镜像（如 2026-01-14-16-03-d4003f.img），
按 CVITEK OTA 格式打包成 upgrade.zip：
  - 从 MBR 读取分区表，把每个分区以"裸镜像"形式抽出
  - 对每个分区按 16MB 分块加 CIMG 头 + CRC32
  - 连同 utils、metadata.txt 一起打成 zip

依赖：仅 Python3 标准库。
可选：--build-tree 指向 LicheeRV-Nano-Build-20260114 目录，
      会自动从中复制 utils 到 zip。
"""
import argparse
import binascii
import logging
import os
import shutil
import struct
import sys
import tempfile
import zipfile
from hashlib import md5

LOG = logging.getLogger("img_to_upgrade")
MAX_LOAD_SIZE = 16 * 1024 * 1024
CHUNK_TYPE_CRC_CHECK = 1
CHUNK_HEADER_SIZE = 64


def parse_mbr(img_path):
    with open(img_path, "rb") as f:
        mbr = f.read(512)
    if mbr[510:512] != b"\x55\xaa":
        raise ValueError(f"{img_path}: bad MBR magic")
    parts = []
    for i in range(4):
        off = 0x1BE + i * 16
        entry = mbr[off:off + 16]
        ptype = entry[4]
        if ptype == 0:
            continue
        start = struct.unpack("<I", entry[8:12])[0]
        size = struct.unpack("<I", entry[12:16])[0]
        parts.append((start, size, ptype))
    return parts


def extract_partition(img_path, start_sector, num_sectors, out_path):
    bs = 512
    total = num_sectors * bs
    with open(img_path, "rb") as src, open(out_path, "wb") as dst:
        src.seek(start_sector * bs)
        remaining = total
        while remaining > 0:
            chunk = src.read(min(8 * 1024 * 1024, remaining))
            if not chunk:
                break
            dst.write(chunk)
            remaining -= len(chunk)
    return total


def _read_fat_dir(f, part_off, rd, want_name):
    for i in range(0, len(rd) - 31, 32):
        e = rd[i:i + 32]
        if e[0] == 0x00:
            break
        if e[11] == 0x0F or e[0] == 0xE5:
            continue
        name = e[:8].decode("ascii", "replace").rstrip().upper()
        ext = e[8:11].decode("ascii", "replace").rstrip().upper()
        base = name.replace(" ", "")
        want_base, want_ext = want_name.split(".")
        if base == want_base and ext == want_ext:
            cl = struct.unpack_from("<H", e, 26)[0]
            cl |= struct.unpack_from("<H", e, 20)[0] << 16
            sz = struct.unpack_from("<I", e, 28)[0]
            return cl, sz
    return None


def extract_fip_from_fat(img_path, start_sector, out_path):
    BPS = 512
    with open(img_path, "rb") as f:
        f.seek(start_sector * BPS)
        bs = f.read(512)
        if bs[0x36:0x3B] in (b"FAT16", b"FAT32"):
            fs = bs[0x36:0x3B].decode("ascii").strip()
        else:
            fs = "FAT16" if struct.unpack_from("<H", bs, 22)[0] else "FAT32"

        spc = bs[13]
        reserved = struct.unpack_from("<H", bs, 14)[0]
        nfats = bs[16]
        root_entries = struct.unpack_from("<H", bs, 17)[0]

        def rsec(sec, n=1):
            f.seek(start_sector * BPS + sec * BPS)
            return f.read(n * BPS)

        if fs == "FAT32":
            spf = struct.unpack_from("<I", bs, 36)[0]
            root_cl = struct.unpack_from("<I", bs, 44)[0]
            fat_start = reserved
            data_start = reserved + nfats * spf

            def fat_next(c):
                off = c * 4
                sec = fat_start + off // BPS
                ent = (off % BPS) // 4
                return struct.unpack_from("<I", rsec(sec), ent * 4)[0] & 0x0FFFFFFF

            def chain(start_c, size):
                out, c = b"", start_c
                while c < 0x0FFFFFF8 and c >= 2 and len(out) < size:
                    dsec = data_start + (c - 2) * spc
                    out += rsec(dsec, spc)
                    c = fat_next(c)
                return out[:size]

            def read_dir(c):
                return chain(c, 64 * 1024)

            hit = _read_fat_dir(f, start_sector, read_dir(root_cl), "FIP.BIN")
            if not hit:
                return False
            cl, sz = hit
            data = chain(cl, sz)
        else:
            spf = struct.unpack_from("<H", bs, 22)[0]
            root_dir_sec = reserved + nfats * spf
            root_dir_secs = (root_entries * 32 + BPS - 1) // BPS
            data_start = root_dir_sec + root_dir_secs
            fat_start = reserved

            def fat_next(c):
                off = c * 2
                sec = fat_start + off // BPS
                ent = (off % BPS) // 2
                return struct.unpack_from("<H", rsec(sec), ent * 2)[0] & 0xFFFF

            def chain(start_c, size):
                out, c = b"", start_c
                while c < 0xFFF8 and c >= 2 and len(out) < size:
                    dsec = data_start + (c - 2) * spc
                    out += rsec(dsec, spc)
                    c = fat_next(c)
                return out[:size]

            rd = rsec(root_dir_sec, root_dir_secs)
            hit = _read_fat_dir(f, start_sector, rd, "FIP.BIN")
            if not hit:
                return False
            cl, sz = hit
            data = chain(cl, sz)

        if not data or not data.startswith(b"CVBL01"):
            LOG.warning("FIP.BIN found but bad magic: %s", data[:8])
            return False
        with open(out_path, "wb") as o:
            o.write(data)
        LOG.info("extracted FIP.BIN from FAT boot partition: %d bytes", len(data))
        return True


def pack_cimg(in_path, out_path, label, part_size):
    """把裸分区镜像转成 CIMG 分块格式。"""
    file_size = os.path.getsize(in_path)
    chunk_counts = (file_size + MAX_LOAD_SIZE - 1) // MAX_LOAD_SIZE
    total_file_size = file_size + chunk_counts * CHUNK_HEADER_SIZE
    label_bytes = label.encode("ascii")[:32].ljust(32, b"\0")

    with open(in_path, "rb") as fin, open(out_path, "wb") as fout:
        # 全局头
        fout.write(b"CIMG")
        fout.write(struct.pack("<IIII", 1, CHUNK_HEADER_SIZE,
                                chunk_counts, total_file_size))
        fout.write(label_bytes)
        fout.write(b"\0" * 12)

        offset = 0
        op_len = 0
        written = 0
        while written < file_size:
            chunk_sz = min(MAX_LOAD_SIZE, file_size - written)
            chunk = fin.read(chunk_sz)
            crc = binascii.crc32(chunk) & 0xFFFFFFFF
            if chunk_sz == MAX_LOAD_SIZE:
                op_len += chunk_sz
            else:
                op_len = part_size - (op_len if op_len else 0)
            hdr = struct.pack("<IIIIIIIIIIIIIIII",
                              CHUNK_TYPE_CRC_CHECK, chunk_sz, offset,
                              op_len, crc, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
            fout.write(hdr)
            fout.write(chunk)
            written += chunk_sz
            offset += chunk_sz

    LOG.info("packed %s -> %s (%d chunks)", in_path, out_path, chunk_counts)


def md5sum(path):
    m = md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(64 * 1024), b""):
            m.update(chunk)
    return m.hexdigest()


def write_misc_info(tmpdir, storage="sd", chip="sg2002",
                    board="sg2002_licheervnano_sd"):
    p = os.path.join(tmpdir, "misc_info.txt")
    with open(p, "w") as f:
        f.write("Version=1\n")
        f.write(f"STORAGE_TYPE={storage}\n")
        f.write(f"CHIP={chip}\n")
        f.write(f"BOARD={board}\n")
    return p


def collect_utils(build_tree):
    if not build_tree:
        return []
    utils_src = os.path.join(build_tree,
                             "build/tools/common/ota_tool/utils")
    if not os.path.isdir(utils_src):
        LOG.warning("utils dir not found: %s", utils_src)
        return []
    files = []
    cimg = os.path.join(utils_src, "cimg2raw")
    if os.path.isfile(cimg):
        files.append(("utils", cimg))
    example_dir = os.path.join(utils_src, "example")
    if os.path.isdir(example_dir):
        for name in sorted(os.listdir(example_dir)):
            full = os.path.join(example_dir, name)
            if os.path.isfile(full):
                files.append(("utils", full))
    return files


def build_partition_xml(out_path, parts_info):
    """每个分区预留 1KB 容纳 CIMG 头部开销。"""
    with open(out_path, "w") as f:
        f.write('<physical_partition type="sd">\n')
        for label, file_name, size_kb in parts_info:
            adjusted_kb = size_kb + 1
            f.write(f'    <partition label="{label}" '
                    f'size_in_kb="{adjusted_kb}" '
                    f'readonly="false" file="{file_name}"/>\n')
        f.write('</physical_partition>\n')
    return out_path


def main():
    ap = argparse.ArgumentParser(
        description="Generate CVITEK OTA upgrade.zip from a bootable SD image")
    ap.add_argument("img", help="input .img file with MBR partition table")
    ap.add_argument("-o", "--output", default="upgrade.zip",
                    help="output zip path (default: upgrade.zip)")
    ap.add_argument("--build-tree", default=None,
                    help="LicheeRV-Nano-Build-20260114 path for utils")
    ap.add_argument("--partition-xml", default=None,
                    help="partition XML (default: auto-generate from MBR)")
    ap.add_argument("--unzip", action="store_true",
                    help="also unzip upgrade.zip to a folder next to it")
    ap.add_argument("--storage", default="sd",
                    help="STORAGE_TYPE value (default: sd)")
    ap.add_argument("--chip", default="sg2002", help="CHIP value")
    ap.add_argument("--board", default="sg2002_licheervnano_sd",
                    help="BOARD value")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s: %(message)s")

    if not os.path.isfile(args.img):
        LOG.error("input image not found: %s", args.img)
        return 1

    parts = parse_mbr(args.img)
    if not parts:
        LOG.error("no MBR partitions found in %s", args.img)
        return 1
    LOG.info("found %d partition(s) in MBR:", len(parts))
    for i, (s, n, t) in enumerate(parts):
        LOG.info("  P%d: sector %d, %d sectors, type 0x%02x (%.1f MB)",
                 i + 1, s, n, t, n * 512 / 1024 / 1024)

    default_labels = ["BOOT", "ROOTFS", "DATA", "MISC"]

    with tempfile.TemporaryDirectory() as tmpdir:
        parts_info = []
        for i, (start, size_sec, _ptype) in enumerate(parts):
            label = default_labels[i] if i < len(default_labels) else f"P{i+1}"
            file_name = f"{label.lower()}.sd"
            raw_path = os.path.join(tmpdir, f"raw_{file_name}")
            cimg_path = os.path.join(tmpdir, file_name)

            LOG.info("extracting %s (sectors %d-%d)...", file_name, start,
                     start + size_sec - 1)
            extract_partition(args.img, start, size_sec, raw_path)

            part_size_bytes = size_sec * 512
            pack_cimg(raw_path, cimg_path, label, part_size_bytes)

            parts_info.append((label, file_name, part_size_bytes // 1024))
            os.remove(raw_path)

        fip_path = os.path.join(tmpdir, "fip.bin")
        has_fip = False
        for start, _sz, ptype in parts:
            if ptype in (0x01, 0x04, 0x06, 0x0B, 0x0C, 0x0E):
                try:
                    if extract_fip_from_fat(args.img, start, fip_path):
                        has_fip = True
                        break
                except Exception as ex:
                    LOG.warning("fip extraction failed: %s", ex)

        if args.partition_xml:
            xml_path = args.partition_xml
        else:
            xml_path = os.path.join(tmpdir, "partition_sd.xml")
            build_partition_xml(xml_path, parts_info)

        misc_path = write_misc_info(tmpdir, args.storage,
                                    args.chip, args.board)
        utils_files = collect_utils(args.build_tree)

        out_zip = os.path.abspath(args.output)
        LOG.info("packing %s ...", out_zip)
        with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as z:
            metadata_lines = []

            if has_fip:
                z.write(fip_path, "fip.bin")
                m = md5sum(fip_path)
                metadata_lines.append(f"{m}  fip.bin")
                LOG.info("  + fip.bin (%s)", m)

            for label, file_name, _kb in parts_info:
                cimg_path = os.path.join(tmpdir, file_name)
                z.write(cimg_path, file_name)
                m = md5sum(cimg_path)
                metadata_lines.append(f"{m}  {file_name}")
                LOG.info("  + %s (%s)", file_name, m)

            for in_dir, src in utils_files:
                name = os.path.basename(src)
                in_zip = os.path.join(in_dir, name)
                z.write(src, in_zip)
                m = md5sum(src)
                metadata_lines.append(f"{m}  {in_zip}")
                LOG.info("  + %s (%s)", in_zip, m)

            z.write(misc_path, os.path.join("META", "misc_info.txt"))
            m = md5sum(misc_path)
            metadata_lines.append(f"{m}  META/misc_info.txt")

            meta_path = os.path.join(tmpdir, "metadata.txt")
            with open(meta_path, "w") as f:
                for line in metadata_lines:
                    f.write(line + "\n")
            z.write(meta_path, os.path.join("META", "metadata.txt"))

            z.write(xml_path, os.path.basename(xml_path))

        LOG.info("done: %s (%.1f MB)", out_zip,
                 os.path.getsize(out_zip) / 1024 / 1024)

        if args.unzip:
            unzip_dir = os.path.splitext(out_zip)[0]
            if os.path.exists(unzip_dir):
                shutil.rmtree(unzip_dir)
            os.makedirs(unzip_dir)
            with zipfile.ZipFile(out_zip) as z:
                z.extractall(unzip_dir)
            LOG.info("unzipped to: %s", unzip_dir)

    return 0


if __name__ == "__main__":
    sys.exit(main())