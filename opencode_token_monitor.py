from __future__ import annotations

import argparse
import csv
import ctypes
from ctypes import wintypes
import json
import logging
import math
import os
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import winsound
from dataclasses import dataclass
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

APP_NAME = "OpenCode Token Monitor"
APP_ID = "OpenCode.TokenMonitor"
VERSION = "3.0.5"
DEFAULT_WINDOW_WIDTH = 700
DEFAULT_WINDOW_HEIGHT = 700
MUTEX_NAME = "Local\\OpenCodeTokenMonitorSingleton"
SYNC_MUTEX_NAME = "Local\\OpenCodeTokenMonitorDataWriter"
PROCESS_POLL_SECONDS = 2.0
OPENCODE_PROCESS_NAMES = frozenset({"opencode.exe", "opencode-cli.exe", "opencode-desktop.exe"})
DASHBOARD_WINDOW: Any = None
PRICING_MEMORY: dict[str, Any] = {}
PRICING_MEMORY_META: dict[str, Any] = {}
PRICING_RATE_FIELDS = ("input", "output", "reasoning", "cache_read", "cache_write")
PRICING_MODES = {"official", "multiplier", "custom"}

TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
)
LEDGER_CSV_HEADER = (
    "recorded_at",
    "event_time",
    "date",
    "provider",
    "model",
    "variant",
    "project_id",
    "project",
    "project_path",
    "session_id",
    "agent",
    "source",
    "input_delta",
    "output_delta",
    "reasoning_delta",
    "cache_read_delta",
    "cache_write_delta",
    "total_with_cache_delta",
    "total_without_cache_read_delta",
    "cost_delta",
)

DEFAULT_CONFIG: dict[str, Any] = {
    "opencode_db": "",
    "sample_interval_seconds": 300,
    "daily_alert_tokens": 10_000_000,
    "spike_window_minutes": 30,
    "spike_alert_tokens": 10_000_000,
    "color_max_tokens": 100_000_000,
    "notifications": True,
    "sound": True,
    "auto_start": True,
    "full_reconcile_hours": 24,
    "ui_theme": "dark",
    "use_model_dev_pricing": True,
    "pricing_refresh_hours": 24,
    "pricing_mode": "official",
    "pricing_multiplier": 1.0,
    "display_currency": "USD",
    "usd_cny_rate": 7.20,
    "custom_pricing": {"default": {}, "models": []},
}


def app_data_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "OpenCodeTokenMonitor"


def executable_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable)
    return Path(__file__).resolve()


def app_command(*arguments: str) -> list[str]:
    if getattr(sys, "frozen", False):
        return [str(Path(sys.executable)), *arguments]
    return [sys.executable, str(Path(__file__).resolve()), *arguments]


def resource_path(relative: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / relative


def now_ms() -> int:
    return int(time.time() * 1000)


def local_day_bounds(ts_ms: int) -> tuple[str, int, int]:
    dt = datetime.fromtimestamp(ts_ms / 1000).astimezone()
    start = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return start.date().isoformat(), int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def format_int(value: int | float) -> str:
    value = int(value)
    sign = "-" if value < 0 else ""
    value = abs(value)
    if value >= 1_000_000_000:
        return f"{sign}{value / 1_000_000_000:.2f}B"
    if value >= 1_000_000:
        return f"{sign}{value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"{sign}{value / 1_000:.1f}K"
    return f"{sign}{value}"


def format_money(value: float) -> str:
    value = max(0.0, float(value))
    if 0 < value < 0.0001:
        return "<$0.0001"
    return f"${value:,.4f}"


def safe_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def safe_float(value: Any) -> float:
    try:
        return max(0.0, float(value or 0))
    except (TypeError, ValueError):
        return 0.0


def normalize_rates(value: Any) -> dict[str, float]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, float] = {}
    for field in PRICING_RATE_FIELDS:
        if field not in value:
            continue
        try:
            parsed = float(value[field])
            if math.isfinite(parsed):
                result[field] = max(0.0, parsed)
        except (TypeError, ValueError):
            continue
    return result


def pricing_mode(config: dict[str, Any] | None) -> str:
    mode = str((config or {}).get("pricing_mode") or "official").strip().lower()
    return mode if mode in PRICING_MODES else "official"


def pricing_multiplier(config: dict[str, Any] | None) -> float:
    try:
        value = float((config or {}).get("pricing_multiplier", 1.0))
    except (TypeError, ValueError):
        value = 1.0
    return min(1_000_000.0, max(0.0, value))


def display_currency(config: dict[str, Any] | None) -> str:
    value = str((config or {}).get("display_currency") or "USD").strip().upper()
    return "CNY" if value == "CNY" else "USD"


def usd_cny_rate(config: dict[str, Any] | None) -> float:
    try:
        value = float((config or {}).get("usd_cny_rate", 7.20))
    except (TypeError, ValueError):
        value = 7.20
    return min(1000.0, max(0.000001, value))


def custom_pricing_config(config: dict[str, Any] | None) -> dict[str, Any]:
    raw = (config or {}).get("custom_pricing")
    if not isinstance(raw, dict):
        raw = {}
    models = raw.get("models")
    if not isinstance(models, list):
        models = []
    return {"default": normalize_rates(raw.get("default")), "models": models}


def custom_rates_for_row(config: dict[str, Any] | None, row: dict[str, Any]) -> tuple[dict[str, float] | None, str]:
    custom = custom_pricing_config(config)
    default_rates = custom["default"]
    provider = str(row.get("provider_id") or "").casefold()
    model = str(row.get("model_id") or "").casefold()
    variant = str(row.get("variant") or "default").casefold()
    fallback: tuple[dict[str, float], str] | None = None
    for item in custom["models"]:
        if not isinstance(item, dict):
            continue
        item_provider = str(item.get("provider") or "").casefold()
        item_model = str(item.get("model") or "").casefold()
        item_variant = str(item.get("variant") or "default").casefold()
        rates = normalize_rates(item.get("rates"))
        if not rates or item_provider != provider or item_model != model:
            continue
        if item_variant == variant:
            merged = dict(default_rates)
            merged.update(rates)
            return merged, f"custom:{item.get('provider') or ''}/{item.get('model') or ''}/{item.get('variant') or 'default'}"
        if item_variant == "default" and fallback is None:
            merged = dict(default_rates)
            merged.update(rates)
            fallback = (merged, f"custom:{item.get('provider') or ''}/{item.get('model') or ''}/default")
    if fallback:
        return fallback
    if default_rates:
        return default_rates, "custom:default"
    return None, "custom:unconfigured"


def load_config(path: Path) -> dict[str, Any]:
    config = dict(DEFAULT_CONFIG)
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8-sig"))
            if isinstance(loaded, dict):
                config.update(loaded)
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Cannot read config {path}: {exc}") from exc
    return config


