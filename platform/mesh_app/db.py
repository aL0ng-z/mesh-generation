"""SQLite 连接、事务与显式迁移管理。"""

from __future__ import annotations

import argparse
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .config import Settings


LATEST_SCHEMA_VERSION = 2


class DatabaseVersionError(RuntimeError):
    """数据库版本与代码不匹配。"""

    def __init__(self, current: int, expected: int) -> None:
        self.current = current
        self.expected = expected
        super().__init__(
            f"数据库版本为 {current}，代码要求版本 {expected}；请先执行显式迁移命令"
        )


def connect_database(
    path: str | Path,
    *,
    busy_timeout_ms: int = 5000,
) -> sqlite3.Connection:
    """打开一个已配置 WAL、外键和等待超时的 SQLite 连接。"""

    database_path = Path(path)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(
        database_path,
        timeout=busy_timeout_ms / 1000.0,
        isolation_level=None,
        check_same_thread=False,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = NORMAL")
    return connection


@contextmanager
def transaction(
    connection: sqlite3.Connection,
    *,
    immediate: bool = False,
) -> Iterator[sqlite3.Connection]:
    """在现有连接上执行事务，并在异常时可靠回滚。"""

    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield connection
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


class Database:
    """为 API 和 Worker 提供短连接事务边界。"""

    def __init__(
        self,
        path: str | Path,
        migrations_dir: str | Path | None = None,
        busy_timeout_ms: int = 5000,
    ) -> None:
        self.path = Path(path).resolve()
        self.migrations_dir = (
            Path(migrations_dir).resolve()
            if migrations_dir is not None
            else Path(__file__).resolve().parents[1] / "migrations"
        )
        self.busy_timeout_ms = busy_timeout_ms

    def connect(self) -> sqlite3.Connection:
        """创建调用者负责关闭的数据库连接。"""

        return connect_database(self.path, busy_timeout_ms=self.busy_timeout_ms)

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """创建短连接事务，退出上下文后自动关闭。"""

        connection = self.connect()
        try:
            with transaction(connection, immediate=immediate) as active:
                yield active
        finally:
            connection.close()

    @contextmanager
    def reading(self) -> Iterator[sqlite3.Connection]:
        """创建只用于读取的短连接。"""

        connection = self.connect()
        try:
            yield connection
        finally:
            connection.close()

    def current_version(self) -> int:
        """读取 SQLite ``user_version``。"""

        with self.reading() as connection:
            return int(connection.execute("PRAGMA user_version").fetchone()[0])

    def require_current(self) -> None:
        """数据库不是当前版本时拒绝继续运行。"""

        current = self.current_version()
        if current != LATEST_SCHEMA_VERSION:
            raise DatabaseVersionError(current, LATEST_SCHEMA_VERSION)

    def migrate(self, *, target_version: int = LATEST_SCHEMA_VERSION) -> int:
        """按文件名顺序显式应用迁移，绝不由请求路径调用。"""

        if target_version < 0 or target_version > LATEST_SCHEMA_VERSION:
            raise ValueError(f"迁移目标版本必须位于 0 到 {LATEST_SCHEMA_VERSION} 之间")
        current = self.current_version()
        if current > target_version:
            raise DatabaseVersionError(current, target_version)
        migrations = _migration_files(self.migrations_dir)
        for version in range(current + 1, target_version + 1):
            migration_path = migrations.get(version)
            if migration_path is None:
                raise RuntimeError(f"缺少数据库迁移文件：版本 {version}")
            script = migration_path.read_text(encoding="utf-8")
            connection = self.connect()
            try:
                connection.executescript(script)
                actual = int(connection.execute("PRAGMA user_version").fetchone()[0])
                if actual != version:
                    raise RuntimeError(
                        f"迁移 {migration_path.name} 未将数据库版本更新为 {version}"
                    )
            finally:
                connection.close()
        return self.current_version()


def _migration_files(directory: Path) -> dict[int, Path]:
    """返回版本号到 SQL 文件的严格映射。"""

    if not directory.is_dir():
        raise RuntimeError(f"找不到数据库迁移目录：{directory}")
    result: dict[int, Path] = {}
    for path in sorted(directory.glob("[0-9][0-9][0-9][0-9]_*.sql")):
        version = int(path.name.split("_", 1)[0])
        if version in result:
            raise RuntimeError(f"数据库迁移版本重复：{version}")
        result[version] = path
    return result


def migrate_database(
    path: str | Path,
    migrations_dir: str | Path | None = None,
    *,
    target_version: int = LATEST_SCHEMA_VERSION,
    busy_timeout_ms: int = 5000,
) -> int:
    """函数式迁移入口，便于部署脚本和测试调用。"""

    return Database(path, migrations_dir, busy_timeout_ms).migrate(target_version=target_version)


def main(argv: list[str] | None = None) -> int:
    """提供 ``python -m mesh_app.db migrate`` 显式迁移命令。"""

    parser = argparse.ArgumentParser(description="网格平台 SQLite 显式迁移工具")
    parser.add_argument("command", choices=("migrate", "check"))
    parser.add_argument("--database", type=Path, help="数据库文件；默认读取 MESH_DATABASE_PATH")
    parser.add_argument("--migrations", type=Path, help="迁移目录")
    args = parser.parse_args(argv)
    settings = Settings.from_env()
    database = Database(
        args.database or settings.database_path,
        args.migrations or settings.migrations_dir,
        settings.busy_timeout_ms,
    )
    if args.command == "migrate":
        settings.ensure_directories()
        version = database.migrate()
        print(f"数据库迁移完成，当前版本：{version}")
    else:
        database.require_current()
        print(f"数据库版本检查通过：{LATEST_SCHEMA_VERSION}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "Database",
    "DatabaseVersionError",
    "LATEST_SCHEMA_VERSION",
    "connect_database",
    "migrate_database",
    "transaction",
]
