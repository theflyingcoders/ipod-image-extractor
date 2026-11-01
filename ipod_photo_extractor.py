#!/usr/bin/env python3
"""
iPod Photo Extractor
====================
Extracts photos from connected Apple iPods (iPod Classic, Video 5G/5.5G, Photo 4G, Nano, etc.).

Features:
- Zero external dependencies required (uses standard library Python 3).
- Recovers full-resolution original photos (if "Include full-resolution photos" was synced).
- Parses the Apple Photo Database (`Photo Database`) for exact offsets, sizes, and file mapping.
- Automatically handles multi-part cache files (e.g., F1067_1.ithmb and F1067_2.ithmb).
- Automatically selects the highest available resolution for each photo.
- Auto-crops letterbox/pillarbox padding using database coordinates so photos are perfectly framed.
- Supports RGB565 (LE/BE), BGR555, UYVY 4:2:2 Interlaced (Format 1019), and YCbCr 4:2:0 Planar (Format 1067).
- Auto-detects connected iPods mounted under /Volumes.
"""

import argparse
import os
import re
import shutil
import struct
import sys
import zlib
from pathlib import Path


# ── Profile & Format Definitions ─────────────────────────────────────────────

# Known format definitions across iPod generations
# Native buffer dimensions and slot sizes in .ithmb files
PROFILES = {
    # iPod Photo / Video / Classic high-res TV-out
    1019: {"desc": "TV-out 720x480 (Interlaced UYVY)", "width": 720, "height": 480, "encoding": "uyvy_interlaced", "slot_size": 691200},
    1067: {"desc": "Classic 6G 720x480 (Planar YCbCr 4:2:0)", "width": 720, "height": 480, "encoding": "ycbcr_420", "slot_size": 691200},
    1081: {"desc": "High-Res 640x480 (RGB565)", "width": 640, "height": 480, "encoding": "rgb565_le", "slot_size": 614400},
    3008: {"desc": "iPhone/Touch 640x480 (BGR555)", "width": 640, "height": 480, "encoding": "bgr555_le", "slot_size": 614400},

    # Full screen display formats
    1024: {"desc": "Classic/Video Full Screen 320x240 (RGB565)", "width": 320, "height": 240, "encoding": "rgb565_le", "slot_size": 153600},
    1042: {"desc": "Classic/Video Full Screen Alias 320x240 (RGB565)", "width": 320, "height": 240, "encoding": "rgb565_le", "slot_size": 153600},
    1013: {"desc": "iPod Photo 4G Full Screen 220x176 (RGB565 BE)", "width": 220, "height": 176, "encoding": "rgb565_be", "slot_size": 77440},
    1023: {"desc": "iPod Nano 1G/2G Landscape 176x132 (RGB565 BE)", "width": 176, "height": 132, "encoding": "rgb565_be", "slot_size": 46464},
    1015: {"desc": "iPod Photo 4G Browser 130x88 (RGB565)", "width": 130, "height": 88, "encoding": "rgb565_le", "slot_size": 22880},
    1043: {"desc": "iPod Photo 4G Browser Alias 130x88 (RGB565)", "width": 130, "height": 88, "encoding": "rgb565_le", "slot_size": 22880},

    # Album art / square formats
    1060: {"desc": "Cover Art Large 320x320 (RGB565)", "width": 320, "height": 320, "encoding": "rgb565_le", "slot_size": 204800},
    1029: {"desc": "Cover Art Medium 200x200 (RGB565)", "width": 200, "height": 200, "encoding": "rgb565_le", "slot_size": 80000},
    1055: {"desc": "Cover Art Small 128x128 (RGB565)", "width": 128, "height": 128, "encoding": "rgb565_le", "slot_size": 32768},

    # Small thumbnails
    3009: {"desc": "Medium Thumbnail 120x160 (BGR555)", "width": 120, "height": 160, "encoding": "bgr555_le", "slot_size": 40960},
    3011: {"desc": "Small Thumbnail 75x75 (BGR555)", "width": 75, "height": 75, "encoding": "bgr555_le", "slot_size": 12640},
    3004: {"desc": "Micro Thumbnail 55x55 (BGR555)", "width": 55, "height": 55, "encoding": "bgr555_le", "slot_size": 8192},
    1036: {"desc": "Smallest Thumbnail 50x41 (RGB565)", "width": 50, "height": 41, "encoding": "rgb565_le", "slot_size": 4100},
    1061: {"desc": "Small Thumbnail 56x56 (RGB565)", "width": 56, "height": 56, "encoding": "rgb565_le", "slot_size": 6272},
    1066: {"desc": "Square Thumbnail 64x64 (RGB565)", "width": 64, "height": 64, "encoding": "rgb565_le", "slot_size": 8192},
    1017: {"desc": "Cover Art Micro 56x56 (RGB565)", "width": 56, "height": 56, "encoding": "rgb565_le", "slot_size": 6272},
    1009: {"desc": "iPod Photo Micro 42x30 (RGB565)", "width": 42, "height": 30, "encoding": "rgb565_le", "slot_size": 2520},
}


