"""Unit tests for desktop toast notifications and auditory alerts."""

from botsensai.notifications import (
    DesktopAlert,
    notify_candidate_detected,
    notify_veto_alarm,
    send_desktop_notification,
)


def test_desktop_notifications_smoke() -> None:
    alert = DesktopAlert(
        title="Test Alert",
        subtitle="Subtitle",
        message="Test notification payload",
        sound=False,
        critical=False,
    )
    # Execution should not raise any unhandled exceptions
    send_desktop_notification(alert)

    notify_candidate_detected("GIGA", 0.85, "gigamint12345")
    notify_veto_alarm("RUG", "insider_supply_excessive")
    assert True
