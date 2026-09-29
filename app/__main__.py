"""命令行入口：

    python -m app              启动 Web 服务
    python -m app demo         写入演示数据 + 创建样例（预约/放行/签收）+ 启动服务
    python -m app reset        清空本地数据库与照片后重建演示数据
"""
import sys

from . import config
from .db import connect, init_db
from .seed import seed


def _fresh_conn():
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = connect()
    init_db(conn)
    return conn


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    cmd = argv[0] if argv else "serve"

    if cmd == "reset":
        for p in (config.DB_PATH,):
            if p.exists():
                p.unlink()
        for suffix in ("-wal", "-shm"):
            p = config.DB_PATH.with_name(config.DB_PATH.name + suffix)
            if p.exists():
                p.unlink()
        if config.PHOTO_DIR.exists():
            for f in config.PHOTO_DIR.glob("*"):
                f.unlink()
        conn = _fresh_conn()
        seed(conn)
        conn.close()
        print(f"已重置：{config.DB_PATH}")
        return

    if cmd == "demo":
        from .demo import run_demo
        run_demo()

    from .ui import serve
    serve()


if __name__ == "__main__":
    main()
