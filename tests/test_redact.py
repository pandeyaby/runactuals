"""Secrets must never land in recorded previews, headers, or URLs."""

import unittest

from runactuals.redact import REDACTED, redact_body, redact_headers, redact_path_and_query, redact_url, redact_value


class RedactTest(unittest.TestCase):
    def test_authorization_and_api_key_headers(self):
        out = redact_headers({
            "Authorization": "Bearer sk-test-aaaaaaaaaaaaaaaa",
            "x-api-key": "sk-ant-aaaaaaaaaaaaaaaa",
            "x-goog-api-key": "AIzaSyFakeTestKeyAAAAAAAAAAAaa",
            "Content-Type": "application/json",
        })
        self.assertEqual(out["Authorization"], REDACTED)
        self.assertEqual(out["x-api-key"], REDACTED)
        self.assertEqual(out["x-goog-api-key"], REDACTED)
        self.assertEqual(out["Content-Type"], "application/json")
        self.assertNotIn("sk-", str(out))
        self.assertNotIn("AIza", str(out))

    def test_query_key_on_gemini_url(self):
        url = "https://generativelanguage.googleapis.com/v1beta/models/g:generateContent?key=AIzaSyFakeTestKeyAAAAAAAAAAAaa"
        out = redact_url(url)
        self.assertIn(f"key={REDACTED}", out)
        self.assertNotIn("AIza", out)
        path = redact_path_and_query("/v1beta/models/g:generateContent?key=AIzaSyFakeTestKeyAAAAAAAAAAAaa")
        self.assertIn(f"key={REDACTED}", path)
        self.assertNotIn("AIza", path)

    def test_body_json_and_embedded_tokens(self):
        raw = (
            '{"api_key":"sk-secret-bbbbbbbb","note":"Bearer sk-live-cccccccc",'
            '"prompt":"hello","key":"AIzaSyFakeTestKeyAAAAAAAAAAAaa"}'
        )
        out = redact_body(raw)
        self.assertNotIn("sk-secret", out)
        self.assertNotIn("sk-live", out)
        self.assertNotIn("AIza", out)
        self.assertIn("hello", out)
        self.assertIn(REDACTED, out)

    def test_non_secret_key_field_is_kept(self):
        out = redact_body('{"key":"user-id-1","text":"ok"}')
        self.assertIn("user-id-1", out)

    def test_value_redaction_leaves_plain_text(self):
        self.assertEqual(redact_value("a normal sentence"), "a normal sentence")


if __name__ == "__main__":
    unittest.main()
