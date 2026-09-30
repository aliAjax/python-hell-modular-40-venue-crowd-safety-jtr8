"""Diversion command orchestration.

A diversion command is a small saga that links an incident to field actions:

1. claim diversion capacity on the receiving zone
2. freeze affected zones, entry gates and medical points
3. direct gate channels towards the evacuation flow
4. dispatch one rescue task

Completed steps are recorded on the command, so a retry never occupies
capacity twice, freezes twice or creates the same rescue task twice.
The command carries a snapshot of the incident level, zone capacities and
gate channel states; any external change invalidates the command.
"""

from .domain import (
    ConflictError,
    InvalidTransition,
    NotFoundError,
    StaleCommandError,
    ValidationError,
)
from .repository import utcnow
from .rules import ACTIVE_COMMAND_STATUSES, remaining_capacity

FLOW_DIRECTIONS = ("entry", "exit", "bidirectional")
COMMAND_ACTORS = ("supervisor", "coordinator", "admin")


class CommandManager:
    def __init__(self, service):
        self.service = service
        self.repository = service.repository
        self.rules = service.rules

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _as_int(value, field):
        try:
            result = int(value)
        except (TypeError, ValueError):
            raise ValidationError("%s must be an integer" % field)
        return result

    def _get(self, command_id):
        command = self.repository.get_entity(command_id)
        if not command or command["kind"] != "command":
            raise NotFoundError("entity not found: " + str(command_id))
        return command

    def _save(self, command):
        saved = self.repository.update_entity(
            command["id"], command["version"], command["status"], command["data"]
        )
        command.clear()
        command.update(saved)
        return command

    @staticmethod
    def _snapshot_entry(entity):
        entry = {"version": entity["version"], "status": entity["status"]}
        if entity["kind"] == "incident":
            entry["severity"] = entity["data"].get("severity")
        elif entity["kind"] == "zone":
            entry["capacity"] = int(entity["data"].get("capacity", 0))
        elif entity["kind"] == "gate":
            entry["channel_status"] = entity["status"]
        return entry

    def _stale_reasons(self, command):
        reasons = []
        for key, observed in (command["data"].get("snapshot") or {}).items():
            kind, entity_id = key.split(":", 1)
            entity = self.repository.get_entity(entity_id)
            if not entity:
                reasons.append(
                    {"entity_id": entity_id, "kind": kind, "message": "%s no longer exists" % kind}
                )
                continue
            if kind == "incident":
                current = entity["data"].get("severity")
                if current != observed.get("severity"):
                    reasons.append(
                        {
                            "entity_id": entity_id,
                            "kind": kind,
                            "message": "incident level changed from %s to %s"
                            % (observed.get("severity"), current),
                        }
                    )
            elif kind == "zone":
                current = int(entity["data"].get("capacity", 0))
                if current != observed.get("capacity"):
                    reasons.append(
                        {
                            "entity_id": entity_id,
                            "kind": kind,
                            "message": "zone capacity changed from %s to %s"
                            % (observed.get("capacity"), current),
                        }
                    )
            elif kind == "gate":
                if entity["status"] != observed.get("channel_status"):
                    reasons.append(
                        {
                            "entity_id": entity_id,
                            "kind": kind,
                            "message": "gate channel state changed from %s to %s"
                            % (observed.get("channel_status"), entity["status"]),
                        }
                    )
            elif kind in ("zone", "medical_point") and entity["status"] != observed.get("status"):
                reasons.append(
                    {
                        "entity_id": entity_id,
                        "kind": kind,
                        "message": "%s state changed from %s to %s"
                        % (kind, observed.get("status"), entity["status"]),
                    }
                )
        return reasons

    def _mark_stale(self, command, reasons, actor, step_key):
        command["status"] = "stale"
        command["data"]["stale_reasons"] = reasons
        command["data"]["failure"] = {
            "step": step_key,
            "error": reasons[0]["message"],
            "type": "StaleCommandError",
        }
        self._save(command)
        self.service.audit.record(
            command["id"], actor, "stale", "executing", "stale", {"reasons": reasons}
        )
        error = StaleCommandError(
            "diversion command invalidated: " + "; ".join(r["message"] for r in reasons),
            reasons=reasons,
        )
        error.command_id = command["id"]
        raise error

    def _record_failure(self, command, step, exc, actor):
        self._mark_step(command, step["key"], "failed")
        command["status"] = "failed"
        command["data"]["failure"] = {
            "step": step["key"],
            "error": str(exc),
            "type": type(exc).__name__,
        }
        self._save(command)
        self.service.audit.record(
            command["id"],
            actor,
            "fail",
            "executing",
            "failed",
            {"step": step["key"], "error": str(exc), "type": type(exc).__name__},
        )
        exc.command_id = command["id"]
        return exc

    def _refresh_snapshot(self, command, entity):
        command["data"]["snapshot"]["%s:%s" % (entity["kind"], entity["id"])] = (
            self._snapshot_entry(entity)
        )

    # ------------------------------------------------------------------
    # submission
    # ------------------------------------------------------------------
    def submit(self, actor, payload, idempotency_key=None):
        self.rules._ensure_role(actor, COMMAND_ACTORS)
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        validated = self._validate_payload(payload)
        command_id = str(payload.get("id") or "") or None
        from uuid import uuid4

        command_id = command_id or str(uuid4())
        steps = [{"key": "claim", "status": "done"}]
        for zone_id in validated["zone_ids"]:
            steps.append({"key": "freeze:zone:%s" % zone_id, "status": "pending"})
        for gate_id in validated["gate_ids"]:
            steps.append({"key": "freeze:gate:%s" % gate_id, "status": "pending"})
        for gate_id in validated["gate_ids"]:
            steps.append({"key": "direct:gate:%s" % gate_id, "status": "pending"})
        for point_id in validated["medical_point_ids"]:
            steps.append({"key": "freeze:medical:%s" % point_id, "status": "pending"})
        steps.append({"key": "dispatch:task", "status": "pending"})

        snapshot = {}
        for entity in validated["snapshot_entities"]:
            snapshot["%s:%s" % (entity["kind"], entity["id"])] = self._snapshot_entry(entity)

        command_data = {
            "incident_id": validated["incident_id"],
            "venue_id": validated["venue_id"],
            "zone_ids": validated["zone_ids"],
            "gate_ids": validated["gate_ids"],
            "medical_point_ids": validated["medical_point_ids"],
            "target_zone_id": validated["target_zone_id"],
            "divert_count": validated["divert_count"],
            "flow_direction": validated["flow_direction"],
            "team_id": validated["team_id"],
            "task_type": validated["task_type"],
            "task_zone_id": validated.get("task_zone_id"),
            "task_id": None,
            "steps": steps,
            "snapshot": snapshot,
            "failure": None,
            "stale_reasons": [],
            "submitted_by": actor.user_id,
            "reconciled": False,
            "actual_count": None,
        }
        command, target_zone = self.repository.insert_command_and_claim(
            command_id,
            command_data,
            validated["target_zone_id"],
            validated["divert_count"],
            actor.user_id,
        )
        self.service.audit.record(
            command["id"],
            actor,
            "submit",
            None,
            "executing",
            {"divert_count": validated["divert_count"], "target_zone_id": target_zone["id"]},
        )
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, command["id"])
        return self._run(actor, command)

    def _validate_payload(self, payload):
        data = dict(payload or {})
        required = (
            "incident_id",
            "venue_id",
            "zone_ids",
            "gate_ids",
            "target_zone_id",
            "divert_count",
            "flow_direction",
            "team_id",
            "task_type",
        )
        for field in required:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)

        incident = self.repository.get_entity(data["incident_id"])
        if not incident or incident["kind"] != "incident":
            raise ValidationError("incident does not exist")
        if incident["data"].get("venue_id") != data["venue_id"]:
            raise ValidationError("incident must belong to the venue")
        if incident["status"] == "resolved":
            raise ConflictError("cannot divert for a resolved incident")

        target = self.repository.get_entity(data["target_zone_id"])
        if not target or target["kind"] != "zone":
            raise ValidationError("target zone does not exist")
        if target["data"].get("venue_id") != data["venue_id"]:
            raise ValidationError("target zone must belong to the venue")

        divert_count = self._as_int(data["divert_count"], "divert_count")
        if divert_count <= 0:
            raise ValidationError("divert_count must be positive")
        if remaining_capacity(target) < divert_count:
            raise ConflictError(
                "zone remaining capacity conflict: requested %s, remaining %s"
                % (divert_count, remaining_capacity(target))
            )

        zone_ids = list(data["zone_ids"])
        gates = []
        for gate_id in data["gate_ids"]:
            gate = self.repository.get_entity(gate_id)
            if not gate or gate["kind"] != "gate":
                raise ValidationError("gate does not exist: " + str(gate_id))
            if gate["data"].get("venue_id") != data["venue_id"]:
                raise ValidationError("gate must belong to the venue")
            gates.append(gate)

        medical_points = []
        for point_id in data.get("medical_point_ids") or []:
            point = self.repository.get_entity(point_id)
            if not point or point["kind"] != "medical_point":
                raise ValidationError("medical point does not exist: " + str(point_id))
            if point["data"].get("venue_id") != data["venue_id"]:
                raise ValidationError("medical point must belong to the venue")
            medical_points.append(point)

        flow_direction = data["flow_direction"]
        if flow_direction not in FLOW_DIRECTIONS:
            raise ValidationError("unsupported channel direction")

        zones = [target]
        for zone_id in zone_ids:
            zone = self.repository.get_entity(zone_id)
            if not zone or zone["kind"] != "zone":
                raise ValidationError("affected zone does not exist: " + str(zone_id))
            if zone["data"].get("venue_id") != data["venue_id"]:
                raise ValidationError("affected zone must belong to the venue")
            zones.append(zone)

        if data.get("task_zone_id"):
            task_zone = self.repository.get_entity(data["task_zone_id"])
            if not task_zone or task_zone["kind"] != "zone":
                raise ValidationError("task zone does not exist")
            zones.append(task_zone)

        snapshot_entities = [incident] + zones + gates + medical_points
        return {
            "incident_id": incident["id"],
            "venue_id": data["venue_id"],
            "zone_ids": zone_ids,
            "gate_ids": [gate["id"] for gate in gates],
            "medical_point_ids": [point["id"] for point in medical_points],
            "target_zone_id": target["id"],
            "divert_count": divert_count,
            "flow_direction": flow_direction,
            "team_id": data["team_id"],
            "task_type": data["task_type"],
            "task_zone_id": data.get("task_zone_id"),
            "snapshot_entities": snapshot_entities,
        }

    # ------------------------------------------------------------------
    # saga execution
    # ------------------------------------------------------------------
    def _run(self, actor, command):
        while True:
            step = next(
                (
                    item
                    for item in command["data"]["steps"]
                    if item["status"] in ("pending", "failed")
                ),
                None,
            )
            if step is None:
                break
            reasons = self._stale_reasons(command)
            if reasons:
                self._mark_stale(command, reasons, actor, step["key"])
            try:
                self._execute_step(actor, command, step)
            except Exception as exc:
                from .domain import DomainError

                if isinstance(exc, DomainError):
                    self._mark_step(command, step["key"], "failed")
                    recorded = self._record_failure(command, step, exc, actor)
                    # A command submit that fails mid-saga remains retryable,
                    # so surface it as a conflict with the command id attached.
                    if not isinstance(exc, ConflictError):
                        wrapped = ConflictError(str(exc))
                        wrapped.command_id = command["id"]
                        raise wrapped from exc
                    raise recorded
                raise
            # The step data may have been reloaded by an inner save (dispatch),
            # so mark the fresh copy by key.
            self._mark_step(command, step["key"], "done")
            command = self._save(command)
        command["status"] = "completed"
        command["data"]["failure"] = None
        command = self._save(command)
        self.service.audit.record(
            command["id"], actor, "complete", "executing", "completed", {}
        )
        return command

    @staticmethod
    def _mark_step(command, key, status):
        for item in command["data"]["steps"]:
            if item["key"] == key:
                item["status"] = status
                return

    def _execute_step(self, actor, command, step):
        key = step["key"]
        reason = "diversion command " + command["id"]
        if key.startswith("freeze:zone:"):
            self._freeze(actor, command, key.split(":", 2)[2], "zone", reason)
        elif key.startswith("freeze:gate:"):
            self._freeze(actor, command, key.split(":", 2)[2], "gate", reason)
        elif key.startswith("freeze:medical:"):
            self._freeze(actor, command, key.split(":", 2)[2], "medical_point", reason)
        elif key.startswith("direct:gate:"):
            self._direct_gate(actor, command, key.split(":", 2)[2])
        elif key == "dispatch:task":
            self._dispatch_task(actor, command)
        else:
            raise InvalidTransition("unknown command step: " + key)

    def _freeze(self, actor, command, entity_id, kind, reason):
        entity = self.repository.get_entity(entity_id)
        if entity and entity["status"] == "frozen":
            # Idempotent recovery: this same command performed the freeze
            # before crashing. A freeze made by someone else is a conflict.
            if entity["data"].get("frozen_command") == command["id"]:
                return entity
            raise ConflictError(
                "%s %s is already frozen by another command" % (kind, entity_id)
            )
        updated = self.service.transition(
            actor,
            entity_id,
            "freeze",
            {"reason": reason, "command_id": command["id"]},
        )
        self._refresh_snapshot(command, updated)
        return updated

    def _direct_gate(self, actor, command, gate_id):
        direction = command["data"]["flow_direction"]
        gate = self.repository.get_entity(gate_id)
        if (
            gate["status"] == "frozen"
            and gate["data"].get("frozen_command") == command["id"]
            and gate["data"].get("flow_direction") == direction
        ):
            return gate
        if gate["status"] == "frozen" and gate["data"].get("frozen_command") != command["id"]:
            raise ConflictError("gate %s is frozen by another command" % gate_id)
        updated = self.service.transition(
            actor, gate_id, "direct", {"flow_direction": direction}
        )
        self._refresh_snapshot(command, updated)
        return updated

    def _dispatch_task(self, actor, command):
        data = command["data"]
        task_id = data.get("task_id")
        if task_id:
            task = self.repository.get_entity(task_id)
            if task and task["status"] == "draft":
                assigned = self.service.transition(
                    actor, task_id, "assign", {"assigned_at": utcnow()}
                )
                self._refresh_snapshot(command, assigned)
            return
        task = self.service.create(
            actor,
            "task",
            {
                "incident_id": data["incident_id"],
                "venue_id": data["venue_id"],
                "zone_id": data.get("task_zone_id") or data["target_zone_id"],
                "team_id": data["team_id"],
                "task_type": data["task_type"],
            },
        )
        # Persist the task id before assigning so a retry never creates a
        # second rescue task for the same command.
        data["task_id"] = task["id"]
        self._save(command)
        assigned = self.service.transition(
            actor, task["id"], "assign", {"assigned_at": utcnow()}
        )
        self._refresh_snapshot(command, assigned)

    # ------------------------------------------------------------------
    # command lifecycle actions
    # ------------------------------------------------------------------
    def action(self, actor, command_id, action_name, data, expected_version=None):
        command = self._get(command_id)
        if expected_version is not None and command["version"] != int(expected_version):
            raise ConflictError(
                "version conflict: expected %s, found %s"
                % (expected_version, command["version"])
            )
        self.rules._ensure_role(actor, COMMAND_ACTORS)
        if action_name == "retry":
            return self._retry(actor, command)
        if action_name == "cancel":
            return self._cancel(actor, command, dict(data or {}))
        if action_name == "reconcile":
            return self._reconcile(actor, command, dict(data or {}))
        raise InvalidTransition("unknown action %s for command" % action_name)

    def _retry(self, actor, command):
        if command["status"] == "stale":
            error = StaleCommandError(
                "command was invalidated and cannot be retried; cancel and submit a new command",
                reasons=command["data"].get("stale_reasons"),
            )
            error.command_id = command["id"]
            raise error
        if command["status"] != "failed":
            raise InvalidTransition(
                "only failed commands can be retried, current status: " + command["status"]
            )
        for step in command["data"]["steps"]:
            if step["status"] == "failed":
                step["status"] = "pending"
        command["data"]["failure"] = None
        command["status"] = "executing"
        self._save(command)
        self.service.audit.record(command["id"], actor, "retry", "failed", "executing", {})
        return self._run(actor, command)

    def _cancel(self, actor, command, data):
        if command["status"] not in ACTIVE_COMMAND_STATUSES:
            raise InvalidTransition(
                "cannot cancel command with status " + command["status"]
            )
        reason = data.get("reason")
        if not reason:
            raise ValidationError("cancel reason is required")
        command_data = command["data"]

        task_id = command_data.get("task_id")
        if task_id:
            task = self.repository.get_entity(task_id)
            if task and task["status"] in ("draft", "assigned", "enroute", "on_scene"):
                self.service.transition(
                    actor, task_id, "cancel", {"reason": "command cancelled: " + reason}
                )

        for gate_id in command_data["gate_ids"]:
            gate = self.repository.get_entity(gate_id)
            if gate and gate["status"] == "frozen":
                self.service.transition(
                    actor, gate_id, "unfreeze", {"reason": "command cancelled: " + reason}
                )
        for point_id in command_data["medical_point_ids"]:
            point = self.repository.get_entity(point_id)
            if point and point["status"] == "frozen":
                self.service.transition(
                    actor, point_id, "unfreeze", {"reason": "command cancelled: " + reason}
                )
        for zone_id in command_data["zone_ids"]:
            zone = self.repository.get_entity(zone_id)
            if zone and zone["status"] == "frozen":
                self.service.transition(
                    actor, zone_id, "unfreeze", {"reason": "command cancelled: " + reason}
                )

        target = self.repository.get_entity(command_data["target_zone_id"])
        if target:
            reserved = int(target["data"].get("reserved_occupancy", 0))
            target["data"]["reserved_occupancy"] = max(
                0, reserved - int(command_data["divert_count"])
            )
            self.repository.update_entity(
                target["id"], target["version"], target["status"], target["data"]
            )

        command_data["cancel_reason"] = reason
        command_data["cancelled_by"] = actor.user_id
        command_data["cancelled_at"] = utcnow()
        command["status"] = "cancelled"
        self._save(command)
        self.service.audit.record(
            command["id"], actor, "cancel", "executing", "cancelled", {"reason": reason}
        )
        return command

    def _reconcile(self, actor, command, data):
        if command["status"] != "completed":
            raise InvalidTransition(
                "only completed commands can be reconciled, current status: " + command["status"]
            )
        actual_count = self._as_int(data.get("actual_count"), "actual_count")
        divert_count = int(command["data"]["divert_count"])
        if actual_count < 0 or actual_count > divert_count:
            raise ValidationError(
                "actual_count must be between 0 and %s" % divert_count
            )
        target = self.repository.get_entity(command["data"]["target_zone_id"])
        if not target:
            raise NotFoundError("target zone no longer exists")
        occupancy = int(target["data"].get("current_occupancy", 0))
        reserved = int(target["data"].get("reserved_occupancy", 0))
        target["data"]["current_occupancy"] = occupancy + actual_count
        target["data"]["reserved_occupancy"] = max(0, reserved - divert_count)
        self.repository.update_entity(
            target["id"], target["version"], target["status"], target["data"]
        )
        command["data"]["actual_count"] = actual_count
        command["data"]["reconciled"] = True
        command["data"]["reconciled_by"] = actor.user_id
        command["data"]["reconciled_at"] = utcnow()
        command["status"] = "reconciled"
        self._save(command)
        self.service.audit.record(
            command["id"],
            actor,
            "reconcile",
            "completed",
            "reconciled",
            {"actual_count": actual_count, "divert_count": divert_count},
        )
        return command
