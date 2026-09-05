"""Unit tests for high-resolution social share cards."""

from datetime import UTC, datetime

from botsensai.media.fal_cards import SocialCardPayload, generate_card_svg


def test_generate_card_svg() -> None:
    payload = SocialCardPayload(
        symbol="PEPE",
        name="Pepe Frog",
        mint="pepemint12345678901234567890",
        composite=0.742,
        coverage=0.88,
        vetoes=[],
        findings=["Passed all 7 anti-rug vetoes", "High holder dispersion"],
        radar_points=[("Topology", 0.8), ("Social", 0.7)],
        timestamp_str=datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC).strftime("%Y-%m-%d %H:%M UTC"),
        disclosure="Not financial advice. Botsensai testing.",
    )

    svg = generate_card_svg(payload)
    assert "<svg" in svg
    assert "$PEPE" in svg
    assert "0.742" in svg
    assert "BOTSENSAI 2.0" in svg
    assert "Not financial advice" in svg
