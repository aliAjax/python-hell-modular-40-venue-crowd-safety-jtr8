import tempfile
import threading
import unittest
from pathlib import Path

from src.domain import (
    Actor,
    ConflictError,
    DomainError,
    InvalidTransition,
    StaleCommandError,
)
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class CommandWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(
            SQLiteRepository(Path(self.tmp.name) / "commands.db"),
            RuleEngine(),
        )
        self.coordinator = Actor("coordinator", "coordinator")
        self.supervisor = Actor("supervisor", "supervisor")
        self.operator = Actor("operator", "operator")

    def tearDown(self):
        self.tmp.cleanup()

    def _venue(self):
        return self.service.create(
            self.coordinator, "venue", {"name": "Venue", "address": "1 Road"}
        )

    def _zone(self, venue, name, capacity=100):
        return self.service.create(
            self.coordinator,
            "zone",
            {"venue_id": venue["id"], "name": name, "capacity": capacity},
        )

    def _gate(self, venue, zone, name="Gate"):
        gate = self.service.create(
            self.coordinator,
            "gate",
            {"venue_id": venue["id"], "name": name, "zone_ids": [zone["id"]]},
        )
        return self.service.transition(
            self.operator, gate["id"], "open", {"operator_id": "operator"}
        )

    def _incident(self, venue, zone, source_ref, severity="high"):
        return self.service.create(
            self.operator,
            "incident",
            {
                "venue_id": venue["id"],
                "zone_id": zone["id"],
                "source_ref": source_ref,
                "incident_type": "crowd",
                "severity": severity,
                "reported_at": "2026-09-30T10:00:00Z",
            },
        )

    def _open_zones(self, *zones):
        for zone in zones:
            self.service.transition(
                self.operator, zone["id"], "open", {"checklist": "clear"}
            )

    def _medical_point(self, venue, zone):
        return self.service.create(
            self.supervisor,
            "medical_point",
            {
                "venue_id": venue["id"],
                "zone_id": zone["id"],
                "capacity": 4,
                "equipment_level": "basic",
            },
        )

    def _command_payload(self, incident, venue, zones, gates, target, team, **overrides):
        payload = {
            "incident_id": incident["id"],
            "venue_id": venue["id"],
            "zone_ids": [zone["id"] for zone in zones],
            "gate_ids": [gate["id"] for gate in gates],
            "medical_point_ids": [],
            "target_zone_id": target["id"],
            "divert_count": 30,
            "flow_direction": "exit",
            "team_id": team,
            "task_type": "evacuation",
        }
        payload.update(overrides)
        return payload

    def _submit(self, payload):
        try:
            return self.service.create(self.coordinator, "command", payload), None
        except ConflictError as exc:
            return self.service.get(exc.command_id), exc

    def test_full_diversion_command_freezes_claims_directs_and_dispatches(self):
        venue = self._venue()
        source = self._zone(venue, "Source")
        target = self._zone(venue, "Target")
        gate = self._gate(venue, source)
        point = self._medical_point(venue, source)
        self._open_zones(source, target)
        incident = self._incident(venue, source, "radio-1")

        command = self.service.create(
            self.coordinator,
            "command",
            self._command_payload(
                incident, venue, [source], [gate], target, "team-1",
                medical_point_ids=[point["id"]],
            ),
        )

        self.assertEqual(command["status"], "completed")
        self.assertTrue(all(step["status"] == "done" for step in command["data"]["steps"]))
        self.assertEqual(self.service.get(source["id"])["status"], "frozen")
        gate = self.service.get(gate["id"])
        self.assertEqual(gate["status"], "frozen")
        self.assertEqual(gate["data"]["flow_direction"], "exit")
        self.assertEqual(self.service.get(point["id"])["status"], "frozen")
        target = self.service.get(target["id"])
        self.assertEqual(target["data"]["reserved_occupancy"], 30)
        task = self.service.get(command["data"]["task_id"])
        self.assertEqual(task["status"], "assigned")

        # frozen field arrangements must stop admitting people
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                self.operator,
                source["id"],
                "admit",
                {"gate_id": gate["id"], "count": 1, "admitted_at": "t"},
            )

        # actual headcount reconciliation consumes the reservation
        command = self.service.perform_action(
            self.supervisor, command["id"], "reconcile", {"actual_count": 27}
        )
        target = self.service.get(target["id"])
        self.assertEqual(command["status"], "reconciled")
        self.assertEqual(target["data"]["current_occupancy"], 27)
        self.assertEqual(target["data"]["reserved_occupancy"], 0)

    def test_concurrent_commands_only_one_winner_with_capacity_conflict(self):
        venue = self._venue()
        source = self._zone(venue, "Source")
        target = self._zone(venue, "Target")
        gate = self._gate(venue, source)
        self._open_zones(source, target)
        incident = self._incident(venue, source, "radio-2")

        # close the gate so the winner claims capacity but the saga then fails
        # at its first freeze step, leaving an active command behind
        self.service.transition(
            self.supervisor, gate["id"], "close", {"reason": "pre-situation check"}
        )

        payload = self._command_payload(
            incident, venue, [source], [gate], target, "team-a", divert_count=60
        )
        outcomes = []
        barrier = threading.Barrier(2)

        def submit(team):
            data = dict(payload)
            data["team_id"] = team
            barrier.wait()
            try:
                command = self.service.create(self.coordinator, "command", data)
                outcomes.append(
                    ("ok", command["id"], command["status"], command["id"])
                )
            except DomainError as exc:
                outcomes.append(
                    ("conflict", str(exc), type(exc).__name__, getattr(exc, "command_id", None))
                )

        threads = [
            threading.Thread(target=submit, args=("team-a",)),
            threading.Thread(target=submit, args=("team-b",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        winners = [item for item in outcomes if item[0] == "ok" or item[3]]
        losers = [item for item in outcomes if item[0] == "conflict" and not item[3]]
        self.assertEqual(len(winners), 1)
        winner_id = winners[0][3]
        winner = self.service.get(winner_id)
        self.assertEqual(winner["status"], "failed")
        # the loser lost the atomic claim before any command was persisted
        self.assertEqual(len(losers), 1)
        self.assertEqual(losers[0][2], "ConflictError")
        self.assertIn("zone remaining capacity conflict", losers[0][1])
        target = self.service.get(target["id"])
        self.assertEqual(target["data"]["reserved_occupancy"], 60)

        # completed steps are preserved on the winner command
        self.assertEqual(winner["data"]["steps"][0]["status"], "done")
        self.assertEqual(winner["data"]["failure"]["step"], "freeze:gate:%s" % gate["id"])

        # the winner is the only active command for the incident
        active = [
            command
            for command in self.service.list("command")
            if command["data"]["incident_id"] == incident["id"]
        ]
        self.assertEqual([command["id"] for command in active], [winner_id])

        # while the failed command stays active, another submit cannot create
        # a second command for the same incident
        with self.assertRaises(ConflictError):
            self.service.create(
                self.coordinator,
                "command",
                self._command_payload(
                    incident, venue, [source], [gate], target, "team-c", divert_count=5
                ),
            )

    def test_failed_command_resumes_without_double_occupancy_or_dispatch(self):
        venue = self._venue()
        source = self._zone(venue, "Source")
        target = self._zone(venue, "Target")
        gate = self._gate(venue, source)
        self._open_zones(source, target)

        # team-1 is already committed to another active task
        blocker_incident = self._incident(venue, source, "radio-blocker", "medium")
        blocker = self.service.create(
            self.supervisor,
            "task",
            {
                "incident_id": blocker_incident["id"],
                "venue_id": venue["id"],
                "zone_id": source["id"],
                "team_id": "team-1",
                "task_type": "medical",
            },
        )
        self.service.transition(
            self.coordinator, blocker["id"], "assign", {"assigned_at": "t0"}
        )

        incident = self._incident(venue, source, "radio-3")
        command, error = self._submit(
            self._command_payload(
                incident, venue, [source], [gate], target, "team-1", divert_count=40
            )
        )

        self.assertIsNotNone(error)
        self.assertEqual(command["status"], "failed")
        self.assertEqual(command["data"]["failure"]["step"], "dispatch:task")
        self.assertEqual(self.service.get(source["id"])["status"], "frozen")
        self.assertEqual(self.service.get(target["id"])["data"]["reserved_occupancy"], 40)
        draft_task_id = command["data"]["task_id"]
        self.assertEqual(self.service.get(draft_task_id)["status"], "draft")

        # release the team, then retry: only the dispatch step runs again
        self.service.transition(
            self.coordinator, blocker["id"], "cancel", {"reason": "reassigned"}
        )
        command = self.service.perform_action(self.coordinator, command["id"], "retry", {})
        self.assertEqual(command["status"], "completed")
        self.assertEqual(command["data"]["task_id"], draft_task_id)
        self.assertEqual(self.service.get(draft_task_id)["status"], "assigned")
        self.assertEqual(self.service.get(target["id"])["data"]["reserved_occupancy"], 40)
        self.assertEqual(
            len(
                [
                    task
                    for task in self.service.list("task")
                    if task["data"].get("team_id") == "team-1"
                    and task["data"].get("incident_id") == incident["id"]
                ]
            ),
            1,
        )

    def test_incident_escalation_zone_resize_and_channel_change_invalidate_command(self):
        venue = self._venue()
        source = self._zone(venue, "Source")
        target = self._zone(venue, "Target")
        gate = self._gate(venue, source)
        self._open_zones(source, target)
        blocker_incident = self._incident(venue, source, "radio-blocker", "low")
        blocker = self.service.create(
            self.supervisor,
            "task",
            {
                "incident_id": blocker_incident["id"],
                "venue_id": venue["id"],
                "zone_id": source["id"],
                "team_id": "team-busy",
                "task_type": "medical",
            },
        )
        self.service.transition(
            self.coordinator, blocker["id"], "assign", {"assigned_at": "t0"}
        )
        incident = self._incident(venue, source, "radio-4", "medium")

        # gate channel state changes externally before retry
        command, _ = self._submit(
            self._command_payload(
                incident, venue, [source], [gate], target, "team-busy"
            )
        )
        self.service.transition(
            self.supervisor, gate["id"], "unfreeze", {"reason": "manual override"}
        )
        with self.assertRaises(StaleCommandError) as caught:
            self.service.perform_action(self.coordinator, command["id"], "retry", {})
        self.assertEqual(
            caught.exception.reasons[0]["kind"], "gate"
        )
        self.assertEqual(self.service.get(command["id"])["status"], "stale")

        # zone capacity change invalidates another failed command
        gate = self.service.transition(
            self.operator, gate["id"], "open", {"operator_id": "operator"}
        )
        target_two = self._zone(venue, "Second target", capacity=300)
        self._open_zones(target_two)
        incident_two = self._incident(venue, source, "radio-5", "medium")
        command_two, _ = self._submit(
            self._command_payload(
                incident_two, venue, [source], [gate], target_two, "team-busy"
            )
        )
        self.service.transition(
            self.coordinator, target_two["id"], "resize",
            {"capacity": 250, "reason": "layout"},
        )
        with self.assertRaises(StaleCommandError) as caught_two:
            self.service.perform_action(self.coordinator, command_two["id"], "retry", {})
        self.assertEqual(caught_two.exception.reasons[0]["kind"], "zone")

        # incident escalation updates the level and score
        incident = self.service.transition(
            self.supervisor, incident["id"], "escalate", {"severity": "critical"}
        )
        self.assertEqual(incident["data"]["severity"], "critical")
        self.assertEqual(incident["status"], "reported")
        with self.assertRaises(ConflictError):
            self.service.transition(
                self.supervisor, incident["id"], "escalate", {"severity": "low"}
            )

    def test_cancel_releases_reservation_unfreezes_and_cancels_task(self):
        venue = self._venue()
        source = self._zone(venue, "Source")
        target = self._zone(venue, "Target")
        gate = self._gate(venue, source)
        point = self._medical_point(venue, source)
        self._open_zones(source, target)
        blocker_incident = self._incident(venue, source, "radio-blocker", "low")
        blocker = self.service.create(
            self.supervisor,
            "task",
            {
                "incident_id": blocker_incident["id"],
                "venue_id": venue["id"],
                "zone_id": source["id"],
                "team_id": "team-busy",
                "task_type": "medical",
            },
        )
        self.service.transition(
            self.coordinator, blocker["id"], "assign", {"assigned_at": "t0"}
        )
        incident = self._incident(venue, source, "radio-6")
        command, _ = self._submit(
            self._command_payload(
                incident, venue, [source], [gate], target, "team-busy",
                medical_point_ids=[point["id"]], divert_count=25,
            )
        )
        task_id = command["data"]["task_id"]

        command = self.service.perform_action(
            self.coordinator, command["id"], "cancel", {"reason": "all clear"}
        )
        self.assertEqual(command["status"], "cancelled")
        self.assertEqual(self.service.get(target["id"])["data"]["reserved_occupancy"], 0)
        self.assertEqual(self.service.get(source["id"])["status"], "open")
        self.assertEqual(self.service.get(gate["id"])["status"], "closed")
        self.assertEqual(self.service.get(gate["id"])["data"]["flow_direction"], "entry")
        self.assertEqual(self.service.get(point["id"])["status"], "active")
        self.assertEqual(self.service.get(task_id)["status"], "cancelled")

        with self.assertRaises(InvalidTransition):
            self.service.perform_action(
                self.coordinator, command["id"], "cancel", {"reason": "again"}
            )


if __name__ == "__main__":
    unittest.main()
