"""外部系统模拟适配器：车辆登记、地磅称重、保护照片存储。

真实部署时 VehicleAdapter / WeighbridgeAdapter 可替换为对接门岗系统与
地磅设备的实现；本地运行全部使用内置模拟数据。
"""
import base64
import struct
import zlib
from datetime import datetime
from pathlib import Path

from . import config

# 演示车辆白名单（车牌 -> 车辆/司机信息）
VEHICLES = {
    "沪A·12345": {"driver": "王师傅", "company": "顺达建材配送", "tare_kg": 2800},
    "沪B·66888": {"driver": "李建国", "company": "宏宇物流", "tare_kg": 3200},
    "沪C·90210": {"driver": "赵大勇", "company": "鑫源运输队", "tare_kg": 2600},
    "浙D·55667": {"driver": "陈志强", "company": "江南货运", "tare_kg": 3500},
}


class VehicleAdapter:
    """模拟车辆登记系统：车牌识别 + 车辆信息查询。"""

    def recognize(self, plate):
        return self.find(plate) is not None

    def find(self, plate):
        info = VEHICLES.get(normalize_plate(plate))
        if not info:
            return None
        return {"plate": normalize_plate(plate), **info}

    def all_plates(self):
        return sorted(VEHICLES.keys())


class WeighbridgeAdapter:
    """模拟地磅：毛重 = 车辆整备质量(tare) + 货物实称重量。

    为让“超重/重量边界”可演示，称重结果可通过 overrides 注入偏差；
    默认实称重量 = 申报重量（准确）。
    """

    def __init__(self, overrides=None):
        # overrides: {plate: 实称货物重量kg 或 (系数, 加量)}
        self._overrides = overrides or {}

    def weigh(self, plate, declared_kg):
        info = VEHICLES.get(normalize_plate(plate))
        tare = info["tare_kg"] if info else 3000
        actual_goods = self._overrides.get(normalize_plate(plate), declared_kg)
        return round(tare + float(actual_goods), 1), round(float(actual_goods), 1)


def normalize_plate(plate):
    """统一车牌展示格式：去空格，中点统一为 ·。"""
    if plate is None:
        return ""
    p = plate.strip().replace(" ", "").replace(".", "·")
    if "·" not in p and len(p) >= 3:
        # 沪A12345 -> 沪A·12345（第2个字符后插入中点）
        p = p[:2] + "·" + p[2:]
    return p


# ---------- 保护照片：本地目录存储，默认生成纯色占位 PNG ----------

def _png_placeholder(path: Path, rgb):
    """无依赖生成纯色 PNG 占位图。"""
    w = h = 320

    def chunk(typ, data):
        c = struct.pack(">I", len(data)) + typ + data
        return c + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF)

    raw = b""
    row = b"\x00" + bytes(rgb) * w
    raw = row * h
    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw, 9))
           + chunk(b"IEND", b""))
    path.write_bytes(png)


def save_photo(appointment_id: int, point_id: int, uploaded=None) -> str:
    """保存保护点照片。无上传时生成深森林绿占位图。返回相对 PHOTO_DIR 的路径。"""
    config.ensure_dirs()
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    name = f"a{appointment_id}_p{point_id}_{ts}.png"
    dest = config.PHOTO_DIR / name
    if uploaded is not None:
        dest.write_bytes(uploaded)
    else:
        _png_placeholder(dest, (27, 67, 50))  # 深森林绿
    return name


def photo_abspath(rel: str) -> Path:
    return config.PHOTO_DIR / rel
