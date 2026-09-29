"""保护照片本地存储工具。

真实场景由物业巡检 App 拍照上传；演示环境无摄像头，用标准库
（zlib + struct 手写 PNG）生成带时间戳文件名的占位照片，
同样落盘到本地保护照片目录。
"""
import struct
import zlib

from . import services


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    chunk = tag + data
    return struct.pack(">I", len(data)) + chunk + struct.pack(">I", zlib.crc32(chunk) & 0xFFFFFFFF)


def placeholder_png(label: str = "", w: int = 480, h: int = 320,
                    bg=(20, 67, 47), stripe=(242, 106, 27)) -> bytes:
    """生成深森林绿底、工程橙斜纹的占位 PNG（label 仅记录在 tEXt 块）。"""
    row = bytearray()
    for y in range(h):
        row.append(0)  # filter type 0
        for x in range(w):
            band = ((x + y) // 24) % 8 == 0 and 40 < y < h - 40 and 40 < x < w - 40
            border = x < 8 or y < 8 or x >= w - 8 or y >= h - 8
            if border:
                r, g, b = stripe
            elif band:
                r, g, b = (bg[0] // 2 + 60, bg[1] // 2 + 60, bg[2] // 2 + 60)
            else:
                r, g, b = bg
            row += bytes((r, g, b))
    raw = zlib.compress(bytes(row), 9)

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)  # 8-bit RGB
    text = (b"Label\x00" + label.encode("utf-8"))[:78]
    return b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", ihdr) + \
        _png_chunk(b"tEXt", text) + _png_chunk(b"IDAT", raw) + _png_chunk(b"IEND", b"")


def capture_demo_photo(appointment_id: int, point_id: int, point_name: str) -> str:
    data = placeholder_png(f"appt {appointment_id} point {point_id}:{point_name}")
    return services.save_photo(appointment_id, point_id, data, suffix="png")
