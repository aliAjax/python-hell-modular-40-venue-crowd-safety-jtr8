from .domain import ConflictError, InvalidTransition, PermissionDenied, ValidationError


PASSAGE_DIRECTIONS = ("inbound", "outbound", "bidirectional")
PROTECTIVE_STATUSES = ("frozen", "evacuating", "closed")
SEVERITY_SCORES = {"low": 10, "medium": 30, "high": 60, "critical": 90}


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _step(key, kind, target_id, spec=None):
    return {
        "key": key,
        "kind": kind,
        "target_id": target_id,
        "status": "pending",
        "attempts": 0,
        "result": {"spec": spec} if spec is not None else None,
        "error": None,
        "applied_at": None,
    }


def incident_priority(severity, incident_type):
    type_bonus = {
        "fire": 10,
        "stampede": 12,
        "medical": 8,
        "crowd": 6,
        "security": 5,
        "structural": 10,
    }
    if severity not in SEVERITY_SCORES:
        raise ValidationError("unsupported severity")
    return min(100, SEVERITY_SCORES[severity] + type_bonus.get(incident_type, 0))


def capacity_available(capacity, occupancy, requested):
    return int(occupancy) + int(requested) <= int(capacity)


def _validate_venue(actor, data, lookup):
    if not str(data.get("name", "")).strip():
        raise ValidationError("venue name is required")
    return {}


def _validate_zone(actor, data, lookup):
    if not _find_one(lookup, "venue", "id", data.get("venue_id")):
        raise ValidationError("venue does not exist")
    if int(data.get("capacity", 0)) <= 0:
        raise ValidationError("zone capacity must be positive")
    return {"current_occupancy": 0}


def _validate_gate(actor, data, lookup):
    venue = _find_one(lookup, "venue", "id", data.get("venue_id"))
    if not venue:
        raise ValidationError("venue does not exist")
    zone_ids = data.get("zone_ids") or []
    if not zone_ids:
        raise ValidationError("gate must connect at least one zone")
    for zone_id in zone_ids:
        zone = _find_one(lookup, "zone", "id", zone_id)
        if not zone or zone["data"].get("venue_id") != venue["id"]:
            raise ValidationError("gate zones must belong to the venue")
    return {}


def _validate_post(actor, data, lookup):
    zone = _find_one(lookup, "zone", "id", data.get("zone_id"))
    if not zone or zone["data"].get("venue_id") != data.get("venue_id"):
        raise ValidationError("post zone must belong to the venue")
    if int(data.get("staff_count", 0)) <= 0:
        raise ValidationError("staff_count must be positive")
    return {}


def _validate_medical_point(actor, data, lookup):
    zone = _find_one(lookup, "zone", "id", data.get("zone_id"))
    if not zone or zone["data"].get("venue_id") != data.get("venue_id"):
        raise ValidationError("medical point zone must belong to the venue")
    if int(data.get("capacity", 0)) <= 0:
        raise ValidationError("medical point capacity must be positive")
    return {"patients": 0}


def _validate_incident(actor, data, lookup):
    zone = _find_one(lookup, "zone", "id", data.get("zone_id"))
    if not zone or zone["data"].get("venue_id") != data.get("venue_id"):
        raise ValidationError("incident zone must belong to the venue")
    incident_key = "%s:%s" % (data["venue_id"], data["source_ref"])
    if _find_one(lookup, "incident", "incident_key", incident_key):
        raise ConflictError("duplicate incident source reference: " + incident_key)
    return {
        "incident_key": incident_key,
        "priority_score": incident_priority(data.get("severity"), data.get("incident_type")),
    }


def _validate_task(actor, data, lookup):
    incident = _find_one(lookup, "incident", "id", data.get("incident_id"))
    if not incident or incident["data"].get("venue_id") != data.get("venue_id"):
        raise ValidationError("task incident must belong to the venue")
    if not _find_one(lookup, "zone", "id", data.get("zone_id")):
        raise ValidationError("task zone does not exist")
    return {}


