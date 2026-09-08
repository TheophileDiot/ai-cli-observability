from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

tmpdir = tempfile.TemporaryDirectory()
unittest.addModuleCleanup(tmpdir.cleanup)
root = Path(tmpdir.name)
pricing_dir = root / "pricing"
pricing_dir.mkdir()
os.environ["DB_PATH"] = str(root / "metrics.db")
os.environ["PRICING_DIR"] = str(pricing_dir)
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "true"

import exporter  # noqa: E402


class PricingTests(unittest.TestCase):
    def test_current_codex_prices_are_exported(self) -> None:
        exporter.sync_litellm_to_db()
        exporter.write_pricing_files()

        expected = {
            "GPT54_MINI_IN": "0.75",
            "GPT54_MINI_CACHED": "0.075",
            "GPT54_MINI_OUT": "4.5",
            "GPT56_SOL_IN": "4",
            "GPT56_SOL_CACHED": "0.4",
            "GPT56_SOL_OUT": "20",
            "GPT56_TERRA_IN": "2",
            "GPT56_TERRA_CACHED": "0.2",
            "GPT56_TERRA_OUT": "12",
            "GPT56_LUNA_IN": "0.2",
            "GPT56_LUNA_CACHED": "0.02",
            "GPT56_LUNA_OUT": "1.2",
            "GPT6_ASTRA_IN": "10",
            "GPT6_ASTRA_CACHED": "1",
            "GPT6_ASTRA_OUT": "50",
        }
        for name, value in expected.items():
            self.assertEqual((pricing_dir / name).read_text(), value)

        terra_prices = dict(
            exporter.db.execute(
                "SELECT direction, price_per_million FROM model_pricing"
                " WHERE model='gpt-5.6-terra'"
            )
        )
        self.assertEqual(set(terra_prices), {"input", "cached", "output"})
        for direction, price in {"input": 2.0, "cached": 0.2, "output": 12.0}.items():
            self.assertAlmostEqual(terra_prices[direction], price)
        sources = exporter.db.execute(
            "SELECT model, source FROM model_pricing"
            " WHERE model IN ('gpt-5.6-sol', 'gpt-5.6-terra')"
            " GROUP BY model, source ORDER BY model"
        ).fetchall()
        self.assertEqual(
            sources,
            [
                ("gpt-5.6-sol", "openai-official"),
                ("gpt-5.6-terra", "openai-official"),
            ],
        )

    def test_current_claude_prices_are_exported(self) -> None:
        exporter.sync_litellm_to_db()
        for model, prices in {
            "claude-fable-5-1": {"input": 10.0, "cached": 0.25, "output": 50.0},
            "claude-opus-5": {"input": 5.0, "cached": 0.5, "output": 25.0},
            "claude-sonnet-5": {"input": 2.0, "cached": 0.2, "output": 10.0},
        }.items():
            with self.subTest(model=model):
                rows = dict(
                    exporter.db.execute(
                        "SELECT direction, price_per_million FROM model_pricing"
                        " WHERE model=? AND provider='anthropic'",
                        (model,),
                    )
                )
                self.assertEqual(rows.keys(), prices.keys())
                for direction, price in prices.items():
                    self.assertAlmostEqual(rows[direction], price)

    def test_every_otel_price_has_a_model_cost(self) -> None:
        for name, (model, provider, direction) in exporter.OTEL_ENV_MAP.items():
            with self.subTest(name=name):
                info = exporter.model_cost[model]
                self.assertEqual(
                    exporter.PROVIDER_MAP[info["litellm_provider"]], provider
                )
                self.assertIsNotNone(info.get(exporter.COST_FIELDS[direction]))


if __name__ == "__main__":
    unittest.main()
