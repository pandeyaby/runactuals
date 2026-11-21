import json
import os
import tempfile
import unittest

from runactuals.prices import cost_usd, lookup, normalize_model


class PricesTest(unittest.TestCase):
    def test_longest_prefix_wins(self):
        self.assertEqual(lookup("gpt-4o-mini-2024-07-18")["input"], 0.15)
        self.assertEqual(lookup("gpt-4o-2024-08-06")["input"], 2.50)
        self.assertNotEqual(lookup("gpt-4o-mini")["input"], lookup("gpt-4o")["input"])

    def test_gemini_models_prefix_and_dated_claude(self):
        self.assertEqual(normalize_model("models/gemini-2.0-flash"), "gemini-2.0-flash")
        self.assertIsNotNone(lookup("models/gemini-2.0-flash"))
        self.assertEqual(lookup("claude-sonnet-4-20250514")["input"], 3.0)

    def test_cost_math(self):
        # 10 input + 20 output of gpt-4o-mini at 0.15 / 0.60 per 1M
        self.assertEqual(cost_usd("gpt-4o-mini", 10, 20), 10 / 1e6 * 0.15 + 20 / 1e6 * 0.60)

    def test_unknown_model_is_none(self):
        self.assertIsNone(lookup("totally-unknown-model-xyz"))
        self.assertIsNone(cost_usd("totally-unknown-model-xyz", 100, 100))

    def test_override_file(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "prices.json")
            with open(path, "w") as f:
                json.dump({"custom-model": {"input": 1.0, "output": 2.0}}, f)
            self.assertEqual(cost_usd("custom-model", 1_000_000, 1_000_000,
                                      table=__import__("runactuals.prices", fromlist=["load_prices"]).load_prices(path)),
                             3.0)


if __name__ == "__main__":
    unittest.main()
