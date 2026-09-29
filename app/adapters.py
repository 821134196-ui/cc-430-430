"""外部系统模拟适配器。

- VehicleAdapter：车辆/承运商档案（演示数据），真实系统中对接车场登记库。
- WeighbridgeAdapter：地磅称重，真实系统中对接门口地磅；模拟时可注入偏差与故障。

适配器接口刻意保持简单、可替换：服务层只依赖这两个方法签名。
"""
import random
from dataclasses import dataclass


@dataclass(frozen=True)
class Vehicle:
    plate: str
    carrier: str
    driver: str
    driver_phone: str
    permitted_buildings: tuple  # 备案可进入的楼栋（用于错楼栋判定）


class VehicleAdapter:
    """车辆档案模拟适配器。"""

    def __init__(self, vehicles=None):
        if vehicles is None:
            vehicles = [
                Vehicle("沪A·D3268", "稳达货运", "周建国", "13800001001", ("1", "2", "3")),
                Vehicle("沪B·K7751", "安居配送", "李茂生", "13800002002", ("1", "2")),
                Vehicle("沪C·8F209", "稳达货运", "陈广路", "13800003003", ("3",)),
                Vehicle("沪D·Q1046", "鲁班整装物流", "赵铁锤", "13800004004", ("1", "2", "3")),
                Vehicle("沪E·55T88", "安居配送", "孙承运", "13800005005", ("2", "3")),
            ]
        self._by_plate = {v.plate: v for v in vehicles}

    def get(self, plate: str):
        return self._by_plate.get(plate)

    def all_plates(self):
        return tuple(self._by_plate)


class WeighbridgeAdapter:
    """地磅模拟适配器。

    mode:
      exact  —— 称重 = 申报总重
      jitter —— 在偏差比例内浮动（默认 ±3%）
      overweight —— 强制叠加超重吨数，用于演练超重登记
      fault  —— 地磅故障，抛 WeighbridgeError
    """

    def __init__(self, mode="exact", jitter=0.03, force_extra_kg=0.0, seed=None):
        self.mode = mode
        self.jitter = jitter
        self.force_extra_kg = force_extra_kg
        self._rng = random.Random(seed or 20260929)

    def weigh(self, plate: str, declared_total_kg: float) -> float:
        if self.mode == "fault":
            raise WeighbridgeError("地磅通信超时")
        if self.mode == "overweight":
            return round(declared_total_kg + self.force_extra_kg, 1)
        if self.mode == "jitter":
            factor = 1 + self._rng.uniform(-self.jitter, self.jitter)
            return round(declared_total_kg * factor, 1)
        return round(float(declared_total_kg), 1)


class WeighbridgeError(RuntimeError):
    pass
