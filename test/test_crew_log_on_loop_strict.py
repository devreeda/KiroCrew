"""The crew-log unit lock refuses an on-loop take under its strict switch.

``file_lock`` takes one attempt on the event-loop thread, so a crew-log call made
straight from an ``async def`` fails only when the writer or the eager folder holds
the lock at that instant -- a flake on whichever shard is slow. With
``KIROCREW_STRICT_ON_LOOP_CREW_LOG`` set, every on-loop take raises, held or not,
so the failure is the same on every run. Unset, nothing changes: the same on-loop
take of a free lock still succeeds.
"""

from __future__ import annotations

import asyncio

import pytest

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.crew_log import projection as crew_log
from kiro_crew.crew_log import store
from kiro_crew.crew_log.store import CrewLog
from kiro_crew.on_loop_db import OnLoopStoreError

UNIT = "acp-on-loop-strict"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Own data home, crew log on, strict switches cleared, no warm fold."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("KIROCREW_CREW_LOG", "1")
    monkeypatch.delenv(store.STRICT_ON_LOOP_ENV, raising=False)
    monkeypatch.delenv("KIROCREW_DEV_MODE", raising=False)
    crew_log_emit.reset_caches()
    crew_log.forget_slot_folds()
    yield
    crew_log_emit.reset_caches()
    crew_log.forget_slot_folds()


def _create() -> None:
    CrewLog.create(lg.KIND_SESSION, UNIT, owner="owner", agent="lead", slot="slot-a")


def _open_on_loop() -> CrewLog:
    async def on_the_loop() -> CrewLog:
        return CrewLog.open(lg.KIND_SESSION, UNIT)

    return asyncio.run(on_the_loop())


def _open_off_loop() -> CrewLog:
    async def through_a_thread() -> CrewLog:
        return await asyncio.to_thread(CrewLog.open, lg.KIND_SESSION, UNIT)

    return asyncio.run(through_a_thread())


def test_switch_name_is_this_surfaces_own() -> None:
    # One switch per surface (on_loop_db): the shared persist switch CI already
    # exports must not arm this store.
    assert store.STRICT_ON_LOOP_ENV == "KIROCREW_STRICT_ON_LOOP_CREW_LOG"


def test_strict_on_loop_open_raises_even_when_the_lock_is_free(monkeypatch) -> None:
    _create()
    monkeypatch.setenv(store.STRICT_ON_LOOP_ENV, "1")
    with pytest.raises(OnLoopStoreError, match="event-loop thread"):
        _open_on_loop()


def test_strict_on_loop_create_raises_before_touching_disk(monkeypatch) -> None:
    monkeypatch.setenv(store.STRICT_ON_LOOP_ENV, "1")

    async def on_the_loop() -> None:
        _create()

    with pytest.raises(OnLoopStoreError):
        asyncio.run(on_the_loop())
    assert not store._lock_path(lg.KIND_SESSION, UNIT).exists()


def test_strict_off_loop_open_succeeds(monkeypatch) -> None:
    _create()
    monkeypatch.setenv(store.STRICT_ON_LOOP_ENV, "1")
    assert isinstance(_open_off_loop(), CrewLog)


def test_strict_plain_sync_caller_succeeds(monkeypatch) -> None:
    _create()
    monkeypatch.setenv(store.STRICT_ON_LOOP_ENV, "1")
    assert isinstance(CrewLog.open(lg.KIND_SESSION, UNIT), CrewLog)


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"KIROCREW_STRICT_ON_LOOP_CREW_LOG": "0"},
        {"KIROCREW_STRICT_ON_LOOP_CREW_LOG": "off"},
        # Dev mode arms the history guard, not this one.
        {"KIROCREW_DEV_MODE": "1"},
        {"KIROCREW_STRICT_ON_LOOP_PERSIST": "1"},
    ],
    ids=["unset", "zero", "off", "dev-mode", "persist-switch"],
)
def test_not_strict_on_loop_open_of_a_free_lock_is_unchanged(monkeypatch, env) -> None:
    _create()
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert isinstance(_open_on_loop(), CrewLog)
