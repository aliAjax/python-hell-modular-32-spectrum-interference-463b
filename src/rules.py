import math
from datetime import datetime, timezone

from .domain import DomainError

ENTITY_TYPE = "spectrum_interference"
INITIAL_STATUS = "pending"
CREATE_ROLES = {"analyst", "monitor", "duty_officer"}
SOURCE_ROLES = {"analyst", "monitor", "field_operator", "duty_officer", "regulator"}
ACTION_ROLES = {
    "assess": {"analyst", "monitor"},
    "locate": {"field_operator", "analyst"},
    "suspend": {"coordinator", "regulator"},
    "coordinate": {"coordinator"},
    "resolve": {"coordinator", "regulator"},
    "correct_measurement": {"analyst", "monitor"},
    "cancel": {"coordinator"},
    "review_cross_region": {"regulator"},
}
ENFORCE_REGION = True
REGION_SENSITIVE_ACTIONS = {"suspend", "coordinate", "resolve", "cancel"}
ACTION_REQUIRES_VERSION = {"suspend", "coordinate", "resolve", "cancel", "review_cross_region"}


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


def _text(payload, name):
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise DomainError("field_required", "%s 不能为空" % name)
    return value.strip()


def _require_cross_region_review(current):
    # 跨区记录由监管角色复核，未复核前普通协调员不能处理。
    if current.get("cross_region") and not current.get("cross_region_reviewed"):
        raise DomainError("cross_region_review_required", "跨区记录需监管角色复核后才能处理", 403)


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
        # 测量数据更新后，评估和定位立即失效，处置人要按最新结果重新确认。
        current.pop("assessment", None)
        current.pop("location", None)
        return "pending", current, {"revision": revision}

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
        _require_cross_region_review(current)
        authorization = _text(payload, "authorization_code")
        if not authorization.startswith("REG-"):
            raise DomainError("invalid_authorization", "停用授权编号无效", 403)
        current["suspend_authorization"] = authorization
        return "suspended", current, {"authorization_code": authorization}

    if action == "coordinate":
        _need_status(item, {"suspended"})
        _require_cross_region_review(current)
        agreement = _text(payload, "coordination_agreement")
        current["coordination_agreement"] = agreement
        current["coordination_note"] = payload.get("note", "")
        return "coordinating", current, {"coordination_agreement": agreement}

    if action == "resolve":
        _need_status(item, {"coordinating"})
        _require_cross_region_review(current)
        if not payload.get("measurement_cleared"):
            raise DomainError("interference_present", "干扰尚未消除，不能结案", 409)
        current["resolution"] = {"evidence": _text(payload, "evidence"), "cleared": True}
        return "resolved", current, {"evidence": current["resolution"]["evidence"]}

    if action == "cancel":
        _need_status(item, {"pending", "assessed"})
        _require_cross_region_review(current)
        reason = _text(payload, "reason")
        current["cancellation"] = {"reason": reason, "actor": actor}
        return "cancelled", current, {"reason": reason}

    if action == "review_cross_region":
        # 跨区记录由监管角色复核，复核后普通协调员才能处理。
        _need_status(item, {"pending", "assessed", "located", "suspended"})
        current["cross_region_reviewed"] = True
        current["cross_region_review"] = {
            "actor": actor,
            "role": role,
            "reviewed_at": datetime.now(timezone.utc).isoformat(),
        }
        return status, current, {"cross_region_reviewed": True}

    raise DomainError("unknown_action", "不支持的操作")
