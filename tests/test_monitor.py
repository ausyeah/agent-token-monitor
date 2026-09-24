from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import opencode_token_monitor as module


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

    def test_config_argument_is_accepted_before_and_after_subcommand(self) -> None:
        parser = module.build_parser()
        before = parser.parse_args(["--config", "before.json", "tray"])
        after = parser.parse_args(["dashboard", "--config", "after.json"])
        self.assertEqual(before.config, Path("before.json"))
        self.assertEqual(after.config, Path("after.json"))

    def test_interpolation(self) -> None:
        self.assertEqual(module.interpolate_color(0, 100_000_000)[:3], (34, 178, 95))
        self.assertEqual(module.interpolate_color(100_000_000, 100_000_000)[:3], (220, 38, 38))

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