def _validate_zone_admit(actor, entity, data, lookup):
    try:
        count = int(data.get("count"))
    except (TypeError, ValueError):
        raise ValidationError("admission count must be an integer")
    if count <= 0:
        raise ValidationError("admission count must be positive")
    gate = _find_one(lookup, "gate", "id", data.get("gate_id"))
    if not gate or gate["status"] != "open":
        raise ConflictError("entry gate is not open")
    if entity["id"] not in (gate["data"].get("zone_ids") or []):
        raise ValidationError("gate does not serve this zone")
    occupancy = int(entity["data"].get("current_occupancy", 0))
    capacity = int(entity["data"].get("capacity", 0))
    if not capacity_available(capacity, occupancy, count):
        raise ConflictError("zone capacity would be exceeded")
    if entity["status"] == "limited":
        limit = int(entity["data"].get("admit_limit", capacity))
        if occupancy + count > limit:
            raise ConflictError("zone admission limit would be exceeded")
    return {
        "current_occupancy": occupancy + count,
        "last_admission_at": data.get("admitted_at"),
        "last_gate_id": gate["id"],
    }


def _validate_gate_open(actor, entity, data, lookup):
    for zone_id in entity["data"].get("zone_ids") or []:
        zone = _find_one(lookup, "zone", "id", zone_id)
        if zone and zone["status"] == "evacuating":
            raise ConflictError("gate cannot open while a connected zone is evacuating")
    return {"opened_by": actor.user_id}


def _validate_task_assign(actor, entity, data, lookup):
    active = {"assigned", "enroute", "on_scene"}
    for task in lookup("task", "team_id", entity["data"].get("team_id")) or []:
        if task["id"] != entity["id"] and task["status"] in active:
            raise ConflictError("team already has an active task")
    return {"assigned_by": actor.user_id}


def _validate_correct(actor, entity, data, lookup):
    if not data.get("reason"):
        raise ValidationError("correction reason is required")
    history = list(entity["data"].get("correction_history") or [])
    history.append({"actor_id": actor.user_id, "reason": data["reason"], "from_status": entity["status"]})
    return {"correction_history": history}


def _validate_zone_adjust_capacity(actor, entity, data, lookup):
    try:
        capacity = int(data.get("capacity"))
    except (TypeError, ValueError):
        raise ValidationError("capacity must be an integer")
    if capacity <= 0:
        raise ValidationError("capacity must be positive")
    return {"capacity": capacity}


def _validate_incident_escalate(actor, entity, data, lookup):
    severity = data.get("severity")
    if severity not in SEVERITY_SCORES:
        raise ValidationError("severity must be low, medium, high, or critical")
    incident_type = entity["data"].get("incident_type")
    return {
        "severity": severity,
        "priority_score": incident_priority(severity, incident_type),
        "last_escalated_by": actor.user_id,
    }


def _validate_freeze(actor, entity, data, lookup):
    return {"frozen_by": actor.user_id}


