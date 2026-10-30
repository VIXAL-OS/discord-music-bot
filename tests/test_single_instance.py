from __future__ import annotations

from pathlib import Path

from music_event_bot.single_instance import acquire_instance_lock


def test_second_instance_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "bot.lock"
    first = acquire_instance_lock(path)
    assert first is not None
    try:
        assert acquire_instance_lock(path) is None
    finally:
        first.close()


def test_lock_frees_when_the_holder_goes_away(tmp_path: Path) -> None:
    path = tmp_path / "data" / "bot.lock"
    first = acquire_instance_lock(path)
    assert first is not None
    first.close()

    second = acquire_instance_lock(path)
    assert second is not None
    second.close()
