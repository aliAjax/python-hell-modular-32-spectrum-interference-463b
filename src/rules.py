import math

from .domain import DomainError

ENTITY_TYPE = "spectrum_interference"
INITIAL_STATUS = "pending"
CREATE_ROLES = {"analyst", "monitor"}
SOURCE_ROLES = {"analyst", "monitor", "field_operator"}
ACTION_ROLES = {
    "assess": {"analyst", "monitor"},
    "locate": {"field_operator", "analyst"},
    "suspend": {"coordinator", "regulator"},
    "coordinate": {"coordinator"},
    "resolve": {"coordinator", "regulator"},
    "correct_measurement": {"analyst", "monitor"},
    "cancel": {"coordinator"},
}
ENFORCE_REGION = True
REGION_SENSITIVE_ACTIONS = {"suspend", "coordinate", "resolve", "cancel"}
ACTION_REQUIRES_VERSION = {"suspend", "coordinate", "resolve", "cancel"}
TERMINAL_STATUSES = {"resolved", "cancelled"}
INVALIDATED_ON_MEASUREMENT_UPDATE = ("assessment", "location")
REPORT_SOURCE_TYPE = "station_report"


def assess(payload):
    strength = float(payload.get("strength_dbm", -120))
    bandwidth = max(float(payload.get("bandwidth_mhz", 0.1)), 0.001)
    impact = strength + 10.0 * math.log10(bandwidth * 1000.0)
    if impact >= -37:
        level = "critical"
    elif impact >= -50:
        level = "high"
    elif impact >= -65:
        level = "medium"
    else:
        level = "low"
    score = round(max(0.0, min(100.0, 100.0 + impact)), 2)
    return {"score": score, "level": level, "impact_value": round(impact, 2)}


def _need_status(item, allowed):
    if item["status"] not in allowed:
        raise DomainError("invalid_state", "当前状态 %s 不允许执行该操作" % item["status"])


def bands_overlap(first, second):
    first_center = float(first.get("frequency_mhz", 0))
    second_center = float(second.get("frequency_mhz", 0))
    first_width = max(float(first.get("bandwidth_mhz", 0)), 0.0)
    second_width = max(float(second.get("bandwidth_mhz", 0)), 0.0)
    return abs(first_center - second_center) * 2.0 <= (first_width + second_width)


def build_report(normalized):
    return {
        "station_id": normalized["station_id"],
        "region": normalized["region"],
        "strength_dbm": normalized["strength_dbm"],
        "detected_at": normalized["detected_at"],
        "reporter": normalized["reporter"],
        "frequency_mhz": normalized["frequency_mhz"],
        "bandwidth_mhz": normalized["bandwidth_mhz"],
    }


def invalidate_measurements(current):
    invalidated = []
    for key in INVALIDATED_ON_MEASUREMENT_UPDATE:
        if key in current:
            current.pop(key)
            invalidated.append(key)
    return invalidated


def merge_report(item, report):
    current = dict(item["payload"])
    reports = list(current.get("reports") or [])
    if not reports:
        reports.append(build_report(current))
    reports.append(report)
    current["reports"] = reports
    current["regions"] = sorted({entry.get("region") for entry in reports if entry.get("region")})
    current["strength_dbm"] = report["strength_dbm"]
    current["detected_at"] = report["detected_at"]
    current["bandwidth_mhz"] = report["bandwidth_mhz"]
    current["station_id"] = report["station_id"]
    current["reporter"] = report["reporter"]
    invalidated = invalidate_measurements(current)
    status = item["status"]
    if status in {"assessed", "located"}:
        status = INITIAL_STATUS
    event = {
        "report": report,
        "merged_into": item["id"],
        "reports_total": len(reports),
        "invalidated": invalidated,
    }
    return status, current, event


def _text(payload, name):
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise DomainError("field_required", "%s 不能为空" % name)
    return value.strip()


def apply_action(item, action, payload, actor, role):
    status = item["status"]
    current = dict(item["payload"])

    if action == "assess":
        _need_status(item, {"pending", "assessed"})
        current["assessment"] = assess(current)
        return "assessed", current, {"assessment": current["assessment"]}

    if action == "correct_measurement":
        _need_status(item, {"pending", "assessed", "located"})
        try:
            strength = float(payload["strength_dbm"])
        except (KeyError, TypeError, ValueError):
            raise DomainError("field_required", "strength_dbm 不能为空")
        revision = {
            "old_strength_dbm": current.get("strength_dbm"),
            "new_strength_dbm": strength,
            "reason": _text(payload, "reason"),
            "actor": actor,
        }
        current.setdefault("measurement_revisions", []).append(revision)
        current["strength_dbm"] = strength
        invalidated = invalidate_measurements(current)
        return INITIAL_STATUS, current, {"revision": revision, "invalidated": invalidated}

    if action == "locate":
        _need_status(item, {"assessed", "located"})
        location = _text(payload, "location")
        confidence = float(payload.get("confidence", 0))
        if confidence < 0.6:
            raise DomainError("low_location_confidence", "定位置信度低于0.6，不能进入处置", 409)
        current["location"] = {"label": location, "confidence": confidence}
        return "located", current, {"location": current["location"]}

    if action == "suspend":
        _need_status(item, {"located", "suspended"})
        authorization = _text(payload, "authorization_code")
        if not authorization.startswith("REG-"):
            raise DomainError("invalid_authorization", "停用授权编号无效", 403)
        current["suspend_authorization"] = authorization
        return "suspended", current, {"authorization_code": authorization}

    if action == "coordinate":
        _need_status(item, {"suspended"})
        agreement = _text(payload, "coordination_agreement")
        current["coordination_agreement"] = agreement
        current["coordination_note"] = payload.get("note", "")
        return "coordinating", current, {"coordination_agreement": agreement}

    if action == "resolve":
        _need_status(item, {"coordinating"})
        if not payload.get("measurement_cleared"):
            raise DomainError("interference_present", "干扰尚未消除，不能结案", 409)
        current["resolution"] = {"evidence": _text(payload, "evidence"), "cleared": True}
        return "resolved", current, {"evidence": current["resolution"]["evidence"]}

    if action == "cancel":
        _need_status(item, {"pending", "assessed"})
        reason = _text(payload, "reason")
        current["cancellation"] = {"reason": reason, "actor": actor}
        return "cancelled", current, {"reason": reason}

    raise DomainError("unknown_action", "不支持的操作")
