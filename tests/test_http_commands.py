import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

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
        self.server = create_server(
            "127.0.0.1", 0, service, service.rules,
            str(Path(__file__).resolve().parent.parent / "static"),
        )
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.service = service

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tmp.cleanup()

    def _request(self, method, path, body=None, role="coordinator", user="cmd"):
        url = "http://127.0.0.1:%s%s" % (self.port, path)
        data = None
        headers = {"Content-Type": "application/json"}
        if role:
            headers["X-Role"] = role
            headers["X-User-Id"] = user
        if body is not None:
            data = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def _seed(self):
        _, venue = self._request("POST", "/api/venues", {"name": "V", "address": "A"})
        _, source = self._request(
            "POST", "/api/zones", {"venue_id": venue["id"], "name": "S", "capacity": 100}
        )
        _, target = self._request(
            "POST", "/api/zones", {"venue_id": venue["id"], "name": "T", "capacity": 100}
        )
        _, gate = self._request(
            "POST",
            "/api/gates",
            {"venue_id": venue["id"], "name": "G", "zone_ids": [source["id"]]},
        )
        self._request(
            "POST",
            "/api/entities/%s/actions" % gate["id"],
            {"action": "open", "data": {"operator_id": "op"}},
            role="operator",
        )
        for zone in (source, target):
            self._request(
                "POST",
                "/api/entities/%s/actions" % zone["id"],
                {"action": "open", "data": {"checklist": "clear"}},
                role="operator",
            )
        _, incident = self._request(
            "POST",
            "/api/incidents",
            {
                "venue_id": venue["id"],
                "zone_id": source["id"],
                "source_ref": "radio-9",
                "incident_type": "crowd",
                "severity": "high",
                "reported_at": "2026-09-30T10:00:00Z",
            },
            role="operator",
        )
        return venue, source, target, gate, incident

    def test_command_submit_retry_and_cancel_over_http(self):
        venue, source, target, gate, incident = self._seed()

        # gate closed -> saga fails at gate freeze, command returned in error body
        self._request(
            "POST",
            "/api/entities/%s/actions" % gate["id"],
            {"action": "close", "data": {"reason": "check"}},
            role="supervisor",
        )
        payload = {
            "incident_id": incident["id"],
            "venue_id": venue["id"],
            "zone_ids": [source["id"]],
            "gate_ids": [gate["id"]],
            "target_zone_id": target["id"],
            "divert_count": 30,
            "flow_direction": "exit",
            "team_id": "team-1",
            "task_type": "evacuation",
        }
        status, body = self._request("POST", "/api/commands", payload)
        self.assertEqual(status, 409)
        self.assertIn("command_id", body)
        command_id = body["command_id"]

        # the page can read the command and see why it is blocked
        status, command = self._request("GET", "/api/entities/%s" % command_id)
        self.assertEqual(status, 200)
        self.assertEqual(command["status"], "failed")
        self.assertEqual(command["data"]["failure"]["step"], "freeze:gate:%s" % gate["id"])

        # operators cannot manage commands
        status, body = self._request(
            "POST",
            "/api/entities/%s/actions" % command_id,
            {"action": "cancel", "data": {"reason": "no"}},
            role="operator",
        )
        self.assertEqual(status, 403)

        # coordinator cancels: reservation released
        status, command = self._request(
            "POST",
            "/api/entities/%s/actions" % command_id,
            {"action": "cancel", "data": {"reason": "stand down"}},
        )
        self.assertEqual(status, 200)
        self.assertEqual(command["status"], "cancelled")
        _, target_now = self._request("GET", "/api/entities/%s" % target["id"])
        self.assertEqual(target_now["data"]["reserved_occupancy"], 0)

    def test_stale_error_carries_blocking_reasons(self):
        venue, source, target, gate, incident = self._seed()

        # create a blocking active task so dispatch fails
        _, blocker_incident = self._request(
            "POST",
            "/api/incidents",
            {
                "venue_id": venue["id"],
                "zone_id": source["id"],
                "source_ref": "radio-blocker",
                "incident_type": "medical",
                "severity": "low",
                "reported_at": "2026-09-30T09:00:00Z",
            },
            role="operator",
        )
        _, blocker = self._request(
            "POST",
            "/api/tasks",
            {
                "incident_id": blocker_incident["id"],
                "venue_id": venue["id"],
                "zone_id": source["id"],
                "team_id": "team-busy",
                "task_type": "medical",
            },
            role="supervisor",
        )
        self._request(
            "POST",
            "/api/entities/%s/actions" % blocker["id"],
            {"action": "assign", "data": {"assigned_at": "t0"}},
        )

        payload = {
            "incident_id": incident["id"],
            "venue_id": venue["id"],
            "zone_ids": [source["id"]],
            "gate_ids": [gate["id"]],
            "target_zone_id": target["id"],
            "divert_count": 20,
            "flow_direction": "exit",
            "team_id": "team-busy",
            "task_type": "evacuation",
        }
        status, body = self._request("POST", "/api/commands", payload)
        self.assertEqual(status, 409)
        command_id = body["command_id"]

        # external gate change invalidates the command
        self._request(
            "POST",
            "/api/entities/%s/actions" % gate["id"],
            {"action": "unfreeze", "data": {"reason": "manual"}},
            role="supervisor",
        )
        status, body = self._request(
            "POST", "/api/entities/%s/actions" % command_id, {"action": "retry"}
        )
        self.assertEqual(status, 409)
        self.assertEqual(body["type"], "StaleCommandError")
        self.assertTrue(body["reasons"])
        self.assertEqual(body["reasons"][0]["kind"], "gate")


if __name__ == "__main__":
    unittest.main()