def _validate_diversion_order_create(actor, data, lookup):
    venue = _find_one(lookup, "venue", "id", data.get("venue_id"))
    if not venue:
        raise ValidationError("venue does not exist")
    incident = _find_one(lookup, "incident", "id", data.get("incident_id"))
    if not incident or incident["data"].get("venue_id") != venue["id"]:
        raise ValidationError("incident does not exist or belongs to another venue")

    zone_ids = data.get("zone_ids") or []
    if not zone_ids:
        raise ValidationError("at least one affected zone is required")
    for zone_id in zone_ids:
        zone = _find_one(lookup, "zone", "id", zone_id)
        if not zone or zone["data"].get("venue_id") != venue["id"]:
            raise ValidationError("affected zone does not exist or belongs to another venue")

    gate_ids = data.get("gate_ids") or []
    if not gate_ids:
        raise ValidationError("at least one gate is required")
    for gate_id in gate_ids:
        gate = _find_one(lookup, "gate", "id", gate_id)
        if not gate or gate["data"].get("venue_id") != venue["id"]:
            raise ValidationError("gate does not exist or belongs to another venue")

    medical_point_ids = data.get("medical_point_ids") or []
    for medical_id in medical_point_ids:
        medical = _find_one(lookup, "medical_point", "id", medical_id)
        if not medical or medical["data"].get("venue_id") != venue["id"]:
            raise ValidationError("medical point does not exist or belongs to another venue")

    target_zone = _find_one(lookup, "zone", "id", data.get("target_zone_id"))
    if not target_zone or target_zone["data"].get("venue_id") != venue["id"]:
        raise ValidationError("target zone does not exist")
    if target_zone["id"] in zone_ids:
        raise ValidationError("target zone must be distinct from affected zones")

    transfer_gate_id = data.get("transfer_gate_id")
    if transfer_gate_id:
        if transfer_gate_id in gate_ids:
            raise ValidationError("transfer gate cannot be one of the frozen gates")
        transfer_gate = _find_one(lookup, "gate", "id", transfer_gate_id)
        if not transfer_gate or transfer_gate["data"].get("venue_id") != venue["id"]:
            raise ValidationError("transfer gate does not exist")
        if target_zone["id"] not in (transfer_gate["data"].get("zone_ids") or []):
            raise ValidationError("transfer gate must serve the target zone")

    try:
        transfer_count = int(data.get("transfer_count"))
    except (TypeError, ValueError):
        raise ValidationError("transfer_count must be an integer")
    if transfer_count <= 0:
        raise ValidationError("transfer_count must be positive")

    passage_direction = data.get("passage_direction")
    if passage_direction not in PASSAGE_DIRECTIONS:
        raise ValidationError("passage_direction must be one of %s" % ", ".join(PASSAGE_DIRECTIONS))

    rescue_tasks = data.get("rescue_tasks") or []
    if not isinstance(rescue_tasks, list):
        raise ValidationError("rescue_tasks must be a list")
    for task in rescue_tasks:
        if not isinstance(task, dict) or not task.get("team_id"):
            raise ValidationError("each rescue task requires a team_id")
        if not task.get("task_type"):
            raise ValidationError("each rescue task requires a task_type")

    steps = []
    for zone_id in zone_ids:
        steps.append(_step("freeze_zone:%s" % zone_id, "freeze_zone", zone_id))
    for gate_id in gate_ids:
        steps.append(_step("freeze_gate:%s" % gate_id, "freeze_gate", gate_id))
    for medical_id in medical_point_ids:
        steps.append(_step("freeze_medical:%s" % medical_id, "freeze_medical", medical_id))
    steps.append(_step("transfer", "transfer", target_zone["id"]))
    for task in rescue_tasks:
        key = "task:%s:%s" % (task["team_id"], task["task_type"])
        steps.append(_step(key, "dispatch_task", key, spec=task))

    fingerprint = {"incident": {}, "zones": {}, "gates": {}, "medical_points": {}}
    fingerprint["incident"] = {
        "severity": incident["data"].get("severity"),
        "priority": incident["data"].get("priority_score"),
    }
    for zone_id in zone_ids:
        zone = _find_one(lookup, "zone", "id", zone_id)
        fingerprint["zones"][zone_id] = {
            "capacity": zone["data"].get("capacity"),
            "status": zone["status"],
            "role": "affected",
        }
    fingerprint["zones"][target_zone["id"]] = {
        "capacity": target_zone["data"].get("capacity"),
        "status": target_zone["status"],
        "role": "target",
    }
    for gate_id in gate_ids:
        gate = _find_one(lookup, "gate", "id", gate_id)
        fingerprint["gates"][gate_id] = {"status": gate["status"], "will_freeze": True}
    for medical_id in medical_point_ids:
        medical = _find_one(lookup, "medical_point", "id", medical_id)
        fingerprint["medical_points"][medical_id] = {
            "status": medical["status"],
            "will_freeze": True,
        }

    return {
        "steps": steps,
        "fingerprint": fingerprint,
        "transferred_count": 0,
        "write_off": 0,
        "reconciled": False,
        "blocked_reason": None,
        "blocked_step": None,
        "cancel_reason": None,
        "invalidated_reason": None,
    }


