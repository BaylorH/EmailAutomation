from typing import Any, Dict, Optional


RESULTS_FEATURE_ADMIN_UIDS = {
    "NO7lVYVp6BaplKYEfMlWCgBnpdh2",
    "C4X3UH1r6QhgP3ivXD1QjyhuGyI2",
}

TOUR_EMAIL_ALLOWED_UIDS = {
    "NO7lVYVp6BaplKYEfMlWCgBnpdh2",
}

RESULTS_FEATURE_PAUSED_REASON = (
    "Tour-planning emails are temporarily limited to the SiteSift test account "
    "while tour scheduling is hardened."
)


def is_results_feature_admin_user(user_id: Optional[str]) -> bool:
    return bool(user_id and user_id in RESULTS_FEATURE_ADMIN_UIDS)


def is_tour_email_allowed_user(user_id: Optional[str]) -> bool:
    return bool(user_id and user_id in TOUR_EMAIL_ALLOWED_UIDS)


def is_tour_invite_outbox(data: Optional[Dict[str, Any]] = None) -> bool:
    data = data or {}
    return bool(
        str(data.get("actionType") or "").strip().lower() == "tour_invite"
        or isinstance(data.get("tourInvite"), dict)
        or str(data.get("source") or "").strip().lower() == "dashboard_tour_planner"
    )


def should_pause_results_outbox_for_user(
    user_id: Optional[str],
    data: Optional[Dict[str, Any]] = None,
) -> bool:
    return is_tour_invite_outbox(data) and not is_tour_email_allowed_user(user_id)
