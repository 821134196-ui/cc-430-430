"""全局配置：路径、时段、占位时效与界面配色。"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("APP_DATA_DIR", ROOT / "data"))
DB_PATH = Path(os.environ.get("APP_DB_PATH", DATA_DIR / "booking.db"))
PHOTO_DIR = Path(os.environ.get("APP_PHOTO_DIR", DATA_DIR / "photos"))

# 时段粒度
SLOT_MINUTES = 30
DAY_START = "08:00"
DAY_END = "18:00"

# HELD 占位有效期（秒）：物业未在此时效内确认保护方案即自动释放
HOLD_TTL_SECONDS = int(os.environ.get("APP_HOLD_TTL", 15 * 60))
# 车辆晚于时段结束多少分钟仍可放行
LATE_GRACE_MINUTES = 10

# 配色（白色 / 深森林绿 / 工程橙）
COLOR_FOREST = "#14432f"
COLOR_FOREST_DARK = "#0d2e20"
COLOR_ORANGE = "#f26a1b"
COLOR_WHITE = "#ffffff"
COLOR_INK = "#1c2a24"
COLOR_MIST = "#eef3ef"

HTTP_PORT = int(os.environ.get("APP_PORT", "8000"))