class RuleEngine:
    ALIASES = {
        "venues": "venue",
        "zones": "zone",
        "gates": "gate",
        "posts": "post",
        "medical_points": "medical_point",
        "incidents": "incident",
        "tasks": "task",
        "diversion_orders": "diversion_order",
    }
    INITIAL_STATUS = {
        "venue": "ready",
        "zone": "closed",
        "gate": "closed",
        "post": "planned",
        "medical_point": "standby",
        "incident": "reported",
        "task": "draft",
        "diversion_order": "draft",
    }
    TRANSITIONS = {
        "venue": {
            "limit": (("ready",), "limited"),
            "close": (("ready", "limited"), "closed"),
            "reopen": (("limited", "closed"), "ready"),
        },
        "zone": {
            "open": (("closed",), "open"),
            "admit": (("open", "limited"), "open"),
            "restrict": (("open",), "limited"),
            "freeze": (("open", "limited"), "frozen"),
            "unfreeze": (("frozen",), "open"),
            "adjust_capacity": (("open", "limited", "frozen"), "same"),
            "evacuate": (("open", "limited", "frozen"), "evacuating"),
            "recover": (("evacuating", "limited"), "open"),
            "close": (("open", "limited", "frozen"), "closed"),
            "correct": (("closed", "open", "limited", "evacuating", "frozen"), "closed"),
        },
        "gate": {
            "open": (("closed", "frozen"), "open"),
            "restrict": (("open",), "restricted"),
            "freeze": (("open", "restricted"), "frozen"),
            "unfreeze": (("frozen",), "open"),
            "close": (("open", "restricted", "frozen"), "closed"),
            "restore": (("restricted",), "open"),
        },
        "post": {
            "activate": (("planned", "suspended"), "active"),
            "suspend": (("active",), "suspended"),
        },
        "medical_point": {
            "activate": (("standby", "closed"), "active"),
            "freeze": (("standby", "active"), "frozen"),
            "unfreeze": (("frozen",), "standby"),
            "mark_full": (("active",), "full"),
            "close": (("standby", "active", "full", "frozen", "closed"), "closed"),
        },
        "incident": {
            "triage": (("reported",), "triaged"),
            "dispatch": (("triaged",), "dispatched"),
            "escalate": (("reported", "triaged", "dispatched", "reopened"), "same"),
            "resolve": (("dispatched", "reopened"), "resolved"),
            "reopen": (("resolved",), "reopened"),
            "correct": (("reported", "triaged", "dispatched", "resolved", "reopened"), "triaged"),
        },
        "task": {
            "assign": (("draft",), "assigned"),
            "acknowledge": (("assigned",), "enroute"),
            "arrive": (("enroute",), "on_scene"),
            "complete": (("on_scene",), "completed"),
            "cancel": (("draft", "assigned", "enroute", "on_scene"), "cancelled"),
        },
        "diversion_order": {
            "submit": (("draft",), "in_progress"),
            "retry": (("blocked", "invalidated"), "in_progress"),
            "cancel": (("draft", "blocked", "invalidated"), "cancelled"),
            "reconcile": (("completed", "blocked", "invalidated", "in_progress"), "same"),
        },
    }
    CREATE_REQUIRED = {
        "venue": ("name", "address"),
        "zone": ("venue_id", "name", "capacity"),
        "gate": ("venue_id", "name", "zone_ids"),
        "post": ("venue_id", "zone_id", "staff_count", "duty"),
        "medical_point": ("venue_id", "zone_id", "capacity", "equipment_level"),
        "incident": ("venue_id", "zone_id", "source_ref", "incident_type", "severity", "reported_at"),
        "task": ("incident_id", "venue_id", "zone_id", "team_id", "task_type"),
        "diversion_order": (
            "venue_id",
            "incident_id",
            "zone_ids",
            "gate_ids",
            "transfer_count",
            "target_zone_id",
            "passage_direction",
        ),
    }
    ACTION_REQUIRED = {
        ("venue", "limit"): ("reason", "capacity_limit"),
        ("venue", "close"): ("reason",),
        ("zone", "admit"): ("gate_id", "count", "admitted_at"),
        ("zone", "restrict"): ("reason", "admit_limit"),
        ("zone", "freeze"): ("reason",),
        ("zone", "unfreeze"): ("reason",),
        ("zone", "adjust_capacity"): ("reason", "capacity"),
        ("zone", "evacuate"): ("reason",),
        ("zone", "recover"): ("checklist",),
        ("zone", "close"): ("reason",),
        ("zone", "correct"): ("reason",),
        ("gate", "open"): ("operator_id",),
        ("gate", "restrict"): ("reason", "flow_limit"),
        ("gate", "freeze"): ("reason",),
        ("gate", "unfreeze"): ("reason",),
        ("gate", "close"): ("reason",),
        ("post", "suspend"): ("reason",),
        ("medical_point", "freeze"): ("reason",),
        ("medical_point", "unfreeze"): ("reason",),
        ("medical_point", "mark_full"): ("reason",),
        ("medical_point", "close"): ("reason",),
        ("incident", "triage"): ("priority",),
        ("incident", "dispatch"): ("commander_id",),
        ("incident", "escalate"): ("severity",),
        ("incident", "resolve"): ("resolution",),
        ("incident", "reopen"): ("reason",),
        ("incident", "correct"): ("reason",),
        ("task", "assign"): ("assigned_at",),
        ("task", "acknowledge"): ("acknowledged_at",),
        ("task", "arrive"): ("arrived_at",),
        ("task", "complete"): ("completed_at", "outcome"),
        ("task", "cancel"): ("reason",),
        ("diversion_order", "cancel"): ("reason",),
    }
    CREATE_ROLES = {
        "venue": ("coordinator", "admin"),
        "zone": ("coordinator", "admin"),
        "gate": ("coordinator", "admin"),
        "post": ("supervisor", "coordinator", "admin"),
        "medical_point": ("supervisor", "coordinator", "admin"),
        "incident": ("operator", "supervisor", "coordinator", "admin"),
        "task": ("supervisor", "coordinator", "admin"),
        "diversion_order": ("supervisor", "coordinator", "admin"),
    }
    ROLE_ACTIONS = {
        "limit": ("coordinator", "supervisor", "admin"),
        "close": ("coordinator", "supervisor", "admin"),
        "reopen": ("coordinator", "admin"),
        "open": ("operator", "supervisor", "coordinator", "admin"),
        "admit": ("operator", "supervisor", "admin"),
        "restrict": ("supervisor", "coordinator", "admin"),
        "freeze": ("operator", "supervisor", "coordinator", "admin"),
        "unfreeze": ("operator", "supervisor", "coordinator", "admin"),
        "adjust_capacity": ("coordinator", "admin"),
        "evacuate": ("supervisor", "coordinator", "admin"),
        "recover": ("supervisor", "coordinator", "admin"),
        "correct": ("supervisor", "admin"),
        "restore": ("operator", "supervisor", "admin"),
        "activate": ("supervisor", "admin"),
        "suspend": ("supervisor", "admin"),
        "mark_full": ("operator", "supervisor", "admin"),
        "triage": ("supervisor", "coordinator", "admin"),
        "dispatch": ("coordinator", "admin"),
        "escalate": ("coordinator", "admin"),
        "resolve": ("supervisor", "coordinator", "admin"),
        "assign": ("supervisor", "coordinator", "admin"),
        "acknowledge": ("operator", "supervisor", "admin"),
        "arrive": ("operator", "supervisor", "admin"),
        "complete": ("operator", "supervisor", "admin"),
        "cancel": ("supervisor", "coordinator", "admin"),
        "submit": ("supervisor", "coordinator", "admin"),
        "retry": ("supervisor", "coordinator", "admin"),
        "reconcile": ("supervisor", "coordinator", "admin"),
    }
    CUSTOM_CREATE = {
        "venue": _validate_venue,
        "zone": _validate_zone,
        "gate": _validate_gate,
        "post": _validate_post,
        "medical_point": _validate_medical_point,
        "incident": _validate_incident,
        "task": _validate_task,
        "diversion_order": _validate_diversion_order_create,
    }
    CUSTOM_TRANSITIONS = {
        ("zone", "admit"): _validate_zone_admit,
        ("zone", "correct"): _validate_correct,
        ("zone", "adjust_capacity"): _validate_zone_adjust_capacity,
        ("zone", "freeze"): _validate_freeze,
        ("gate", "open"): _validate_gate_open,
        ("gate", "freeze"): _validate_freeze,
        ("medical_point", "freeze"): _validate_freeze,
        ("incident", "correct"): _validate_correct,
        ("incident", "escalate"): _validate_incident_escalate,
        ("task", "assign"): _validate_task_assign,
    }

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    @staticmethod
    def _ensure_role(actor, allowed):
        if actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    @staticmethod
    def _require(data, fields):
        for field in fields:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        self._ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        self._require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = self.CUSTOM_CREATE.get(kind)
        return custom(actor, data, lookup) if custom else {}

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if next_status == "same":
            next_status = entity["status"]
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition("cannot %s from status %s" % (action, entity["status"]))
        allowed_roles = self.ROLE_ACTIONS.get((kind, action), self.ROLE_ACTIONS.get(action, ("admin",)))
        self._ensure_role(actor, allowed_roles)
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = self.CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch
