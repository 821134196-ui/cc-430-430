"""SQLite schema 与连接管理。

所有写操作在事务内执行，关键竞争路径（时段确认、签收）使用
BEGIN IMMEDIATE 行级锁串行化，保证同一时段只有一辆车确认成功。
"""
import sqlite3
from contextlib import contextmanager

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS building (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,          -- 如 3 号楼
    address     TEXT NOT NULL,                 -- 演示地址
    loading_bays INTEGER NOT NULL DEFAULT 1    -- 装卸区容量（同时卸货车数）
);

CREATE TABLE IF NOT EXISTS elevator (
    id          INTEGER PRIMARY KEY,
    building_id INTEGER NOT NULL REFERENCES building(id),
    code        TEXT NOT NULL,                  -- 如 1#梯
    load_kg     INTEGER NOT NULL,               -- 电梯额定载重 kg
    cabin_cm    TEXT NOT NULL,                  -- 轿厢尺寸 长x宽x高 cm
    door_cm     TEXT NOT NULL,                  -- 门洞净尺寸 宽x高 cm
    status      TEXT NOT NULL DEFAULT 'normal', -- normal | fault
    UNIQUE(building_id, code)
);

CREATE TABLE IF NOT EXISTS unit (
    id          INTEGER PRIMARY KEY,
    building_id INTEGER NOT NULL REFERENCES building(id),
    room_no     TEXT NOT NULL,                  -- 房号
    resident    TEXT NOT NULL,                   -- 住户（演示数据）
    phone       TEXT NOT NULL,                   -- 住户电话（演示数据，门岗端不可见）
    UNIQUE(building_id, room_no)
);

CREATE TABLE IF NOT EXISTS protection_point (
    id          INTEGER PRIMARY KEY,
    building_id INTEGER NOT NULL REFERENCES building(id),
    name        TEXT NOT NULL,                   -- 如 一层大堂地面 / 电梯轿厢壁板
    kind        TEXT NOT NULL,                   -- floor | wall | corner
    required    INTEGER NOT NULL DEFAULT 1      -- 是否必设保护板
);

CREATE TABLE IF NOT EXISTS material (
    id          INTEGER PRIMARY KEY,
    unit_id     INTEGER NOT NULL REFERENCES unit(id),
    name        TEXT NOT NULL,
    weight_kg   REAL NOT NULL,
    length_cm   REAL NOT NULL,
    width_cm    REAL NOT NULL,
    height_cm   REAL NOT NULL,
    overlong    INTEGER NOT NULL DEFAULT 0,     -- 是否超长（人工标记，超门洞即需走货梯方案）
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 车辆主数据由模拟适配器提供，表中仅保存预约关联快照（脱敏视图只取必要字段）
CREATE TABLE IF NOT EXISTS appointment (
    id           INTEGER PRIMARY KEY,
    unit_id      INTEGER NOT NULL REFERENCES unit(id),
    building_id  INTEGER NOT NULL REFERENCES building(id),
    elevator_id  INTEGER NOT NULL REFERENCES elevator(id),
    slot_date    TEXT NOT NULL,                  -- YYYY-MM-DD
    slot_start   TEXT NOT NULL,                  -- HH:MM
    slot_end     TEXT NOT NULL,
    vehicle_plate TEXT NOT NULL,
    driver_name  TEXT NOT NULL,
    driver_phone TEXT NOT NULL,                  -- 门岗端不可见
    carrier      TEXT NOT NULL,                  -- 承运商
    status       TEXT NOT NULL DEFAULT 'held',   -- held | confirmed | released | gate_passed | arrived | signed
    total_weight_kg REAL NOT NULL DEFAULT 0,
    has_overlong INTEGER NOT NULL DEFAULT 0,
    hold_expires_at TEXT,                        -- 占位过期时刻（epoch 秒，文本存储）
    confirmed_at TEXT,
    gate_passed_at TEXT,
    arrived_at   TEXT,
    signed_at    TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    note         TEXT
);

-- 同一时段同一电梯至多一条"占用中"预约：
-- held / confirmed / gate_passed / arrived / signed 均占用；released 不占用。
-- 用部分唯一索引在数据库层兜底容量竞争。
CREATE UNIQUE INDEX IF NOT EXISTS ux_slot_elevator
    ON appointment(elevator_id, slot_date, slot_start)
    WHERE status IN ('held','confirmed','gate_passed','arrived','signed');

-- 同一时段同一楼栋的装卸区也有容量限制，计数在服务层校验，
-- 这里用按楼栋时段的计数表把"第 N 个车位"也变成可串行约束的行。
CREATE TABLE IF NOT EXISTS bay_ticket (
    id           INTEGER PRIMARY KEY,
    building_id  INTEGER NOT NULL REFERENCES building(id),
    slot_date    TEXT NOT NULL,
    slot_start   TEXT NOT NULL,
    bay_no       INTEGER NOT NULL,
    appointment_id INTEGER REFERENCES appointment(id),
    UNIQUE(building_id, slot_date, slot_start, bay_no)
);

CREATE TABLE IF NOT EXISTS protection_check (
    id              INTEGER PRIMARY KEY,
    appointment_id  INTEGER NOT NULL REFERENCES appointment(id),
    point_id        INTEGER NOT NULL REFERENCES protection_point(id),
    present         INTEGER NOT NULL,            -- 保护板是否到位
    photo_path      TEXT,                        -- 本地照片相对路径
    checked_by      TEXT NOT NULL,
    checked_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(appointment_id, point_id)
);

CREATE TABLE IF NOT EXISTS protection_confirmation (
    id              INTEGER PRIMARY KEY,
    appointment_id  INTEGER NOT NULL UNIQUE REFERENCES appointment(id),
    approved        INTEGER NOT NULL,            -- 物业是否确认保护方案
    approved_by     TEXT NOT NULL,
    approved_at     TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    remark          TEXT
);

-- 门岗放行记录；每次尝试登记一行，成功的放行推动预约状态。
CREATE TABLE IF NOT EXISTS gate_record (
    id              INTEGER PRIMARY KEY,
    appointment_id  INTEGER NOT NULL REFERENCES appointment(id),
    result          TEXT NOT NULL,               -- pass | reject
    issue_code      TEXT,                        -- late | overweight | no_protection |
                                                 -- elevator_fault | wrong_building | none
    detail          TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 签收：每个预约至多一条有效签收，重复提交幂等返回首条。
CREATE TABLE IF NOT EXISTS signoff (
    id              INTEGER PRIMARY KEY,
    appointment_id  INTEGER NOT NULL UNIQUE REFERENCES appointment(id),
    signed_by       TEXT NOT NULL,
    signed_at       TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    note            TEXT
);

-- 签收明细差异：按材料登记实收数量/重量差。
CREATE TABLE IF NOT EXISTS signoff_item (
    id              INTEGER PRIMARY KEY,
    signoff_id      INTEGER NOT NULL REFERENCES signoff(id),
    material_id     INTEGER NOT NULL REFERENCES material(id),
    declared_weight_kg REAL NOT NULL,
    received_weight_kg REAL,
    declared_qty    INTEGER NOT NULL DEFAULT 1,
    received_qty    INTEGER,
    diff_code       TEXT,                        -- short | damaged | extra | ok
    UNIQUE(signoff_id, material_id)
);

CREATE TABLE IF NOT EXISTS material_addition (
    -- 临时增加材料：物业须重新核验保护方案后方可放行
    id              INTEGER PRIMARY KEY,
    appointment_id  INTEGER NOT NULL REFERENCES appointment(id),
    material_id     INTEGER NOT NULL REFERENCES material(id),
    rechecked       INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
"""

# 合法状态流转
OCCUPYING = ("held", "confirmed", "gate_passed", "arrived", "signed")


def connect(db_path=None) -> sqlite3.Connection:
    path = str(db_path or config.DB_PATH)
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


@contextmanager
def immediate_tx(conn: sqlite3.Connection):
    """BEGIN IMMEDIATE：取写锁后再读计数，杜绝并发双确认。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
