import json
import tempfile
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from src.http_api import create_server
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class HttpApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        service = DomainService(
            SQLiteRepository(Path(self.tmp.name) / "http.db"), RuleEngine()
        )
        static = str(Path(__file__).resolve().parent.parent / "static")
        self.server = create_server("127.0.0.1", 0, service, RuleEngine(), static)
        self.port = self.server.server_address[1]
        self.base = "http://127.0.0.1:%d" % self.port
        import threading

        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def _call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = Request(
            self.base + path,
            data=data,
            headers={
                "X-User-Id": "commander",
                "X-Role": "coordinator",
                "Content-Type": "application/json",
            },
            method=method,
        )
        try:
            with urlopen(req) as resp:
                raw = resp.read().decode("utf-8")
                try:
                    return resp.status, json.loads(raw)
                except json.JSONDecodeError:
                    return resp.status, raw
        except HTTPError as exc:
            raw = exc.read().decode("utf-8")
            try:
                return exc.code, json.loads(raw)
            except json.JSONDecodeError:
                return exc.code, raw

    def _post(self, kind, body):
        return self._call("POST", "/api/" + kind, body)

    def _act(self, entity_id, action, data=None):
        return self._call(
            "POST",
            "/api/entities/%s/actions" % entity_id,
            {"action": action, "data": data or {}},
        )

    def test_page_and_health(self):
        status, body = self._call("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")
        status, body = self._call("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("疏导命令", body)

    def test_full_order_flow_over_http(self):
        _, venue = self._post("venue", {"name": "V", "address": "A"})
        _, danger = self._post(
            "zone", {"venue_id": venue["id"], "name": "Danger", "capacity": 500}
        )
        _, safe = self._post(
            "zone", {"venue_id": venue["id"], "name": "Safe", "capacity": 300}
        )
        _, gate_d = self._post(
            "gate", {"venue_id": venue["id"], "name": "GD", "zone_ids": [danger["id"]]}
        )
        _, gate_s = self._post(
            "gate", {"venue_id": venue["id"], "name": "GS", "zone_ids": [safe["id"]]}
        )
        _, med = self._post(
            "medical_point",
            {
                "venue_id": venue["id"],
                "zone_id": danger["id"],
                "capacity": 10,
                "equipment_level": "advanced",
            },
        )
        _, inc = self._post(
            "incident",
            {
                "venue_id": venue["id"],
                "zone_id": danger["id"],
                "source_ref": "r",
                "incident_type": "crowd",
                "severity": "high",
                "reported_at": "t",
            },
        )
        self._act(inc["id"], "triage", {"priority": "crowd"})
        for g in (gate_d, gate_s):
            self._act(g["id"], "open", {"operator_id": "op"})
        for z in (danger, safe):
            self._act(z["id"], "open", {"checklist": "c"})

        _, order = self._post(
            "diversion_order",
            {
                "venue_id": venue["id"],
                "incident_id": inc["id"],
                "zone_ids": [danger["id"]],
                "gate_ids": [gate_d["id"]],
                "medical_point_ids": [med["id"]],
                "transfer_count": 100,
                "target_zone_id": safe["id"],
                "transfer_gate_id": gate_s["id"],
                "passage_direction": "outbound",
                "rescue_tasks": [{"team_id": "team-1", "task_type": "rescue"}],
            },
        )
        self.assertEqual(order["status"], "draft")
        _, submitted = self._act(order["id"], "submit", {})
        self.assertEqual(submitted["status"], "completed")
        # frozen states visible through the API
        _, d = self._call("GET", "/api/entities/%s" % danger["id"])
        self.assertEqual(d["status"], "frozen")
        _, g = self._call("GET", "/api/entities/%s" % gate_d["id"])
        self.assertEqual(g["status"], "frozen")
        _, m = self._call("GET", "/api/entities/%s" % med["id"])
        self.assertEqual(m["status"], "frozen")
        _, s = self._call("GET", "/api/entities/%s" % safe["id"])
        self.assertEqual(s["data"]["current_occupancy"], 100)
        # order list endpoint
        _, lst = self._call("GET", "/api/diversion_order")
        self.assertEqual(len(lst["items"]), 1)
        # escalation invalidates the order
        self._act(inc["id"], "escalate", {"severity": "critical"})
        _, inv = self._call("GET", "/api/entities/%s" % order["id"])
        self.assertEqual(inv["status"], "invalidated")
        # validation error surfaces as 400
        status, err = self._post("diversion_order", {"venue_id": venue["id"]})
        self.assertEqual(status, 400)
        self.assertIn("incident_id", err["error"])


if __name__ == "__main__":
    unittest.main()
