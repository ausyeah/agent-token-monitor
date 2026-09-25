from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
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
            # real ~/.workbuddy or ~/.dsh directory of whoever runs the suite.
            config["workbuddy_enabled"] = False
            config["dsh_enabled"] = False
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
            monitor = module.Monitor(config, root / "monitor", module.logging.getLogger("test-health"))
            try:
                monitor.sync()
                health = monitor.store.source_health_report(monitor.source_health)
                self.assertFalse(health["opencode"]["available"])
                self.assertEqual(health["opencode"]["reason"], "schema_unrecognized")
                self.assertTrue(health["workbuddy"]["available"])
            finally:
                monitor.close()

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
        self.assertIn('<select id="source-filter"><option value="">', html)

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
        # The source switcher must survive compact mode with a visible label.
        self.assertIn('body[data-layout="compact"] #source-field { display: flex;', html)
        self.assertIn('body[data-layout="compact"] #source-field .filter-field-label { display: inline;', html)

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