# ── Color Conversion & Decoding ──────────────────────────────────────────────

# Precomputed lookup tables for fast RGB565 / BT.601 decoding
_CLIP_TABLE = [0] * 512 + list(range(256)) + [255] * 512

# BT.601 constants: Cr -> R, Cb -> B, Cb -> G, Cr -> G
_CR_TO_R = [(cr * 359) >> 8 for cr in range(-128, 128)]
_CB_TO_B = [(cb * 454) >> 8 for cb in range(-128, 128)]
_CB_TO_G = [(cb * 88) >> 8 for cb in range(-128, 128)]
_CR_TO_G = [(cr * 183) >> 8 for cr in range(-128, 128)]

# Precomputed RGB565 lookup tables for 65536 possible 16-bit values
_RGB565_R = bytearray(65536)
_RGB565_G = bytearray(65536)
_RGB565_B = bytearray(65536)

for _val in range(65536):
    _r5 = (_val >> 11) & 0x1F
    _g6 = (_val >> 5) & 0x3F
    _b5 = _val & 0x1F
    _RGB565_R[_val] = (_r5 << 3) | (_r5 >> 2)
    _RGB565_G[_val] = (_g6 << 2) | (_g6 >> 4)
    _RGB565_B[_val] = (_b5 << 3) | (_b5 >> 2)


def decode_rgb565(raw_bytes, width, height, big_endian=False):
    """Decode 16-bit RGB 5/6/5 pixel stream into raw 24-bit RGB bytearray."""
    expected = width * height * 2
    if len(raw_bytes) < expected:
        return None

    rgb = bytearray(width * height * 3)
    rgb_idx = 0

    if big_endian:
        for i in range(0, expected, 2):
            val = (raw_bytes[i] << 8) | raw_bytes[i + 1]
            rgb[rgb_idx] = _RGB565_R[val]
            rgb[rgb_idx + 1] = _RGB565_G[val]
            rgb[rgb_idx + 2] = _RGB565_B[val]
            rgb_idx += 3
    else:
        for i in range(0, expected, 2):
            val = raw_bytes[i] | (raw_bytes[i + 1] << 8)
            rgb[rgb_idx] = _RGB565_R[val]
            rgb[rgb_idx + 1] = _RGB565_G[val]
            rgb[rgb_idx + 2] = _RGB565_B[val]
            rgb_idx += 3

    return rgb


def decode_bgr555(raw_bytes, width, height):
    """Decode 15-bit BGR (xBBBBBGGGGGRRRRR) little-endian pixel stream into 24-bit RGB."""
    expected = width * height * 2
    if len(raw_bytes) < expected:
        return None

    rgb = bytearray(width * height * 3)
    rgb_idx = 0
    for i in range(0, expected, 2):
        val = raw_bytes[i] | (raw_bytes[i + 1] << 8)
        b5 = (val >> 10) & 0x1F
        g5 = (val >> 5) & 0x1F
        r5 = val & 0x1F
        rgb[rgb_idx] = (r5 << 3) | (r5 >> 2)
        rgb[rgb_idx + 1] = (g5 << 3) | (g5 >> 2)
        rgb[rgb_idx + 2] = (b5 << 3) | (b5 >> 2)
        rgb_idx += 3
    return rgb


