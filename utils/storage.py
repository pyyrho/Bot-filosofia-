"""Armazenamento simples com PostgreSQL opcional e fallback JSON.

Em Railway, defina DATABASE_URL para persistência em PostgreSQL. Sem essa
variável, o bot usa DATA_DIR/bot_data.json. Para persistência com arquivo,
monte um Railway Volume e configure DATA_DIR para o caminho do volume.
"""
from __future__ import annotations

import asyncio
import copy
import time
import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("bot.storage")


class StorageUnavailable(RuntimeError):
    """Operação não confirmada no banco; não indica sucesso nem troca de backend."""


class KVStore:
    def __init__(self) -> None:
        self._pool: Any = None
        self._ready = False
        self._init_lock = asyncio.Lock()
        self._file_lock = asyncio.Lock()
        self._cache: dict[tuple, tuple[float, Any]] = {}
        self._read_locks: dict[tuple, asyncio.Lock] = {}
        data_dir = Path(os.environ.get("DATA_DIR", "data"))
        self._path = data_dir / "bot_data.json"

    @property
    def using_postgres(self) -> bool:
        return self._pool is not None

    async def init(self) -> None:
        if self._ready:
            return
        async with self._init_lock:
            if self._ready:
                return

            database_url = os.environ.get("DATABASE_URL", "").strip()
            if database_url:
                try:
                    import asyncpg

                    self._pool = await asyncpg.create_pool(
                        dsn=database_url,
                        min_size=1,
                        max_size=5,
                        command_timeout=5,
                        timeout=10,
                        init=self._configure_connection,
                    )
                    async with self._pool.acquire() as conn:
                        await conn.execute(
                            """
                            CREATE TABLE IF NOT EXISTS bot_kv (
                                guild_id BIGINT NOT NULL,
                                namespace TEXT NOT NULL,
                                key TEXT NOT NULL,
                                value TEXT NOT NULL,
                                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                                PRIMARY KEY (guild_id, namespace, key)
                            )
                            """
                        )
                    log.info("Armazenamento PostgreSQL inicializado.")
                except Exception:
                    log.exception("Falha ao iniciar PostgreSQL. Usando fallback JSON.")
                    self._pool = None

            if self._pool is None:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                if not self._path.exists():
                    self._write_json_sync({})
                log.info("Armazenamento JSON inicializado em %s", self._path)

            self._ready = True

    @staticmethod
    async def _configure_connection(connection) -> None:
        await connection.execute("SET lock_timeout = '3s'")
        await connection.execute("SET statement_timeout = '5s'")

    def _cached(self, key: tuple):
        entry = self._cache.get(key)
        if entry and entry[0] > time.monotonic():
            return True, copy.deepcopy(entry[1])
        self._cache.pop(key, None)
        return False, None

    def _remember(self, key: tuple, value: Any) -> None:
        if len(self._cache) >= 2048:
            self._cache.pop(next(iter(self._cache)))
        self._cache[key] = (time.monotonic() + 10, copy.deepcopy(value))

    async def _query(self, method: str, *args):
        for attempt in range(2):
            try:
                return await asyncio.wait_for(getattr(self._pool, method)(*args, timeout=5), timeout=6)
            except Exception as exc:
                code = str(getattr(exc, 'sqlstate', '') or '')
                transient = isinstance(exc, (TimeoutError, OSError, ConnectionError)) or code.startswith('08') or code in {'55P03', '57014', '40001', '40P01'}
                if not transient:
                    raise
                if attempt:
                    raise StorageUnavailable('O banco não confirmou a operação. Tente novamente em instantes.') from exc
                log.warning('Banco lento/indisponível; repetindo uma operação uma vez.')
                await asyncio.sleep(0.2)

    async def close(self) -> None:
        if self._pool is not None:
            try:
                await asyncio.wait_for(self._pool.close(), timeout=6)
            except asyncio.TimeoutError:
                self._pool.terminate()
            self._pool = None
        self._ready = False
        self._cache.clear()

    def _read_json_sync(self) -> dict[str, Any]:
        try:
            with self._path.open("r", encoding="utf-8") as fp:
                data = json.load(fp)
            return data if isinstance(data, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _write_json_sync(self, data: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temp = self._path.with_suffix(".tmp")
        with temp.open("w", encoding="utf-8") as fp:
            json.dump(data, fp, ensure_ascii=False, indent=2)
        temp.replace(self._path)

    async def get(
        self,
        guild_id: int,
        namespace: str,
        key: str,
        default: Any = None,
    ) -> Any:
        await self.init()
        if self._pool is not None:
            cache_key = (guild_id, namespace, key)
            found, value = self._cached(cache_key)
            if found:
                return value
            lock = self._read_locks.setdefault(cache_key, asyncio.Lock())
            try:
                async with lock:
                    found, value = self._cached(cache_key)
                    if found:
                        return value
                    raw = await self._query('fetchval',
                        "SELECT value FROM bot_kv WHERE guild_id=$1 AND namespace=$2 AND key=$3",
                        guild_id, namespace, key)
                    if raw is None:
                        return default
                    try:
                        value = json.loads(raw)
                    except json.JSONDecodeError:
                        return default
                    self._remember(cache_key, value)
                    return copy.deepcopy(value)
            finally:
                # Uma referência à trava ainda aguardada permanece válida;
                # cache compartilhado evita repetir a consulta após o primeiro.
                if not lock.locked() and not getattr(lock, '_waiters', None):
                    self._read_locks.pop(cache_key, None)

        async with self._file_lock:
            data = self._read_json_sync()
            return data.get(str(guild_id), {}).get(namespace, {}).get(key, default)

    async def set(self, guild_id: int, namespace: str, key: str, value: Any) -> None:
        await self.init()
        raw = json.dumps(value, ensure_ascii=False)
        if self._pool is not None:
            await self._query("execute",
                """
                INSERT INTO bot_kv (guild_id, namespace, key, value)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (guild_id, namespace, key)
                DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()
                WHERE bot_kv.value IS DISTINCT FROM EXCLUDED.value
                """,
                guild_id,
                namespace,
                key,
                raw,
            )
            self._cache.pop((guild_id, namespace, None), None)
            self._remember((guild_id, namespace, key), value)
            return

        async with self._file_lock:
            data = self._read_json_sync()
            guild_data = data.setdefault(str(guild_id), {})
            namespace_data = guild_data.setdefault(namespace, {})
            namespace_data[key] = value
            self._write_json_sync(data)

    async def delete(self, guild_id: int, namespace: str, key: str) -> None:
        await self.init()
        if self._pool is not None:
            await self._query("execute",
                "DELETE FROM bot_kv WHERE guild_id=$1 AND namespace=$2 AND key=$3",
                guild_id,
                namespace,
                key,
            )
            self._cache.pop((guild_id, namespace, key), None)
            self._cache.pop((guild_id, namespace, None), None)
            return

        async with self._file_lock:
            data = self._read_json_sync()
            namespace_data = data.get(str(guild_id), {}).get(namespace, {})
            namespace_data.pop(key, None)
            self._write_json_sync(data)

    async def all(self, guild_id: int, namespace: str) -> dict[str, Any]:
        await self.init()
        if self._pool is not None:
            found, cached = self._cached((guild_id, namespace, None))
            if found:
                return cached
            rows = await self._query("fetch",
                "SELECT key, value FROM bot_kv WHERE guild_id=$1 AND namespace=$2",
                guild_id,
                namespace,
            )
            result: dict[str, Any] = {}
            for row in rows:
                try:
                    result[row["key"]] = json.loads(row["value"])
                except json.JSONDecodeError:
                    continue
            self._remember((guild_id, namespace, None), result)
            return result

        async with self._file_lock:
            data = self._read_json_sync()
            namespace_data = data.get(str(guild_id), {}).get(namespace, {})
            return dict(namespace_data) if isinstance(namespace_data, dict) else {}


store = KVStore()
