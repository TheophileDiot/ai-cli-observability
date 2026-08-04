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
            "GPT56_SOL_IN": "5",
            "GPT56_SOL_CACHED": "0.5",
            "GPT56_SOL_OUT": "30",
        }
        for name, value in expected.items():
            self.assertEqual((pricing_dir / name).read_text(), value)

        terra_prices = dict(
            exporter.db.execute(
                "SELECT direction, price_per_million FROM model_pricing"
                " WHERE model='gpt-5.6-terra'"
            )
        )
        self.assertEqual(terra_prices, {"input": 2.5, "cached": 0.25, "output": 15.0})
        sources = dict(
            exporter.db.execute(
                "SELECT model, source FROM model_pricing"
                " WHERE model IN ('gpt-5.6-sol', 'gpt-5.6-terra')"
                " GROUP BY model, source ORDER BY model"
            )
        )
        for model in ("gpt-5.6-sol", "gpt-5.6-terra"):
            expected = (
                "openai-official"
                if model in exporter.official_price_models
                else "litellm"
            )
            self.assertEqual(sources[model], expected)

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
