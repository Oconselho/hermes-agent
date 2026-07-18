"""Regression tests for the Feegow response envelope used by the secretary."""

from gateway.platforms.feegow_api import FeegowClient


def test_available_slots_unwraps_feegow_content_envelope(monkeypatch):
    client = FeegowClient(token="test-token")
    monkeypatch.setattr(
        client,
        "search_appointments",
        lambda start, end: {
            "success": True,
            "total": 1,
            "content": [
                {"data": "21-07-2026", "horario": "09:30:00"},
            ],
        },
    )

    text = client.get_available_slots_text("18-07-2026", "01-08-2026")

    assert "21-07-2026 às 09:30:00" in text
    assert "problema técnico" not in text