def save_config(path: Path, config: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def detect_opencode_db(config: dict[str, Any]) -> Path:
    candidates: list[Path] = []
    configured = str(config.get("opencode_db") or "").strip()
    if configured:
        configured_path = Path(configured).expanduser()
        # Do not invoke the OpenCode CLI merely to discover a path while the
        # monitor starts in the background. A valid explicit path is enough.
        if configured_path.is_file():
            return configured_path.resolve()
        candidates.append(configured_path)
    env_db = os.environ.get("OPENCODE_DB")
    if env_db:
        candidates.append(Path(env_db).expanduser())

    opencode = shutil_which("opencode")
    if opencode:
        try:
            result = subprocess.run(
                [opencode, "debug", "paths", "db"],
                capture_output=True,
                text=True,
                timeout=8,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if result.returncode == 0:
                value = result.stdout.strip().splitlines()[-1].strip()
                if value:
                    candidates.append(Path(value))
        except (OSError, subprocess.SubprocessError):
            pass

    home = Path.home()
    candidates.extend(
        [
            home / ".local" / "share" / "opencode" / "opencode.db",
            home / "AppData" / "Local" / "opencode" / "opencode.db",
            home / "AppData" / "Roaming" / "opencode" / "opencode.db",
        ]
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise FileNotFoundError("OpenCode database not found; set opencode_db in config.json")


def is_opencode_process_name(name: str) -> bool:
    return name.strip().lower() in OPENCODE_PROCESS_NAMES


def opencode_is_running() -> bool:
    """Return whether an OpenCode CLI or desktop process is currently running.

    This checks Windows process names only. It never opens or queries the
    OpenCode database, so the tray can remain idle cheaply while OpenCode is
    closed. The desktop background service is named ``opencode-cli.exe`` and
    the desktop application itself is named ``OpenCode.exe``.
    """
    if os.name != "nt":
        return True

    class ProcessEntry32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.windll.kernel32
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
    invalid_handle = ctypes.c_void_p(-1).value
    if not snapshot or snapshot == invalid_handle:
        return False
    try:
        entry = ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(ProcessEntry32W)
        if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            return False
        while True:
            if is_opencode_process_name(entry.szExeFile):
                return True
            if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                return False
    finally:
        kernel32.CloseHandle(snapshot)


def shutil_which(name: str) -> str | None:
    # Kept separate so tests can patch it without importing shutil globally.
    import shutil

    return shutil.which(name)


@dataclass
class UsageRecord:
    message_id: str
    source: str
    source_rank: int
    session_id: str
    project_id: str
    project_name: str
    project_path: str
    provider_id: str
    model_id: str
    variant: str
    agent: str
    event_time: int
    source_created: int
    source_updated: int
    input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost: float

    @property
    def total_with_cache(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.reasoning_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )

    @property
    def total_without_cache_read(self) -> int:
        return self.input_tokens + self.output_tokens + self.reasoning_tokens + self.cache_write_tokens

    def values(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "source": self.source,
            "source_rank": self.source_rank,
            "session_id": self.session_id,
            "project_id": self.project_id,
            "project_name": self.project_name,
            "project_path": self.project_path,
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "variant": self.variant,
            "agent": self.agent,
            "event_time": self.event_time,
            "source_created": self.source_created,
            "source_updated": self.source_updated,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "total_with_cache": self.total_with_cache,
            "total_without_cache_read": self.total_without_cache_read,
            "cost": self.cost,
        }


@dataclass
class SyncResult:
    records_seen: int = 0
    records_changed: int = 0
    ledger_rows: int = 0
    alerts: list[dict[str, Any]] | None = None
    full_reconcile: bool = False


class Store:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = data_dir / "monitor.db"
        self.csv_dir = data_dir / "csv"
        self.csv_dir.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path, timeout=15)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._schema()

    def _schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS usage_events (
                message_id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                source_rank INTEGER NOT NULL,
                session_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                project_name TEXT NOT NULL,
                project_path TEXT NOT NULL,
                provider_id TEXT NOT NULL,
                model_id TEXT NOT NULL,
                variant TEXT NOT NULL,
                agent TEXT NOT NULL,
                event_time INTEGER NOT NULL,
                source_created INTEGER NOT NULL,
                source_updated INTEGER NOT NULL,
                input_tokens INTEGER NOT NULL,
                output_tokens INTEGER NOT NULL,
                reasoning_tokens INTEGER NOT NULL,
                cache_read_tokens INTEGER NOT NULL,
                cache_write_tokens INTEGER NOT NULL,
                total_with_cache INTEGER NOT NULL,
                total_without_cache_read INTEGER NOT NULL,
                cost REAL NOT NULL,
                first_seen INTEGER NOT NULL,
                last_seen INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_usage_event_time ON usage_events(event_time);
            CREATE INDEX IF NOT EXISTS idx_usage_provider ON usage_events(provider_id, event_time);
            CREATE INDEX IF NOT EXISTS idx_usage_model ON usage_events(model_id, event_time);
            CREATE INDEX IF NOT EXISTS idx_usage_project ON usage_events(project_id, event_time);
            CREATE TABLE IF NOT EXISTS usage_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                detected_at INTEGER NOT NULL,
                message_id TEXT NOT NULL,
                source TEXT NOT NULL,
                session_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                project_name TEXT NOT NULL,
                project_path TEXT NOT NULL,
                provider_id TEXT NOT NULL,
                model_id TEXT NOT NULL,
                variant TEXT NOT NULL,
                agent TEXT NOT NULL,
                event_time INTEGER NOT NULL,
                input_delta INTEGER NOT NULL,
                output_delta INTEGER NOT NULL,
                reasoning_delta INTEGER NOT NULL,
                cache_read_delta INTEGER NOT NULL,
                cache_write_delta INTEGER NOT NULL,
                total_with_cache_delta INTEGER NOT NULL,
                total_without_cache_read_delta INTEGER NOT NULL,
                cost_delta REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_ledger_event_time ON usage_ledger(event_time);
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at INTEGER NOT NULL,
                kind TEXT NOT NULL,
                alert_key TEXT NOT NULL,
                amount INTEGER NOT NULL,
                threshold INTEGER NOT NULL,
                message TEXT NOT NULL
            );
            """
        )
        self.conn.commit()

    def get_meta(self, key: str, default: str = "") -> str:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )

    def existing_event(self, message_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM usage_events WHERE message_id=?", (message_id,)).fetchone()

    def upsert_event(self, record: UsageRecord) -> bool:
        old = self.existing_event(record.message_id)
        if old is not None and safe_int(old["source_rank"]) > record.source_rank:
            return False
        values = record.values()
        token_changed = old is None or any(safe_int(old[key]) != values[key] for key in TOKEN_FIELDS)
        metadata_changed = old is None or any(
            str(old[key] or "") != str(values[key] or "")
            for key in (
                "source",
                "session_id",
                "project_id",
                "project_name",
                "project_path",
                "provider_id",
                "model_id",
                "variant",
                "agent",
                "event_time",
                "source_created",
                "source_updated",
            )
        ) or abs(safe_float(old["cost"]) - record.cost) > 1e-12
        if not token_changed and not metadata_changed:
            self.conn.execute("UPDATE usage_events SET last_seen=? WHERE message_id=?", (now_ms(), record.message_id))
            return False

        timestamp = now_ms()
        self.conn.execute(
            """
            INSERT INTO usage_events(
                message_id,source,source_rank,session_id,project_id,project_name,project_path,
                provider_id,model_id,variant,agent,event_time,source_created,source_updated,
                input_tokens,output_tokens,reasoning_tokens,cache_read_tokens,cache_write_tokens,
                total_with_cache,total_without_cache_read,cost,first_seen,last_seen
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(message_id) DO UPDATE SET
                source=excluded.source,
                source_rank=excluded.source_rank,
                session_id=excluded.session_id,
                project_id=excluded.project_id,
                project_name=excluded.project_name,
                project_path=excluded.project_path,
                provider_id=excluded.provider_id,
                model_id=excluded.model_id,
                variant=excluded.variant,
                agent=excluded.agent,
                event_time=excluded.event_time,
                source_created=excluded.source_created,
                source_updated=excluded.source_updated,
                input_tokens=excluded.input_tokens,
                output_tokens=excluded.output_tokens,
                reasoning_tokens=excluded.reasoning_tokens,
                cache_read_tokens=excluded.cache_read_tokens,
                cache_write_tokens=excluded.cache_write_tokens,
                total_with_cache=excluded.total_with_cache,
                total_without_cache_read=excluded.total_without_cache_read,
                cost=excluded.cost,
                last_seen=excluded.last_seen
            """,
            (
                record.message_id,
                record.source,
                record.source_rank,
                record.session_id,
                record.project_id,
                record.project_name,
                record.project_path,
                record.provider_id,
                record.model_id,
                record.variant,
                record.agent,
                record.event_time,
                record.source_created,
                record.source_updated,
                record.input_tokens,
                record.output_tokens,
                record.reasoning_tokens,
                record.cache_read_tokens,
                record.cache_write_tokens,
                record.total_with_cache,
                record.total_without_cache_read,
                record.cost,
                old["first_seen"] if old else timestamp,
                timestamp,
            ),
        )

        deltas = {
            field: safe_int(record.values()[field]) - (safe_int(old[field]) if old else 0) for field in TOKEN_FIELDS
        }
        with_delta = (
            deltas["input_tokens"]
            + deltas["output_tokens"]
            + deltas["reasoning_tokens"]
            + deltas["cache_read_tokens"]
            + deltas["cache_write_tokens"]
        )
        without_read_delta = (
            deltas["input_tokens"]
            + deltas["output_tokens"]
            + deltas["reasoning_tokens"]
            + deltas["cache_write_tokens"]
        )
        cost_delta = record.cost - (safe_float(old["cost"]) if old else 0.0)
        if any(deltas.values()) or abs(cost_delta) > 1e-12:
            self.conn.execute(
                """
                INSERT INTO usage_ledger(
                    detected_at,message_id,source,session_id,project_id,project_name,project_path,
                    provider_id,model_id,variant,agent,event_time,input_delta,output_delta,
                    reasoning_delta,cache_read_delta,cache_write_delta,total_with_cache_delta,
                    total_without_cache_read_delta,cost_delta
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    timestamp,
                    record.message_id,
                    record.source,
                    record.session_id,
                    record.project_id,
                    record.project_name,
                    record.project_path,
                    record.provider_id,
                    record.model_id,
                    record.variant,
                    record.agent,
                    record.event_time,
                    deltas["input_tokens"],
                    deltas["output_tokens"],
                    deltas["reasoning_tokens"],
                    deltas["cache_read_tokens"],
                    deltas["cache_write_tokens"],
                    with_delta,
                    without_read_delta,
                    cost_delta,
                ),
            )
        return True

    def summary(self, start_ms: int, end_ms: int | None = None) -> dict[str, Any]:
        params: list[Any] = [start_ms]
        where = "event_time >= ?"
        if end_ms is not None:
            where += " AND event_time < ?"
            params.append(end_ms)
        row = self.conn.execute(
            f"""
            SELECT COUNT(*) AS requests,
                   COALESCE(SUM(input_tokens),0) AS input_tokens,
                   COALESCE(SUM(output_tokens),0) AS output_tokens,
                   COALESCE(SUM(reasoning_tokens),0) AS reasoning_tokens,
                   COALESCE(SUM(cache_read_tokens),0) AS cache_read_tokens,
                   COALESCE(SUM(cache_write_tokens),0) AS cache_write_tokens,
                   COALESCE(SUM(total_with_cache),0) AS total_with_cache,
                   COALESCE(SUM(total_without_cache_read),0) AS total_without_cache_read,
                   COALESCE(SUM(cost),0) AS cost
            FROM usage_events WHERE {where}
            """,
            params,
        ).fetchone()
        return dict(row)

    def filter_options(self) -> dict[str, list[Any]]:
        providers = [
            str(row[0])
            for row in self.conn.execute("SELECT DISTINCT provider_id FROM usage_events ORDER BY provider_id COLLATE NOCASE")
            if str(row[0] or "")
        ]
        models: list[dict[str, str]] = []
        for row in self.conn.execute(
            "SELECT DISTINCT provider_id,model_id,variant FROM usage_events ORDER BY provider_id,model_id,variant"
        ):
            provider, model, variant = (str(row[0] or ""), str(row[1] or ""), str(row[2] or ""))
            label = f"{provider}/{model}" + (f" · {variant}" if variant and variant != "default" else "")
            models.append({"provider": provider, "model": model, "variant": variant, "label": label})
        return {"providers": providers, "models": models}

    @staticmethod
    def _analytics_where(
        start_ms: int,
        end_ms: int,
        provider_id: str | None = None,
        model_key: tuple[str, str, str] | None = None,
        project_name: str | None = None,
    ) -> tuple[str, list[Any]]:
        clauses = ["event_time >= ?", "event_time < ?"]
        params: list[Any] = [int(start_ms), int(end_ms)]
        if provider_id:
            clauses.append("provider_id = ?")
            params.append(provider_id)
        if model_key:
            clauses.extend(("provider_id = ?", "model_id = ?", "variant = ?"))
            params.extend(model_key)
        if project_name:
            clauses.append("project_name = ?")
            params.append(project_name)
        return " AND ".join(clauses), params

    @staticmethod
    def _fill_time_buckets(rows: list[dict[str, Any]], start_ms: int, end_ms: int, granularity: str) -> list[dict[str, Any]]:
        by_bucket = {str(row["bucket"]): row for row in rows}
        cursor_dt = datetime.fromtimestamp(start_ms / 1000).astimezone()
        if granularity == "hour":
            cursor_dt = cursor_dt.replace(minute=0, second=0, microsecond=0)
            step = timedelta(hours=1)
        else:
            cursor_dt = cursor_dt.replace(hour=0, minute=0, second=0, microsecond=0)
            step = timedelta(days=1)
        result: list[dict[str, Any]] = []
        while int(cursor_dt.timestamp() * 1000) < end_ms:
            bucket = cursor_dt.strftime("%Y-%m-%d %H:00" if granularity == "hour" else "%Y-%m-%d")
            row = by_bucket.get(bucket)
            if row is None:
                row = {
                    "bucket": bucket,
                    "requests": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "reasoning_tokens": 0,
                    "cache_read_tokens": 0,
                    "cache_write_tokens": 0,
                    "total_with_cache": 0,
                    "total_without_cache_read": 0,
                    "cost": 0.0,
                }
            result.append(row)
            cursor_dt += step
        return result

    def analytics(
        self,
        start_ms: int,
        end_ms: int,
        *,
        provider_id: str | None = None,
        model_key: tuple[str, str, str] | None = None,
        project_name: str | None = None,
        granularity: str = "day",
        event_limit: int = 1000,
    ) -> dict[str, Any]:
        where, params = self._analytics_where(start_ms, end_ms, provider_id, model_key, project_name)
        if start_ms <= 0:
            min_row = self.conn.execute(f"SELECT MIN(event_time) FROM usage_events WHERE {where}", params).fetchone()
            if min_row and min_row[0] is not None:
                start_ms = int(datetime.fromtimestamp(safe_int(min_row[0]) / 1000).astimezone().replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
                where, params = self._analytics_where(start_ms, end_ms, provider_id, model_key, project_name)
            else:
                start_ms = int(datetime.fromtimestamp(end_ms / 1000).astimezone().replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
                where, params = self._analytics_where(start_ms, end_ms, provider_id, model_key, project_name)

        aggregate = """
            COUNT(*) AS requests,
            COALESCE(SUM(input_tokens),0) AS input_tokens,
            COALESCE(SUM(output_tokens),0) AS output_tokens,
            COALESCE(SUM(reasoning_tokens),0) AS reasoning_tokens,
            COALESCE(SUM(cache_read_tokens),0) AS cache_read_tokens,
            COALESCE(SUM(cache_write_tokens),0) AS cache_write_tokens,
            COALESCE(SUM(total_with_cache),0) AS total_with_cache,
            COALESCE(SUM(total_without_cache_read),0) AS total_without_cache_read,
            COALESCE(SUM(cost),0) AS cost
        """
        summary = dict(self.conn.execute(f"SELECT {aggregate} FROM usage_events WHERE {where}", params).fetchone())
        bucket_expression = (
            "strftime('%Y-%m-%d %H:00',event_time / 1000, 'unixepoch', 'localtime')"
            if granularity == "hour"
            else "date(event_time / 1000, 'unixepoch', 'localtime')"
        )
        trend = [
            dict(row)
            for row in self.conn.execute(
                f"SELECT {bucket_expression} AS bucket,{aggregate} FROM usage_events WHERE {where} GROUP BY bucket ORDER BY bucket",
                params,
            ).fetchall()
        ]
        trend = self._fill_time_buckets(trend, start_ms, end_ms, granularity)

        providers = [
            dict(row)
            for row in self.conn.execute(
                f"""
                SELECT provider_id AS name,{aggregate}
                FROM usage_events WHERE {where}
                GROUP BY provider_id ORDER BY total_with_cache DESC
                """,
                params,
            ).fetchall()
        ]
        models = [
            dict(row)
            for row in self.conn.execute(
                f"""
                SELECT provider_id,model_id,variant,{aggregate}
                FROM usage_events WHERE {where}
                GROUP BY provider_id,model_id,variant
                ORDER BY total_with_cache DESC
                """,
                params,
            ).fetchall()
        ]
        for row in models:
            row["name"] = f"{row['provider_id']}/{row['model_id']}" + (
                f" · {row['variant']}" if row["variant"] and row["variant"] != "default" else ""
            )
        projects = [
            dict(row)
            for row in self.conn.execute(
                f"""
                SELECT project_id,COALESCE(NULLIF(project_name,''),project_id) AS name,{aggregate}
                FROM usage_events WHERE {where}
                GROUP BY project_id,project_name ORDER BY total_with_cache DESC
                """,
                params,
            ).fetchall()
        ]
        total_tokens = safe_int(summary["total_with_cache"])
        for group in (providers, models, projects):
            for row in group:
                row["share"] = (safe_int(row["total_with_cache"]) / total_tokens * 100) if total_tokens else 0.0
        events: list[dict[str, Any]] = []
        if int(event_limit) > 0:
            events = [
                dict(row)
                for row in self.conn.execute(
                    f"""
                    SELECT event_time,message_id,provider_id,model_id,variant,project_id,project_name,agent,
                           input_tokens,output_tokens,reasoning_tokens,cache_read_tokens,cache_write_tokens,
                           total_with_cache,total_without_cache_read,cost
                    FROM usage_events WHERE {where}
                    ORDER BY event_time DESC LIMIT ?
                    """,
                    (*params, max(1, int(event_limit))),
                ).fetchall()
            ]
        return {
            "start_ms": start_ms,
            "end_ms": end_ms,
            "granularity": granularity,
            "summary": summary,
            "trend": trend,
            "providers": providers,
            "models": models,
            "projects": projects,
            "events": events,
        }

    def costing_rows(
        self,
        start_ms: int,
        end_ms: int,
        provider_id: str | None = None,
        model_key: tuple[str, str, str] | None = None,
        project_name: str | None = None,
    ) -> list[dict[str, Any]]:
        where, params = self._analytics_where(start_ms, end_ms, provider_id, model_key, project_name)
        return [
            dict(row)
            for row in self.conn.execute(
                f"""
                SELECT event_time,message_id,provider_id,model_id,variant,project_id,project_name,
                       input_tokens,output_tokens,reasoning_tokens,cache_read_tokens,cache_write_tokens,
                       total_with_cache,cost
                FROM usage_events WHERE {where}
                ORDER BY event_time
                """,
                params,
            ).fetchall()
        ]

    def top_groups(self, column: str, start_ms: int, limit: int = 5) -> list[dict[str, Any]]:
        if column not in {"provider_id", "model_id", "project_id", "project_name"}:
            raise ValueError(column)
        rows = self.conn.execute(
            f"""
            SELECT {column} AS name, COUNT(*) AS requests,
                   SUM(total_with_cache) AS total_with_cache,
                   SUM(total_without_cache_read) AS total_without_cache_read,
                   SUM(cost) AS cost
            FROM usage_events WHERE event_time >= ?
            GROUP BY {column} ORDER BY total_with_cache DESC LIMIT ?
            """,
            (start_ms, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def recent_alerts(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT created_at,kind,amount,threshold,message FROM alerts ORDER BY id DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
        return [dict(row) for row in rows]

    def daily_rows(self, days: int) -> list[dict[str, Any]]:
        start = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        start -= timedelta(days=max(0, days - 1))
        rows = self.conn.execute(
            """
            SELECT date(event_time / 1000, 'unixepoch', 'localtime') AS day,
                   COUNT(*) AS requests,
                   SUM(total_with_cache) AS total_with_cache,
                   SUM(total_without_cache_read) AS total_without_cache_read,
                   SUM(cost) AS cost
            FROM usage_events WHERE event_time >= ?
            GROUP BY day ORDER BY day DESC
            """,
            (int(start.timestamp() * 1000),),
        ).fetchall()
        return [dict(row) for row in rows]

    def close(self) -> None:
        self.conn.close()


class Monitor:
    def __init__(self, config: dict[str, Any], data_dir: Path, logger: logging.Logger):
        self.config = config
        self.data_dir = data_dir
        self.logger = logger
        self.store = Store(data_dir)
        self.csv_dir = self.store.csv_dir
        self.source_db = detect_opencode_db(config)
        self.config["opencode_db"] = str(self.source_db)

    def _read_source_rows(self, full: bool) -> tuple[list[UsageRecord], dict[str, int]]:
        records: list[UsageRecord] = []
        watermarks: dict[str, int] = {}
        overlap_ms = 10 * 60 * 1000
        uri = f"file:{self.source_db.as_posix()}?mode=ro"
        source = sqlite3.connect(uri, uri=True, timeout=5)
        source.row_factory = sqlite3.Row
        try:
            source.execute("PRAGMA query_only=ON")
            tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "session_message" in tables:
                old = safe_int(self.store.get_meta("watermark_v2", "0"))
                threshold = 0 if full else max(0, old - overlap_ms)
                sql = """
                    SELECT sm.id,sm.session_id,sm.time_created,sm.time_updated,sm.data,
                           sm.type,s.project_id,s.directory,
                           COALESCE(p.worktree,'') AS worktree,COALESCE(p.name,'') AS project_name
                    FROM session_message sm
                    LEFT JOIN session_v2 s ON s.id=sm.session_id
                    LEFT JOIN project p ON p.id=s.project_id
                    WHERE sm.type='assistant' AND sm.time_updated >= ?
                """
                rows = source.execute(sql, (threshold,)).fetchall()
                if rows:
                    watermarks["v2"] = max(safe_int(row["time_updated"]) for row in rows)
                for row in rows:
                    record = self._parse_v2(row)
                    if record:
                        records.append(record)
            if "message" in tables:
                old = safe_int(self.store.get_meta("watermark_v1", "0"))
                threshold = 0 if full else max(0, old - overlap_ms)
                sql = """
                    SELECT m.id,m.session_id,m.time_created,m.time_updated,m.data,
                           s.project_id,s.directory,
                           COALESCE(p.worktree,'') AS worktree,COALESCE(p.name,'') AS project_name
                    FROM message m
                    LEFT JOIN session s ON s.id=m.session_id
                    LEFT JOIN project p ON p.id=s.project_id
                    WHERE m.time_updated >= ?
                """
                rows = source.execute(sql, (threshold,)).fetchall()
                assistant_rows = []
                for row in rows:
                    try:
                        data = json.loads(row["data"])
                    except json.JSONDecodeError:
                        continue
                    if data.get("role") == "assistant":
                        assistant_rows.append(row)
                if assistant_rows:
                    watermarks["v1"] = max(safe_int(row["time_updated"]) for row in assistant_rows)
                for row in assistant_rows:
                    record = self._parse_v1(row)
                    if record:
                        records.append(record)
            if "session_message" not in tables and "message" not in tables:
                raise RuntimeError("OpenCode database has neither session_message nor message table")
        finally:
            source.close()
        return records, watermarks

    def _base_fields(self, row: sqlite3.Row, data: dict[str, Any]) -> dict[str, str]:
        project_id = str(row["project_id"] or "global")
        project_path = str(row["worktree"] or row["directory"] or "")
        project_name = str(row["project_name"] or "")
        if not project_name:
            project_name = Path(project_path).name if project_path else project_id
        return {
            "session_id": str(row["session_id"] or ""),
            "project_id": project_id,
            "project_name": project_name,
            "project_path": project_path,
        }

    def _tokens(self, data: dict[str, Any]) -> dict[str, int]:
        tokens = data.get("tokens") or {}
        cache = tokens.get("cache") or {}
        return {
            "input_tokens": safe_int(tokens.get("input")),
            "output_tokens": safe_int(tokens.get("output")),
            "reasoning_tokens": safe_int(tokens.get("reasoning")),
            "cache_read_tokens": safe_int(cache.get("read")),
            "cache_write_tokens": safe_int(cache.get("write")),
        }

    def _event_time(self, data: dict[str, Any], row: sqlite3.Row) -> int:
        timing = data.get("time") or {}
        return safe_int(timing.get("completed") or timing.get("streamed") or row["time_updated"] or row["time_created"])

    def _parse_v2(self, row: sqlite3.Row) -> UsageRecord | None:
        try:
            data = json.loads(row["data"])
        except json.JSONDecodeError:
            return None
        model = data.get("model") or {}
        tokens = self._tokens(data)
        if not any(tokens.values()):
            return None
        base = self._base_fields(row, data)
        event_time = self._event_time(data, row)
        if event_time <= 0:
            return None
        return UsageRecord(
            message_id=str(row["id"]),
            source="v2",
            source_rank=2,
            provider_id=str(model.get("providerID") or "unknown"),
            model_id=str(model.get("id") or model.get("modelID") or "unknown"),
            variant=str(model.get("variant") or "default"),
            agent=str(data.get("agent") or ""),
            event_time=event_time,
            source_created=safe_int(row["time_created"]),
            source_updated=safe_int(row["time_updated"]),
            cost=safe_float(data.get("cost")),
            **base,
            **tokens,
        )

    def _parse_v1(self, row: sqlite3.Row) -> UsageRecord | None:
        data = json.loads(row["data"])
        tokens = self._tokens(data)
        if not any(tokens.values()):
            return None
        base = self._base_fields(row, data)
        event_time = self._event_time(data, row)
        if event_time <= 0:
            return None
        model = data.get("model") or {}
        return UsageRecord(
            message_id=str(row["id"]),
            source="v1",
            source_rank=1,
            provider_id=str(data.get("providerID") or model.get("providerID") or "unknown"),
            model_id=str(data.get("modelID") or model.get("id") or "unknown"),
            variant=str(data.get("variant") or model.get("variant") or "default"),
            agent=str(data.get("agent") or ""),
            event_time=event_time,
            source_created=safe_int(row["time_created"]),
            source_updated=safe_int(row["time_updated"]),
            cost=safe_float(data.get("cost")),
            **base,
            **tokens,
        )

    def _initial_import_done(self) -> bool:
        return self.store.get_meta("initialized") == "1"

    def _evaluate_alerts(self) -> list[dict[str, Any]]:
        if not self._initial_import_done():
            return []
        timestamp = now_ms()
        day, start, end = local_day_bounds(timestamp)
        today = self.store.summary(start, end)
        daily_value = safe_int(today["total_with_cache"])
        daily_step = max(1, safe_int(self.config["daily_alert_tokens"]))
        highest_daily = (daily_value // daily_step) * daily_step
        state_key = f"daily_alert:{day}"
        previous = safe_int(self.store.get_meta(state_key, "0"))
        alerts: list[dict[str, Any]] = []
        if highest_daily > previous and highest_daily > 0:
            message = f"今日 OpenCode Token 已达到 {format_int(highest_daily)}（含缓存读取）。"
            self.store.set_meta(state_key, str(highest_daily))
            alerts.append({"kind": "daily", "key": state_key, "amount": daily_value, "threshold": highest_daily, "message": message})

        window_minutes = max(1, safe_int(self.config["spike_window_minutes"]))
        spike_threshold = max(1, safe_int(self.config["spike_alert_tokens"]))
        spike_start = timestamp - window_minutes * 60 * 1000
        spike = self.store.summary(spike_start, timestamp + 60 * 1000)
        spike_value = safe_int(spike["total_with_cache"])
        active = self.store.get_meta("spike_active", "0") == "1"
        if spike_value >= spike_threshold and not active:
            message = f"最近 {window_minutes} 分钟 OpenCode Token 突增 {format_int(spike_value)}（含缓存读取）。"
            self.store.set_meta("spike_active", "1")
            alerts.append({"kind": "spike", "key": "spike_active", "amount": spike_value, "threshold": spike_threshold, "message": message})
        elif spike_value < spike_threshold * 0.8 and active:
            self.store.set_meta("spike_active", "0")

        for alert in alerts:
            self.store.conn.execute(
                "INSERT INTO alerts(created_at,kind,alert_key,amount,threshold,message) VALUES(?,?,?,?,?,?)",
                (timestamp, alert["kind"], alert["key"], alert["amount"], alert["threshold"], alert["message"]),
            )
        return alerts

    def sync(self) -> SyncResult:
        mutex = acquire_named_mutex(SYNC_MUTEX_NAME)
        if mutex is None:
            raise RuntimeError("Timed out waiting for the monitor data writer lock")
        try:
            return self._sync_unlocked()
        finally:
            release_named_mutex(mutex)

    def _sync_unlocked(self) -> SyncResult:
        full = False
        last_full = safe_int(self.store.get_meta("last_full_reconcile", "0"))
        interval_ms = max(1, safe_int(self.config["full_reconcile_hours"])) * 60 * 60 * 1000
        if not last_full or now_ms() - last_full >= interval_ms:
            full = True
        records, watermarks = self._read_source_rows(full)
        result = SyncResult(records_seen=len(records), full_reconcile=full)
        self.store.conn.execute("BEGIN")
        try:
            for record in records:
                if self.store.upsert_event(record):
                    result.records_changed += 1
            for source, watermark in watermarks.items():
                self.store.set_meta(f"watermark_{source}", str(watermark))
            if full:
                self.store.set_meta("last_full_reconcile", str(now_ms()))
            if not self._initial_import_done():
                self.store.set_meta("initialized", "1")
            result.alerts = self._evaluate_alerts()
            self.store.set_meta("last_sync", str(now_ms()))
            self.store.set_meta("last_error", "")
            ledger_before = safe_int(self.store.get_meta("ledger_row_count", "0"))
            ledger_after = safe_int(self.store.conn.execute("SELECT COUNT(*) FROM usage_ledger").fetchone()[0])
            result.ledger_rows = max(0, ledger_after - ledger_before)
            self.store.set_meta("ledger_row_count", str(ledger_after))
            self.store.conn.commit()
        except Exception:
            self.store.conn.rollback()
            raise
        self.export_incremental_csv()
        self.export_summary_csv_files()
        return result

    def mark_error(self, error: Exception) -> None:
        message = f"{type(error).__name__}: {error}"
        self.logger.exception("Monitor sync failed")
        self.store.conn.execute("BEGIN")
        try:
            self.store.set_meta("last_error", message[:1000])
            self.store.set_meta("last_error_at", str(now_ms()))
            self.store.conn.commit()
        except Exception:
            self.store.conn.rollback()

    def current_status(self) -> dict[str, Any]:
        timestamp = now_ms()
        day, day_start, day_end = local_day_bounds(timestamp)
        week_start = int((datetime.now().astimezone() - timedelta(days=7)).timestamp() * 1000)
        month_start = int((datetime.now().astimezone() - timedelta(days=30)).timestamp() * 1000)
        spike_start = timestamp - max(1, safe_int(self.config["spike_window_minutes"])) * 60 * 1000
        return {
            "timestamp": timestamp,
            "opencode_running": opencode_is_running(),
            "day": day,
            "today": self.store.summary(day_start, day_end),
            "seven_days": self.store.summary(week_start, timestamp + 1),
            "thirty_days": self.store.summary(month_start, timestamp + 1),
            "spike_window": self.store.summary(spike_start, timestamp + 1),
            "providers": self.store.top_groups("provider_id", week_start, 8),
            "models": self.store.top_groups("model_id", week_start, 8),
            "projects": self.store.top_groups("project_name", week_start, 8),
            "alerts": self.store.recent_alerts(8),
            "last_sync": safe_int(self.store.get_meta("last_sync", "0")),
            "last_error": self.store.get_meta("last_error", ""),
        }

    def _ledger_rows_after(self, cursor: int) -> list[sqlite3.Row]:
        return self.store.conn.execute("SELECT * FROM usage_ledger WHERE id>? ORDER BY id", (cursor,)).fetchall()

    def export_incremental_csv(self) -> None:
        path = self.csv_dir / "usage_ledger.csv"
        cursor = safe_int(self.store.get_meta("csv_ledger_cursor", "0"))
        rows = self._ledger_rows_after(cursor)
        if not rows and path.exists():
            return
        exists = path.exists() and path.stat().st_size > 0
        with path.open("a", newline="", encoding="utf-8-sig" if not exists else "utf-8") as handle:
            writer = csv.writer(handle)
            if not exists:
                writer.writerow(LEDGER_CSV_HEADER)
            for row in rows:
                day, _, _ = local_day_bounds(safe_int(row["event_time"]))
                writer.writerow(
                    [
                        datetime.fromtimestamp(safe_int(row["detected_at"]) / 1000).astimezone().isoformat(),
                        datetime.fromtimestamp(safe_int(row["event_time"]) / 1000).astimezone().isoformat(),
                        day,
                        row["provider_id"],
                        row["model_id"],
                        row["variant"],
                        row["project_id"],
                        row["project_name"],
                        row["project_path"],
                        row["session_id"],
                        row["agent"],
                        row["source"],
                        row["input_delta"],
                        row["output_delta"],
                        row["reasoning_delta"],
                        row["cache_read_delta"],
                        row["cache_write_delta"],
                        row["total_with_cache_delta"],
                        row["total_without_cache_read_delta"],
                        row["cost_delta"],
                    ]
                )
        if rows:
            self.store.set_meta("csv_ledger_cursor", str(safe_int(rows[-1]["id"])))
            self.store.conn.commit()

    def _write_query_csv(self, filename: str, header: list[str], query: str) -> None:
        path = self.csv_dir / filename
        temp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        rows = self.store.conn.execute(query).fetchall()
        with temp.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow(header)
            for row in rows:
                writer.writerow([row[key] for key in header])
        os.replace(temp, path)

    def export_summary_csv_files(self) -> None:
        aggregate = "COUNT(*) AS requests, SUM(total_with_cache) AS total_with_cache, SUM(total_without_cache_read) AS total_without_cache_read, SUM(cost) AS cost"
        self._write_query_csv(
            "daily_summary.csv",
            ["day", "requests", "total_with_cache", "total_without_cache_read", "cost"],
            f"SELECT date(event_time / 1000, 'unixepoch', 'localtime') AS day,{aggregate} FROM usage_events GROUP BY day ORDER BY day",
        )
        self._write_query_csv(
            "provider_summary.csv",
            ["day", "provider_id", "requests", "total_with_cache", "total_without_cache_read", "cost"],
            f"SELECT date(event_time / 1000, 'unixepoch', 'localtime') AS day,provider_id,{aggregate} FROM usage_events GROUP BY day,provider_id ORDER BY day,total_with_cache DESC",
        )
        self._write_query_csv(
            "model_summary.csv",
            ["day", "provider_id", "model_id", "variant", "requests", "total_with_cache", "total_without_cache_read", "cost"],
            f"SELECT date(event_time / 1000, 'unixepoch', 'localtime') AS day,provider_id,model_id,variant,{aggregate} FROM usage_events GROUP BY day,provider_id,model_id,variant ORDER BY day,total_with_cache DESC",
        )
        self._write_query_csv(
            "project_summary.csv",
            ["day", "project_id", "project_name", "project_path", "requests", "total_with_cache", "total_without_cache_read", "cost"],
            f"SELECT date(event_time / 1000, 'unixepoch', 'localtime') AS day,project_id,project_name,project_path,{aggregate} FROM usage_events GROUP BY day,project_id,project_name,project_path ORDER BY day,total_with_cache DESC",
        )

    def export_full_csv(self) -> None:
        mutex = acquire_named_mutex(SYNC_MUTEX_NAME)
        if mutex is None:
            raise RuntimeError("Timed out waiting for the CSV export lock")
        try:
            self._export_full_csv_unlocked()
        finally:
            release_named_mutex(mutex)

    def _export_full_csv_unlocked(self) -> None:
        event_header = [
            "event_time",
            "day",
            "provider_id",
            "model_id",
            "variant",
            "project_id",
            "project_name",
            "project_path",
            "session_id",
            "agent",
            "source",
            "input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "total_with_cache",
            "total_without_cache_read",
            "cost",
        ]
        self._write_query_csv(
            "usage_events.csv",
            event_header,
            "SELECT event_time,date(event_time / 1000, 'unixepoch', 'localtime') AS day,provider_id,model_id,variant,project_id,project_name,project_path,session_id,agent,source,input_tokens,output_tokens,reasoning_tokens,cache_read_tokens,cache_write_tokens,total_with_cache,total_without_cache_read,cost FROM usage_events ORDER BY event_time,message_id",
        )
        # Rebuild the append-only ledger too, making `export` a complete recovery path.
        path = self.csv_dir / "usage_ledger.csv"
        temp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        with temp.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow(LEDGER_CSV_HEADER)
            for row in self.store.conn.execute("SELECT * FROM usage_ledger ORDER BY id"):
                day, _, _ = local_day_bounds(safe_int(row["event_time"]))
                writer.writerow(
                    [
                        datetime.fromtimestamp(safe_int(row["detected_at"]) / 1000).astimezone().isoformat(),
                        datetime.fromtimestamp(safe_int(row["event_time"]) / 1000).astimezone().isoformat(),
                        day,
                        row["provider_id"],
                        row["model_id"],
                        row["variant"],
                        row["project_id"],
                        row["project_name"],
                        row["project_path"],
                        row["session_id"],
                        row["agent"],
                        row["source"],
                        row["input_delta"],
                        row["output_delta"],
                        row["reasoning_delta"],
                        row["cache_read_delta"],
                        row["cache_write_delta"],
                        row["total_with_cache_delta"],
                        row["total_without_cache_read_delta"],
                        row["cost_delta"],
                    ]
                )
        os.replace(temp, path)
        last = self.store.conn.execute("SELECT COALESCE(MAX(id),0) FROM usage_ledger").fetchone()[0]
        self.store.set_meta("csv_ledger_cursor", str(safe_int(last)))
        self.store.conn.commit()
        self.export_summary_csv_files()

    def close(self) -> None:
        self.store.close()


def interpolate_color(value: float, maximum: float) -> tuple[int, int, int, int]:
    if value < 0:
        value = 0
    ratio = 1.0 if maximum <= 0 else min(1.0, value / maximum)
    if ratio <= 0.1:
        local = ratio / 0.1
        start, end = (34, 178, 95, 255), (234, 179, 8, 255)
    elif ratio <= 0.5:
        local = (ratio - 0.1) / 0.4
        start, end = (234, 179, 8, 255), (249, 115, 22, 255)
    else:
        local = (ratio - 0.5) / 0.5
        start, end = (249, 115, 22, 255), (220, 38, 38, 255)
    return tuple(int(start[i] + (end[i] - start[i]) * local) for i in range(4))


def make_icon(value: float, maximum: float, error: bool = False) -> Image.Image:
    color = (220, 38, 38, 255) if error else interpolate_color(value, maximum)
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((4, 4, 60, 60), radius=14, fill=color)
    draw.ellipse((12, 12, 52, 52), outline=(255, 255, 255, 235), width=3)
    draw.text((20, 18), "!" if error else "OC", fill=(255, 255, 255, 255), stroke_width=1)
    return image


def make_ui_mark(size: int = 64) -> Image.Image:
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((3, 3, size - 4, size - 4), radius=max(10, size // 5), fill=(37, 99, 235, 255))
    draw.ellipse((size // 6, size // 6, size - size // 6, size - size // 6), outline="white", width=max(2, size // 18))
    draw.line((size * 0.32, size * 0.53, size * 0.46, size * 0.67, size * 0.70, size * 0.38), fill="white", width=max(3, size // 14), joint="curve")
    return image


def make_idle_icon() -> Image.Image:
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((4, 4, 60, 60), radius=14, fill=(100, 116, 139, 255))
    draw.rounded_rectangle((20, 22, 27, 42), radius=3, fill=(255, 255, 255, 235))
    draw.rounded_rectangle((37, 22, 44, 42), radius=3, fill=(255, 255, 255, 235))
    return image


class Notifier:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.icon: Any = None
        try:
            import winotify  # type: ignore

            self._winotify = winotify
        except Exception:
            self._winotify = None

    def notify(self, title: str, message: str, sound: bool = True) -> None:
        if self.config.get("notifications", True):
            delivered = False
            if self._winotify is not None:
                try:
                    notification = self._winotify.Notification(app_id=APP_ID, title=title, msg=message)
                    notification.show()
                    delivered = True
                except Exception:
                    delivered = False
            if not delivered and self.icon is not None:
                try:
                    self.icon.notify(message, title)
                except Exception:
                    pass
        if sound and self.config.get("sound", True):
            try:
                winsound.PlaySound(
                    "SystemExclamation",
                    winsound.SND_ALIAS | winsound.SND_ASYNC | winsound.SND_NODEFAULT,
                )
            except RuntimeError:
                try:
                    winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
                except RuntimeError:
                    pass


def run_startup_command(command: str) -> None:
    if os.name != "nt":
        raise RuntimeError("Automatic startup is currently implemented for Windows only")
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, APP_ID, 0, winreg.REG_SZ, command)


def remove_startup_command() -> None:
    if os.name != "nt":
        return
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, APP_ID)
    except FileNotFoundError:
        pass


def startup_enabled() -> bool:
    if os.name != "nt":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
            value, _ = winreg.QueryValueEx(key, APP_ID)
            return APP_ID in str(executable_path()).lower() or "opencode token monitor" in str(value).lower()
    except OSError:
        return False


def install_startup(config_path: Path) -> None:
    arguments = app_command("tray", "--config", str(config_path))
    command = subprocess.list2cmdline(arguments)
    run_startup_command(command)


def acquire_named_mutex(name: str, timeout_ms: int = 15_000) -> int | None:
    if os.name != "nt":
        return 0
    ctypes.windll.kernel32.CreateMutexW.restype = wintypes.HANDLE
    handle = ctypes.windll.kernel32.CreateMutexW(None, False, name)
    wait_result = ctypes.windll.kernel32.WaitForSingleObject(handle, timeout_ms)
    if wait_result not in {0, 0x00000080}:  # acquired or abandoned
        ctypes.windll.kernel32.CloseHandle(handle)
        return None
    return int(handle)


def release_named_mutex(handle: int | None) -> None:
    if os.name == "nt" and handle is not None:
        ctypes.windll.kernel32.ReleaseMutex(handle)
        ctypes.windll.kernel32.CloseHandle(handle)


def acquire_single_instance() -> int | None:
    if os.name != "nt":
        return None
    handle = ctypes.windll.kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        ctypes.windll.kernel32.CloseHandle(handle)
        return None
    return int(handle)


def setup_logging(data_dir: Path) -> logging.Logger:
    data_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("opencode-token-monitor")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = RotatingFileHandler(data_dir / "monitor.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    return logger


def status_text(status: dict[str, Any], maximum: float) -> str:
    """Return a Windows tray-tooltip title that fits pystray's 128-char limit."""
    today = status["today"]
    running = bool(status.get("opencode_running"))
    last_sync = (
        datetime.fromtimestamp(status["last_sync"] / 1000).astimezone().isoformat(timespec="seconds")
        if status["last_sync"]
        else "never"
    )
    title = "\n".join(
        (
            f"{APP_NAME} {VERSION}",
            f"OpenCode: {'running' if running else 'stopped'}",
            f"Today: {format_int(today['total_with_cache'])} tokens",
            f"Cache read: {format_int(today['cache_read_tokens'])}",
            f"Synced: {last_sync}",
        )
    )
    # pystray/Windows rejects a shell tooltip longer than 127 characters
    # (128 including the terminating NUL). Keep the useful first lines and
    # make the hard limit explicit so localized or unusually large values
    # cannot take down the tray worker again.
    return title[:127]


def show_existing_dashboard() -> bool:
    """Restore and focus an already-running Dashboard window, if present."""
    if os.name != "nt":
        return False
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW(None, f"{APP_NAME} {VERSION}")
        if not hwnd:
            return False
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE also reveals a hidden window.
        user32.SetForegroundWindow(hwnd)
        return True
    except (AttributeError, OSError):
        return False


def tray_instance_exists() -> bool:
    """Check the tray singleton without taking ownership of its mutex."""
    if os.name != "nt":
        return False
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
        if not handle:
            return False
        try:
            return kernel32.GetLastError() == 183  # ERROR_ALREADY_EXISTS
        finally:
            kernel32.CloseHandle(handle)
    except (AttributeError, OSError):
        return False


def ensure_tray_running(config_path: Path) -> None:
    """Start the tray once when Dashboard was launched without the tray."""
    if tray_instance_exists():
        return
    try:
        subprocess.Popen(
            app_command("tray", "--config", str(config_path)),
            close_fds=True,
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
    except OSError:
        # The Dashboard can still be hidden if tray startup is unavailable;
        # the user can always launch the tray executable again.
        pass


def open_dashboard(config_path: Path) -> None:
    if show_existing_dashboard():
        return
    command = app_command("dashboard", "--config", str(config_path))
    subprocess.Popen(
        command,
        close_fds=True,
        creationflags=getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )


def run_tray(config_path: Path) -> int:
    try:
        import pystray  # type: ignore
    except ImportError as exc:
        print("pystray is required to run tray mode", file=sys.stderr)
        return 2

    mutex = acquire_single_instance()
    if mutex is None:
        return 0
    config = load_config(config_path)
    data_dir = app_data_dir()
    logger = setup_logging(data_dir)
    csv_dir = data_dir / "csv"
    csv_dir.mkdir(parents=True, exist_ok=True)
    save_config(config_path, config)
    notifier = Notifier(config)
    stop_event = threading.Event()
    sync_lock = threading.Lock()
    tray_state = {"idle": False}
    maximum = max(1, safe_int(config["color_max_tokens"]))

    def read_status_in_current_thread() -> dict[str, Any]:
        # SQLite connections are intentionally thread-affine. The tray menu
        # runs on the icon thread while synchronization runs on a worker, so
        # each thread uses its own short-lived Monitor connection.
        status_monitor = Monitor(config, data_dir, logger)
        try:
            return status_monitor.current_status()
        finally:
            status_monitor.close()

    def set_idle_state(force: bool = False) -> None:
        if tray_state["idle"] and not force:
            return
        status = read_status_in_current_thread()
        icon.icon = make_idle_icon()
        icon.title = status_text(status, maximum)
        tray_state["idle"] = True
        icon.update_menu()

    def sync_once(initial: bool = False) -> None:
        # This guard is intentionally repeated here (the worker also checks),
        # so a manual refresh can never query the source database while
        # OpenCode is closed.
        if not opencode_is_running():
            set_idle_state()
            return
        if not sync_lock.acquire(blocking=False):
            return
        worker_monitor: Monitor | None = None
        try:
            worker_monitor = Monitor(config, data_dir, logger)
            previous_error = worker_monitor.store.get_meta("last_error", "")
            try:
                result = worker_monitor.sync()
                if not initial and previous_error:
                    notifier.notify(APP_NAME, "监控已恢复，Token 记录已继续。", sound=True)
                for alert in result.alerts or []:
                    notifier.notify(f"{APP_NAME} 告警", alert["message"], sound=True)
            except Exception as exc:
                worker_monitor.mark_error(exc)
                if not previous_error:
                    notifier.notify(f"{APP_NAME} 错误", str(exc)[:500], sound=True)
            status = worker_monitor.current_status()
            value = safe_int(status["today"]["total_with_cache"])
            error = bool(status["last_error"])
            icon.icon = make_icon(value, maximum, error)
            icon.title = status_text(status, maximum)
            tray_state["idle"] = False
            icon.update_menu()
        except Exception as exc:
            logger.exception("Tray synchronization worker failed")
            if notifier.icon is not None:
                notifier.notify(f"{APP_NAME} 错误", str(exc)[:500], sound=True)
        finally:
            if worker_monitor is not None:
                worker_monitor.close()
            sync_lock.release()

    def worker() -> None:
        initial = True
        next_sync_at = 0.0
        interval = max(30, safe_int(config["sample_interval_seconds"]))
        logger.info("Tray worker started (OpenCode running=%s)", opencode_is_running())
        while not stop_event.is_set():
            running = opencode_is_running()
            if not running:
                set_idle_state()
                next_sync_at = 0.0
                stop_event.wait(PROCESS_POLL_SECONDS)
                continue

            now = time.monotonic()
            if next_sync_at <= 0 or now >= next_sync_at:
                logger.info("OpenCode detected; syncing local database")
                sync_once(initial)
                initial = False
                next_sync_at = time.monotonic() + interval
                logger.info("Sync pass finished; next pass in %s seconds", interval)

            wait_seconds = PROCESS_POLL_SECONDS
            if next_sync_at > 0:
                wait_seconds = min(wait_seconds, max(0.1, next_sync_at - time.monotonic()))
            stop_event.wait(wait_seconds)

    initially_running = opencode_is_running()
    initial_icon = make_icon(0, maximum) if initially_running else make_idle_icon()
    tray_state["idle"] = not initially_running
    icon = pystray.Icon(APP_ID, initial_icon, APP_NAME, menu=pystray.Menu())
    notifier.icon = icon

    def refresh(_icon: Any = None, _item: Any = None) -> None:
        if not opencode_is_running():
            set_idle_state(force=True)
            return
        threading.Thread(target=sync_once, daemon=True).start()

    def show_status(_icon: Any = None, _item: Any = None) -> None:
        status = read_status_in_current_thread()
        today = status["today"]
        state = "运行中，监控已启用" if status["opencode_running"] else "未运行，监控处于空闲状态"
        print(f"OpenCode: {state}")
        print(f"Today: {format_int(today['total_with_cache'])} (cache included)")
        print(f"No cache read: {format_int(today['total_without_cache_read'])}")
        print(f"Open data: {data_dir}")

    def open_folder(_icon: Any = None, _item: Any = None) -> None:
        os.startfile(data_dir)  # type: ignore[attr-defined]

    def open_csv(_icon: Any = None, _item: Any = None) -> None:
        os.startfile(csv_dir)  # type: ignore[attr-defined]

    def test_alert(_icon: Any = None, _item: Any = None) -> None:
        notifier.notify(f"{APP_NAME} 测试", "系统通知、声音和托盘颜色工作正常。", sound=True)

    def toggle_startup(_icon: Any = None, _item: Any = None) -> None:
        if startup_enabled():
            remove_startup_command()
        else:
            install_startup(config_path)
        icon.update_menu()

    def quit_app(_icon: Any = None, _item: Any = None) -> None:
        stop_event.set()
        icon.stop()

    def menu() -> Any:
        status = read_status_in_current_thread()
        today = status["today"]
        spike = status["spike_window"]
        running = bool(status["opencode_running"])
        startup = "Disable auto-start" if startup_enabled() else "Enable auto-start"
        return pystray.Menu(
            pystray.MenuItem(
                "OpenCode: Running — monitoring active" if running else "OpenCode: Not running — idle",
                None,
                enabled=False,
            ),
            pystray.MenuItem(
                f"Today: {format_int(today['total_with_cache'])} tokens (cache included)",
                None,
                enabled=False,
            ),
            pystray.MenuItem(
                f"Today without cache read: {format_int(today['total_without_cache_read'])}",
                None,
                enabled=False,
            ),
            pystray.MenuItem(
                f"Last {config['spike_window_minutes']} min: {format_int(spike['total_with_cache'])}",
                None,
                enabled=False,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Open dashboard", lambda _i=None, _m=None: open_dashboard(config_path), default=True),
            pystray.MenuItem("Refresh now", refresh, enabled=running),
            pystray.MenuItem("Open data folder", open_folder),
            pystray.MenuItem("Open CSV folder", open_csv),
            pystray.MenuItem("Test alert", test_alert),
            pystray.MenuItem(startup, toggle_startup),
            pystray.MenuItem("Exit", quit_app),
        )

    icon.menu = menu()
    threading.Thread(target=worker, daemon=True).start()
    try:
        icon.run()
    finally:
        stop_event.set()
        if mutex is not None:
            ctypes.windll.kernel32.CloseHandle(mutex)
    return 0


class PricingCatalog:
    URL = "https://models.dev/api.json?type=all"

    def __init__(self, data_dir: Path, refresh_hours: int = 24):
        self.cache_path = data_dir / "models.dev.all.json"
        self.refresh_seconds = max(1, int(refresh_hours)) * 60 * 60

    def load(self) -> tuple[dict[str, Any], dict[str, Any]]:
        global PRICING_MEMORY, PRICING_MEMORY_META
        cache_key = str(self.cache_path)
        if PRICING_MEMORY and time.time() - float(PRICING_MEMORY_META.get("loaded_monotonic", 0)) < self.refresh_seconds:
            return PRICING_MEMORY, {**PRICING_MEMORY_META, "state": "memory"}
        cached: dict[str, Any] | None = None
        if self.cache_path.is_file():
            try:
                cached = json.loads(self.cache_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                cached = None
        if cached and time.time() - self.cache_path.stat().st_mtime < self.refresh_seconds:
            PRICING_MEMORY = cached
            PRICING_MEMORY_META = {"source": "models.dev", "state": "cached", "updated_at": int(self.cache_path.stat().st_mtime * 1000), "loaded_monotonic": time.monotonic()}
            return cached, {**PRICING_MEMORY_META, "state": "cached"}
        try:
            request = urllib.request.Request(self.URL, headers={"User-Agent": f"OpenCodeTokenMonitor/{VERSION}"})
            with urllib.request.urlopen(request, timeout=12) as response:
                fresh = json.load(response)
            if not isinstance(fresh, dict):
                raise RuntimeError("models.dev returned an invalid catalog")
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.cache_path.with_name(f"{self.cache_path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            temp.write_text(json.dumps(fresh, ensure_ascii=False), encoding="utf-8")
            os.replace(temp, self.cache_path)
            PRICING_MEMORY = fresh
            PRICING_MEMORY_META = {"source": "models.dev", "state": "updated", "updated_at": now_ms(), "loaded_monotonic": time.monotonic(), "cache_key": cache_key}
            return fresh, {**PRICING_MEMORY_META, "state": "updated"}
        except (OSError, ValueError, RuntimeError, urllib.error.URLError) as exc:
            if cached:
                PRICING_MEMORY = cached
                PRICING_MEMORY_META = {"source": "models.dev", "state": "offline-cache", "updated_at": int(self.cache_path.stat().st_mtime * 1000), "loaded_monotonic": time.monotonic(), "error": str(exc)[:200]}
                return cached, {**PRICING_MEMORY_META, "state": "offline-cache"}
            return {}, {"source": "models.dev", "state": "unavailable", "updated_at": 0, "error": str(exc)[:200]}

    @staticmethod
    def _canonical_provider(model_id: str) -> str | None:
        model = model_id.lower()
        if model.startswith(("gpt-", "gpt-", "o1", "o3", "o4", "codex")):
            return "openai"
        if model.startswith("claude"):
            return "anthropic"
        if model.startswith("gemini"):
            return "google"
        if model.startswith("grok"):
            return "xai"
        if model.startswith("deepseek"):
            return "deepseek"
        if model.startswith("glm"):
            return "zai"
        if model.startswith("step"):
            return "stepfun"
        if model.startswith("kimi"):
            return "moonshotai"
        if model.startswith("minimax"):
            return "minimax"
        if model.startswith("qwen"):
            return "alibaba"
        if model.startswith("mimo"):
            return "xiaomi"
        if model.startswith("sensenova"):
            return "sensenova"
        return None

    @staticmethod
    def _model_candidates(model_id: str) -> list[str]:
        model = model_id.lower()
        candidates = [model]
        aliases = {
            "deepseek-v4-flash-vision": ["deepseek-v4-flash-vision-exp"],
            "deepseek-flash": ["deepseek-v4-flash", "deepseek-v4.1-flash"],
            "deepseek-v4-flash": ["deepseek-v4-flash-latest"],
        }
        candidates.extend(aliases.get(model, []))
        if "/" in model:
            candidates.append(model.rsplit("/", 1)[-1])
        return list(dict.fromkeys(candidates))

    @classmethod
    def resolve(cls, catalog: dict[str, Any], provider_id: str, model_id: str) -> tuple[dict[str, Any] | None, str]:
        if not catalog:
            return None, "unavailable"
        exact_provider = catalog.get(provider_id) or {}
        exact_models = exact_provider.get("models") or {}
        exact = exact_models.get(model_id) or {}
        if not exact:
            folded = model_id.casefold()
            exact = next((value for key, value in exact_models.items() if str(key).casefold() == folded), {})
        exact_cost = exact.get("cost")
        if isinstance(exact_cost, dict) and ("input" in exact_cost or "output" in exact_cost):
            return exact_cost, f"models.dev:{provider_id}/{model_id}"
        canonical = cls._canonical_provider(model_id)
        if canonical:
            canonical_models = (catalog.get(canonical) or {}).get("models") or {}
            for candidate in cls._model_candidates(model_id):
                model = canonical_models.get(candidate) or {}
                if not model:
                    folded = candidate.casefold()
                    model = next((value for key, value in canonical_models.items() if str(key).casefold() == folded), {})
                cost = model.get("cost")
                if isinstance(cost, dict) and ("input" in cost or "output" in cost):
                    return cost, f"models.dev:{canonical}/{candidate}"
        return None, "unpriced"

    @staticmethod
    def event_cost(catalog: dict[str, Any], row: dict[str, Any], pricing_config: dict[str, Any] | None = None) -> dict[str, Any]:
        provider = str(row.get("provider_id") or "")
        model = str(row.get("model_id") or "")
        mode = pricing_mode(pricing_config)
        rates: dict[str, Any] | None
        source: str
        match_type = "unknown"
        if mode == "custom":
            rates, source = custom_rates_for_row(pricing_config, row)
            match_type = "custom-default" if source == "custom:default" else ("custom" if rates is not None else "unknown")
        else:
            rates, source = PricingCatalog.resolve(catalog, provider, model)
            if rates is not None:
                source_provider = source.split(":", 1)[1].split("/", 1)[0] if ":" in source else ""
                match_type = "exact" if source_provider == provider else "canonical"
        if rates is None:
            billed = safe_float(row.get("cost"))
            return {
                "billed_cost": billed,
                "estimated_cost": 0.0,
                "total_cost": billed,
                "pricing_source": source,
                "pricing_match": "unknown",
                "pricing_status": "actual" if billed > 0 else "unpriced",
                "pricing_warnings": ["pricing unavailable"],
            }
        effective = dict(rates)
        if mode != "custom":
            context_tokens = safe_int(row.get("input_tokens")) + safe_int(row.get("cache_read_tokens")) + safe_int(row.get("cache_write_tokens"))
            context_tiers = sorted(
                [tier for tier in (rates.get("tiers") or []) if (tier.get("tier") or {}).get("type") == "context"],
                key=lambda tier: safe_int((tier.get("tier") or {}).get("size")),
                reverse=True,
            )
            selected_tier = next((tier for tier in context_tiers if context_tokens > safe_int((tier.get("tier") or {}).get("size"))), None)
            if selected_tier is None and context_tokens > 200_000 and isinstance(rates.get("context_over_200k"), dict):
                selected_tier = {"tier": {"type": "context", "size": 200_000}, **rates["context_over_200k"]}
            if selected_tier:
                effective.update({key: float(value) for key, value in selected_tier.items() if key not in {"tier", "context_over_200k"}})
        input_rate = max(0.0, float(effective.get("input") or 0.0))
        output_rate = max(0.0, float(effective.get("output") or 0.0))
        reasoning_rate = max(0.0, float(effective.get("reasoning", output_rate) or 0.0))
        warnings: list[str] = []
        if "cache_read" in effective:
            cache_read_rate = max(0.0, float(effective.get("cache_read") or 0.0))
        else:
            cache_read_rate = 0.0
            if safe_int(row.get("cache_read_tokens")):
                warnings.append("cache_read rate unavailable")
        if "cache_write" in effective:
            cache_write_rate = max(0.0, float(effective.get("cache_write") or 0.0))
        else:
            cache_write_rate = 0.0
            if safe_int(row.get("cache_write_tokens")):
                warnings.append("cache_write rate unavailable")
        estimated = (
            safe_int(row.get("input_tokens")) * input_rate
            + safe_int(row.get("output_tokens")) * output_rate
            + safe_int(row.get("reasoning_tokens")) * reasoning_rate
            + safe_int(row.get("cache_read_tokens")) * cache_read_rate
            + safe_int(row.get("cache_write_tokens")) * cache_write_rate
        ) / 1_000_000
        multiplier = pricing_multiplier(pricing_config) if mode == "multiplier" else 1.0
        estimated *= multiplier
        if mode == "multiplier":
            source = f"{source} × {multiplier:g}"
            match_type = f"multiplier-{match_type}"
        billed = safe_float(row.get("cost"))
        total = billed if billed > 0 else estimated
        if billed > 0:
            status = "actual"
        elif mode == "custom":
            status = "partial-custom" if warnings else ("custom-free" if estimated == 0 else "custom-estimate")
        elif mode == "multiplier":
            status = "partial-estimate" if warnings else "multiplied-estimate"
        elif warnings:
            status = "partial-estimate"
            match_type = "partial"
        elif estimated == 0 and match_type == "exact":
            status = "free"
        else:
            status = "reference-estimate"
        return {
            "billed_cost": billed,
            "estimated_cost": estimated,
            "total_cost": total,
            "pricing_source": source,
            "pricing_match": match_type,
            "pricing_status": status,
            "pricing_warnings": warnings,
        }


def _empty_cost_bucket() -> dict[str, Any]:
    return {
        "billed_cost": 0.0,
        "estimated_cost": 0.0,
        "total_cost": 0.0,
        "exact_priced_tokens": 0,
        "custom_priced_tokens": 0,
        "estimated_priced_tokens": 0,
        "unpriced_tokens": 0,
    }


def _merge_cost(bucket: dict[str, Any], row: dict[str, Any], calculated: dict[str, Any]) -> None:
    bucket["billed_cost"] += calculated["billed_cost"]
    bucket["estimated_cost"] += calculated["estimated_cost"]
    bucket["total_cost"] += calculated["total_cost"]
    tokens = safe_int(row.get("total_with_cache"))
    if str(calculated.get("pricing_match") or "").startswith("custom"):
        bucket["custom_priced_tokens"] += tokens
    elif calculated.get("pricing_match") in {"exact", "multiplier-exact"} or calculated.get("pricing_status") == "actual":
        bucket["exact_priced_tokens"] += tokens
    elif calculated.get("pricing_match") in {"canonical", "multiplier-canonical"}:
        bucket["estimated_priced_tokens"] += tokens
    else:
        bucket["unpriced_tokens"] += tokens


def _finish_cost(bucket: dict[str, Any]) -> dict[str, Any]:
    exact = safe_int(bucket.get("exact_priced_tokens"))
    custom = safe_int(bucket.get("custom_priced_tokens"))
    estimated = safe_int(bucket.get("estimated_priced_tokens"))
    unpriced = safe_int(bucket.get("unpriced_tokens"))
    denominator = exact + custom + estimated + unpriced
    bucket["pricing_coverage"] = ((exact + custom + estimated) / denominator * 100) if denominator else 100.0
    bucket["exact_pricing_coverage"] = ((exact + custom) / denominator * 100) if denominator else 100.0
    bucket["custom_pricing_coverage"] = (custom / denominator * 100) if denominator else 0.0
    bucket["reference_pricing_coverage"] = (estimated / denominator * 100) if denominator else 0.0
    bucket["cost"] = bucket["total_cost"]
    return bucket


def attach_model_dev_costs(analytics: dict[str, Any], rows: list[dict[str, Any]], catalog: dict[str, Any], pricing_config: dict[str, Any] | None = None) -> dict[str, Any]:
    summary = _empty_cost_bucket()
    trend: dict[str, dict[str, Any]] = {str(row["bucket"]): _empty_cost_bucket() for row in analytics["trend"]}
    providers: dict[str, dict[str, Any]] = {str(row["name"]): _empty_cost_bucket() for row in analytics["providers"]}
    models: dict[tuple[str, str, str], dict[str, Any]] = {
        (str(row["provider_id"]), str(row["model_id"]), str(row["variant"])): _empty_cost_bucket() for row in analytics["models"]
    }
    projects: dict[tuple[str, str], dict[str, Any]] = {
        (str(row["project_id"]), str(row["name"])): _empty_cost_bucket() for row in analytics["projects"]
    }
    event_costs: dict[str, dict[str, Any]] = {}
    granularity = str(analytics["granularity"])
    for row in rows:
        calculated = PricingCatalog.event_cost(catalog, row, pricing_config)
        event_costs[str(row["message_id"])] = calculated
        _merge_cost(summary, row, calculated)
        event_dt = datetime.fromtimestamp(safe_int(row["event_time"]) / 1000).astimezone()
        bucket = event_dt.strftime("%Y-%m-%d %H:00" if granularity == "hour" else "%Y-%m-%d")
        if bucket in trend:
            _merge_cost(trend[bucket], row, calculated)
        provider = str(row["provider_id"] or "")
        if provider in providers:
            _merge_cost(providers[provider], row, calculated)
        model_key = (provider, str(row["model_id"] or ""), str(row["variant"] or ""))
        if model_key in models:
            _merge_cost(models[model_key], row, calculated)
        project_name = str(row["project_name"] or row["project_id"] or "")
        project_key = (str(row["project_id"] or ""), project_name)
        if project_key in projects:
            _merge_cost(projects[project_key], row, calculated)
    analytics["summary"].update(_finish_cost(summary))
    for row in analytics["trend"]:
        row.update(_finish_cost(trend.get(str(row["bucket"]), _empty_cost_bucket())))
    for row in analytics["providers"]:
        row.update(_finish_cost(providers.get(str(row["name"]), _empty_cost_bucket())))
    for row in analytics["models"]:
        key = (str(row["provider_id"]), str(row["model_id"]), str(row["variant"]))
        row.update(_finish_cost(models.get(key, _empty_cost_bucket())))
    for row in analytics["projects"]:
        key = (str(row["project_id"]), str(row["name"]))
        row.update(_finish_cost(projects.get(key, _empty_cost_bucket())))
    for row in analytics["events"]:
        calculated = event_costs.get(str(row["message_id"]), {"billed_cost": 0.0, "estimated_cost": 0.0, "total_cost": 0.0, "pricing_source": "unpriced", "pricing_match": "unknown", "pricing_status": "unpriced", "pricing_warnings": ["pricing unavailable"]})
        row.update(calculated)
    return analytics


class DashboardApi:
    def __init__(self, config_path: Path):
        self._config_path = str(config_path)

    @staticmethod
    def _period_bounds(period: str, custom_start: str = "", custom_end: str = "") -> tuple[int, int, str]:
        now = datetime.now().astimezone()
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        tomorrow = today + timedelta(days=1)
        if period == "today":
            return int(today.timestamp() * 1000), int(tomorrow.timestamp() * 1000), "hour"
        if period == "24h":
            start = now - timedelta(hours=24)
            return int(start.timestamp() * 1000), now_ms() + 1, "hour"
        if period == "custom":
            try:
                start_dt = datetime.strptime(custom_start, "%Y-%m-%d").astimezone()
                end_dt = datetime.strptime(custom_end, "%Y-%m-%d").astimezone() + timedelta(days=1)
                if end_dt <= start_dt:
                    raise ValueError
                granularity = "hour" if (end_dt - start_dt).total_seconds() <= 48 * 60 * 60 else "day"
                return int(start_dt.timestamp() * 1000), int(end_dt.timestamp() * 1000), granularity
            except (TypeError, ValueError):
                period = "30d"
        days = {"7d": 7, "30d": 30, "90d": 90}.get(period)
        if days:
            start = today - timedelta(days=days - 1)
            return int(start.timestamp() * 1000), int(tomorrow.timestamp() * 1000), "day"
        return 0, int(tomorrow.timestamp() * 1000), "day"

    def _previous_period_bounds(self, period: str, start_ms: int, end_ms: int) -> tuple[int, int, str] | None:
        if period == "today":
            start = datetime.fromtimestamp(start_ms / 1000).astimezone()
            end = datetime.fromtimestamp(end_ms / 1000).astimezone()
            return int((start - timedelta(days=1)).timestamp() * 1000), int(start.timestamp() * 1000), "hour"
        if period == "24h":
            return start_ms - 24 * 60 * 60 * 1000, start_ms, "hour"
        if period in {"7d", "30d", "90d"}:
            span = end_ms - start_ms
            return start_ms - span, start_ms, "day"
        return None

    def get_dashboard(
        self,
        period: str = "today",
        provider: str = "",
        model: str = "",
        view: str = "overview",
        custom_start: str = "",
        custom_end: str = "",
    ) -> dict[str, Any]:
        config = load_config(Path(self._config_path))
        data_dir = app_data_dir()
        store = Store(data_dir)
        try:
            options = store.filter_options()
            model_key: tuple[str, str, str] | None = None
            for item in options["models"]:
                if item["label"] == model:
                    model_key = (item["provider"], item["model"], item["variant"])
                    break
            start_ms, end_ms, granularity = self._period_bounds(period, custom_start, custom_end)
            event_limit = 100 if view == "events" else 0
            analytics = store.analytics(
                start_ms,
                end_ms,
                provider_id=provider or None,
                model_key=model_key,
                granularity=granularity,
                event_limit=event_limit,
            )
            pricing_enabled = bool(config.get("use_model_dev_pricing", True))
            selected_pricing_mode = pricing_mode(config)
            pricing_catalog: dict[str, Any] = {}
            pricing_status: dict[str, Any] = {"source": "models.dev", "state": "disabled", "updated_at": 0}
            if pricing_enabled and selected_pricing_mode != "custom":
                catalog = PricingCatalog(data_dir, safe_int(config["pricing_refresh_hours"]))
                pricing_catalog, pricing_status = catalog.load()
            elif selected_pricing_mode == "custom":
                pricing_status = {"source": "custom", "state": "enabled", "updated_at": now_ms()}
            costing_rows = store.costing_rows(start_ms, end_ms, provider or None, model_key)
            attach_model_dev_costs(analytics, costing_rows, pricing_catalog, config)
            pricing_status["enabled"] = pricing_enabled or selected_pricing_mode == "custom"
            pricing_status["mode"] = selected_pricing_mode
            pricing_status["multiplier"] = pricing_multiplier(config) if selected_pricing_mode == "multiplier" else 1.0
            custom = custom_pricing_config(config)
            pricing_status["custom_model_count"] = len(custom["models"])
            pricing_status["has_custom_default"] = bool(custom["default"])
            pricing_status["coverage"] = safe_float(analytics["summary"].get("pricing_coverage"))
            pricing_status["updated_text"] = (
                datetime.fromtimestamp(safe_int(pricing_status.get("updated_at")) / 1000).astimezone().strftime("%Y-%m-%d %H:%M")
                if safe_int(pricing_status.get("updated_at"))
                else "尚未更新"
            )
            comparison: dict[str, Any] | None = None
            previous_bounds = self._previous_period_bounds(period, start_ms, end_ms)
            if previous_bounds:
                previous_start, previous_end, previous_granularity = previous_bounds
                previous = store.analytics(
                    previous_start,
                    previous_end,
                    provider_id=provider or None,
                    model_key=model_key,
                    granularity=previous_granularity,
                    event_limit=0,
                )
                previous_rows = store.costing_rows(previous_start, previous_end, provider or None, model_key)
                attach_model_dev_costs(previous, previous_rows, pricing_catalog, config)
                comparison = {}
                for key in ("requests", "total_with_cache", "total_cost", "cache_read_tokens"):
                    current_value = safe_float(analytics["summary"].get(key))
                    previous_value = safe_float(previous["summary"].get(key))
                    comparison[key] = {
                        "current": current_value,
                        "previous": previous_value,
                        "change": current_value - previous_value,
                        "change_percent": ((current_value - previous_value) / previous_value * 100) if previous_value else None,
                    }
            for event in analytics["events"]:
                event["time"] = datetime.fromtimestamp(safe_int(event["event_time"]) / 1000).astimezone().strftime("%Y-%m-%d %H:%M:%S")
                event["model"] = str(event["model_id"]) + (
                    f" · {event['variant']}" if event["variant"] and event["variant"] != "default" else ""
                )
                event["project_name"] = event["project_name"] or event["project_id"]
            alerts: list[dict[str, Any]] = []
            if view == "alerts":
                kind_names = {"daily": "每日档位", "spike": "30 分钟突增", "error": "读取错误", "recovery": "恢复"}
                for alert in store.recent_alerts(200):
                    alerts.append(
                        {
                            "time": datetime.fromtimestamp(safe_int(alert["created_at"]) / 1000).astimezone().strftime("%Y-%m-%d %H:%M:%S"),
                            "kind": kind_names.get(str(alert["kind"]), str(alert["kind"])),
                            "amount": safe_int(alert["amount"]),
                            "threshold": safe_int(alert["threshold"]),
                            "message": str(alert["message"]),
                        }
                    )
            last_sync = safe_int(store.get_meta("last_sync", "0"))
            return {
                "version": VERSION,
                "theme": "light" if str(config.get("ui_theme", "dark")) == "light" else "dark",
                "period": period,
                "view": view,
                "options": options,
                "summary": analytics["summary"],
                "trend": analytics["trend"],
                "granularity": analytics["granularity"],
                "providers": analytics["providers"],
                "models": analytics["models"],
                "projects": analytics["projects"],
                "events": analytics["events"],
                "alerts": alerts,
                "comparison": comparison,
                "runtime": {
                    "opencode_running": opencode_is_running(),
                    "last_sync": last_sync,
                    "last_sync_text": datetime.fromtimestamp(last_sync / 1000).astimezone().strftime("%Y-%m-%d %H:%M:%S") if last_sync else "尚未同步",
                    "last_error": store.get_meta("last_error", ""),
                    "source_db": str(config.get("opencode_db", "")),
                    "daily_alert_tokens": safe_int(config["daily_alert_tokens"]),
                    "spike_alert_tokens": safe_int(config["spike_alert_tokens"]),
                    "pricing": pricing_status,
                    "display_currency": display_currency(config),
                    "usd_cny_rate": usd_cny_rate(config),
                    "timezone": datetime.now().astimezone().tzname() or "Local",
                    "today": store.summary(*local_day_bounds(now_ms())[1:]),
                    "spike": store.summary(now_ms() - max(1, safe_int(config["spike_window_minutes"])) * 60 * 1000, now_ms() + 1),
                },
            }
        finally:
            store.close()

    def refresh(self) -> dict[str, Any]:
        if opencode_is_running():
            config = load_config(Path(self._config_path))
            data_dir = app_data_dir()
            logger = setup_logging(data_dir)
            monitor = Monitor(config, data_dir, logger)
            try:
                monitor.sync()
                save_config(Path(self._config_path), config)
            finally:
                monitor.close()
        return {"ok": True, "opencode_running": opencode_is_running()}

    def set_theme(self, theme: str) -> dict[str, str]:
        config = load_config(Path(self._config_path))
        config["ui_theme"] = "light" if theme == "light" else "dark"
        save_config(Path(self._config_path), config)
        return {"theme": config["ui_theme"]}

    def set_display_currency(self, currency: str, rate: float | None = None) -> dict[str, Any]:
        config = load_config(Path(self._config_path))
        config["display_currency"] = "CNY" if str(currency or "USD").upper() == "CNY" else "USD"
        if rate is not None:
            try:
                config["usd_cny_rate"] = min(1000.0, max(0.000001, float(rate)))
            except (TypeError, ValueError):
                pass
        save_config(Path(self._config_path), config)
        return {"display_currency": display_currency(config), "usd_cny_rate": usd_cny_rate(config)}

    def get_pricing_settings(self) -> dict[str, Any]:
        config = load_config(Path(self._config_path))
        custom = custom_pricing_config(config)
        store = Store(app_data_dir())
        try:
            options = store.filter_options()
        finally:
            store.close()
        return {
            "mode": pricing_mode(config),
            "multiplier": pricing_multiplier(config),
            "display_currency": display_currency(config),
            "usd_cny_rate": usd_cny_rate(config),
            "use_model_dev_pricing": bool(config.get("use_model_dev_pricing", True)),
            "default_rates": custom["default"],
            "models": custom["models"],
            "options": options,
            "catalog": {
                "source": "models.dev",
                "url": PricingCatalog.URL,
                "unit": "USD / 1M tokens",
            },
        }

    def save_pricing_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(settings, dict):
            raise ValueError("Pricing settings must be an object")
        config = load_config(Path(self._config_path))
        mode = str(settings.get("mode") or "official").strip().lower()
        config["pricing_mode"] = mode if mode in PRICING_MODES else "official"
        if config["pricing_mode"] in {"official", "multiplier"}:
            config["use_model_dev_pricing"] = True
        try:
            config["pricing_multiplier"] = min(1_000_000.0, max(0.0, float(settings.get("multiplier", 1.0))))
        except (TypeError, ValueError):
            config["pricing_multiplier"] = 1.0
        config["display_currency"] = "CNY" if str(settings.get("display_currency") or "USD").upper() == "CNY" else "USD"
        try:
            config["usd_cny_rate"] = min(1000.0, max(0.000001, float(settings.get("usd_cny_rate", 7.20))))
        except (TypeError, ValueError):
            config["usd_cny_rate"] = 7.20
        raw_models = settings.get("models")
        models: list[dict[str, Any]] = []
        if isinstance(raw_models, list):
            for item in raw_models[:500]:
                if not isinstance(item, dict):
                    continue
                provider = str(item.get("provider") or "").strip()
                model = str(item.get("model") or "").strip()
                rates = normalize_rates(item.get("rates"))
                if not provider or not model or not rates:
                    continue
                models.append({
                    "provider": provider,
                    "model": model,
                    "variant": str(item.get("variant") or "default").strip() or "default",
                    "rates": rates,
                })
        config["custom_pricing"] = {
            "default": normalize_rates(settings.get("default_rates")),
            "models": models,
        }
        save_config(Path(self._config_path), config)
        return self.get_pricing_settings()

    def export_csv(self) -> dict[str, Any]:
        config = load_config(Path(self._config_path))
        monitor = Monitor(config, app_data_dir(), setup_logging(app_data_dir()))
        store = monitor.store
        try:
            monitor.export_full_csv()
            catalog: dict[str, Any] = {}
            if config.get("use_model_dev_pricing", True) and pricing_mode(config) != "custom":
                catalog, _ = PricingCatalog(app_data_dir(), safe_int(config["pricing_refresh_hours"])).load()
            rows = store.costing_rows(0, now_ms() + 1)
            path = store.csv_dir / "pricing_estimates.csv"
            temp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            with temp.open("w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.writer(handle)
                writer.writerow([
                    "event_time", "provider", "model", "variant", "project", "billed_cost",
                    "estimated_cost", "reference_total", "pricing_source", "pricing_match", "pricing_status",
                ])
                for row in rows:
                    cost = PricingCatalog.event_cost(catalog, row, config)
                    writer.writerow([
                        datetime.fromtimestamp(safe_int(row["event_time"]) / 1000).astimezone().isoformat(),
                        row["provider_id"], row["model_id"], row["variant"], row["project_name"],
                        f"{cost['billed_cost']:.8f}", f"{cost['estimated_cost']:.8f}", f"{cost['total_cost']:.8f}",
                        cost["pricing_source"], cost.get("pricing_match", "unknown"), cost["pricing_status"],
                    ])
            os.replace(temp, path)
            return {"ok": True, "path": str(store.csv_dir), "pricing": str(path)}
        finally:
            monitor.close()

    def open_data_dir(self) -> dict[str, str]:
        os.startfile(app_data_dir())  # type: ignore[attr-defined]
        return {"path": str(app_data_dir())}

    def open_csv_dir(self) -> dict[str, str]:
        path = app_data_dir() / "csv"
        os.startfile(path)  # type: ignore[attr-defined]
        return {"path": str(path)}

    def maximize_window(self) -> dict[str, bool]:
        if DASHBOARD_WINDOW is not None:
            DASHBOARD_WINDOW.maximize()
        return {"ok": True}


def run_dashboard(config_path: Path) -> int:
    # A second dashboard command restores the existing window instead of
    # creating another hidden WebView process after close-to-tray is used.
    if show_existing_dashboard():
        return 0
    try:
        import webview  # type: ignore
    except ImportError as exc:
        raise RuntimeError("pywebview is required to run the desktop dashboard") from exc

    html_path = resource_path("dashboard.html")
    html = html_path.read_text(encoding="utf-8")
    api = DashboardApi(config_path)
    window = webview.create_window(
        f"{APP_NAME} {VERSION}",
        html=html,
        js_api=api,
        width=DEFAULT_WINDOW_WIDTH,
        height=DEFAULT_WINDOW_HEIGHT,
        min_size=(360, 480),
        resizable=True,
        background_color="#0a0a0b",
        text_select=True,
        zoomable=False,
        confirm_close=False,
    )
    global DASHBOARD_WINDOW
    DASHBOARD_WINDOW = window

    tray_start_attempted = False

    def hide_to_tray() -> bool:
        nonlocal tray_start_attempted
        if not tray_start_attempted:
            ensure_tray_running(config_path)
            tray_start_attempted = True
        # Returning False cancels pywebview's close event; the native window
        # remains alive and can be restored from the tray or desktop shortcut.
        window.hide()
        return False

    window.events.closing += hide_to_tray
    webview.start(debug=False, gui="edgechromium")
    return 0


def print_status(monitor: Monitor, as_json: bool = False) -> None:
    status = monitor.current_status()
    if as_json:
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return
    print(f"{APP_NAME} {VERSION}")
    print(f"Source: {monitor.source_db}")
    print(f"Data:   {monitor.data_dir}")
    for label, key in (("Today", "today"), ("7 days", "seven_days"), ("30 days", "thirty_days"), ("Spike window", "spike_window")):
        item = status[key]
        print(
            f"{label:12} {format_int(item['total_with_cache']):>10} incl. cache | "
            f"{format_int(item['total_without_cache_read']):>10} excl. cache read | "
            f"{format_money(item['cost'])} | {item['requests']} requests"
        )
    print("\nTop providers (7 days)")
    for row in status["providers"]:
        print(f"  {row['name']:<28} {format_int(row['total_with_cache']):>10}")
    print("\nTop models (7 days)")
    for row in status["models"]:
        print(f"  {row['name']:<38} {format_int(row['total_with_cache']):>10}")
    if status["last_error"]:
        print(f"\nERROR: {status['last_error']}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="OpenCodeTokenMonitor", description="Record and monitor local OpenCode token usage")
    parser.add_argument("--config", type=Path, default=app_data_dir() / "config.json")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("tray", help="run the Windows tray monitor")
    sub.add_parser("dashboard", help="open the native Windows dashboard")
    sync = sub.add_parser("sync", help="scan OpenCode once")
    sync.add_argument("--json", action="store_true")
    status = sub.add_parser("status", help="show current token totals")
    status.add_argument("--json", action="store_true")
    history = sub.add_parser("history", help="show daily totals")
    history.add_argument("--days", type=int, default=30)
    export = sub.add_parser("export", help="rebuild all CSV exports")
    export.add_argument("--path", type=Path, help="copy the CSV directory to this path")
    sub.add_parser("install", help="enable Windows auto-start")
    sub.add_parser("uninstall", help="disable Windows auto-start")
    sub.add_parser("test-alert", help="show a test notification and play a sound")
    sub.add_parser("paths", help="print config, data, database, and CSV paths")
    for child in sub.choices.values():
        child.add_argument("--config", type=Path, default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command or "tray"
    if command == "tray":
        return run_tray(args.config)
    if command == "dashboard":
        return run_dashboard(args.config)

    config = load_config(args.config)
    data_dir = app_data_dir()
    logger = setup_logging(data_dir)
    if command == "install":
        install_startup(args.config)
        print(f"Auto-start enabled: {APP_ID}")
        return 0
    if command == "uninstall":
        remove_startup_command()
        print("Auto-start disabled")
        return 0
    if command == "paths":
        print(f"exe={executable_path()}")
        print(f"config={args.config}")
        print(f"data={data_dir}")
        try:
            print(f"database={detect_opencode_db(config)}")
        except Exception as exc:
            print(f"database=<not found: {exc}>")
        print(f"csv={data_dir / 'csv'}")
        return 0

    notifier = Notifier(config)
    if command == "test-alert":
        notifier.notify(f"{APP_NAME} 测试", "系统通知、声音和托盘颜色工作正常。", sound=True)
        return 0

    monitor = Monitor(config, data_dir, logger)
    save_config(args.config, config)
    try:
        if command == "sync":
            result = monitor.sync()
            if args.json:
                print(json.dumps(result.__dict__, ensure_ascii=False, indent=2))
            else:
                print(
                    f"Sync complete: seen={result.records_seen}, changed={result.records_changed}, "
                    f"ledger={result.ledger_rows}, alerts={len(result.alerts or [])}, full={result.full_reconcile}"
                )
            for alert in result.alerts or []:
                notifier.notify(f"{APP_NAME} 告警", alert["message"], sound=True)
        elif command == "status":
            print_status(monitor, args.json)
        elif command == "history":
            for row in reversed(monitor.store.daily_rows(max(1, args.days))):
                print(
                    f"{row['day']}  {format_int(row['total_with_cache']):>10} incl. cache | "
                    f"{format_int(row['total_without_cache_read']):>10} excl. cache read | "
                    f"{format_money(row['cost'])} | {row['requests']} requests"
                )
        elif command == "export":
            monitor.export_full_csv()
            destination = args.path.resolve() if args.path else monitor.store.csv_dir
            if args.path:
                destination.mkdir(parents=True, exist_ok=True)
                import shutil

                for source in monitor.store.csv_dir.glob("*.csv"):
                    shutil.copy2(source, destination / source.name)
            print(f"CSV exported to {destination}")
    except Exception as exc:
        monitor.mark_error(exc)
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        monitor.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
