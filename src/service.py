from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, InvalidTransition, NotFoundError, PermissionDenied, ValidationError
from .repository import utcnow
from .rules import PROTECTIVE_STATUSES, RuleEngine


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        validated = self.rules.validate_create(actor, kind, payload, self._lookup)
        if validated:
            payload.update(validated)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        kind = self.rules.normalize_kind(entity["kind"])
        expected = int(expected_version) if expected_version is not None else entity["version"]

        if kind == "diversion_order":
            if action == "submit" and entity["status"] in (
                "submitted",
                "in_progress",
                "completed",
                "blocked",
            ):
                return entity
            if action == "retry" and entity["status"] == "in_progress":
                return entity
            self.rules.validate_transition(actor, entity, action, dict(data or {}), self._lookup)
            return self._handle_diversion_order(actor, entity, action, data or {}, expected)

        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        if kind in ("incident", "zone", "gate"):
            self._invalidate_orders_for(kind, entity_id)
        return updated

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)

    # ------------------------------------------------------------------
    # Diversion order orchestration (saga-style)
    # ------------------------------------------------------------------

    def _handle_diversion_order(self, actor, order, action, data, expected_version):
        if action == "submit":
            result = self._submit_order(actor, order, expected_version)
        elif action == "retry":
            result = self._retry_order(actor, order, expected_version)
        elif action == "cancel":
            result = self._cancel_order(actor, order, data, expected_version)
        elif action == "reconcile":
            self._reconcile_order_data(order)
            result = self._save_order(order, expected_version)
        else:
            raise InvalidTransition("unknown diversion order action: %s" % action)
        self.audit.record(
            order["id"],
            actor,
            action,
            order["status"],
            result["status"],
            {"blocked_reason": result["data"].get("blocked_reason")},
        )
        return result

    def _submit_order(self, actor, order, expected_version):
        if order["status"] in ("submitted", "in_progress", "completed", "blocked"):
            return order
        if order["status"] != "draft":
            raise InvalidTransition("cannot submit order in status %s" % order["status"])
        matches, reason = self._fingerprint_matches(order)
        if not matches:
            return self._mark_invalidated(order, reason, expected_version)
        return self._run_steps(actor, order, expected_version)

    def _retry_order(self, actor, order, expected_version):
        if order["status"] not in ("blocked", "invalidated"):
            raise InvalidTransition("cannot retry order in status %s" % order["status"])
        matches, reason = self._fingerprint_matches(order)
        if not matches:
            if order["status"] == "invalidated":
                order["data"]["invalidated_reason"] = reason
                order["data"]["blocked_reason"] = reason
                return self._save_order(order, expected_version)
            return self._mark_invalidated(order, reason, expected_version)
        for step in order["data"]["steps"]:
            if step["status"] == "failed":
                step["status"] = "pending"
                step["error"] = None
        order["status"] = "in_progress"
        order["data"]["blocked_reason"] = None
        order["data"]["blocked_step"] = None
        return self._run_steps(actor, order, expected_version)

    def _cancel_order(self, actor, order, data, expected_version):
        if order["status"] not in ("draft", "blocked", "invalidated"):
            raise InvalidTransition("cannot cancel order in status %s" % order["status"])
        reason = (data or {}).get("reason")
        if not reason:
            raise ValidationError("cancel reason is required")
        order["status"] = "cancelled"
        order["data"]["cancel_reason"] = reason
        return self._save_order(order, expected_version)

    def _run_steps(self, actor, order, expected_version):
        for step in order["data"]["steps"]:
            if step["status"] == "done":
                continue
            step["attempts"] = int(step.get("attempts", 0)) + 1
            try:
                result = self._apply_step(actor, step, order)
                step["status"] = "done"
                step["result"] = result
                step["error"] = None
                step["applied_at"] = utcnow()
            except (ConflictError, ValidationError, NotFoundError, PermissionDenied) as exc:
                step["status"] = "failed"
                step["error"] = str(exc)
                order["data"]["blocked_reason"] = str(exc)
                order["data"]["blocked_step"] = step["key"]
                order["status"] = "blocked"
                self._save_order(order, expected_version)
                raise
        order["status"] = "completed"
        order["data"]["blocked_reason"] = None
        order["data"]["blocked_step"] = None
        order["data"]["invalidated_reason"] = None
        self._reconcile_order_data(order)
        return self._save_order(order, expected_version)

    def _apply_step(self, actor, step, order):
        kind = step["kind"]
        if kind == "freeze_zone":
            return self._apply_freeze_zone(actor, step, order)
        if kind == "freeze_gate":
            return self._apply_freeze_gate(actor, step, order)
        if kind == "freeze_medical":
            return self._apply_freeze_medical(actor, step, order)
        if kind == "transfer":
            return self._apply_transfer(actor, step, order)
        if kind == "dispatch_task":
            return self._apply_dispatch_task(actor, step, order)
        raise ValidationError("unknown step kind: %s" % kind)

    def _apply_freeze_zone(self, actor, step, order):
        zone = self.repository.get_entity(step["target_id"])
        if not zone:
            raise NotFoundError("zone not found: " + step["target_id"])
        if zone["status"] in PROTECTIVE_STATUSES:
            return {"status": zone["status"], "note": "already protected"}
        if zone["status"] in ("open", "limited"):
            updated = self.transition(
                actor,
                zone["id"],
                "freeze",
                {"reason": "diversion order %s" % order["id"]},
            )
            return {"status": updated["status"], "frozen_by": actor.user_id}
        raise ConflictError(
            "zone %s cannot be frozen from status %s" % (zone["id"], zone["status"])
        )

    def _apply_freeze_gate(self, actor, step, order):
        gate = self.repository.get_entity(step["target_id"])
        if not gate:
            raise NotFoundError("gate not found: " + step["target_id"])
        if gate["status"] in ("frozen", "closed"):
            return {"status": gate["status"], "note": "already protected"}
        if gate["status"] in ("open", "restricted"):
            updated = self.transition(
                actor,
                gate["id"],
                "freeze",
                {"reason": "diversion order %s" % order["id"]},
            )
            return {"status": updated["status"], "frozen_by": actor.user_id}
        raise ConflictError(
            "gate %s cannot be frozen from status %s" % (gate["id"], gate["status"])
        )

    def _apply_freeze_medical(self, actor, step, order):
        medical = self.repository.get_entity(step["target_id"])
        if not medical:
            raise NotFoundError("medical point not found: " + step["target_id"])
        if medical["status"] in ("frozen", "closed", "full"):
            return {"status": medical["status"], "note": "already protected"}
        if medical["status"] in ("standby", "active"):
            updated = self.transition(
                actor,
                medical["id"],
                "freeze",
                {"reason": "diversion order %s" % order["id"]},
            )
            return {"status": updated["status"], "frozen_by": actor.user_id}
        raise ConflictError(
            "medical point %s cannot be frozen from status %s"
            % (medical["id"], medical["status"])
        )

    def _resolve_transfer_gate(self, zone_id, preferred_gate_id):
        if preferred_gate_id:
            gate = self.repository.get_entity(preferred_gate_id)
            if (
                gate
                and gate["status"] == "open"
                and zone_id in (gate["data"].get("zone_ids") or [])
            ):
                return preferred_gate_id
        for gate in self.repository.list_entities(kind="gate", status="open"):
            if zone_id in (gate["data"].get("zone_ids") or []):
                return gate["id"]
        raise ConflictError("no open gate serves target zone %s" % zone_id)

    def _apply_transfer(self, actor, step, order):
        data = order["data"]
        target_id = data["target_zone_id"]
        count = int(data["transfer_count"])
        gate_id = self._resolve_transfer_gate(target_id, data.get("transfer_gate_id"))
        updated, applied = self.repository.admit_people(
            target_id, count, gate_id, utcnow(), order["id"]
        )
        if not applied:
            return {
                "status": "skipped",
                "occupancy": updated["data"]["current_occupancy"],
                "note": "already transferred",
            }
        order["data"]["transferred_count"] = int(data.get("transferred_count", 0)) + count
        return {
            "status": "transferred",
            "count": count,
            "occupancy": updated["data"]["current_occupancy"],
            "gate_id": gate_id,
        }

    def _apply_dispatch_task(self, actor, step, order):
        spec = (step.get("result") or {}).get("spec") or {}
        team_id = spec.get("team_id")
        task_type = spec.get("task_type", "rescue")
        zone_id = spec.get("zone_id") or order["data"]["target_zone_id"]
        incident_id = order["data"]["incident_id"]
        idem_key = "%s:task:%s:%s" % (order["id"], team_id, task_type)
        task = self.create(
            actor,
            "task",
            {
                "incident_id": incident_id,
                "venue_id": order["data"]["venue_id"],
                "zone_id": zone_id,
                "team_id": team_id,
                "task_type": task_type,
                "note": spec.get("note", ""),
            },
            idem_key,
        )
        if task["status"] == "draft":
            task = self.transition(actor, task["id"], "assign", {"assigned_at": utcnow()})
        return {
            "task_id": task["id"],
            "task_status": task["status"],
            "team_id": team_id,
        }

    def _fingerprint_matches(self, order):
        fp = order["data"].get("fingerprint")
        if not fp:
            return True, None
        incident = self.repository.get_entity(order["data"].get("incident_id"))
        if not incident:
            return False, "incident record is missing"
        if incident["data"].get("severity") != (fp.get("incident") or {}).get("severity"):
            return False, "incident severity changed after order was placed"
        for zone_id, fz in (fp.get("zones") or {}).items():
            zone = self.repository.get_entity(zone_id)
            if not zone:
                return False, "zone %s is missing" % zone_id
            if int(zone["data"].get("capacity", 0)) != int(fz.get("capacity", 0)):
                return False, "zone %s capacity changed after order was placed" % zone_id
            if fz.get("role") == "affected":
                acceptable = {fz.get("status"), "frozen", "evacuating"}
            else:
                acceptable = {"open", "limited"}
            if zone["status"] not in acceptable:
                return False, "zone %s status changed to %s after order was placed" % (
                    zone_id,
                    zone["status"],
                )
        for gate_id, fg in (fp.get("gates") or {}).items():
            gate = self.repository.get_entity(gate_id)
            if not gate:
                return False, "gate %s is missing" % gate_id
            acceptable = (
                {fg.get("status"), "frozen"}
                if fg.get("will_freeze")
                else {fg.get("status")}
            )
            if gate["status"] not in acceptable:
                return False, "gate %s status changed to %s after order was placed" % (
                    gate_id,
                    gate["status"],
                )
        for medical_id, fm in (fp.get("medical_points") or {}).items():
            medical = self.repository.get_entity(medical_id)
            if not medical:
                return False, "medical point %s is missing" % medical_id
            acceptable = (
                {fm.get("status"), "frozen"}
                if fm.get("will_freeze")
                else {fm.get("status")}
            )
            if medical["status"] not in acceptable:
                return False, "medical point %s status changed to %s after order was placed" % (
                    medical_id,
                    medical["status"],
                )
        return True, None

    def _invalidate_orders_for(self, kind, entity_id):
        active = {"draft", "submitted", "in_progress", "blocked", "completed"}
        for order in self.repository.list_entities(kind="diversion_order"):
            if order["status"] not in active:
                continue
            fp = order["data"].get("fingerprint") or {}
            referenced = False
            if kind == "incident" and fp.get("incident"):
                referenced = True
            elif kind == "zone" and entity_id in (fp.get("zones") or {}):
                referenced = True
            elif kind == "gate" and entity_id in (fp.get("gates") or {}):
                referenced = True
            if not referenced:
                continue
            matches, reason = self._fingerprint_matches(order)
            if not matches:
                self._mark_invalidated(order, reason, order["version"])

    def _mark_invalidated(self, order, reason, expected_version):
        order["status"] = "invalidated"
        order["data"]["invalidated_reason"] = reason
        order["data"]["blocked_reason"] = reason
        self._reconcile_order_data(order)
        return self._save_order(order, expected_version)

    def _save_order(self, order, expected_version):
        return self.repository.update_entity(
            order["id"], expected_version, order["status"], order["data"]
        )

    def _reconcile_order_data(self, order):
        data = order["data"]
        target_id = data.get("target_zone_id")
        actual = 0
        if target_id:
            zone = self.repository.get_entity(target_id)
            if zone:
                transfers = zone["data"].get("diversion_transfers") or {}
                record = transfers.get(order["id"])
                if record:
                    actual = int(record.get("count", 0))
        recorded = int(data.get("transferred_count", 0))
        data["write_off"] = recorded - actual
        data["transferred_count"] = actual
        data["reconciled"] = True
