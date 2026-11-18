"""Cross-check the stdlib protobuf reader against the official OTLP protobuf classes.

Skipped unless `opentelemetry-proto` is installed (CI installs it).
"""

import unittest

from runactuals.otlp import decode_request

try:
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
    from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
    from opentelemetry.proto.trace.v1.trace_pb2 import Span, Status
except ImportError:  # pragma: no cover
    ExportTraceServiceRequest = None


@unittest.skipIf(ExportTraceServiceRequest is None, "opentelemetry-proto not installed")
class InteropTest(unittest.TestCase):
    def test_official_encoding_round_trips(self):
        req = ExportTraceServiceRequest()
        rs = req.resource_spans.add()
        rs.resource.attributes.append(KeyValue(key="service.name", value=AnyValue(string_value="bot")))
        sp = rs.scope_spans.add().spans.add()
        sp.trace_id, sp.span_id, sp.parent_span_id = b"\x11" * 16, b"\x22" * 8, b"\x33" * 8
        sp.name, sp.kind = "execute_tool wait_for_ci", Span.SPAN_KIND_INTERNAL
        sp.start_time_unix_nano, sp.end_time_unix_nano = 1_800_000_000_000_000_000, 1_800_000_001_000_000_000
        sp.status.CopyFrom(Status(code=Status.STATUS_CODE_ERROR, message="timeout"))
        for k, v in {"gen_ai.tool.name": AnyValue(string_value="wait_for_ci"),
                     "gen_ai.usage.input_tokens": AnyValue(int_value=1200),
                     "runactuals.cost.usd": AnyValue(double_value=0.0042),
                     "runactuals.retry": AnyValue(bool_value=True)}.items():
            sp.attributes.append(KeyValue(key=k, value=v))
        ev = sp.events.add()
        ev.name, ev.time_unix_nano = "exception", 1_800_000_000_500_000_000
        [s] = decode_request(req.SerializeToString(), "application/x-protobuf")
        self.assertEqual(s["trace_id"], "11" * 16)
        self.assertEqual(s["parent_span_id"], "33" * 8)
        self.assertEqual(s["end_ns"], 1_800_000_001_000_000_000)
        self.assertEqual((s["status_code"], s["status_message"]), (2, "timeout"))
        self.assertEqual(s["attributes"], {"gen_ai.tool.name": "wait_for_ci",
                                           "gen_ai.usage.input_tokens": 1200,
                                           "runactuals.cost.usd": 0.0042, "runactuals.retry": True})
        self.assertEqual(s["events"][0]["name"], "exception")
        self.assertEqual(s["resource"], {"service.name": "bot"})


if __name__ == "__main__":
    unittest.main()
