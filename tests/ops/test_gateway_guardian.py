from pathlib import Path


SCRIPT = (
    Path(__file__).parents[2] / "ops" / "gateway-guardian-safe.sh"
).read_text(encoding="utf-8")


def test_guardian_has_maintenance_lock() -> None:
    assert "MAINTENANCE_LOCK" in SCRIPT
    assert "maintenance lock present" in SCRIPT


def test_idle_log_staleness_does_not_trigger_restart() -> None:
    assert "log_extreme_stale" not in SCRIPT
    assert "bridge=true log=${log_age}s" in SCRIPT
    assert "— no restart" in SCRIPT


def test_guardian_never_uses_sigkill() -> None:
    assert "systemctl kill" not in SCRIPT
    assert "kill -s" not in SCRIPT
    assert "systemctl restart" in SCRIPT
    assert "no forced-kill fallback" in SCRIPT
