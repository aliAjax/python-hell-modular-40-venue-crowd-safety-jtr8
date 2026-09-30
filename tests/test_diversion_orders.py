import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, InvalidTransition, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class DiversionOrderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(
            SQLiteRepository(Path(self.tmp.name) / "orders.db"),
            RuleEngine(),
        )
        self.cmd = Actor("commander", "coordinator")
        self.op = Actor("operator", "operator")
        self._build_world()

    def tearDown(self):
        self.tmp.cleanup()

    def _build_world(self):
        self.venue = self.service.create(
            self.cmd, "venue", {"name": "Venue", "address": "1 Road"}
        )
        self.danger = self.service.create(
            self.cmd,
            "zone",
            {"venue_id": self.venue["id"], "name": "Danger", "capacity": 500},
        )
        self.safe = self.service.create(
            self.cmd,
            "zone",
            {"venue_id": self.venue["id"], "name": "Safe", "capacity": 300},
        )
        self.gate_danger = self.service.create(
            self.cmd,
            "gate",
            {"venue_id": self.venue["id"], "name": "Gate-Danger", "zone_ids": [self.danger["id"]]},
        )
        self.gate_safe = self.service.create(
            self.cmd,
            "gate",
            {"venue_id": self.venue["id"], "name": "Gate-Safe", "zone_ids": [self.safe["id"]]},
        )
        self.medical = self.service.create(
            self.cmd,
            "medical_point",
            {
                "venue_id": self.venue["id"],
                "zone_id": self.danger["id"],
                "capacity": 10,
                "equipment_level": "advanced",
            },
        )
        self.incident = self.service.create(
            self.op,
            "incident",
            {
                "venue_id": self.venue["id"],
                "zone_id": self.danger["id"],
                "source_ref": "radio-1",
                "incident_type": "crowd",
                "severity": "high",
                "reported_at": "2026-09-30T10:00:00Z",
            },
        )
        self.service.transition(self.cmd, self.incident["id"], "triage", {"priority": "crowd"})
        for gate in (self.gate_danger, self.gate_safe):
            self.service.transition(self.cmd, gate["id"], "open", {"operator_id": "operator"})
        for zone in (self.danger, self.safe):
            self.service.transition(self.cmd, zone["id"], "open", {"checklist": "clear"})

    def _order_payload(self, team="team-1", count=100):
        return {
            "venue_id": self.venue["id"],
            "incident_id": self.incident["id"],
            "zone_ids": [self.danger["id"]],
            "gate_ids": [self.gate_danger["id"]],
            "medical_point_ids": [self.medical["id"]],
            "transfer_count": count,
            "target_zone_id": self.safe["id"],
            "transfer_gate_id": self.gate_safe["id"],
            "passage_direction": "outbound",
            "rescue_tasks": [{"team_id": team, "task_type": "rescue"}],
        }

    def _create_order(self, team="team-1", count=100):
        return self.service.create(self.cmd, "diversion_order", self._order_payload(team, count))

    def test_full_order_freezes_transfers_and_dispatches(self):
        order = self._create_order()
        self.assertEqual(order["status"], "draft")
        order = self.service.transition(self.cmd, order["id"], "submit", {})
        self.assertEqual(order["status"], "completed")
        # affected entities are frozen
        self.assertEqual(self.service.get(self.danger["id"])["status"], "frozen")
        self.assertEqual(self.service.get(self.gate_danger["id"])["status"], "frozen")
        self.assertEqual(self.service.get(self.medical["id"])["status"], "frozen")
        # safe zone received people
        self.assertEqual(
            self.service.get(self.safe["id"])["data"]["current_occupancy"], 100
        )
        # all steps done
        self.assertTrue(all(s["status"] == "done" for s in order["data"]["steps"]))
        # rescue task dispatched and assigned
        task_step = [s for s in order["data"]["steps"] if s["kind"] == "dispatch_task"][0]
        self.assertEqual(task_step["status"], "done")
        task = self.service.get(task_step["result"]["task_id"])
        self.assertEqual(task["status"], "assigned")

    def test_resubmit_is_idempotent_no_double_occupancy(self):
        order = self._create_order(count=100)
        order = self.service.transition(self.cmd, order["id"], "submit", {})
        self.assertEqual(self.service.get(self.safe["id"])["data"]["current_occupancy"], 100)
        # sequential re-submit returns the same order without re-applying
        again = self.service.transition(self.cmd, order["id"], "submit", {})
        self.assertEqual(again["id"], order["id"])
        self.assertEqual(
            self.service.get(self.safe["id"])["data"]["current_occupancy"], 100
        )

    def test_concurrent_orders_only_one_fits_capacity(self):
        first = self._create_order(team="team-a", count=200)
        second = self._create_order(team="team-b", count=200)
        first = self.service.transition(self.cmd, first["id"], "submit", {})
        self.assertEqual(first["status"], "completed")
        with self.assertRaises(ConflictError) as ctx:
            self.service.transition(self.cmd, second["id"], "submit", {})
        self.assertIn("remaining capacity", str(ctx.exception))
        second = self.service.get(second["id"])
        self.assertEqual(second["status"], "blocked")
        # completed steps are kept, transfer failed, task still pending
        steps = {s["key"]: s for s in second["data"]["steps"]}
        self.assertEqual(steps["freeze_zone:%s" % self.danger["id"]]["status"], "done")
        self.assertEqual(steps["transfer"]["status"], "failed")
        self.assertEqual(steps["transfer"]["error"], str(ctx.exception))
        self.assertEqual(steps["task:team-b:rescue"]["status"], "pending")
        # occupancy only increased once
        self.assertEqual(
            self.service.get(self.safe["id"])["data"]["current_occupancy"], 200
        )

    def test_retry_after_team_conflict_dispatches_without_duplicate(self):
        first = self._create_order(team="shared-team")
        second = self._create_order(team="shared-team")
        first = self.service.transition(self.cmd, first["id"], "submit", {})
        with self.assertRaises(ConflictError):
            self.service.transition(self.cmd, second["id"], "submit", {})
        second = self.service.get(second["id"])
        self.assertEqual(second["status"], "blocked")
        # resolve the conflict by completing the first team's task
        first_task_id = [
            s["result"]["task_id"] for s in first["data"]["steps"] if s["kind"] == "dispatch_task"
        ][0]
        self.service.transition(self.op, first_task_id, "acknowledge", {"acknowledged_at": "t"})
        self.service.transition(self.op, first_task_id, "arrive", {"arrived_at": "t"})
        self.service.transition(
            self.op, first_task_id, "complete", {"completed_at": "t", "outcome": "done"}
        )
        # retry the blocked order
        second = self.service.transition(self.cmd, second["id"], "retry", {})
        self.assertEqual(second["status"], "completed")
        task_step = [s for s in second["data"]["steps"] if s["kind"] == "dispatch_task"][0]
        self.assertEqual(task_step["status"], "done")
        # no duplicate: the second order's task is distinct from the first's
        second_task_id = task_step["result"]["task_id"]
        self.assertNotEqual(first_task_id, second_task_id)

    def test_invalidation_on_incident_severity_change(self):
        order = self._create_order()
        order = self.service.transition(self.cmd, order["id"], "submit", {})
        self.assertEqual(order["status"], "completed")
        self.service.transition(self.cmd, self.incident["id"], "escalate", {"severity": "critical"})
        order = self.service.get(order["id"])
        self.assertEqual(order["status"], "invalidated")
        self.assertIn("severity", order["data"]["invalidated_reason"])
        # completed steps are preserved and reconciled
        self.assertTrue(all(s["status"] == "done" for s in order["data"]["steps"]))
        self.assertTrue(order["data"]["reconciled"])

    def test_invalidation_on_zone_capacity_change(self):
        order = self._create_order()
        order = self.service.transition(self.cmd, order["id"], "submit", {})
        self.assertEqual(order["status"], "completed")
        self.service.transition(
            self.cmd, self.safe["id"], "adjust_capacity", {"reason": "refit", "capacity": 500}
        )
        order = self.service.get(order["id"])
        self.assertEqual(order["status"], "invalidated")
        self.assertIn("capacity", order["data"]["invalidated_reason"])

    def test_invalidation_on_gate_status_change(self):
        order = self._create_order()
        order = self.service.transition(self.cmd, order["id"], "submit", {})
        self.assertEqual(order["status"], "completed")
        # externally close the gate the order had frozen
        self.service.transition(
            self.cmd, self.gate_danger["id"], "close", {"reason": "external closure"}
        )
        order = self.service.get(order["id"])
        self.assertEqual(order["status"], "invalidated")
        self.assertIn("gate", order["data"]["invalidated_reason"])

    def test_cancel_order(self):
        order = self._create_order()
        order = self.service.transition(
            self.cmd, order["id"], "cancel", {"reason": "no longer needed"}
        )
        self.assertEqual(order["status"], "cancelled")
        self.assertEqual(order["data"]["cancel_reason"], "no longer needed")
        with self.assertRaises(InvalidTransition):
            self.service.transition(self.cmd, order["id"], "submit", {})

    def test_reconcile_writes_off_actual_count(self):
        order = self._create_order(count=100)
        order = self.service.transition(self.cmd, order["id"], "submit", {})
        self.assertTrue(order["data"]["reconciled"])
        self.assertEqual(order["data"]["transferred_count"], 100)
        self.assertEqual(order["data"]["write_off"], 0)

    def test_validation_errors(self):
        with self.assertRaises(ValidationError):
            self.service.create(
                self.cmd,
                "diversion_order",
                {
                    "venue_id": self.venue["id"],
                    "incident_id": self.incident["id"],
                    "zone_ids": [self.danger["id"]],
                    "gate_ids": [self.gate_danger["id"]],
                    "transfer_count": 10,
                    "target_zone_id": self.danger["id"],  # same as affected
                    "passage_direction": "outbound",
                },
            )
        with self.assertRaises(ValidationError):
            self.service.create(
                self.cmd,
                "diversion_order",
                {
                    "venue_id": self.venue["id"],
                    "incident_id": self.incident["id"],
                    "zone_ids": [self.danger["id"]],
                    "gate_ids": [self.gate_danger["id"]],
                    "transfer_count": 10,
                    "target_zone_id": self.safe["id"],
                    "passage_direction": "sideways",
                },
            )


if __name__ == "__main__":
    unittest.main()
