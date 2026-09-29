"""全局配置：路径与业务常量。"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("APP_DATA_DIR", BASE_DIR / "data"))
DB_PATH = DATA_DIR / "renovation.db"
PHOTO_DIR = DATA_DIR / "photos"

# 占位预约保留时长（分钟）：物业未在此时长内确认保护方案则自动释放
HOLD_MINUTES = int(os.environ.get("APP_HOLD_MINUTES", "20"))
# 迟到宽限（分钟）：超过时段开始时间多少分钟算迟到
LATE_GRACE_MINUTES = int(os.environ.get("APP_LATE_GRACE_MINUTES", "15"))
# 超长材料阈值（米）：任一单边长度达到该值标记为超长
OVERLONG_M = float(os.environ.get("APP_OVERLONG_M", "2.4"))

# 颜色（界面使用）：白 / 深森林绿 / 工程橙
COLOR_WHITE = "#ffffff"
COLOR_FOREST = "#1b4332"
COLOR_FOREST_2 = "#2d6a4f"
COLOR_ORANGE = "#f77f00"
COLOR_ORANGE_DARK = "#e85d04"

# 异常类型
EX_LATE = "late"                    # 车辆迟到
EX_OVERWEIGHT = "overweight"        # 材料超重
EX_MISSING_PROTECTION = "missing_protection"  # 保护板缺失
EX_ELEVATOR_FAULT = "elevator_fault"          # 电梯故障
EX_WRONG_BUILDING = "wrong_building"          # 错楼栋
EX_SIGNOFF_DIFF = "signoff_diff"              # 签收差异

EXCEPTION_LABELS = {
    EX_LATE: "车辆迟到",
    EX_OVERWEIGHT: "材料超重",
    EX_MISSING_PROTECTION: "保护板缺失",
    EX_ELEVATOR_FAULT: "电梯故障",
    EX_WRONG_BUILDING: "错楼栋",
    EX_SIGNOFF_DIFF: "签收差异",
}

# 预约状态
ST_HELD = "held"            # 已占位，等待物业确认保护方案
ST_PROTECTED = "protected"  # 保护方案已确认，可放行
ST_PASSED = "passed"        # 门岗已放行
ST_SIGNED = "signed"        # 已签收（终态，不可回退）
ST_EXPIRED = "expired"      # 过期释放（终态）
ST_CANCELLED = "cancelled"  # 取消（终态）

ACTIVE_STATUSES = (ST_HELD, ST_PROTECTED, ST_PASSED)


def ensure_dirs():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PHOTO_DIR.mkdir(parents=True, exist_ok=True)