def decode_uyvy_interlaced(raw_bytes, width, height):
    """
    Decode 720x480 interlaced UYVY 4:2:2 pixel stream.
    First half of raw_bytes contains even rows (0, 2, 4...), second half odd rows (1, 3, 5...).
    BT.601 color conversion with clamped RGB.
    """
    field_size = (width * (height // 2)) * 2
    if len(raw_bytes) < field_size * 2:
        return None

    rgb = bytearray(width * height * 3)
    stride = width * 3

    def process_field(field_bytes, start_row, step):
        idx = 0
        for y in range(start_row, height, step):
            row_idx = y * stride
            for _ in range(width // 2):
                u = field_bytes[idx] - 128
                y0 = field_bytes[idx + 1]
                v = field_bytes[idx + 2] - 128
                y1 = field_bytes[idx + 3]
                idx += 4

                u_idx = u + 128
                v_idx = v + 128

                # Pixel 0
                rgb[row_idx] = _CLIP_TABLE[512 + y0 + _CR_TO_R[v_idx]]
                rgb[row_idx + 1] = _CLIP_TABLE[512 + y0 - _CB_TO_G[u_idx] - _CR_TO_G[v_idx]]
                rgb[row_idx + 2] = _CLIP_TABLE[512 + y0 + _CB_TO_B[u_idx]]

                # Pixel 1
                rgb[row_idx + 3] = _CLIP_TABLE[512 + y1 + _CR_TO_R[v_idx]]
                rgb[row_idx + 4] = _CLIP_TABLE[512 + y1 - _CB_TO_G[u_idx] - _CR_TO_G[v_idx]]
                rgb[row_idx + 5] = _CLIP_TABLE[512 + y1 + _CB_TO_B[u_idx]]

                row_idx += 6

    process_field(raw_bytes[:field_size], 0, 2)
    process_field(raw_bytes[field_size:field_size * 2], 1, 2)
    return rgb


def decode_ycbcr_420(raw_bytes, width, height):
    """
    Decode Planar YCbCr 4:2:0 (BT.601) with nearest-neighbor chroma upsampling.
    Plane 0: Y (width * height)
    Plane 1: Cb (width/2 * height/2)
    Plane 2: Cr (width/2 * height/2)
    """
    y_size = width * height
    c_size = (width // 2) * (height // 2)
    if len(raw_bytes) < y_size + 2 * c_size:
        return None

    y_plane = raw_bytes[:y_size]
    cb_plane = raw_bytes[y_size:y_size + c_size]
    cr_plane = raw_bytes[y_size + c_size:y_size + 2 * c_size]

    rgb = bytearray(width * height * 3)
    c_width = width // 2

    rgb_idx = 0
    for y in range(height):
        cy = y // 2
        for x in range(width):
            cx = x // 2
            c_offset = cy * c_width + cx
            y_val = y_plane[y * width + x]
            u = cb_plane[c_offset] - 128
            v = cr_plane[c_offset] - 128

            u_idx = u + 128
            v_idx = v + 128

            rgb[rgb_idx] = _CLIP_TABLE[512 + y_val + _CR_TO_R[v_idx]]
            rgb[rgb_idx + 1] = _CLIP_TABLE[512 + y_val - _CB_TO_G[u_idx] - _CR_TO_G[v_idx]]
            rgb[rgb_idx + 2] = _CLIP_TABLE[512 + y_val + _CB_TO_B[u_idx]]
            rgb_idx += 3

    return rgb


def crop_rgb(rgb_bytes, orig_w, orig_h, x1, y1, x2, y2):
    """Crop raw 24-bit RGB bytearray to active rectangle [x1:x2, y1:y2]."""
    crop_w = x2 - x1
    crop_h = y2 - y1
    if crop_w <= 0 or crop_h <= 0 or x2 > orig_w or y2 > orig_h:
        return rgb_bytes, orig_w, orig_h

    cropped = bytearray(crop_w * crop_h * 3)
    out_idx = 0
    for y in range(y1, y2):
        row_start = (y * orig_w + x1) * 3
        row_end = row_start + (crop_w * 3)
        cropped[out_idx:out_idx + crop_w * 3] = rgb_bytes[row_start:row_end]
        out_idx += crop_w * 3

    return cropped, crop_w, crop_h


def decode_chunk(raw_bytes, width, height, encoding):
    """Route chunk decoding based on profile encoding type."""
    if encoding == "uyvy_interlaced":
        return decode_uyvy_interlaced(raw_bytes, width, height)
    elif encoding == "ycbcr_420":
        return decode_ycbcr_420(raw_bytes, width, height)
    elif encoding == "rgb565_be":
        return decode_rgb565(raw_bytes, width, height, big_endian=True)
    elif encoding == "bgr555_le":
        return decode_bgr555(raw_bytes, width, height)
    else:  # default rgb565_le
        return decode_rgb565(raw_bytes, width, height, big_endian=False)


def is_blank_rgb(rgb_bytes, sample_count=1000):
    """Check if decoded image is completely uniform (solid color, blank padding, or empty slot)."""
    if not rgb_bytes or len(rgb_bytes) < 3:
        return True
    total_pixels = len(rgb_bytes) // 3
    step = max(1, total_pixels // sample_count) * 3
    r0, g0, b0 = rgb_bytes[0], rgb_bytes[1], rgb_bytes[2]
    for i in range(0, len(rgb_bytes), step):
        if rgb_bytes[i] != r0 or rgb_bytes[i + 1] != g0 or rgb_bytes[i + 2] != b0:
            return False
    return True


# ── Built-in Zero-Dependency PNG Writer ──────────────────────────────────────

def save_png(output_path, width, height, rgb_bytes):
    """Write an uncompressed/zlib-compressed PNG using only Python standard library."""
    def make_chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    stride = width * 3
    raw_scanlines = bytearray()
    for y in range(height):
        raw_scanlines.append(0)  # Filter byte 0 (None)
        raw_scanlines.extend(rgb_bytes[y * stride:(y + 1) * stride])

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    idat = zlib.compress(bytes(raw_scanlines), level=1)  # Level 1 for ultra-fast writing

    png_data = (
        b"\x89PNG\r\n\x1a\n"
        + make_chunk(b"IHDR", ihdr)
        + make_chunk(b"IDAT", idat)
        + make_chunk(b"IEND", b"")
    )

    with open(output_path, "wb") as f:
        f.write(png_data)


# ── Apple Photo Database (mhXX) Parser ───────────────────────────────────────

def scan_photo_database_signatures(data):
    """
    Robust scanner that finds mhii (photo items) and nested mhni (thumbnail info)
    by magic signatures. Extracts native offsets and crop bounding boxes.
    """
    photos = []
    idx = 0
    data_len = len(data)

    # Determine database endianness
    endian = "<"
    if data.startswith(b"dfhm"):
        endian = ">"

    while idx < data_len:
        mhii_idx = data.find(b"mhii", idx) if endian == "<" else data.find(b"iihm", idx)
        if mhii_idx == -1:
            break

        if mhii_idx + 12 > data_len:
            break

        hdr_len, total_len = struct.unpack_from(endian + "II", data, mhii_idx + 4)
        mhii_end = mhii_idx + (total_len if total_len > 12 else 4096)
        mhii_end = min(mhii_end, data_len)

        reps = []
        c_idx = mhii_idx + (hdr_len if hdr_len >= 12 else 12)

        while c_idx < mhii_end:
            mhni_idx = data.find(b"mhni", c_idx, mhii_end) if endian == "<" else data.find(b"inhm", c_idx, mhii_end)
            if mhni_idx == -1:
                break

            rep = parse_mhni_block(data, mhni_idx, endian)
            if rep:
                reps.append(rep)

            c_idx = mhni_idx + 36

        if reps:
            photos.append(reps)

        step = total_len if total_len > 12 else 12
        idx = mhii_idx + step

    return photos


def parse_photo_database(db_path):
    """
    Parse an Apple Photo Database file.
    Uses signature scanning to reliably locate photo records and thumbnail references.
    """
    try:
        with open(db_path, "rb") as f:
            data = f.read()
    except Exception as e:
        print(f"  [!] Failed to read Photo Database: {e}")
        return []

    if not (data.startswith(b"mhfd") or data.startswith(b"dfhm")):
        print("  [!] Photo Database does not start with valid mhfd header.")
        return []

    return scan_photo_database_signatures(data)


def parse_mhni_block(data, mhni_pos, endian):
    """
    Parse single mhni block.
    Extracts thumbnail offset, container size, crop boundaries, and target filename.
    """
    if mhni_pos + 36 > len(data):
        return None

    mhni_hdr_len = struct.unpack_from(endian + "I", data, mhni_pos + 4)[0]
    mhni_total_len = struct.unpack_from(endian + "I", data, mhni_pos + 8)[0]
    format_id = struct.unpack_from(endian + "I", data, mhni_pos + 16)[0]
    img_offset = struct.unpack_from(endian + "I", data, mhni_pos + 20)[0]
    img_size = struct.unpack_from(endian + "I", data, mhni_pos + 24)[0]

    # mhni layout (libgpod MhniHeader): offset 28 vertical padding (int16), 30 horizontal
    # padding (int16), 32 image height, 34 image width. The image sits at (x1, y1)
    # inside the native frame, so the crop rectangle is padding + size.
    y1, x1, img_h, img_w = struct.unpack_from(endian + "hhHH", data, mhni_pos + 28)
    x2 = x1 + img_w
    y2 = y1 + img_h

    # Find filename child (mhod type=3)
    # Note: On iPod Classic, mhod_type is 0x02000003, so check (type & 0xFFFF) == 3
    filename = None
    c_pos = mhni_pos + mhni_hdr_len
    mhni_end = mhni_pos + mhni_total_len

    while c_pos < mhni_end:
        magic = data[c_pos:c_pos + 4]
        if magic == (b"mhod" if endian == "<" else b"dohm"):
            ch_hdr_len = struct.unpack_from(endian + "I", data, c_pos + 4)[0]
            ch_tot_len = struct.unpack_from(endian + "I", data, c_pos + 8)[0]
            mhod_type = struct.unpack_from(endian + "I", data, c_pos + 12)[0] if c_pos + 16 <= len(data) else 0
            if (mhod_type & 0xFFFF) == 3:
                raw_str = data[c_pos + ch_hdr_len:c_pos + ch_tot_len]
                try:
                    encoding = "utf-16-le" if endian == "<" else "utf-16-be"
                    path_str = raw_str.decode(encoding).rstrip("\x00")
                    # Handle Mac-style paths (e.g. ":Thumbs:F1067_1.ithmb" or ":Thumbs:F1067_2.ithmb")
                    filename = path_str.split(":")[-1].split("/")[-1].split("\\")[-1]
                except Exception:
                    pass
            c_pos += ch_tot_len
        else:
            break

    # Fallback filename from format_id if not explicitly found in mhod
    if not filename and format_id:
        filename = f"F{format_id}_1.ithmb"

    # Native raster buffer dimensions come from the fixed hardware profile
    if format_id in PROFILES:
        native_w = PROFILES[format_id]["width"]
        native_h = PROFILES[format_id]["height"]
    else:
        native_w = max(x2, 720)
        native_h = max(y2, 480)

    # Validate crop rectangle
    if x1 >= 0 and y1 >= 0 and x2 > x1 and y2 > y1 and x2 <= native_w and y2 <= native_h:
        crop_w = x2 - x1
        crop_h = y2 - y1
    else:
        # Default to full native frame
        x1, y1 = 0, 0
        x2, y2 = native_w, native_h
        crop_w, crop_h = native_w, native_h

    return {
        "format_id": format_id,
        "offset": img_offset,
        "size": img_size,
        "native_w": native_w,
        "native_h": native_h,
        "x1": x1,
        "y1": y1,
        "x2": x2,
        "y2": y2,
        "crop_w": crop_w,
        "crop_h": crop_h,
        "filename": filename,
    }


# ── Extraction Operations ───────────────────────────────────────────────────

FULL_RES_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp"}


def extract_full_resolution(ipod_root, output_dir):
    """Copy original full-resolution photos if synced with iTunes."""
    photos_dir = ipod_root / "Photos"
    if not photos_dir.is_dir():
        return 0

    target_dir = output_dir / "full_resolution"
    saved = 0

    # List the folder once and match the name loosely: iPod volumes ignore case, so probing
    # "Full_Resolution" and "full_resolution" separately finds the same folder twice.
    for d in sorted(photos_dir.iterdir()):
        if not d.is_dir() or re.sub(r"[ _]", "", d.name).lower() != "fullresolution":
            continue
        print(f"[*] Found full-resolution photo directory: {d}")
        for img_file in sorted(d.rglob("*")):
            # Skip macOS AppleDouble "._" stubs, which carry a photo extension but are not photos
            if img_file.name.startswith("._") or img_file.suffix.lower() not in FULL_RES_EXTS or not img_file.is_file():
                continue
            target_dir.mkdir(parents=True, exist_ok=True)
            dest_file = target_dir / img_file.name
            if dest_file.exists():
                dest_file = target_dir / f"{img_file.stem}_{saved:04d}{img_file.suffix}"
            shutil.copy2(img_file, dest_file)
            saved += 1

    return saved


def extract_from_database(photos, thumbs_dirs, output_dir, extract_all_sizes=False, no_crop=False):
    """
    Extract photos indexed in Photo Database.
    Decodes the raw raster buffer at native dimensions, then applies active crop.
    """
    target_dir = output_dir / "database_photos"
    target_dir.mkdir(parents=True, exist_ok=True)

    file_handles = {}

    def get_file_handle(filename):
        if filename in file_handles:
            return file_handles[filename]
        for d in thumbs_dirs:
            if not d.exists():
                continue
            for f in d.iterdir():
                if f.name.lower() == filename.lower():
                    handle = open(f, "rb")
                    file_handles[filename] = (handle, f)
                    return handle, f
        file_handles[filename] = (None, None)  # remember misses so we scan the folders once
        return None, None

    saved = 0
    total_photos = len(photos)

    print(f"[*] Processing {total_photos} photos from Photo Database...")

    for idx, representations in enumerate(photos, start=1):
        # Sort representations by native pixel area (descending) to pick best quality (e.g. 720x480 > 320x240)
        representations.sort(key=lambda r: r["native_w"] * r["native_h"], reverse=True)
        reps_to_process = representations if extract_all_sizes else [representations[0]]

        for rep in reps_to_process:
            fid = rep["format_id"]
            offset = rep["offset"]
            fname = rep["filename"]
            native_w = rep["native_w"]
            native_h = rep["native_h"]

            if not fname:
                continue
            handle, _ = get_file_handle(fname)
            if not handle:
                continue

            profile = PROFILES.get(fid, {
                "desc": f"Format {fid}",
                "width": native_w,
                "height": native_h,
                "encoding": "rgb565_le",
                "slot_size": native_w * native_h * 2,
            })

            encoding = profile["encoding"]
            slot_size = profile.get("slot_size", native_w * native_h * 2)

            handle.seek(offset)
            chunk = handle.read(slot_size)
            if len(chunk) < slot_size:
                continue

            # Always decode the full native raster buffer first
            rgb_data = decode_chunk(chunk, native_w, native_h, encoding)
            if not rgb_data or is_blank_rgb(rgb_data):
                continue

            # Crop letterbox/pillarbox borders unless user requested --no-crop
            if not no_crop and (rep["crop_w"] < native_w or rep["crop_h"] < native_h):
                final_rgb, out_w, out_h = crop_rgb(
                    rgb_data, native_w, native_h,
                    rep["x1"], rep["y1"], rep["x2"], rep["y2"]
                )
            else:
                final_rgb, out_w, out_h = rgb_data, native_w, native_h

            out_filename = f"photo_{idx:05d}_{fid}_{out_w}x{out_h}.png"
            save_png(target_dir / out_filename, out_w, out_h, final_rgb)
            saved += 1
            print(f"\r  [{idx}/{total_photos}] Extracted: {saved} photos", end="", flush=True)

    print()

    for handle, _ in file_handles.values():
        if handle:
            handle.close()

    return saved


def carve_ithmb_file(file_path, output_dir, format_id=None):
    """Carve raw frames directly from an .ithmb file (useful if database is missing)."""
    if format_id is None:
        match = re.search(r"F(\d{4})_\d+", file_path.name, re.IGNORECASE)
        if match:
            format_id = int(match.group(1))

    if format_id not in PROFILES:
        return 0

    profile = PROFILES[format_id]
    w = profile["width"]
    h = profile["height"]
    slot_size = profile["slot_size"]
    encoding = profile["encoding"]

    file_size = file_path.stat().st_size
    num_slots = file_size // slot_size

    if num_slots == 0:
        return 0

    target_dir = output_dir / "carved_photos" / f"format_{format_id}_{w}x{h}"
    target_dir.mkdir(parents=True, exist_ok=True)

    print(f"[*] Carving {file_path.name}: {num_slots} frames ({w}x{h}, {profile['desc']})...")

    saved = 0
    with open(file_path, "rb") as f:
        for i in range(num_slots):
            f.seek(i * slot_size)
            chunk = f.read(slot_size)
            if len(chunk) < slot_size:
                break

            rgb_data = decode_chunk(chunk, w, h, encoding)
            if not rgb_data or is_blank_rgb(rgb_data):
                continue

            out_filename = f"{file_path.stem}_{i + 1:05d}.png"
            save_png(target_dir / out_filename, w, h, rgb_data)
            saved += 1
            print(f"\r  [{i + 1}/{num_slots}] Saved: {saved}", end="", flush=True)

    print()
    return saved


# ── iPod Volume Detection ───────────────────────────────────────────────────

def find_ipod_volumes():
    """Locate connected iPod volumes mounted under /Volumes on macOS."""
    candidates = []
    volumes_dir = Path("/Volumes")
    if not volumes_dir.exists():
        return candidates

    try:
        for entry in volumes_dir.iterdir():
            if not entry.is_dir() or entry.name == "Macintosh HD":
                continue
            has_photos = (entry / "Photos").exists() or (entry / "photos").exists()
            has_control = (entry / "iPod_Control").exists() or (entry / "ipod_control").exists()
            if has_photos or has_control:
                candidates.append(entry)
    except Exception as e:
        print(f"  [!] Note while scanning /Volumes: {e}")

    return candidates


# ── Main Entrypoint ─────────────────────────────────────────────────────────

def ithmb_priority(path):
    """True for the high-resolution formats, so they are carved first."""
    match = re.search(r"F(\d{4})", path.name)
    return bool(match) and int(match.group(1)) in (1019, 1067, 1081, 1024)


def main():
    parser = argparse.ArgumentParser(
        description="Extract photos from an Apple iPod Classic / Video / Photo / Nano.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 ipod_photo_extractor.py                       # Auto-detect connected iPod
  python3 ipod_photo_extractor.py "/Volumes/IPOD"       # Extract from specific iPod volume
  python3 ipod_photo_extractor.py ./dump_folder -o out  # Extract from local backup folder
  python3 ipod_photo_extractor.py --all-sizes           # Extract all thumbnail sizes
  python3 ipod_photo_extractor.py --no-crop             # Keep black TV-out pillarboxes
        """
    )

    parser.add_argument("path", nargs="?", default=None, help="Path to iPod mount point or folder containing Photos")
    parser.add_argument("-o", "--output", default="extracted_photos", help="Output directory (default: extracted_photos)")
    parser.add_argument("--all-sizes", action="store_true", help="Extract all thumbnail sizes instead of highest resolution only")
    parser.add_argument("--no-crop", action="store_true", help="Do not crop letterbox/pillarbox borders; keep full 720x480 frame")
    parser.add_argument("--carve-only", action="store_true", help="Skip database parsing and directly carve .ithmb files")
    parser.add_argument("--full-res-only", action="store_true", help="Only extract full-resolution photos")

    args = parser.parse_args()

    # 1. Determine Source Path
    source_path = None
    if args.path:
        source_path = Path(args.path).resolve()
        if not source_path.exists():
            print(f"[!] Error: Specified path does not exist: {source_path}")
            sys.exit(1)
    else:
        # Check current directory
        if (Path("Photos").exists() or any(Path(".").glob("*.ithmb")) or Path("Photo Database").exists()):
            source_path = Path(".").resolve()
        else:
            detected = find_ipod_volumes()
            if len(detected) == 1:
                source_path = detected[0]
                print(f"[*] Auto-detected iPod at: {source_path}")
            elif len(detected) > 1:
                print("[?] Multiple iPods detected. Please select one by running:")
                for d in detected:
                    print(f"    python3 ipod_photo_extractor.py '{d}'")
                sys.exit(0)
            else:
                print("[!] No mounted iPod automatically detected.")
                print("    Please specify the iPod volume path directly:")
                print("    python3 ipod_photo_extractor.py \"/Volumes/Your iPod Name\"")
                sys.exit(1)

    output_dir = Path(args.output).resolve()
    print(f"==================================================")
    print(f" iPod Photo Extractor")
    print(f" Source: {source_path}")
    print(f" Output: {output_dir}")
    print(f"==================================================")

    total_full_res = 0
    total_db_photos = 0
    total_carved = 0

    # 2. Phase 1: Recover Full-Resolution Photos
    print("\n[Phase 1] Checking for full-resolution original photos...")
    total_full_res = extract_full_resolution(source_path, output_dir)
    if total_full_res > 0:
        print(f"[+] Successfully extracted {total_full_res} full-resolution original photos!")
    else:
        print("[-] No full-resolution originals found (iTunes downscaling was used).")

    if args.full_res_only:
        print("\nFinished (--full-res-only specified).")
        return

    # Locate Photo Database and Thumbs folders
    db_candidates = [
        source_path / "Photos" / "Photo Database",
        source_path / "Photos" / "photo database",
        source_path / "Photo Database",
        source_path / "photo database",
        source_path / "iPod_Control" / "Artwork" / "ArtworkDB",
    ]

    photo_db_path = None
    for candidate in db_candidates:
        if candidate.exists() and candidate.is_file():
            photo_db_path = candidate
            break

    thumbs_dirs = [
        source_path / "Photos" / "Thumbs",
        source_path / "Photos" / "thumbs",
        source_path / "Thumbs",
        source_path / "thumbs",
        source_path / "Photos",
        source_path / "iPod_Control" / "Artwork",
        source_path,
    ]

    # 3. Phase 2: Extract via Photo Database
    if photo_db_path and not args.carve_only:
        print(f"\n[Phase 2] Found Photo Database at: {photo_db_path}")
        photos = parse_photo_database(photo_db_path)
        if photos:
            print(f"[+] Successfully parsed {len(photos)} photos from database.")
            total_db_photos = extract_from_database(
                photos, thumbs_dirs, output_dir,
                extract_all_sizes=args.all_sizes,
                no_crop=args.no_crop
            )
            print(f"[+] Extracted {total_db_photos} database photos to: {output_dir / 'database_photos'}")
        else:
            print("[!] Photo Database parsing yielded 0 photos. Falling back to direct carving...")
    else:
        if not args.carve_only:
            print("\n[-] Photo Database not found. Proceeding to direct .ithmb carving...")

    # 4. Phase 3: Direct Stream Carving fallback
    if total_db_photos == 0 or args.carve_only:
        print("\n[Phase 3] Scanning for raw .ithmb files to carve...")
        all_ithmb_files = sorted(source_path.rglob("*.ithmb"))
        if all_ithmb_files:
            print(f"[*] Found {len(all_ithmb_files)} .ithmb file(s).")
            all_ithmb_files.sort(key=ithmb_priority, reverse=True)

            for ithmb in all_ithmb_files:
                count = carve_ithmb_file(ithmb, output_dir)
                total_carved += count
        else:
            print("[-] No .ithmb files found.")

    # 5. Summary
    grand_total = total_full_res + total_db_photos + total_carved
    print(f"\n==================================================")
    print(f" Extraction Summary")
    print(f"==================================================")
    print(f" Full-Resolution Originals : {total_full_res}")
    print(f" Database Photos Extracted : {total_db_photos}")
    print(f" Raw .ithmb Frames Carved  : {total_carved}")
    print(f" Total Recovered Images    : {grand_total}")
    print(f" Destination Folder        : {output_dir}")
    print(f"==================================================\n")


if __name__ == "__main__":
    main()
