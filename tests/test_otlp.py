import gzip
import json
import unittest

from runactuals.otlp import OTLPDecodeError, decode_request

from helpers import pb_request


class OTLPTest(unittest.TestCase):
    def test_protobuf_span(self):
        body = pb_request(bytes(range(16)), bytes(range(8)), "invoke_agent bot", 5, 9,
                          {"gen_ai.agent.name": "bot", "n": -3, "x": 1.5, "ok": True},
                          status_code=2, parent=b"\x01" * 8)
        [s] = decode_request(body, "application/x-protobuf")
        self.assertEqual(s["trace_id"], bytes(range(16)).hex())
        self.assertEqual(s["parent_span_id"], "01" * 8)
        self.assertEqual((s["start_ns"], s["end_ns"], s["status_code"]), (5, 9, 2))
        self.assertEqual(s["attributes"], {"gen_ai.agent.name": "bot", "n": -3, "x": 1.5, "ok": True})
        self.assertEqual(s["resource"], {"service.name": "svc"})

    def test_gzip_protobuf(self):
        body = gzip.compress(pb_request(b"\x02" * 16, b"\x03" * 8, "s", 1, 2, {}))
        [s] = decode_request(body, "application/x-protobuf", "gzip")
        self.assertEqual(s["span_id"], "03" * 8)

    def test_json_span(self):
        body = json.dumps({"resourceSpans": [{"resource": {"attributes": [
            {"key": "service.name", "value": {"stringValue": "svc"}}]},
            "scopeSpans": [{"spans": [{"traceId": "AB" * 16, "spanId": "cd" * 8, "name": "x",
                                       "startTimeUnixNano": "10", "endTimeUnixNano": "20",
                                       "status": {"code": "STATUS_CODE_ERROR"},
                                       "attributes": [{"key": "k", "value": {"intValue": "7"}},
                                                      {"key": "a", "value": {"arrayValue": {"values": [
                                                          {"stringValue": "p"}]}}}]}]}]}]}).encode()
        [s] = decode_request(body, "application/json")
        self.assertEqual(s["trace_id"], "ab" * 16)
        self.assertEqual(s["status_code"], 2)
        self.assertEqual(s["attributes"], {"k": 7, "a": ["p"]})

    def test_rejects_garbage(self):
        with self.assertRaises(OTLPDecodeError):
            decode_request(b"\x0a\xff\xff", "application/x-protobuf")
        with self.assertRaises(OTLPDecodeError):
            decode_request(b"{", "application/json")
        with self.assertRaises(OTLPDecodeError):
            decode_request(b"", "text/plain")


if __name__ == "__main__":
    unittest.main()
