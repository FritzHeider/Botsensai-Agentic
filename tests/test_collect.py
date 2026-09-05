"""Tests for the continuous collection loop behind `botsensai collect`.

The loop's whole job is to keep running when things go wrong, and to leave
behind enough evidence to reconstruct when it was not running. Both properties
fail silently: a daemon that died at 3am and a market that produced nothing
between 3am and 7am leave an identical corpus, and the only thing that can tell
them apart is a heartbeat row that is absent for those four hours.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

from botsensai.config import Settings
from botsensai.models import utcnow
from botsensai.pipeline import HEARTBEAT_SURFACE, Pipeline, SweepReport
from botsensai.store.db import Database


def _pipeline(tmp_path, **kw) -> Pipeline:
    """A pipeline with a real store and no collectors, so nothing hits a network."""
    settings = Settings()
    db = Database(str(tmp_path / "collect.db"))
    return Pipeline(settings, db=db, collectors=[], **kw)


def _heartbeats(db: Database) -> list[dict]:
    return [r for r in db.recent_runs(limit=100) if r["surface"] == HEARTBEAT_SURFACE]


async def test_collect_writes_one_heartbeat_per_sweep(tmp_path):
    pipeline = _pipeline(tmp_path)
    sweeps = 0

    async def fake_sweep(discover_limit=60, max_candidates=25):
        nonlocal sweeps
        sweeps += 1
        started = utcnow()
        return SweepReport(started_at=started, finished_at=utcnow(), discovered=7, scored=3)

    pipeline.sweep = fake_sweep  # type: ignore[assignment]
    session = await pipeline.collect(interval_seconds=0.0, max_sweeps=3)

    assert sweeps == 3
    assert session.sweeps == 3
    assert session.discovered == 21
    assert session.stopped_because == "max_sweeps"

    beats = _heartbeats(pipeline.db)
    assert len(beats) == 3, "a sweep that left no heartbeat is an undetectable gap"
    assert all(b["ok"] for b in beats)
    assert all(b["records"] == 7 for b in beats)
    assert len({b["run_id"] for b in beats}) == 3, "heartbeats must not collapse onto one row"
    pipeline.db.close()


async def test_collect_survives_a_sweep_that_raises(tmp_path):
    """One collector outage costs one sweep, not the session."""
    pipeline = _pipeline(tmp_path)
    calls = 0

    async def flaky_sweep(discover_limit=60, max_candidates=25):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("dexscreener exploded")
        return SweepReport(started_at=utcnow(), finished_at=utcnow(), discovered=4)

    pipeline.sweep = flaky_sweep  # type: ignore[assignment]
    session = await pipeline.collect(interval_seconds=0.0, max_sweeps=3)

    assert calls == 3, "the loop stopped at the failure instead of continuing"
    assert session.sweeps == 3
    assert session.failed_sweeps == 1

    beats = _heartbeats(pipeline.db)
    failed = [b for b in beats if not b["ok"]]
    assert len(failed) == 1
    assert "dexscreener exploded" in failed[0]["error"]
    assert failed[0]["finished_at"] is not None, "a failed sweep still bounds its own window"
    pipeline.db.close()


async def test_collect_cuts_off_a_sweep_that_hangs(tmp_path):
    """A hung collector must cost one sweep, not the whole window."""
    pipeline = _pipeline(tmp_path)

    async def hanging_sweep(discover_limit=60, max_candidates=25):
        await asyncio.sleep(30)
        raise AssertionError("should have been cut off")

    pipeline.sweep = hanging_sweep  # type: ignore[assignment]
    session = await pipeline.collect(
        interval_seconds=0.0, max_sweeps=2, sweep_timeout=0.05
    )

    assert session.sweeps == 2
    assert session.failed_sweeps == 2
    beats = _heartbeats(pipeline.db)
    assert len(beats) == 2
    assert all("budget" in (b["error"] or "") for b in beats)
    pipeline.db.close()


async def test_collect_stops_at_the_deadline(tmp_path):
    """`--hours N` must bound wall-clock time, not just sweep count."""
    pipeline = _pipeline(tmp_path)

    async def quick_sweep(discover_limit=60, max_candidates=25):
        await asyncio.sleep(0.02)
        return SweepReport(started_at=utcnow(), finished_at=utcnow(), discovered=1)

    pipeline.sweep = quick_sweep  # type: ignore[assignment]
    started = utcnow()
    session = await pipeline.collect(hours=0.5 / 3600.0, interval_seconds=0.01)
    elapsed = (utcnow() - started).total_seconds()

    assert session.stopped_because == "deadline"
    assert session.sweeps >= 2, "the loop should have swept repeatedly inside the window"
    assert elapsed < 5.0, f"the deadline was not enforced ({elapsed:.1f}s for a 0.5s window)"
    assert len(_heartbeats(pipeline.db)) == session.sweeps
    pipeline.db.close()


async def test_collect_does_not_start_a_sweep_the_window_has_no_room_for(tmp_path):
    """A sweep truncated by the deadline would alarm on every single run."""
    pipeline = _pipeline(tmp_path)

    async def slow_sweep(discover_limit=60, max_candidates=25):
        started = utcnow()
        await asyncio.sleep(0.4)
        return SweepReport(started_at=started, finished_at=utcnow(), discovered=2)

    pipeline.sweep = slow_sweep  # type: ignore[assignment]
    session = await pipeline.collect(hours=0.6 / 3600.0, interval_seconds=0.0)

    assert session.sweeps == 1, "a second sweep could not have finished inside the window"
    assert session.failed_sweeps == 0
    beats = _heartbeats(pipeline.db)
    assert len(beats) == 1 and beats[0]["ok"], (
        "the deadline must not be recorded as a collection failure"
    )
    pipeline.db.close()


async def test_collect_closes_its_heartbeat_when_interrupted(tmp_path):
    """Ctrl-C must be distinguishable from a crash when the gap is read back.

    `asyncio.run` delivers Ctrl-C as a cancellation of the running task rather
    than as KeyboardInterrupt inside it, which is what this reproduces.
    """
    pipeline = _pipeline(tmp_path)

    async def slow_sweep(discover_limit=60, max_candidates=25):
        await asyncio.sleep(30)
        raise AssertionError("should have been interrupted")

    pipeline.sweep = slow_sweep  # type: ignore[assignment]
    task = asyncio.create_task(pipeline.collect(interval_seconds=0.0))
    await asyncio.sleep(0.05)
    task.cancel()
    session = await task

    assert session.stopped_because == "interrupted"
    assert session.sweeps == 1
    beats = _heartbeats(pipeline.db)
    assert len(beats) == 1, "an interrupted sweep must still leave a heartbeat"
    assert beats[0]["ok"] is False
    assert "interrupted" in beats[0]["error"]
    assert beats[0]["finished_at"] is not None
    pipeline.db.close()


async def test_collect_persists_what_each_sweep_produced(tmp_path):
    """The loop is only worth running if the corpus grows and survives it."""
    from botsensai.util.synthetic import generate_token

    pipeline = _pipeline(tmp_path)
    seeds = iter([11, 12, 13])

    async def storing_sweep(discover_limit=60, max_candidates=25):
        token = generate_token("organic", seed=next(seeds))
        pipeline.db.upsert_launch(token.launch)
        pipeline.db.insert_snapshots(token.snapshots)
        return SweepReport(started_at=utcnow(), finished_at=utcnow(), discovered=1)

    pipeline.sweep = storing_sweep  # type: ignore[assignment]
    await pipeline.collect(interval_seconds=0.0, max_sweeps=3)

    counts = pipeline.db.counts()
    assert counts["launches"] == 3
    assert counts["market_snapshots"] > 0
    pipeline.db.close()


def test_collection_gaps_are_recoverable_from_the_heartbeats(tmp_path):
    """The reason the heartbeat exists: proving when nothing was collecting."""
    db = Database(str(tmp_path / "gaps.db"))
    t0 = utcnow() - timedelta(hours=3)

    # Three sweeps a minute apart, then a four-hour hole, then two more.
    stamps = [t0, t0 + timedelta(minutes=1), t0 + timedelta(minutes=2)]
    stamps += [t0 + timedelta(hours=4), t0 + timedelta(hours=4, minutes=1)]
    for index, started in enumerate(stamps):
        db.record_run(
            run_id=f"run{index}",
            surface=HEARTBEAT_SURFACE,
            started_at=started,
            finished_at=started + timedelta(seconds=20),
            ok=True,
            records=5,
        )

    gaps = db.collection_gaps(tolerance_seconds=180.0)

    assert len(gaps) == 1, f"expected exactly the four-hour hole, got {gaps}"
    gap = gaps[0]
    assert 3 * 3600 < gap["seconds"] < 4 * 3600
    assert gap["after"] < gap["before"]
    assert db.collection_gaps(tolerance_seconds=6 * 3600.0) == [], (
        "a tolerance wider than the hole must report nothing"
    )
    db.close()


def test_collection_gaps_ignore_other_surfaces(tmp_path):
    """Per-surface enrich rows are far denser; they must not mask a sweep gap."""
    db = Database(str(tmp_path / "gaps2.db"))
    t0 = utcnow() - timedelta(hours=2)
    db.record_run(run_id="a", surface=HEARTBEAT_SURFACE, started_at=t0,
                  finished_at=t0, ok=True, records=1)
    db.record_run(run_id="b", surface="x", started_at=t0 + timedelta(minutes=30),
                  finished_at=t0 + timedelta(minutes=30), ok=True, records=1)
    db.record_run(run_id="c", surface=HEARTBEAT_SURFACE,
                  started_at=t0 + timedelta(hours=1), finished_at=t0 + timedelta(hours=1),
                  ok=True, records=1)

    gaps = db.collection_gaps(tolerance_seconds=600.0)

    assert len(gaps) == 1
    assert gaps[0]["seconds"] == 3600.0
    db.close()


def test_collect_command_is_registered_and_bounded():
    """`collect --hours` must exist as a command with a bounded window."""
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "botsensai.cli", "collect", "--help"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "--hours" in result.stdout
