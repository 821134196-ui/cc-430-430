"""SQLite 连接与建表。

容量竞争控制：所有对同一 (时段, 电梯/装卸区) 名额的占用与放行确认，
都在单条 ``BEGIN IMMEDIATE`` 写事务中完成，SQLite 写锁串行化，
保证“同一时段并发确认只允许一辆车成功”。
"""
import sqlite3
from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS building (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,          -- 楼栋名，如 3号楼
    address     TEXT NOT NULL,                 -- 演示地址
    dock_count  INTEGER NOT NULL DEFAULT 1     -- 装卸区车位数
);

CREATE TABLE IF NOT EXISTS elevator (
    id          INTEGER PRIMARY KEY,
    building_id INTEGER NOT NULL REFERENCES building(id),
    code        TEXT NOT NULL,                  -- 电梯编号，如 1#梯
    max_load_kg REAL NOT NULL DEFAULT 1000,     -- 电梯载重上限
    status      TEXT NOT NULL DEFAULT 'ok'      -- ok / fault
);

CREATE TABLE IF NOT EXISTS loading_dock (
    id          INTEGER PRIMARY KEY,
    building_id INTEGER NOT NULL REFERENCES building(id),
    code        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS unit (
    id          INTEGER PRIMARY KEY,
    building_id INTEGER NOT NULL REFERENCES building(id),
    room_no     TEXT NOT NULL,                  -- 房号
    resident    TEXT NOT NULL,                  -- 住户（演示数据）
    phone       TEXT NOT NULL,
    UNIQUE(building_id, room_no)
);

-- 材料清单（按单元的一次装修申报；appointment 引用其快照版本）
CREATE TABLE IF NOT EXISTS material_list (
    id           INTEGER PRIMARY KEY,
    unit_id      INTEGER NOT NULL REFERENCES unit(id),
    version      INTEGER NOT NULL DEFAULT 1,    -- 每次临时增改 +1
    status       TEXT NOT NULL DEFAULT 'draft', -- draft / verified / recheck
    total_weight REAL NOT NULL DEFAULT 0,
    has_overlong INTEGER NOT NULL DEFAULT 0,
    note         TEXT,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS material_item (
    id          INTEGER PRIMARY KEY,
    list_id     INTEGER NOT NULL REFERENCES material_list(id),
    name        TEXT NOT NULL,
    qty         REAL NOT NULL DEFAULT 1,
    unit        TEXT NOT NULL DEFAULT '件',
    weight_kg   REAL NOT NULL,                  -- 单项总重
    length_m    REAL NOT NULL DEFAULT 0,
    width_m     REAL NOT NULL DEFAULT 0,
    height_m    REAL NOT NULL DEFAULT 0,
    overlong    INTEGER NOT NULL DEFAULT 0,     -- 是否超长
    removed     INTEGER NOT NULL DEFAULT 0      -- 软删除（保留版本痕迹）
);

CREATE TABLE IF NOT EXISTS appointment (
    id            INTEGER PRIMARY KEY,
    code          TEXT NOT NULL UNIQUE,          -- 预约单号
    building_id   INTEGER NOT NULL REFERENCES building(id),
    elevator_id   INTEGER NOT NULL REFERENCES elevator(id),
    dock_id       INTEGER NOT NULL REFERENCES loading_dock(id),
    unit_id       INTEGER NOT NULL REFERENCES unit(id),
    plate         TEXT NOT NULL,                 -- 车牌
    driver        TEXT NOT NULL,
    slot_date     TEXT NOT NULL,                 -- YYYY-MM-DD
    slot_start    TEXT NOT NULL,                 -- HH:MM
    slot_end      TEXT NOT NULL,
    status        TEXT NOT NULL,                 -- held/protected/passed/signed/expired/cancelled
    list_version  INTEGER NOT NULL,              -- 预约时材料清单版本快照
    list_weight   REAL NOT NULL,                 -- 重量快照
    list_overlong INTEGER NOT NULL,
    hold_expires  TEXT NOT NULL,                 -- 占位过期时刻 ISO
    confirmed_at  TEXT,                          -- 物业确认保护方案时间
    passed_at     TEXT,
    signed_at     TEXT,
    created_at    TEXT NOT NULL,
    note          TEXT
);

-- 容量计数（按资源+日期+时段）。active=1 的预约占用名额。
CREATE TABLE IF NOT EXISTS slot_occupancy (
    id          INTEGER PRIMARY KEY,
    resource    TEXT NOT NULL,                  -- 'elev:3' / 'dock:2'
    slot_date   TEXT NOT NULL,
    slot_start  TEXT NOT NULL,
    slot_end    TEXT NOT NULL,
    capacity    INTEGER NOT NULL,
    used        INTEGER NOT NULL DEFAULT 0,
    UNIQUE(resource, slot_date, slot_start)
);

-- 电梯保护点位
CREATE TABLE IF NOT EXISTS protection_point (
    id           INTEGER PRIMARY KEY,
    building_id  INTEGER NOT NULL REFERENCES building(id),
    elevator_id  INTEGER REFERENCES elevator(id),
    name         TEXT NOT NULL,                  -- 如 轿厢内壁 / 门套
    required     INTEGER NOT NULL DEFAULT 1      -- 是否必检点
);

CREATE TABLE IF NOT EXISTS protection_check (
    id            INTEGER PRIMARY KEY,
    appointment_id INTEGER NOT NULL REFERENCES appointment(id),
    point_id      INTEGER NOT NULL REFERENCES protection_point(id),
    ok            INTEGER NOT NULL DEFAULT 0,
    photo_path    TEXT,
    checked_by    TEXT,
    checked_at    TEXT,
    UNIQUE(appointment_id, point_id)
);

-- 门岗放行记录
CREATE TABLE IF NOT EXISTS gate_pass (
    id             INTEGER PRIMARY KEY,
    appointment_id INTEGER NOT NULL UNIQUE REFERENCES appointment(id),
    plate          TEXT NOT NULL,
    guard          TEXT NOT NULL,
    passed         INTEGER NOT NULL,            -- 1 放行 / 0 拒绝
    reason         TEXT,
    weighed_kg     REAL,                          -- 模拟地磅读数
    created_at     TEXT NOT NULL
);

-- 签收记录（终态；按 appointment 唯一 => 签收幂等）
CREATE TABLE IF NOT EXISTS signoff (
    id             INTEGER PRIMARY KEY,
    appointment_id INTEGER NOT NULL UNIQUE REFERENCES appointment(id),
    receiver       TEXT NOT NULL,
    matched        INTEGER NOT NULL,             -- 清单与实收是否一致
    diff_detail    TEXT,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS signoff_item (
    id          INTEGER PRIMARY KEY,
    signoff_id  INTEGER NOT NULL REFERENCES signoff(id),
    name        TEXT NOT NULL,
    expected_qty REAL NOT NULL,
    actual_qty  REAL NOT NULL,
    unit        TEXT NOT NULL
);

-- 异常登记（迟到/超重/保护板缺失/电梯故障/错楼栋/签收差异）
CREATE TABLE IF NOT EXISTS exception_log (
    id             INTEGER PRIMARY KEY,
    appointment_id INTEGER REFERENCES appointment(id),
    kind           TEXT NOT NULL,
    detail         TEXT,
    created_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_appt_slot ON appointment(slot_date, slot_start, building_id);
CREATE INDEX IF NOT EXISTS idx_appt_status ON appointment(status);
CREATE INDEX IF NOT EXISTS idx_exc_appt ON exception_log(appointment_id);
"""


def connect(db_path=None):
    path = str(db_path or config.DB_PATH)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(db_path=None):
    config.ensure_dirs()
    conn = connect(db_path)
    conn.executescript(SCHEMA)
    return conn
