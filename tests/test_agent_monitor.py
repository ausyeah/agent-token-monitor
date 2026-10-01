from __future__ import annotations

import datetime as dt
import json
import os
import re
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import agent_token_monitor as module


class MonitorTests(unittest.TestCase):
    def make_source_db(self, path: Path, v2_rows: list[dict], v1_rows: list[dict] | None = None) -> None:
        if path.exists():
            path.unlink()
        con = sqlite3.connect(path)
        con.executescript(
            """
            CREATE TABLE project(id TEXT PRIMARY KEY, worktree TEXT, name TEXT);
            CREATE TABLE session_v2(id TEXT PRIMARY KEY, project_id TEXT, directory TEXT);
            CREATE TABLE session(id TEXT PRIMARY KEY, project_id TEXT, directory TEXT);
            CREATE TABLE session_message(
                id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, time_updated INTEGER,
                data TEXT, type TEXT
            );
            CREATE TABLE message(
                id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, time_updated INTEGER,
                data TEXT
            );
            """
        )
        con.execute("INSERT INTO project VALUES(?,?,?)", ("p1", "E:/work", "work"))
        con.execute("INSERT INTO session_v2 VALUES(?,?,?)", ("s2", "p1", "E:/work"))
        con.execute("INSERT INTO session VALUES(?,?,?)", ("s1", "p1", "E:/work"))
        for row in v2_rows:
            con.execute(
                "INSERT INTO session_message VALUES(?,?,?,?,?,?)",
                (row["id"], "s2", row["created"], row["updated"], json.dumps(row["data"]), "assistant"),
            )
        for row in v1_rows or []:
            con.execute(
                "INSERT INTO message VALUES(?,?,?,?,?)",
                (row["id"], "s1", row["created"], row["updated"], json.dumps(row["data"])),
            )
        con.commit()
        con.close()

    def test_v1_v2_ingest_dedup_delta_and_csv(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "opencode.db"
            data = root / "monitor"
            t = 1_700_000_000_000
            v2 = {
                "id": "m2",
                "created": t,
                "updated": t + 100,
                "data": {
                    "time": {"created": t, "completed": t + 100},
                    "agent": "build",
                    "model": {"providerID": "opencode", "id": "space-bunny-free", "variant": "max"},
                    "tokens": {"input": 10, "output": 5, "reasoning": 2, "cache": {"read": 100, "write": 1}},
                    "cost": 0,
                },
            }
            v1 = {
                "id": "m1",
                "created": t - 100,
                "updated": t - 50,
                "data": {
                    "role": "assistant",
                    "time": {"created": t - 100, "completed": t - 50},
                    "providerID": "anthropic",
                    "modelID": "claude",
                    "variant": "high",
                    "agent": "build",
                    "tokens": {"input": 3, "output": 2, "reasoning": 1, "cache": {"read": 4, "write": 0}},
                    "cost": 0.1,
                },
            }
            self.make_source_db(source, [v2], [v1])
            config = dict(module.DEFAULT_CONFIG)
            config["opencode_db"] = str(source)
            # Keep this test hermetic: auto-detection would otherwise read the
            # real ~/.workbuddy, ~/.dsh or ~/.codex directory of whoever runs
            # the suite.
            config["workbuddy_enabled"] = False
            config["dsh_enabled"] = False
            config["codex_enabled"] = False
            logger = module.logging.getLogger("test-monitor")
            monitor = module.Monitor(config, data, logger)
            first = monitor.sync()
            self.assertEqual(first.records_seen, 2)
            self.assertEqual(monitor.store.conn.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0], 2)
            summary = monitor.store.summary(0, t + 1000)
            self.assertEqual(summary["total_with_cache"], 128)
            self.assertEqual(summary["total_without_cache_read"], 24)
            day, day_start, day_end = module.local_day_bounds(t)
            analytics = monitor.store.analytics(day_start, day_end, granularity="hour")
            self.assertEqual(analytics["summary"]["requests"], 2)
            self.assertEqual(sum(row["total_with_cache"] for row in analytics["trend"]), 128)
            self.assertEqual(len(analytics["trend"]), 24)
            provider_filtered = monitor.store.analytics(day_start, day_end, provider_id="anthropic")
            self.assertEqual(provider_filtered["summary"]["total_with_cache"], 10)
            self.assertEqual(provider_filtered["providers"][0]["name"], "anthropic")
            self.assertTrue(any(item["label"].startswith("opencode/space-bunny-free") for item in monitor.store.filter_options()["models"]))


            # Update the V2 source row and a V1 row on the next scan.
            overlap = dict(v1)
            overlap["data"] = dict(v1["data"])
            overlap["data"]["tokens"] = {"input": 999, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}}
            v2["updated"] += 100
            v2["data"] = dict(v2["data"])
            v2["data"]["tokens"] = {"input": 15, "output": 5, "reasoning": 2, "cache": {"read": 120, "write": 1}}
            # Recreate the source database with both updated rows.
            self.make_source_db(source, [v2], [overlap])
            monitor.store.conn.execute("UPDATE meta SET value='0' WHERE key='watermark_v2'")
            monitor.store.conn.execute("UPDATE meta SET value='0' WHERE key='watermark_v1'")
            monitor.store.conn.commit()
            second = monitor.sync()
            self.assertEqual(second.records_changed, 2)
            row = monitor.store.existing_event("m2")
            self.assertEqual(row["input_tokens"], 15)
            self.assertTrue((data / "csv" / "usage_ledger.csv").exists())
            monitor.export_full_csv()
            self.assertTrue((data / "csv" / "usage_events.csv").exists())
            monitor.close()

    def test_model_dev_cost_estimation(self) -> None:
        catalog = {
            "openai": {
                "models": {
                    "priced-model": {
                        "cost": {"input": 2.0, "output": 10.0, "reasoning": 10.0, "cache_read": 0.2, "cache_write": 2.5}
                    }
                }
            }
        }
        row = {
            "provider_id": "custom-provider",
            "model_id": "gpt-priced-model",
            "input_tokens": 1_000_000,
            "output_tokens": 1_000_000,
            "reasoning_tokens": 1_000_000,
            "cache_read_tokens": 1_000_000,
            "cache_write_tokens": 1_000_000,
            "cost": 0.0,
        }
        # The generic gpt-* canonical-provider fallback intentionally does not match this synthetic id.
        result = module.PricingCatalog.event_cost(catalog, row)
        self.assertEqual(result["pricing_status"], "unpriced")
        row["model_id"] = "gpt-5.4"
        catalog["openai"]["models"]["gpt-5.4"] = {"cost": {"input": 2.0, "output": 10.0, "reasoning": 10.0, "cache_read": 0.2, "cache_write": 2.5}}
        result = module.PricingCatalog.event_cost(catalog, row)
        self.assertAlmostEqual(result["estimated_cost"], 24.7)
        self.assertEqual(result["total_cost"], 24.7)
        row["cost"] = 20.0
        result = module.PricingCatalog.event_cost(catalog, row)
        self.assertEqual(result["billed_cost"], 20.0)
        self.assertEqual(result["total_cost"], 20.0)

        catalog["openai"]["models"]["gpt-5.4"]["cost"]["tiers"] = [
            {"tier": {"type": "context", "size": 200_000}, "input": 3.0, "output": 4.0},
            {"tier": {"type": "context", "size": 500_000}, "input": 5.0, "output": 6.0, "cache_read": 0.5, "cache_write": 7.5},
        ]
        row["cost"] = 0.0
        result = module.PricingCatalog.event_cost(catalog, row)
        self.assertAlmostEqual(result["estimated_cost"], 29.0)

    def test_custom_and_multiplier_pricing(self) -> None:
        row = {
            "provider_id": "opencode",
            "model_id": "space-bunny-free",
            "variant": "max",
            "input_tokens": 1_000_000,
            "output_tokens": 1_000_000,
            "reasoning_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "cost": 0.0,
        }
        custom = {
            "pricing_mode": "custom",
            "custom_pricing": {
                "default": {},
                "models": [{"provider": "opencode", "model": "space-bunny-free", "variant": "max", "rates": {"input": 2.0, "output": 8.0}}],
            },
        }
        result = module.PricingCatalog.event_cost({}, row, custom)
        self.assertAlmostEqual(result["estimated_cost"], 10.0)
        self.assertEqual(result["pricing_status"], "custom-estimate")
        self.assertEqual(result["pricing_match"], "custom")
        multiplied = {
            "pricing_mode": "multiplier",
            "pricing_multiplier": 2.0,
            "custom_pricing": {"default": {}, "models": []},
        }
        catalog = {"openai": {"models": {"gpt-priced": {"cost": {"input": 2.0, "output": 10.0}}}}}
        row["model_id"] = "gpt-priced"
        result = module.PricingCatalog.event_cost(catalog, row, multiplied)
        self.assertAlmostEqual(result["estimated_cost"], 24.0)
        self.assertEqual(result["pricing_status"], "multiplied-estimate")

    # --- multi-agent sources -------------------------------------------------

    def make_workbuddy_log(self, path: Path, entries: list[dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            for entry in entries:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    @staticmethod
    def workbuddy_entry(
        entry_id: str,
        *,
        timestamp: int,
        session: str = "sess-1",
        model_id: str = "hy3",
        model_name: str = "Hy3",
        cwd: str = r"d:\work\demo",
        agent: str = "cli",
        prompt_tokens: int = 1000,
        completion_tokens: int = 100,
        cache_hit: int = 600,
        cache_miss: int | None = None,
        reasoning: int = 20,
        credit: float = 0.5,
    ) -> dict:
        if cache_miss is None:
            cache_miss = prompt_tokens - cache_hit
        return {
            "id": entry_id,
            "timestamp": timestamp,
            "type": "message",
            "role": "assistant",
            "sessionId": session,
            "cwd": cwd,
            "providerData": {
                "messageId": f"msg-{entry_id}",
                "model": model_id,
                "requestModelId": model_id,
                "requestModelName": model_name,
                "agent": agent,
                "usage": {
                    "requests": 1,
                    "inputTokens": prompt_tokens,
                    "outputTokens": completion_tokens,
                    "totalTokens": prompt_tokens + completion_tokens,
                },
                "rawUsage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens,
                    "prompt_cache_hit_tokens": cache_hit,
                    "prompt_cache_miss_tokens": cache_miss,
                    "completion_thinking_tokens": reasoning,
                    "credit": credit,
                },
            },
        }

    def test_workbuddy_jsonl_ingest_and_cache_split(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = root / "monitor"
            wb_root = root / ".workbuddy"
            log = wb_root / "projects" / "d-work-demo" / "sess-1.jsonl"
            t = 1_700_000_000_000
            self.make_workbuddy_log(
                log,
                [
                    self.workbuddy_entry("a", timestamp=t),
                    self.workbuddy_entry(
                        "b", timestamp=t + 1000,
                        model_id="deepseek-v4.1-flash", model_name="Deepseek-V4.1-Flash",
                    ),
                    {"id": "bad", "timestamp": t, "providerData": {"rawUsage": {}}},
                ],
            )
            with log.open("a", encoding="utf-8") as handle:
                handle.write("{not json\n")
            # A real but empty SQLite file keeps OpenCode detection from
            # falling back to the real database on the test machine.
            empty_source = root / "opencode.db"
            sqlite3.connect(empty_source).close()
            config = dict(module.DEFAULT_CONFIG)
            config["workbuddy_enabled"] = True
            config["workbuddy_root"] = str(wb_root)
            config["dsh_enabled"] = False
            config["codex_enabled"] = False
            config["opencode_db"] = str(empty_source)
            monitor = module.Monitor(config, data, module.logging.getLogger("test-workbuddy"))
            try:
                result = monitor.sync()
                self.assertEqual(result.records_seen, 2)
                # prompt_tokens includes the cached prefix, so input must
                # become the cache-miss count rather than double counting.
                row = monitor.store.existing_event("workbuddy:sess-1:a")
                self.assertIsNotNone(row)
                self.assertEqual(row["input_tokens"], 400)
                self.assertEqual(row["cache_read_tokens"], 600)
                # Thinking tokens are a subset of completion_tokens, so
                # output holds the remainder and the two must not double count.
                self.assertEqual(row["output_tokens"], 80)
                self.assertEqual(row["reasoning_tokens"], 20)
                self.assertEqual(row["source"], "workbuddy")
                self.assertEqual(row["provider_id"], "workbuddy")
                self.assertEqual(row["variant"], "default")
                self.assertEqual(row["project_path"], r"d:\work\demo")
                self.assertEqual(row["project_name"], "demo")
                self.assertEqual(row["total_with_cache"], 1000 + 100)
                self.assertEqual(row["total_without_cache_read"], 400 + 100)

                custom_log = wb_root / "projects" / "d-work-demo" / "sess-2.jsonl"
                self.make_workbuddy_log(
                    custom_log,
                    [self.workbuddy_entry(
                        "c", timestamp=t + 2000, session="sess-2",
                        model_id="custom-local:gpt-5.6-terra", model_name="gpt-5.6-terra",
                    )],
                )
                monitor.sync()
                custom_row = monitor.store.existing_event("workbuddy:sess-2:c")
                self.assertEqual(custom_row["variant"], "custom")
                self.assertEqual(custom_row["model_id"], "gpt-5.6-terra")

                before = monitor.store.conn.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0]
                monitor.sync()
                after = monitor.store.conn.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0]
                self.assertEqual(after, before)
            finally:
                monitor.close()

    def make_dsh_log(self, path: Path, events: list[dict]) -> None:
        import zstandard

        path.parent.mkdir(parents=True, exist_ok=True)
        payload = ("\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n").encode("utf-8")
        path.write_bytes(zstandard.ZstdCompressor().compress(payload))

    @staticmethod
    def dsh_events(t: int) -> list[dict]:
        return [
            {
                "type": "session", "version": 4, "id": "sess-1",
                "createdAt": t, "cwd": r"d:\work\dsh-demo", "agentPreset": "standard",
            },
            {"type": "request/context", "seq": 1, "time": t,
             "data": {"provider": "bupt", "model": "deepseek-v4-flash", "contextWindow": 245760}},
            {
                "type": "assistant/message", "seq": 2, "time": t + 1000,
                "data": {
                    "turn": 1, "step": 1,
                    "message": {"role": "assistant", "id": "m-a"},
                    "usage": {"inputTokens": 8197, "outputTokens": 586, "totalTokens": 8783},
                    "stream": [
                        {"type": "chunk", "time": t + 1000,
                         "chunk": {"type": "finish", "reason": {"kind": "stop"},
                                   "replayState": {"response": {"kind": "pi-ai", "provider": "bupt",
                                                               "model": "deepseek-v4-flash"}}}},
                    ],
                },
            },
            {
                # A second request routed elsewhere: the per-request route must
                # win over the session-level one.
                "type": "assistant/message", "seq": 3, "time": t + 2000,
                "data": {
                    "turn": 1, "step": 2,
                    "message": {"role": "assistant", "id": "m-b"},
                    "usage": {"inputTokens": 8934, "outputTokens": 194, "totalTokens": 9128},
                    "stream": [
                        {"type": "chunk", "time": t + 2000,
                         "chunk": {"type": "finish", "reason": {"kind": "stop"},
                                   "replayState": {"response": {"kind": "pi-ai", "provider": "apiko",
                                                               "model": "deepseek-v4.1-flash"}}}},
                    ],
                },
            },
            {"type": "user/message", "seq": 4, "time": t + 2500,
             "data": {"role": "user", "content": "hi"}},
        ]

    def test_deepseek_harness_zstd_ingest(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            dsh_root = root / ".dsh"
            log = dsh_root / "sessions" / "ws" / "session-sess-1" / "session.v4.jsonl.zstd"
            t = 1_700_000_000_000
            self.make_dsh_log(log, self.dsh_events(t))
            empty_source = root / "opencode.db"
            sqlite3.connect(empty_source).close()
            config = dict(module.DEFAULT_CONFIG)
            config["opencode_db"] = str(empty_source)
            config["workbuddy_enabled"] = False
            config["dsh_enabled"] = True
            config["dsh_root"] = str(dsh_root)
            config["codex_enabled"] = False
            monitor = module.Monitor(config, root / "monitor", module.logging.getLogger("test-dsh"))
            try:
                self.assertEqual(monitor.sync().records_seen, 2)
                columns = [
                    "message_id", "provider_id", "model_id", "session_id", "project_name",
                    "project_path", "agent", "input_tokens", "output_tokens",
                    "reasoning_tokens", "cache_read_tokens", "cost", "total_with_cache",
                ]
                rows = monitor.store.conn.execute(
                    "SELECT " + ",".join(columns) +
                    " FROM usage_events WHERE source='dsh' ORDER BY event_time"
                ).fetchall()
                self.assertEqual(len(rows), 2)
                first, second = (dict(zip(columns, row)) for row in rows)
                self.assertTrue(first["message_id"].startswith("dsh:sess-1:"))
                self.assertEqual(first["provider_id"], "bupt")
                self.assertEqual(first["model_id"], "deepseek-v4-flash")
                self.assertEqual(first["session_id"], "sess-1")
                self.assertEqual(first["project_path"], r"d:\work\dsh-demo")
                self.assertEqual(first["project_name"], "dsh-demo")
                self.assertEqual(first["agent"], "standard")
                self.assertEqual(first["input_tokens"], 8197)
                self.assertEqual(first["output_tokens"], 586)
                # DSH reports no cache counters, no reasoning split, no cost.
                self.assertEqual(first["reasoning_tokens"], 0)
                self.assertEqual(first["cache_read_tokens"], 0)
                self.assertEqual(first["cost"], 0.0)
                self.assertEqual(first["total_with_cache"], 8783)
                self.assertEqual(second["provider_id"], "apiko")
                self.assertEqual(second["model_id"], "deepseek-v4.1-flash")

                # A rewritten log must stay idempotent across scans.
                before = monitor.store.conn.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0]
                self.make_dsh_log(log, self.dsh_events(t))
                monitor.sync()
                after = monitor.store.conn.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0]
                self.assertEqual(after, before)
            finally:
                monitor.close()

    def test_dsh_missing_dependency_is_reported(self) -> None:
        """A missing zstandard must surface as a warning, not as silence."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            dsh_root = root / ".dsh"
            (dsh_root / "sessions").mkdir(parents=True)
            empty_source = root / "opencode.db"
            sqlite3.connect(empty_source).close()
            config = dict(module.DEFAULT_CONFIG)
            config["opencode_db"] = str(empty_source)
            config["workbuddy_enabled"] = False
            config["dsh_enabled"] = True
            config["dsh_root"] = str(dsh_root)
            config["codex_enabled"] = False
            monitor = module.Monitor(config, root / "monitor", module.logging.getLogger("test-dep"))
            try:
                import builtins
                real_import = builtins.__import__

                def fake_import(name, *args, **kwargs):
                    if name == "zstandard":
                        raise ImportError("simulated")
                    return real_import(name, *args, **kwargs)

                builtins.__import__ = fake_import
                try:
                    monitor.sync()
                finally:
                    builtins.__import__ = real_import
                health = monitor.store.source_health_report(monitor.source_health)
                self.assertFalse(health["dsh"]["available"])
                self.assertEqual(health["dsh"]["reason"], "missing_dependency")
            finally:
                monitor.close()

    # --- Codex -------------------------------------------------------------

    @staticmethod
    def codex_usage(input_: int, cached: int, output: int, reasoning: int = 0, cache_write: int = 0) -> dict:
        """Build one cumulative ``TokenUsage`` block the way Codex writes it."""
        return {
            "input_tokens": input_,
            "cached_input_tokens": cached,
            "cache_write_input_tokens": cache_write,
            "output_tokens": output,
            "reasoning_output_tokens": reasoning,
            "total_tokens": input_ + output,
        }

    def append_codex_rollout(self, path: Path, envelopes: list[dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            for envelope in envelopes:
                handle.write(json.dumps(envelope, ensure_ascii=False) + "\n")

    @staticmethod
    def codex_meta(ordinal: int, **payload) -> dict:
        return {
            "timestamp": "2026-01-02T03:04:05.000Z",
            "ordinal": ordinal,
            "type": "session_meta",
            "payload": {"timestamp": "2026-01-02T03:04:05.000Z", **payload},
        }

    @staticmethod
    def codex_turn_context(ordinal: int, model: str, effort: str = "high") -> dict:
        return {
            "timestamp": "2026-01-02T03:04:06.000Z",
            "ordinal": ordinal,
            "type": "turn_context",
            "payload": {"model": model, "effort": effort},
        }

    @staticmethod
    def codex_token_count(ordinal: int, ms: int, total: dict) -> dict:
        stamp = datetime.fromtimestamp(ms / 1000).astimezone().isoformat().replace("+00:00", "Z")
        return {
            "timestamp": stamp,
            "ordinal": ordinal,
            "type": "event_msg",
            "payload": {"type": "token_count", "info": {"total_token_usage": total, "model_context_window": 272000}},
        }

    def codex_monitor(self, root: Path, codex_root: Path) -> "module.Monitor":
        empty_source = root / "opencode.db"
        sqlite3.connect(empty_source).close()
        config = dict(module.DEFAULT_CONFIG)
        config["opencode_db"] = str(empty_source)
        config["workbuddy_enabled"] = False
        config["dsh_enabled"] = False
        config["codex_enabled"] = True
        config["codex_root"] = str(codex_root)
        return module.Monitor(config, root / "monitor", module.logging.getLogger("test-codex"))

    def test_codex_rollout_ingest_uses_deltas_and_splits_cached_tokens(self) -> None:
        """Cumulative counters must be turned into per-request records."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            codex_root = root / ".codex"
            rollout = codex_root / "sessions" / "2026" / "01" / "02" / "rollout-abc.jsonl"
            t = 1_700_000_000_000
            self.append_codex_rollout(rollout, [
                self.codex_meta(0, session_id="sess-1", id="sess-1",
                                cwd=r"d:\work\codex-demo", originator="Codex Desktop",
                                model_provider="custom"),
                self.codex_turn_context(1, "gpt-5.6-terra", effort="high"),
                # First request: 5000 in, 4000 of it cached, 500 written, 200 out.
                self.codex_token_count(2, t + 1000, self.codex_usage(5000, 4000, 200, 0, 500)),
                # Codex repeats the event when a turn added no new inference.
                self.codex_token_count(3, t + 1500, self.codex_usage(5000, 4000, 200, 0, 500)),
                # Second request adds 1000 in (900 cached), 100 out, 40 reasoning.
                self.codex_token_count(4, t + 2000, self.codex_usage(6000, 4900, 300, 40, 500)),
            ])
            monitor = self.codex_monitor(root, codex_root)
            try:
                # Three events, one of which repeats an unchanged total.
                self.assertEqual(monitor.sync().records_seen, 2)
                columns = [
                    "message_id", "provider_id", "model_id", "variant", "agent", "session_id",
                    "project_name", "project_path", "input_tokens", "output_tokens",
                    "reasoning_tokens", "cache_read_tokens", "cache_write_tokens", "cost",
                    "total_with_cache", "total_without_cache_read",
                ]
                rows = monitor.store.conn.execute(
                    "SELECT " + ",".join(columns) + " FROM usage_events WHERE source='codex' ORDER BY event_time"
                ).fetchall()
                self.assertEqual(len(rows), 2)
                first, second = (dict(zip(columns, row)) for row in rows)
                self.assertEqual(first["message_id"], "codex:rollout-abc:2")
                self.assertEqual(second["message_id"], "codex:rollout-abc:4")
                # A placeholder model_provider must not become the bucket name.
                self.assertEqual(first["provider_id"], "openai")
                self.assertEqual(first["model_id"], "gpt-5.6-terra")
                self.assertEqual(first["variant"], "high")
                self.assertEqual(first["agent"], "Codex Desktop")
                self.assertEqual(first["session_id"], "sess-1")
                self.assertEqual(first["project_path"], r"d:\work\codex-demo")
                self.assertEqual(first["project_name"], "codex-demo")
                # cached and cache_write are disjoint parts of input_tokens.
                self.assertEqual(first["input_tokens"], 5000 - 4000 - 500)
                self.assertEqual(first["cache_read_tokens"], 4000)
                self.assertEqual(first["cache_write_tokens"], 500)
                self.assertEqual(first["output_tokens"], 200)
                self.assertEqual(first["reasoning_tokens"], 0)
                # Codex records no cost, so the estimate is the only cost shown.
                self.assertEqual(first["cost"], 0.0)
                # total_tokens is exactly input_tokens + output_tokens.
                self.assertEqual(first["total_with_cache"], 5000 + 200)
                self.assertEqual(first["total_without_cache_read"], 5000 + 200 - 4000)
                # The second record is a delta, not the running total.
                self.assertEqual(second["input_tokens"], 1000 - 900)
                self.assertEqual(second["cache_read_tokens"], 900)
                self.assertEqual(second["cache_write_tokens"], 0)
                self.assertEqual(second["reasoning_tokens"], 40)
                self.assertEqual(second["output_tokens"], 100 - 40)
                self.assertEqual(second["total_with_cache"], 1000 + 100)
                summary = monitor.store.summary(0, t + 10_000)
                self.assertEqual(summary["total_with_cache"], 5000 + 200 + 1000 + 100)
            finally:
                monitor.close()

    def test_codex_mid_session_model_switch_uses_the_turn_context(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            codex_root = root / ".codex"
            rollout = codex_root / "sessions" / "2026" / "01" / "02" / "rollout-switch.jsonl"
            t = 1_700_000_000_000
            self.append_codex_rollout(rollout, [
                self.codex_meta(0, session_id="s2", id="s2", cwd=r"d:\work\p",
                                originator="codex_cli_rs", model_provider="openai"),
                self.codex_turn_context(1, "gpt-5.6-terra", effort="high"),
                self.codex_token_count(2, t + 1000, self.codex_usage(1000, 0, 100)),
                self.codex_turn_context(3, "step-5-preview", effort="medium"),
                self.codex_token_count(4, t + 2000, self.codex_usage(2000, 0, 200)),
            ])
            monitor = self.codex_monitor(root, codex_root)
            try:
                monitor.sync()
                rows = monitor.store.conn.execute(
                    "SELECT model_id,variant,provider_id FROM usage_events WHERE source='codex' ORDER BY event_time"
                ).fetchall()
                self.assertEqual([tuple(r) for r in rows], [
                    ("gpt-5.6-terra", "high", "openai"),
                    # The recorded provider is kept, so the family is not used.
                    ("step-5-preview", "medium", "openai"),
                ])
            finally:
                monitor.close()

    def test_codex_provider_falls_back_to_the_model_family(self) -> None:
        self.assertEqual(module.Monitor._codex_provider("", "gpt-5.6-terra"), "openai")
        self.assertEqual(module.Monitor._codex_provider("custom", "step-5-preview"), "stepfun")
        self.assertEqual(module.Monitor._codex_provider("custom", "grok-4.7"), "xai")
        # A real provider id is kept verbatim.
        self.assertEqual(module.Monitor._codex_provider("azure", "gpt-5.6-terra"), "azure")
        # An unrecognised family must still land somewhere nameable.
        self.assertEqual(module.Monitor._codex_provider("custom", "mystery-1"), "openai")

    def test_codex_incremental_append_does_not_double_count(self) -> None:
        """The byte cursor plus the stored cumulative total must be exact."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            codex_root = root / ".codex"
            rollout = codex_root / "sessions" / "2026" / "01" / "02" / "rollout-inc.jsonl"
            t = 1_700_000_000_000
            self.append_codex_rollout(rollout, [
                self.codex_meta(0, session_id="s3", id="s3", cwd=r"d:\work\inc"),
                self.codex_turn_context(1, "gpt-5.6-terra"),
                self.codex_token_count(2, t + 1000, self.codex_usage(1000, 400, 100)),
            ])
            monitor = self.codex_monitor(root, codex_root)
            try:
                self.assertEqual(monitor.sync().records_seen, 1)
                self.assertEqual(monitor.sync().records_seen, 0)
                self.append_codex_rollout(rollout, [
                    self.codex_token_count(3, t + 2000, self.codex_usage(1600, 900, 150)),
                ])
                # The first delta after the cursor still needs the stored
                # baseline, otherwise it would be recorded as the full total.
                self.assertEqual(monitor.sync().records_seen, 1)
                rows = monitor.store.conn.execute(
                    "SELECT input_tokens,cache_read_tokens,output_tokens FROM usage_events "
                    "WHERE source='codex' ORDER BY event_time"
                ).fetchall()
                self.assertEqual([tuple(r) for r in rows], [(600, 400, 100), (100, 500, 50)])
                self.assertEqual(monitor.store.summary(0, t + 10_000)["total_with_cache"], 1600 + 150)
            finally:
                monitor.close()

    def test_codex_full_reconcile_regenerates_the_same_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            codex_root = root / ".codex"
            rollout = codex_root / "sessions" / "2026" / "01" / "02" / "rollout-full.jsonl"
            t = 1_700_000_000_000
            self.append_codex_rollout(rollout, [
                self.codex_meta(0, session_id="s4", id="s4", cwd=r"d:\work\full"),
                self.codex_turn_context(1, "gpt-5.6-terra"),
                self.codex_token_count(2, t + 1000, self.codex_usage(1000, 0, 100)),
                self.codex_token_count(3, t + 2000, self.codex_usage(2000, 0, 200)),
            ])
            monitor = self.codex_monitor(root, codex_root)
            try:
                monitor.sync()
                before = monitor.store.summary(0, t + 10_000)
                self.assertEqual(before["total_with_cache"], 2200)
                self.assertEqual(before["requests"], 2)
                # Force the periodic full reconcile, which ignores every cursor.
                monitor.store.set_meta("last_full_reconcile", "0")
                monitor.store.conn.commit()
                monitor.sync()
                after = monitor.store.summary(0, t + 10_000)
                self.assertEqual(after["total_with_cache"], before["total_with_cache"])
                self.assertEqual(after["requests"], before["requests"])
            finally:
                monitor.close()

    def test_codex_rollouts_of_one_session_are_keyed_per_file(self) -> None:
        """A resumed or forked thread gets its own rollout; both must count."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            codex_root = root / ".codex"
            base = codex_root / "sessions" / "2026" / "01" / "02"
            t = 1_700_000_000_000
            # Both files belong to session "shared" and both restart ordinal at 0.
            self.append_codex_rollout(base / "rollout-shared.jsonl", [
                self.codex_meta(0, session_id="shared", id="shared", cwd=r"d:\work\fork"),
                self.codex_turn_context(1, "gpt-5.6-terra"),
                self.codex_token_count(2, t + 1000, self.codex_usage(1000, 0, 100)),
            ])
            self.append_codex_rollout(base / "rollout-shared_fork.jsonl", [
                self.codex_meta(0, session_id="shared", id="shared", cwd=r"d:\work\fork"),
                self.codex_turn_context(1, "gpt-5.6-terra"),
                self.codex_token_count(2, t + 2000, self.codex_usage(700, 0, 70)),
            ])
            monitor = self.codex_monitor(root, codex_root)
            try:
                self.assertEqual(monitor.sync().records_seen, 2)
                summary = monitor.store.summary(0, t + 10_000)
                self.assertEqual(summary["requests"], 2)
                self.assertEqual(summary["total_with_cache"], (1000 + 100) + (700 + 70))
            finally:
                monitor.close()

    def test_codex_reads_archived_sessions_and_ignores_rotated_backups(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            codex_root = root / ".codex"
            t = 1_700_000_000_000
            self.append_codex_rollout(
                codex_root / "archived_sessions" / "rollout-old.jsonl",
                [
                    self.codex_meta(0, session_id="old", id="old", cwd=r"d:\work\old"),
                    self.codex_turn_context(1, "gpt-5.6-terra"),
                    self.codex_token_count(2, t + 1000, self.codex_usage(1000, 0, 100)),
                ],
            )
            # A rollout that was rotated away is a stale copy, not extra usage.
            self.append_codex_rollout(
                codex_root / "sessions" / "2026" / "01" / "02" / "rollout-new.jsonl.bak-20260102-030405",
                [
                    self.codex_meta(0, session_id="new", id="new", cwd=r"d:\work\new"),
                    self.codex_turn_context(1, "gpt-5.6-terra"),
                    self.codex_token_count(2, t + 1000, self.codex_usage(9999, 0, 999)),
                ],
            )
            monitor = self.codex_monitor(root, codex_root)
            try:
                self.assertEqual(monitor.sync().records_seen, 1)
                health = monitor.store.source_health_report(monitor.source_health)
                self.assertTrue(health["codex"]["available"])
                self.assertEqual(monitor.store.summary(0, t + 10_000)["total_with_cache"], 1100)
            finally:
                monitor.close()

    def test_codex_skips_unusable_lines_without_failing_the_sync(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            codex_root = root / ".codex"
            base = codex_root / "sessions" / "2026" / "01" / "02"
            t = 1_700_000_000_000
            self.append_codex_rollout(base / "rollout-bad.jsonl", [
                self.codex_meta(0, session_id="s5", id="s5", cwd=r"d:\work\bad"),
                self.codex_turn_context(1, "gpt-5.6-terra"),
                {"type": "response_item", "ordinal": 2, "payload": {"type": "message"}},
                {"type": "event_msg", "ordinal": 3, "payload": {"type": "token_count", "info": None}},
                {"type": "event_msg", "ordinal": 4, "payload": {"type": "agent_message"}},
                # An unusable stamp falls back to the session start, so the
                # usage is kept instead of being filed under the current day.
                {"type": "event_msg", "ordinal": 5, "timestamp": "not-a-date",
                 "payload": {"type": "token_count",
                             "info": {"total_token_usage": self.codex_usage(4000, 0, 400)}}},
                self.codex_token_count(6, t + 1000, self.codex_usage(5000, 0, 500)),
            ])
            with (base / "rollout-bad.jsonl").open("a", encoding="utf-8") as handle:
                handle.write("{not json\n")
            # With no session start either, there is no honest time to file the
            # event under, so the record is dropped rather than invented.
            self.append_codex_rollout(base / "rollout-nostamp.jsonl", [
                {"type": "turn_context", "ordinal": 0, "payload": {"model": "gpt-5.6-terra"}},
                {"type": "event_msg", "ordinal": 1, "timestamp": "not-a-date",
                 "payload": {"type": "token_count",
                             "info": {"total_token_usage": self.codex_usage(7000, 0, 700)}}},
            ])
            monitor = self.codex_monitor(root, codex_root)
            try:
                self.assertEqual(monitor.sync().records_seen, 2)
                # The window is wide because the fallback stamp is the session
                # start, which sits outside the other tests' fixed range.
                summary = monitor.store.summary(0, 4_000_000_000_000)
                self.assertEqual(summary["requests"], 2)
                # The first usable event carries the whole running total, the
                # next one only the increment on top of it.
                self.assertEqual(summary["total_with_cache"], (4000 + 400) + (1000 + 100))
                # The malformed trailing line must not abort the pass.
                self.assertTrue(monitor.store.source_health_report(monitor.source_health)["codex"]["available"])
            finally:
                monitor.close()

    def test_codex_without_ordinal_keys_by_consumed_event(self) -> None:
        """Older rollouts carry no line counter, so the index must still be exact."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            codex_root = root / ".codex"
            rollout = codex_root / "sessions" / "2026" / "01" / "02" / "rollout-noord.jsonl"
            t = 1_700_000_000_000

            def token_count(usage: dict) -> dict:
                return {
                    "timestamp": "2026-01-02T03:05:00.000Z",
                    "type": "event_msg",
                    "payload": {"type": "token_count", "info": {"total_token_usage": usage}},
                }

            self.append_codex_rollout(rollout, [
                self.codex_meta(0, session_id="s6", id="s6", cwd=r"d:\work\noord"),
                self.codex_turn_context(1, "gpt-5.6-terra"),
                token_count(self.codex_usage(1000, 0, 100)),
                # A repeat sits between the two real events; it must not make
                # the second one reuse the first one's id.
                token_count(self.codex_usage(1000, 0, 100)),
                token_count(self.codex_usage(1500, 0, 150)),
            ])
            monitor = self.codex_monitor(root, codex_root)
            try:
                self.assertEqual(monitor.sync().records_seen, 2)
                rows = monitor.store.conn.execute(
                    "SELECT message_id,input_tokens FROM usage_events WHERE source='codex' ORDER BY event_time"
                ).fetchall()
                self.assertEqual([tuple(r) for r in rows], [
                    ("codex:rollout-noord:0", 1000),
                    ("codex:rollout-noord:2", 500),
                ])
            finally:
                monitor.close()

    def test_codex_root_detection_prefers_codex_home_env(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            home = root / "codex-home"
            (home / "sessions").mkdir(parents=True)
            original = os.environ.get("CODEX_HOME")
            os.environ["CODEX_HOME"] = str(home)
            try:
                self.assertEqual(module.detect_codex_root({}), home.resolve())
                self.assertEqual(module.detect_codex_root({"codex_root": str(home)}), home.resolve())
                # An explicit but missing directory falls through to the env.
                self.assertEqual(module.detect_codex_root({"codex_root": str(root / "nope")}), home.resolve())
                self.assertIsNone(module.detect_codex_root({"codex_enabled": False}))
            finally:
                if original is None:
                    os.environ.pop("CODEX_HOME", None)
                else:
                    os.environ["CODEX_HOME"] = original

    def test_codex_process_detection_names(self) -> None:
        self.assertTrue(module.is_codex_process_name("Codex.exe"))
        self.assertTrue(module.is_codex_process_name("CODEX.EXE"))
        self.assertFalse(module.is_codex_process_name("AgentTokenMonitor.exe"))
        self.assertIn(module.CODEX_SOURCE, module.SOURCE_ORDER)
        self.assertEqual(module.SOURCE_LABELS[module.CODEX_SOURCE], "Codex")

    def test_rankings_show_five_with_paging_arrows(self) -> None:
        """The provider and model rankings list the top five, then page.

        A page turn repaints from the rows already in memory, so it never
        re-queries. The bar scale deliberately comes from the whole list rather
        than the visible page, otherwise the last page would show its smallest
        entry as a full bar.
        """
        html = (Path(module.__file__).parent / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn("const RANK_PAGE_SIZE = 5;", html)
        for which in ("provider", "model"):
            self.assertIn(f'data-rank="{which}"', html)
            self.assertIn(f'data-rank-page="{which}"', html)
        self.assertEqual(html.count('data-rank-step="-1"'), 2)
        self.assertEqual(html.count('data-rank-step="1"'), 2)
        # Scaled over every row, not over the page.
        self.assertIn("Math.max(1, ...rows.map(r => Number(r.total_with_cache || 0)))", html)
        self.assertNotIn("Math.max(1, ...top.map(r => Number(r.total_with_cache || 0)))", html)
        # The page is always clamped into range.
        self.assertIn("Math.max(0, Math.min(Number(state.rankPage?.[rank] ?? 0) || 0, pages - 1))", html)
        # A turn repaints without going back to the backend.
        self.assertIn("if(state.data)renderCurrent();", html)
        self.assertIn("$$(\"[data-rank-step]\").forEach", html)
        # Changing what is listed starts back at the first page.
        for handler in (
            'state.period=button.dataset.period;state.rankPage={provider:0,model:0};',
            'state.rankPage={provider:0,model:0};$("#model-filter").value="全部模型";',
            'state.rankPage={provider:0,model:0};$("#provider-filter").value=state.provider||"全部供应商";',
            'state.rankPage={provider:0,model:0};$("#provider-filter").value="全部供应商";',
            'state.rankPage={provider:0,model:0};$("#provider-filter").value="全部供应商";$("#model-filter").value="全部模型";$("#source-filter").value="";',
        ):
            self.assertIn(handler, html)

    def test_model_filter_still_selects_usage(self) -> None:
        """Filtering by model is the feature the ranking paging must not cost.

        The model dropdown resolves its label back to a provider, model and
        variant on the backend, and the model filter is the only control that
        narrows the figures down to one model.
        """
        html = (Path(module.__file__).parent / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn('id="model-filter"', html)
        self.assertIn(
            '$("#model-filter").addEventListener("change",e=>{state.model=e.target.value==="全部模型"?"":e.target.value;',
            html,
        )
        self.assertIn(
            "const data=await callApi(\"get_dashboard\",state.period,state.provider,state.model,",
            html,
        )
        with tempfile.TemporaryDirectory() as temp:
            store = module.Store(Path(temp))
            try:
                t = 1_700_000_000_000
                for message_id, provider, model, tokens in (
                    ("a", "openai", "gpt-x", 1000),
                    ("b", "openai", "other", 500),
                    ("c", "custom", "gpt-x", 250),
                ):
                    store.upsert_event(module.UsageRecord(
                        message_id=message_id, source="v2", source_rank=2, session_id="s",
                        project_id="p", project_name="p", project_path="p", provider_id=provider,
                        model_id=model, variant="max", agent="", event_time=t,
                        source_created=t, source_updated=t, input_tokens=tokens,
                        output_tokens=0, reasoning_tokens=0, cache_read_tokens=0,
                        cache_write_tokens=0, cost=0.0,
                    ))
                store.conn.commit()
                # The label is the identity the frontend sends back.
                labels = [item["label"] for item in store.filter_options()["models"]]
                self.assertIn("openai/gpt-x · max", labels)
                # Selecting it narrows the figures to that provider's model.
                key = next(
                    (item["provider"], item["model"], item["variant"])
                    for item in store.filter_options()["models"]
                    if item["label"] == "openai/gpt-x · max"
                )
                picked = store.analytics(t - 1000, t + 1000, model_key=key)
                self.assertEqual(picked["summary"]["requests"], 1)
                self.assertEqual(picked["summary"]["total_with_cache"], 1000)
                self.assertEqual(store.analytics(t - 1000, t + 1000)["summary"]["requests"], 3)
            finally:
                store.close()

    def test_source_filter_is_additive_and_selective(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = module.Store(Path(temp))
            try:
                t = 1_700_000_000_000
                for message_id, source, provider in (
                    ("v2-a", "v2", "opencode"),
                    ("v1-a", "v1", "anthropic"),
                    ("workbuddy-a", "workbuddy", "workbuddy"),
                    ("dsh-a", "dsh", "bupt"),
                ):
                    store.upsert_event(module.UsageRecord(
                        message_id=message_id, source=source, source_rank=1,
                        session_id="s", project_id="p", project_name="p", project_path="p",
                        provider_id=provider, model_id="m", variant="default", agent="",
                        event_time=t, source_created=t, source_updated=t,
                        input_tokens=100, output_tokens=0, reasoning_tokens=0,
                        cache_read_tokens=0, cache_write_tokens=0, cost=0.0,
                    ))
                store.conn.commit()
                # An empty selection must not narrow anything.
                self.assertEqual(store.analytics(t - 1000, t + 1000)["summary"]["requests"], 4)
                self.assertEqual(len(store.costing_rows(t - 1000, t + 1000)), 4)
                # "opencode" covers both the v1 and v2 database layouts.
                self.assertEqual(store.analytics(t - 1000, t + 1000, source="opencode")["summary"]["requests"], 2)
                self.assertEqual(store.analytics(t - 1000, t + 1000, source="workbuddy")["summary"]["requests"], 1)
                self.assertEqual(store.analytics(t - 1000, t + 1000, source="dsh")["summary"]["requests"], 1)
                self.assertEqual(store.source_clause("")[0], "")
                values = {item["value"]: item["events"] for item in store.source_options()}
                self.assertEqual(values["opencode"], 2)
                self.assertEqual(values["workbuddy"], 1)
                self.assertEqual(values["dsh"], 1)
            finally:
                store.close()

    def test_alert_wording_is_not_hardcoded_to_one_product(self) -> None:
        """Alerts aggregate all sources, so the wording must not claim one."""
        with tempfile.TemporaryDirectory() as temp:
            store = module.Store(Path(temp))
            try:
                t = 1_700_000_000_000
                for source, provider in (("workbuddy", "workbuddy"), ("v2", "opencode")):
                    store.upsert_event(module.UsageRecord(
                        message_id=f"{source}-1", source=source, source_rank=1,
                        session_id="s", project_id="p", project_name="p", project_path="p",
                        provider_id=provider, model_id="m", variant="default", agent="",
                        event_time=t, source_created=t, source_updated=t,
                        input_tokens=1000, output_tokens=0, reasoning_tokens=0,
                        cache_read_tokens=0, cache_write_tokens=0, cost=0.0,
                    ))
                store.conn.commit()
                self.assertEqual(store.sources_in_range(t - 1, t + 1), ["v2", "workbuddy"])
            finally:
                store.close()

        source = (Path(module.__file__).parent / "agent_token_monitor.py").read_text(encoding="utf-8")
        for template in ("今日 OpenCode Token 已达到", "分钟 OpenCode Token 突增"):
            self.assertNotIn(template, source)
        self.assertIn("_alert_scope_label", source)

    def test_source_health_surfaces_unreadable_sources(self) -> None:
        """A schema change must be visible, not reported as no usage."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bad_db = root / "opencode.db"
            con = sqlite3.connect(bad_db)
            con.execute("CREATE TABLE something_new(x TEXT)")
            con.commit()
            con.close()
            config = dict(module.DEFAULT_CONFIG)
            config["opencode_db"] = str(bad_db)
            config["workbuddy_enabled"] = False
            config["dsh_enabled"] = False
            config["codex_enabled"] = False
            monitor = module.Monitor(config, root / "monitor", module.logging.getLogger("test-health"))
            try:
                monitor.sync()
                health = monitor.store.source_health_report(monitor.source_health)
                self.assertFalse(health["opencode"]["available"])
                self.assertEqual(health["opencode"]["reason"], "schema_unrecognized")
                self.assertTrue(health["workbuddy"]["available"])
            finally:
                monitor.close()

    def test_codex_not_installed_is_reported_not_silently_empty(self) -> None:
        """A missing Codex home must show up as a probe result, not as no usage."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            empty_source = root / "opencode.db"
            sqlite3.connect(empty_source).close()
            config = dict(module.DEFAULT_CONFIG)
            config["opencode_db"] = str(empty_source)
            config["workbuddy_enabled"] = False
            config["dsh_enabled"] = False
            config["codex_enabled"] = True
            config["codex_root"] = str(root / "no-such-codex-home")
            original = os.environ.pop("CODEX_HOME", None)
            try:
                monitor = module.Monitor(config, root / "monitor", module.logging.getLogger("test-codex-none"))
                try:
                    self.assertIsNone(monitor.codex_root)
                    monitor.sync()
                    health = monitor.store.source_health_report(monitor.source_health)
                    self.assertFalse(health["codex"]["available"])
                    self.assertEqual(health["codex"]["reason"], "not_found")
                finally:
                    monitor.close()
            finally:
                if original is not None:
                    os.environ["CODEX_HOME"] = original

    def test_product_identity_is_multi_agent(self) -> None:
        """The 4.0 rename must drop the OpenCode-only branding."""
        self.assertEqual(module.APP_NAME, "Agent Token Monitor")
        self.assertEqual(module.DATA_DIR_NAME, "AgentTokenMonitor")
        self.assertEqual(module.app_data_dir().name, "AgentTokenMonitor")
        self.assertEqual(module.legacy_data_dir().name, "OpenCodeTokenMonitor")
        # Mutex names must change too, or a stale and a new process could
        # write the same database at once.
        self.assertIn("AgentTokenMonitor", module.MUTEX_NAME)
        self.assertIn("AgentTokenMonitor", module.SYNC_MUTEX_NAME)
        self.assertNotIn("OpenCodeTokenMonitor", module.MUTEX_NAME)

    def test_legacy_data_directory_is_migrated_once(self) -> None:
        """History must survive the rename and must not be imported twice."""
        original_target = module.app_data_dir
        original_legacy = module.legacy_data_dir
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            legacy = root / "OpenCodeTokenMonitor"
            target = root / "AgentTokenMonitor"
            legacy.mkdir()
            (legacy / "monitor.db").write_bytes(b"history")
            (legacy / "config.json").write_text('{"ui_theme":"light"}', encoding="utf-8")
            (legacy / "csv").mkdir()
            (legacy / "csv" / "usage_events.csv").write_text("a,b\n1,2\n", encoding="utf-8")
            module.app_data_dir = lambda: target
            module.legacy_data_dir = lambda: legacy
            try:
                self.assertEqual(module.migrate_legacy_data(), str(target))
                self.assertTrue((target / "monitor.db").is_file())
                self.assertTrue((target / "config.json").is_file())
                self.assertTrue((target / "csv" / "usage_events.csv").is_file())
                self.assertTrue((target / ".migrated-from-opencodetokenmonitor").is_file())
                # A second import must not overwrite what is already there.
                (target / "config.json").write_text('{"ui_theme":"dark"}', encoding="utf-8")
                self.assertIsNone(module.migrate_legacy_data())
                self.assertEqual(
                    (target / "config.json").read_text(encoding="utf-8"), '{"ui_theme":"dark"}'
                )
            finally:
                module.app_data_dir = original_target
                module.legacy_data_dir = original_legacy

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            module.app_data_dir = lambda: root / "AgentTokenMonitor"
            module.legacy_data_dir = lambda: root / "OpenCodeTokenMonitor"
            try:
                self.assertIsNone(module.migrate_legacy_data())
            finally:
                module.app_data_dir = original_target
                module.legacy_data_dir = original_legacy

    def test_sqlite_migration_survives_an_open_source(self) -> None:
        """A plain file copy can yield an empty database; the backup API cannot."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "monitor.db"
            con = sqlite3.connect(source)
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("CREATE TABLE t(x INTEGER)")
            con.executemany("INSERT INTO t VALUES(?)", [(i,) for i in range(500)])
            con.commit()
            # Leave the connection open, like a running tray would.
            destination = root / "copy.db"
            module._copy_sqlite(source, destination)
            check = sqlite3.connect(destination)
            try:
                self.assertEqual(check.execute("SELECT COUNT(*) FROM t").fetchone()[0], 500)
            finally:
                check.close()
                con.close()

    # --- startup resilience --------------------------------------------------

    def test_dashboard_asset_is_verified_before_the_window_opens(self) -> None:
        """A missing or truncated asset must not become a silent black window."""
        original = module.resource_path
        try:
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                module.resource_path = lambda rel: root / rel
                with self.assertRaises(RuntimeError):
                    module.load_dashboard_html()
                (root / "dashboard.html").write_text("<html><body>tiny</body></html>", encoding="utf-8")
                with self.assertRaises(RuntimeError):
                    module.load_dashboard_html()
                (root / "dashboard.html").write_text(
                    "<html><body>" + ("x" * 6000) + "</body></html>", encoding="utf-8"
                )
                self.assertIn("<body>", module.load_dashboard_html())
        finally:
            module.resource_path = original

    def test_dashboard_branding_matches_the_renamed_product(self) -> None:
        """The rendered title and eyebrow must not keep the old product name."""
        html = (Path(module.__file__).parent / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn("<title>Agent Token Monitor</title>", html)
        self.assertIn('<div class="eyebrow">Agent Token Monitor</div>', html)
        self.assertNotIn("OpenCode Token Monitor", html)
        self.assertNotIn("opencode_token_monitor", html)

    def test_dashboard_lock_is_versioned_and_never_fails_silently(self) -> None:
        """An older build must not be able to lock out the upgraded one.

        close_stale_dashboards removes the old window but cannot stop its
        process, which keeps holding the lock for as long as it lives. With a
        shared name the new build found no window to restore, deferred to a lock
        it could never take, and returned success without ever opening anything.
        """
        self.assertIn(module.VERSION, module.DASHBOARD_MUTEX_NAME)
        self.assertIn("AgentTokenMonitor", module.DASHBOARD_MUTEX_NAME)
        source = (Path(module.__file__).parent / "agent_token_monitor.py").read_text(encoding="utf-8")
        # Deferring with no window to restore must tell the user, not exit 0
        # quietly.
        self.assertIn("no window to restore", source)
        self.assertNotIn('logger.info("Another Dashboard instance holds the lock; deferring to it")', source)

    def test_dashboard_has_a_dedicated_single_instance_lock(self) -> None:
        self.assertTrue(module.DASHBOARD_MUTEX_NAME)
        self.assertNotEqual(module.DASHBOARD_MUTEX_NAME, module.MUTEX_NAME)
        self.assertNotEqual(module.DASHBOARD_MUTEX_NAME, module.SYNC_MUTEX_NAME)
        self.assertIn("AgentTokenMonitor", module.DASHBOARD_MUTEX_NAME)

    def test_dashboard_reports_ui_failures_instead_of_going_blank(self) -> None:
        """A bridge failure or script error must render a visible message."""
        html = (Path(module.__file__).parent / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn("function showFatal(", html)
        # The panel is created on demand, so the id is assigned in script.
        self.assertIn('box.id = "fatal-panel"', html)
        self.assertIn("fatal-panel", html)
        self.assertIn("__bridgeWatchdog", html)
        self.assertIn('window.addEventListener("error"', html)
        self.assertIn('window.addEventListener("unhandledrejection"', html)
        self.assertIn("report_ui_error", html)
        # init must be re-entrancy safe: pywebviewready and the immediate check
        # can both fire, which used to bind every handler twice.
        self.assertIn("if (initialised) return;", html)
        self.assertIn("clearTimeout(window.__bridgeWatchdog);", html)
        self.assertTrue(hasattr(module.DashboardApi, "report_ui_error"))

    def test_stale_dashboard_cleanup_only_touches_this_program(self) -> None:
        """Cleanup must never close an unrelated window with a similar title."""
        if os.name != "nt":
            self.skipTest("Windows only")
        source = (Path(module.__file__).parent / "agent_token_monitor.py").read_text(encoding="utf-8")
        self.assertIn("def close_stale_dashboards(", source)
        self.assertIn("WM_CLOSE", source)
        # The executable name has to be compared before anything is closed.
        self.assertIn("QueryFullProcessImageNameW", source)
        self.assertIn("Path(name_buf.value).name.lower()", source)
        # A non-Windows platform must not attempt the enumeration.
        self.assertEqual(module.close_stale_dashboards(None), 0)

    # --- dashboard behaviour -------------------------------------------------

    def test_dashboard_auto_refresh_follows_database_writes(self) -> None:
        """An open window must track writes made by any process.

        The redraw was keyed off "did this window trigger the sync", which is
        false whenever the tray worker had already written newer rows.
        """
        html = (Path(module.__file__).parent / "dashboard.html").read_text(encoding="utf-8")
        self.assertNotIn("if (result && result.synced) await loadView();", html)
        self.assertIn("stamp !== lastSeenSync", html)
        self.assertIn("lastSeenSync=Number(data.runtime?.last_sync||0);", html)
        self.assertIn("FALLBACK_RELOAD_MS", html)
        self.assertIn("staleView", html)
        # A visible window must not inherit the tray's long sync interval.
        self.assertEqual(module.DASHBOARD_SYNC_MAX_AGE_SECONDS, 20)
        self.assertLess(module.DASHBOARD_SYNC_MAX_AGE_SECONDS, 60)

    def test_source_dropdown_keeps_every_backend_source(self) -> None:
        """A source must stay selected instead of snapping back to all.

        The option value used to hold the display label, so a machine key that
        differed from its label (dsh vs "DeepSeek Harness") never matched.
        """
        html = (Path(module.__file__).parent / "dashboard.html").read_text(encoding="utf-8")
        self.assertNotIn("function sourceLabel(value) {", html)
        self.assertIn("function sourceLabel(value, options)", html)
        self.assertIn("options?.sources || []).find(x => x.value === value)", html)
        self.assertIn('state.source=e.target.value||""', html)
        self.assertNotIn("find(x=>x.label===label)", html)
        self.assertIn('<select id="source-filter" aria-label="数据来源"><option value="">', html)

        store = module.Store(module.app_data_dir())
        try:
            labels = {item["value"]: item["label"] for item in store.source_options()}
        finally:
            store.close()
        for key in module.SOURCE_ORDER:
            if key in labels:
                self.assertTrue(labels[key])
        if "dsh" in labels:
            self.assertEqual(labels["dsh"], "DeepSeek Harness")

    def test_control_bar_wraps_and_never_scrolls(self) -> None:
        """Controls must wrap, never hide behind a scroll area."""
        html = (Path(module.__file__).parent / "dashboard.html").read_text(encoding="utf-8")
        style = html.partition("<style>")[2].partition("</style>")[0]
        bar = style.partition(".control-bar {")[2].partition("}")[0]
        self.assertIn("flex-wrap: wrap", bar)
        self.assertNotIn("overflow-x: auto", bar)
        self.assertIn(".segmented::-webkit-scrollbar { display: none; }", style)
        self.assertNotIn("minmax(0,1fr) minmax(0,1fr) auto auto", style)
        self.assertNotIn("repeat(auto-fit, minmax(150px, 1fr))", style)
        # The source switcher must survive compact mode, and it carries no visible
        # label in any layout: the dropdown already reads "全部来源" or the chosen
        # agent's name, so a second "来源" in front of it added nothing, and in
        # compact mode it was the one label left standing next to a bare select.
        self.assertIn('body[data-layout="compact"] #source-field { display: flex;', html)
        self.assertNotIn("filter-field-label", html)
        # The name moves to the control itself rather than being dropped.
        self.assertIn('<select id="source-filter" aria-label="数据来源">', html)

    def test_restoring_the_window_returns_to_its_opening_size(self) -> None:
        """The way back off the single card goes to the default size.

        Maximising would not be the way back: the reader came from the ordinary
        page, so that is the size to return to. The size is recomputed the same
        way it was at startup, so a screen too small for it is still respected.
        """
        import agent_token_monitor as m

        class FakeWindow:
            def __init__(self) -> None:
                self.calls: list = []

            def restore(self) -> None:
                self.calls.append("restore")

            def resize(self, width: int, height: int) -> None:
                self.calls.append(("resize", width, height))

            def maximize(self) -> None:
                self.calls.append("maximize")

        window = FakeWindow()
        original = m.DASHBOARD_WINDOW
        m.DASHBOARD_WINDOW = window
        try:
            result = m.DashboardApi(Path("config.json")).restore_window()
        finally:
            m.DASHBOARD_WINDOW = original

        self.assertEqual(result, {"ok": True})
        # A window that was maximised has to come back from maximised too, or the
        # resize is applied to a maximised window and ignored.
        self.assertEqual(window.calls[0], "restore")
        self.assertEqual(window.calls[1][0], "resize")
        self.assertEqual(
            window.calls[1][1:],
            m._fit_window_to_screen(m.DEFAULT_WINDOW_WIDTH, m.DEFAULT_WINDOW_HEIGHT),
        )
        self.assertNotIn("maximize", window.calls)

        # With no window there is nothing to restore, and saying so is better than
        # raising into the page.
        m.DASHBOARD_WINDOW = None
        self.assertEqual(m.DashboardApi(Path("config.json")).restore_window(), {"ok": False})

    def test_the_smallest_window_shows_one_card_of_figures(self) -> None:
        """Below the mini threshold the chrome goes and one card fills the window.

        The window has a hard minimum size, and at that size the compact layout
        still could not show the figures: the title, the period row, the filter
        row and the tabs each wrapped onto their own lines and pushed the hero
        card below the fold. The reader dragging the window smaller was left
        scrolling for the numbers they had come for.
        """
        html = (Path(module.__file__).parent / "dashboard.html").read_text(encoding="utf-8")
        mini = html.split('body[data-layout="mini"] { overflow: hidden; }')[1].split("@media")[0]
        self.assertTrue(mini.strip(), "no mini layout rules found")
        rules = mini
        # Everything that is not the card is taken out of the way. The detail view is
        # matched by id, since its class is just "view" and ".detail-view" would
        # match nothing.
        for selector in (".topbar", ".control-bar", ".view-tabs", ".compact-breakdown-grid",
                         ".compact-chart-card", ".pulse-strip", ".full-overview", "footer",
                         "#detail-view"):
            self.assertIn(f'body[data-layout="mini"] {selector}', rules,
                          f"{selector} is not hidden in the mini layout")
        # And the card is given the whole window rather than left at its size. The full
        # overview has to go too: left up alongside the compact one, the wide
        # full-layout card shows through and overflows the narrow window.
        self.assertIn('body[data-layout="mini"] .full-overview,', rules)
        self.assertIn("body[data-layout=\"mini\"] .compact-overview { display: block; }", rules)
        self.assertIn("body[data-layout=\"mini\"] .compact-hero { flex: 1 1 auto;", rules)
        # The card carries no trend, so the hint to hover one is advice the reader
        # cannot act on. Left in, it wraps over three lines in the width the two
        # buttons leave and pushes the figures down.
        self.assertIn(
            'caption.textContent = mini ? "当前周期 Token" : "当前周期 Token · 悬停趋势查看详情";',
            html,
        )
        # No scrolling at that size, since there is nothing below to scroll to.
        self.assertIn('body[data-layout="mini"] { overflow: hidden; }', html)
        # The mini threshold sits below the compact one, so a normal compact
        # window is unaffected. Both dimensions have to be small: a narrow window
        # on a tall screen still has room for the ordinary page.
        self.assertIn('if (width < MINI_WIDTH && height < MINI_HEIGHT) return "mini";', html)
        self.assertNotIn('if (width < 480 || height < 520) return "mini";', html)
        self.assertIn('if (width < 760 || height < 680) return "compact";', html)
        # The card carries its own refresh and its own way back, because at that
        # size it is the only thing on screen. The way back returns to the size
        # the window opens at rather than maximising: that is where the reader
        # came from. The expand button stays for the ordinary compact layout.
        self.assertIn('id="mini-refresh"', html)
        self.assertIn('id="mini-restore"', html)
        self.assertIn('class="mini-actions"', html)
        self.assertIn('.mini-actions { display: none;', html)
        self.assertIn('body[data-layout="mini"] .mini-actions { display: flex;', html)
        self.assertIn('body[data-layout="mini"] #expand-button { display: none; }', html)
        self.assertIn('$("#mini-restore").addEventListener("click",()=>callApi("restore_window"));', html)
        # One refresh routine behind both buttons, so they cannot drift apart in
        # what they do or in how they report being busy.
        self.assertIn("const refreshButtons = () => [$(\"#refresh-button\"), $(\"#mini-refresh\")].filter(Boolean);", html)
        self.assertIn("async function refreshNow()", html)
        self.assertIn("const busyRefresh = (show) => refreshButtons().forEach(b => b.classList.toggle(\"busy\", !!show));", html)
        self.assertIn('refreshButtons().forEach(button=>button.addEventListener("click",refreshNow));', html)
        # Charts are not on screen at that size, so they are not drawn into.
        self.assertIn("if(!state.mini){", html)
        self.assertIn("if (state.data?.trend && !mini) scheduleChart();", html)

    def test_token_share_readout_is_rendered_next_to_the_bar(self) -> None:
        """The composition bar shows each segment's percentage inline."""
        html = (Path(module.__file__).parent / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn('id="token-share"', html)
        self.assertIn('id="compact-token-share"', html)
        self.assertIn("function renderTokenShare(", html)
        self.assertIn("renderTokenShare(tokenParts, s);", html)
        # The bar keeps a guaranteed width so the readout cannot squeeze it out.
        self.assertIn("grid-template-columns: auto minmax(80px, 1fr) auto minmax(0, auto);", html)
        for key in ("input", "output", "reasoning", "cache_hit"):
            self.assertIn(f".token-share .share-item i.{key} {{", html)

    def test_config_argument_is_accepted_before_and_after_subcommand(self) -> None:
        parser = module.build_parser()
        before = parser.parse_args(["--config", "before.json", "tray"])
        after = parser.parse_args(["dashboard", "--config", "after.json"])
        self.assertEqual(before.config, Path("before.json"))
        self.assertEqual(after.config, Path("after.json"))

    def test_interpolation(self) -> None:
        self.assertEqual(module.interpolate_color(0, 100_000_000)[:3], (34, 178, 95))
        self.assertEqual(module.interpolate_color(100_000_000, 100_000_000)[:3], (220, 38, 38))

    def test_daily_trend_buckets_are_calendar_days(self) -> None:
        """Each point is one calendar day, midnight to midnight, in local time.

        The number under a date has to be that date's usage and nothing else.
        Buckets of a rolling 24 hours were tried here and are wrong for a chart
        read this way: the bucket labelled 10-01 runs from the previous morning
        to this one, so its total has nothing to do with the day shown beside it.

        The day in progress is genuinely shorter than the others, so it is
        flagged with how much of it has elapsed rather than being padded out or
        quietly re-based.
        """
        day = 86_400_000
        with tempfile.TemporaryDirectory() as temp:
            store = module.Store(Path(temp))
            try:
                # Four events per day, at 03:00, 09:00, 15:00 and 21:00 across
                # three days, so a whole calendar day holds four.
                first = dt.datetime(2026, 3, 10, 3, 0)
                for step in range(12):
                    when = int((first + dt.timedelta(hours=6 * step)).timestamp() * 1000)
                    store.upsert_event(module.UsageRecord(
                        message_id=f"h{step}", source="v2", source_rank=2, session_id="s",
                        project_id="p", project_name="p", project_path="p",
                        provider_id="alpha", model_id="m1", variant="high", agent="",
                        event_time=when, source_created=when, source_updated=when,
                        input_tokens=100, output_tokens=0, reasoning_tokens=0,
                        cache_read_tokens=0, cache_write_tokens=0, cost=2.0,
                    ))
                store.conn.commit()
                lo = int(dt.datetime(2026, 3, 10, 0, 0).timestamp() * 1000)
                hi = int(dt.datetime(2026, 3, 13, 0, 0).timestamp() * 1000)

                rows = store.calendar_day_trend(lo, hi)
                self.assertEqual([row["bucket"] for row in rows],
                                 ["2026-03-10", "2026-03-11", "2026-03-12"])
                self.assertEqual([row["requests"] for row in rows], [4, 4, 4])

                # The bucket label is the day an event falls in, so the total
                # under a date is exactly that day's usage.
                self.assertEqual(module.day_bucket_label(lo), "2026-03-10")
                self.assertEqual(module.day_bucket_label(hi - 1), "2026-03-12")
                for row in rows:
                    day_lo = module.day_start_ms(
                        int(dt.datetime.strptime(row["bucket"], "%Y-%m-%d").timestamp() * 1000))
                    counted = store.conn.execute(
                        "SELECT COUNT(*) FROM usage_events WHERE event_time >= ? AND event_time < ?",
                        (day_lo, day_lo + day)).fetchone()[0]
                    self.assertEqual(row["requests"], counted,
                                     f"{row['bucket']} does not match a direct count of that day")

                # A window that starts partway through a day still opens at that
                # day's midnight, so no day is silently dropped.
                rows = store.calendar_day_trend(
                    int(dt.datetime(2026, 3, 11, 17, 0).timestamp() * 1000), hi)
                self.assertEqual([row["bucket"] for row in rows], ["2026-03-11", "2026-03-12"])

                # A day with no usage is kept at zero, so the line has no gap.
                rows = store.calendar_day_trend(
                    int(dt.datetime(2026, 3, 8, 0, 0).timestamp() * 1000), hi)
                self.assertEqual([row["requests"] for row in rows], [0, 0, 4, 4, 4])

                # The filters reach it, and an empty window still gives a day.
                self.assertEqual(
                    [row["requests"] for row in store.calendar_day_trend(lo, hi, provider_id="nobody")],
                    [0, 0, 0],
                )
                self.assertEqual(len(store.calendar_day_trend(lo, lo)), 0)
                self.assertEqual(len(store.calendar_day_trend(lo, lo + day)), 1)
            finally:
                store.close()

    def test_a_day_in_progress_is_reported_as_partial(self) -> None:
        """A day that has not finished says how much of it has elapsed.

        Left unmarked, a day that is nine hours old sits beside complete days and
        reads as a collapse in usage. The flag is what the hollow chart point and
        the tooltip note are drawn from.
        """
        with tempfile.TemporaryDirectory() as temp:
            store = module.Store(Path(temp))
            try:
                now = module.now_ms()
                today = module.day_start_ms(now)
                store.upsert_event(module.UsageRecord(
                    message_id="a", source="v2", source_rank=2, session_id="s",
                    project_id="p", project_name="p", project_path="p",
                    provider_id="alpha", model_id="m1", variant="high", agent="",
                    event_time=now - 60_000, source_created=now - 60_000,
                    source_updated=now - 60_000, input_tokens=10, output_tokens=0,
                    reasoning_tokens=0, cache_read_tokens=0, cache_write_tokens=0, cost=0.0,
                ))
                store.conn.commit()
                rows = store.calendar_day_trend(today - 2 * 86_400_000, today + 86_400_000)
                self.assertEqual(len(rows), 3)
                self.assertEqual([row["partial"] for row in rows], [False, False, True])
                # A finished day reports a whole 24, the day in progress its own
                # elapsed hours, and never more than a day.
                self.assertEqual(rows[0]["elapsed_hours"], 24.0)
                self.assertTrue(0 < rows[2]["elapsed_hours"] < 24.0)
                for row in rows:
                    self.assertLessEqual(row["elapsed_hours"], 24.0)
            finally:
                store.close()

    def test_daily_trend_never_shows_less_than_a_week(self) -> None:
        """A // comment must not sit in front of code on the same line.

        One did, and it ate the rest of the line: the caption was never set, the
        daily chart was never drawn, and two closing braces went with it. The
        file still parsed as far as the tests that grepped for the caption text,
        because the text was inside the comment. Only a syntax check noticed.
        """
        html = (Path(module.__file__).parent / "dashboard.html").read_text(encoding="utf-8")
        script = html.partition("<script>")[2].rpartition("</script>")[0]
        self.assertTrue(script, "the inline script could not be extracted")
        offenders = [
            (number, line.strip()[:90])
            for number, line in enumerate(script.splitlines(), 1)
            if "//" in line and not line.strip().startswith("//")
        ]
        self.assertEqual(offenders, [], f"a // comment is eating code on these lines: {offenders}")

    def test_daily_trend_never_shows_less_than_a_week(self) -> None:
        """A one-day period still shows a week of days.

        The daily trend answers how the last few days have gone, so a single day
        would be one dot. The floor is measured back from the end of the period,
        so a custom range keeps showing the week leading up to the day it ends
        on, and a longer period is left alone.
        """
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "AgentTokenMonitor"
            root.mkdir(parents=True)
            config = dict(module.DEFAULT_CONFIG)
            # No catalogue and no custom rates, so an event falls back to the
            # cost recorded on the event itself. That keeps the assertion about
            # the widened window independent of any pricing data.
            config.update({
                "opencode_db": str(root / "empty.db"),
                "use_model_dev_pricing": False,
                "pricing_mode": "custom",
                "custom_pricing": {"default": {}, "models": []},
            })
            sqlite3.connect(root / "empty.db").close()
            (root / "config.json").write_text(json.dumps(config), encoding="utf-8")

            today = dt.datetime.now().astimezone().replace(hour=10, minute=0, second=0, microsecond=0)
            store = module.Store(root)
            try:
                # One event per day for the last ten days, each carrying a cost.
                for offset in range(10):
                    when = int((today - dt.timedelta(days=offset)).timestamp() * 1000)
                    store.upsert_event(module.UsageRecord(
                        message_id=f"d{offset}", source="v2", source_rank=2, session_id="s",
                        project_id="p", project_name="p", project_path="p",
                        provider_id="alpha", model_id="m1", variant="high", agent="",
                        event_time=when, source_created=when, source_updated=when,
                        input_tokens=1000, output_tokens=0, reasoning_tokens=0,
                        cache_read_tokens=0, cache_write_tokens=0, cost=1.5,
                    ))
                store.conn.commit()
            finally:
                store.close()

            original_target = module.app_data_dir
            module.app_data_dir = lambda: root
            try:
                api = module.DashboardApi(root / "config.json")
                today_data = api.get_dashboard(period="today", view="overview")
            finally:
                module.app_data_dir = original_target

            self.assertEqual(len(today_data["daily_trend"]), module.DAILY_TREND_MIN_DAYS)
            # The series carries its own cost, which only works if the pricing
            # pass covered the days it reaches back into.
            self.assertTrue(all(row["total_cost"] > 0 for row in today_data["daily_trend"]))
            # Reaching back must not widen the period the series sits beside.
            self.assertEqual(today_data["summary"]["requests"], 1)
            self.assertEqual(today_data["summary"]["total_with_cache"], 1000)
            self.assertEqual(today_data["summary"]["total_cost"], 1.5)
            self.assertEqual(len(today_data["trend"]), 24)  # hourly, one day
            self.assertEqual(today_data["granularity"], "hour")

    def test_longer_periods_are_not_widened(self) -> None:
        """Thirty days stays thirty days; the floor only ever extends a short period."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "AgentTokenMonitor"
            root.mkdir(parents=True)
            config = dict(module.DEFAULT_CONFIG)
            config.update({
                "opencode_db": str(root / "empty.db"),
                "use_model_dev_pricing": False,
                "pricing_mode": "custom",
                "custom_pricing": {"default": {}, "models": []},
            })
            sqlite3.connect(root / "empty.db").close()
            (root / "config.json").write_text(json.dumps(config), encoding="utf-8")
            # A month of usage, one event per day.
            store = module.Store(root)
            try:
                today = dt.datetime.now().astimezone().replace(hour=10, minute=0, second=0, microsecond=0)
                for offset in range(31):
                    when = int((today - dt.timedelta(days=offset)).timestamp() * 1000)
                    store.upsert_event(module.UsageRecord(
                        message_id=f"d{offset}", source="v2", source_rank=2, session_id="s",
                        project_id="p", project_name="p", project_path="p",
                        provider_id="alpha", model_id="m1", variant="high", agent="",
                        event_time=when, source_created=when, source_updated=when,
                        input_tokens=1000, output_tokens=0, reasoning_tokens=0,
                        cache_read_tokens=0, cache_write_tokens=0, cost=1.5,
                    ))
                store.conn.commit()
            finally:
                store.close()

            original_target = module.app_data_dir
            module.app_data_dir = lambda: root
            try:
                api = module.DashboardApi(root / "config.json")
                thirty = api.get_dashboard(period="30d", view="overview")
                seven = api.get_dashboard(period="7d", view="overview")
                week = api.get_dashboard(period="custom", custom_start="2026-09-01", custom_end="2026-09-03",
                                         view="overview")
            finally:
                module.app_data_dir = original_target

            # Thirty days is still thirty points, the week floor is still seven,
            # and a short custom range reaches back to a week.
            self.assertEqual(len(thirty["daily_trend"]), 30)
            self.assertEqual(len(seven["daily_trend"]), 7)
            self.assertEqual(len(week["daily_trend"]), module.DAILY_TREND_MIN_DAYS)
            # A custom range that ends in the past stays in the past, and its
            # last bucket is the last day the range covers rather than the day
            # after it.
            self.assertEqual(week["daily_trend"][-1]["bucket"], "2026-09-03")
            # Nothing in a period that closed days ago is still filling.
            self.assertFalse(any(row["partial"] for row in week["daily_trend"]))

    def test_daily_trend_costs_ride_the_existing_pricing_pass(self) -> None:
        """The daily rows must carry the same cost fields as the trend beside them.

        They are filled from the rows the pass already has in hand, and the bucket
        a row is costed into comes from the caller, so the two cannot disagree
        about where a bucket begins.
        """
        with tempfile.TemporaryDirectory() as temp:
            store = module.Store(Path(temp))
            try:
                anchor = int(dt.datetime(2026, 3, 10, 15, 0).timestamp() * 1000)
                for step in range(6):
                    when = anchor + step * 4 * 3_600_000
                    store.upsert_event(module.UsageRecord(
                        message_id=f"e{step}", source="v2", source_rank=2, session_id="s",
                        project_id="p", project_name="p", project_path="p",
                        provider_id="alpha", model_id="m1", variant="high", agent="",
                        event_time=when, source_created=when, source_updated=when,
                        input_tokens=1000, output_tokens=0, reasoning_tokens=0,
                        cache_read_tokens=0, cache_write_tokens=0, cost=0.0,
                    ))
                store.conn.commit()
                end = module.day_start_ms(anchor) + 2 * 86_400_000
                rows = store.costing_rows(anchor, end)
                analytics = store.analytics(anchor, end, granularity="hour", event_limit=0)
                daily_rows = store.calendar_day_trend(anchor, end)
                bucket_of = module.day_bucket_label
                module.attach_model_dev_costs(
                    analytics, rows, {}, {}, daily_rows, bucket_of,
                )
                fields = lambda row: sorted(k for k in row if "cost" in k or "pricing" in k)
                self.assertEqual(fields(daily_rows[0]), fields(analytics["trend"][0]))
                # A bucket with no events in it still reports a cost, at zero,
                # rather than being left without the fields.
                self.assertTrue(all(row["total_cost"] == 0.0 for row in daily_rows))
                self.assertTrue(all("total_cost" in row for row in daily_rows))
                # Passing no series leaves the previous behaviour untouched.
                plain = store.analytics(anchor, end, granularity="hour", event_limit=0)
                module.attach_model_dev_costs(plain, rows, {}, {})
                self.assertEqual(fields(plain["trend"][0]), fields(analytics["trend"][0]))
            finally:
                store.close()

    def test_refreshing_does_not_cover_the_page(self) -> None:
        """A reload must not blank the numbers the reader is watching.

        Refreshing raised a full-screen scrim over the whole viewport, so the
        window went blank and the existing figures disappeared while it worked.
        The wait is now a two pixel bar at the top edge, which covers nothing and
        never intercepts a click, plus the refresh button spinning on its own.

        A background reload shows neither: the page updates underneath the
        reader, which is the whole point of a counter that refreshes itself.
        """
        html = (Path(module.__file__).parent / "dashboard.html").read_text(encoding="utf-8")
        rule = re.search(r"\.loading \{[^}]*\}", html)
        self.assertIsNotNone(rule, ".loading rule not found")
        style = rule.group(0)
        # A thin bar along the top, not a sheet over the viewport.
        self.assertIn("top: 0", style)
        self.assertIn("height: 2px", style)
        self.assertNotIn("inset: 0", style)
        # It must never take clicks, in either state.
        self.assertIn("pointer-events: none", style)
        self.assertNotIn("backdrop-filter", style)
        self.assertNotIn("place-items: center", style)
        # The refresh button says it is working, and cannot be pressed twice.
        self.assertIn("const busyRefresh = (show) => refreshButtons().forEach(b => b.classList.toggle(\"busy\", !!show));", html)
        self.assertIn(".icon-button.busy { pointer-events: none;", html)
        self.assertIn(".icon-button.busy .icon { animation: spin .7s linear infinite; }", html)
        # The busy state is cleared on every path out, including a failure.
        self.assertIn("finally{if(sequence===state.sequence){showLoading(false); busyRefresh(false);}}", html)
        self.assertIn("showLoading(false); busyRefresh(false);", html)
        # A background reload is silent; only a deliberate one shows the bar.
        self.assertIn("const quiet=state.data&&!force; showLoading(!quiet);", html)
        # The deliberate path asks for it, and re-reads rather than silently
        # reusing the background reload.
        self.assertIn("showLoading(true); busyRefresh(true);", html)
        self.assertIn("await loadView(true);", html)
        # The spinner that used to be centred is no longer the loading mark.
        self.assertNotIn('<div id="loading" class="loading"><div class="loading-mark"></div></div>', html)

    def test_trend_is_drawn_as_a_monotone_curve(self) -> None:
        """The line chart bends without inventing peaks.

        A plain Catmull-Rom or ordinary Bezier spline overshoots between
        samples, which draws humps on stretches where the data is flat. The
        Fritsch-Carlson tangents are clamped, so each stretch between two points
        keeps its direction and the curve cannot reverse or leave the data range.
        """
        html = (Path(module.__file__).parent / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn("function smoothPath(points)", html)
        self.assertIn("Fritsch-Carlson", html)
        # A zero tangent breaks the ratio, so it is handled before dividing.
        self.assertIn("if (slope[i]===0){ m[i]=0; m[i+1]=0; continue; }", html)
        # Tangents are scaled down when they would loop.
        self.assertIn("if (len>3){ m[i]=3*a/len*slope[i]; m[i+1]=3*b/len*slope[i]; }", html)
        # A sign change sets the tangent to zero, which is what stops the overshoot.
        self.assertIn("m[i]=(slope[i-1]*slope[i]<=0) ? 0 : (slope[i-1]+slope[i])/2;", html)
        # Both the stroke and the fill come from the curve, not from the points.
        self.assertIn("const points = rows.map((r,i)=>[x(i),y(values[i])]); const line = smoothPath(points);", html)
        self.assertIn("const area = `${line} L${points.at(-1)[0]}", html)

    def test_overview_shows_a_daily_trend_card(self) -> None:
        """The daily trend sits on the overview in the same card style.

        It follows the period, shares the metric picker above it, and is only
        drawn in the full layout, since the compact layout has its own chart.
        """
        html = (Path(module.__file__).parent / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn('<h2 class="section-title">最近每日趋势</h2>', html)
        self.assertIn('id="daily-trend-caption"', html)
        self.assertIn('id="daily-trend-svg"', html)
        self.assertIn('id="daily-tooltip"', html)
        # The compact layout needs its own copy. On a scaled display the default
        # window lands in the compact layout, so a card that only exists in the
        # full layout is a card nobody sees at the default size.
        # The compact copy carries the same title, so the count of titles is the
        # check: one per layout. Matching title plus caption as a single string
        # would also pass if the caption sat in the wrong card.
        self.assertEqual(html.count('<h2 class="section-title">最近每日趋势</h2>'), 2)
        self.assertIn('<div id="compact-daily-caption" class="section-subtitle">每天 0–24 点</div>', html)
        self.assertIn('id="compact-daily-svg"', html)
        self.assertIn('id="compact-daily-tooltip"', html)
        self.assertIn('drawChart($("#compact-daily-svg"), $("#compact-daily-tooltip"), daily)', html)
        self.assertIn('setText("#compact-daily-caption", `每天 0–24 点 · 共 ${days?days:0} 天${note}`);', html)
        # The caption names the bucketing, and says so when the newest day is
        # still filling rather than leaving a short day to look like a drop.
        self.assertIn(
            'const note=data.daily_partial ? ` · 今天进行中（已过 ${Number(data.daily_elapsed_hours||0).toFixed(0)} 小时）` : "";',
            html,
        )
        self.assertIn('setText("#daily-trend-caption", `每天 0–24 点 · 共 ${days?days:0} 天${note} · 指标同上方`);', html)
        # A bucket the backend flagged as still filling is drawn hollow, and the
        # tooltip says how far into the day it is.
        self.assertIn("rows[i]?.partial?", html)
        self.assertIn('class="chart-point-warming"', html)
        self.assertIn("本日进行中 · 已过 ${Number(row.elapsed_hours||0).toFixed(1)} 小时，非完整一天", html)
        self.assertIn(".chart-wrap.daily-chart { height: 224px; }", html)
        # An unsized svg falls back to the 300x150 default while its viewBox is
        # measured from the full card, so the drawing gets clipped. Every chart
        # svg has to be in that rule, not just the first one that existed.
        self.assertIn(
            "#trend-svg, #daily-trend-svg, #compact-trend-svg, #compact-daily-svg "
            "{ width: 100%; height: 100%; display: block; overflow: hidden; }",
            html,
        )
        # No other chart svg may be left out of it.
        rule, _, _ = html.partition("{ width: 100%; height: 100%; display: block; overflow: hidden; }")
        rule = rule[rule.rfind("}") + 1:]
        for svg_id in re.findall(r'<svg id="([\w-]+)"', html):
            self.assertIn(f"#{svg_id}", rule, f"{svg_id} has no width/height rule")
        # Same card and head classes as the trend above it.
        self.assertRegex(
            html,
            r'<article class="card section-card">\s*<div class="section-head">\s*'
            r'<div><h2 class="section-title">最近每日趋势</h2>',
        )
        # Rendered from the series the backend sends, with its own tooltip.
        self.assertIn('drawChart($("#daily-trend-svg"), $("#daily-tooltip"), daily)', html)
        self.assertIn("const daily=data.daily_trend||[]", html)
        # The card lives in the full overview, which the compact layout hides,
        # so the compact layout carries its own copy of both charts.
        self.assertIn('body[data-layout="compact"] .full-overview { display: none; }', html)
        self.assertIn('body[data-layout="compact"] .compact-overview { display: block; }', html)
        # The layout switch is what decides, so the default window on a scaled
        # display lands in the compact layout. Both charts are drawn in each of
        # the two layouts that show them.
        self.assertIn('if (width < MINI_WIDTH && height < MINI_HEIGHT) return "mini";', html)
        self.assertIn('if (width < 760 || height < 680) return "compact";', html)
        self.assertIn('return "full";', html)
        # Just above the window's own minimum of 360x480, so the single card
        # only replaces the page when the reader has dragged it as small as it
        # goes. Anything looser would take the card away over half the screen.
        self.assertIn("const MINI_WIDTH = 370, MINI_HEIGHT = 490;", html)
        self.assertNotIn('if (width < MINI_WIDTH || height < MINI_HEIGHT) return "mini";', html)
        self.assertIn(
            'if(state.compact)drawChart($("#compact-trend-svg"), $("#compact-tooltip"), data.trend || []);',
            html,
        )
        self.assertIn('else drawChart($("#trend-svg"), $("#chart-tooltip"), data.trend || []);', html)

    def test_default_dashboard_window_is_compact_enough(self) -> None:
        self.assertEqual(module.DEFAULT_WINDOW_WIDTH, 985)
        self.assertEqual(module.DEFAULT_WINDOW_HEIGHT, 975)

    def test_dashboard_merges_cache_write_into_hit_category(self) -> None:
        html = (Path(module.__file__).parent / "dashboard.html").read_text(encoding="utf-8")
        head, _, after = html.partition('<div id="pricing-backdrop"')
        pricing, _, script = after.partition("<script>")
        self.assertTrue(pricing, "pricing modal block not found")
        ui = head + script
        self.assertNotIn("缓存写入", ui)
        self.assertNotIn("缓存读取", ui)
        self.assertNotIn("cache-write", ui)
        self.assertIn("缓存写入", pricing)
        self.assertIn("缓存读取", pricing)
        self.assertGreaterEqual(ui.count('data-cache-value="cache_hit"'), 2)
        self.assertGreaterEqual(ui.count('["cache_hit_tokens","缓存命中"'), 5)
        self.assertIn(".cache-segment.cache_hit", ui)
        self.assertNotIn(".cache-segment.cache_write", ui)
        self.assertNotIn('["cache_write"', ui)

    def test_tray_title_fits_windows_limit(self) -> None:
        status = {
            "opencode_running": True,
            "last_sync": 1_700_000_000_000,
            "today": {"total_with_cache": 123_113_574, "cache_read_tokens": 111_112_805},
        }
        title = module.status_text(status, 100_000_000)
        self.assertLessEqual(len(title), 127)
        self.assertIn("OpenCode: running", title)
        self.assertIn("Today:", title)
        self.assertNotIn("database is not queried", title)

    def test_opencode_process_detection_names(self) -> None:
        self.assertTrue(module.is_opencode_process_name("OpenCode.exe"))
        self.assertTrue(module.is_opencode_process_name("opencode-cli.exe"))
        self.assertTrue(module.is_opencode_process_name("OPENCODE.EXE"))
        self.assertFalse(module.is_opencode_process_name("OpenCodeTokenMonitor.exe"))
        self.assertIsInstance(module.opencode_is_running(), bool)

    def test_configured_database_does_not_invoke_opencode_cli(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "opencode.db"
            source.touch()
            original = module.shutil_which
            module.shutil_which = lambda _name: (_ for _ in ()).throw(AssertionError("CLI must stay idle"))
            try:
                detected = module.detect_opencode_db({"opencode_db": str(source)})
            finally:
                module.shutil_which = original
            self.assertEqual(detected, source.resolve())


if __name__ == "__main__":
    unittest.main()
