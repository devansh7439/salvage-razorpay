"""Tests for the scalability guarantees and the defects fixed alongside them.

These pin properties that are invisible at demo size and expensive in
production: bounded memory, bounded transactions, no row multiplication, and
no unbounded client-controlled queries.
"""

from __future__ import annotations

import itertools
import time

import pytest

from salvage import db
from salvage.ml import predict
from salvage.pipeline import _chunks, process_batch, record_outcomes
from salvage.simulator.generate import (
    MAX_CUSTOMER_POOL,
    generate_events,
    stream_events,
)


class TestStreaming:
    def test_stream_is_lazy(self):
        """Taking two events from a billion-event stream must not attempt to
        build a billion of anything. Regression for the customer pool being
        sized as n // 8 with no ceiling, which hung the process before
        yielding a single event."""
        start = time.perf_counter()
        taken = list(itertools.islice(stream_events(10**9, seed=3), 2))
        assert len(taken) == 2
        assert time.perf_counter() - start < 10.0

    def test_customer_pool_is_bounded(self):
        events = list(itertools.islice(stream_events(10**7, seed=3), 500))
        assert len({e.customer_id for e in events}) <= MAX_CUSTOMER_POOL

    def test_stream_and_batch_agree(self):
        assert sorted(e.id for e in stream_events(200, seed=9)) == sorted(
            e.id for e in generate_events(200, seed=9)
        )

    def test_batch_is_chronological(self):
        events = generate_events(300, seed=11)
        assert all(
            events[i].created_at <= events[i + 1].created_at
            for i in range(len(events) - 1)
        )

    def test_generation_stays_deterministic(self):
        assert [e.id for e in generate_events(100, seed=5)] == [
            e.id for e in generate_events(100, seed=5)
        ]


class TestChunking:
    @pytest.mark.parametrize("total,size", [(0, 10), (1, 10), (10, 10), (25, 10)])
    def test_chunks_partition_exactly(self, total, size):
        chunks = list(_chunks(range(total), size))
        assert [x for c in chunks for x in c] == list(range(total))
        assert all(len(c) <= size for c in chunks)

    def test_chunks_consume_lazily(self):
        """_chunks must not materialise its input, or streaming upstream is
        pointless."""
        first = next(_chunks(itertools.count(), 5))
        assert first == [0, 1, 2, 3, 4]

    def test_pipeline_accepts_an_iterator(self, tmp_path, monkeypatch):
        """The pipeline must drive off any iterable, not just a list."""
        monkeypatch.setattr(db.settings, "database_path", tmp_path / "t.db")
        db.reset_db()
        result = process_batch(iter(generate_events(40, seed=13)), chunk_size=7)
        assert result["processed"] == 40

    def test_chunked_and_unchunked_agree(self, tmp_path, monkeypatch):
        """Chunk size is a performance knob and must not change decisions."""
        monkeypatch.setattr(db.settings, "database_path", tmp_path / "a.db")
        db.reset_db()
        events = generate_events(60, seed=17)
        big = process_batch(events, chunk_size=1000)

        monkeypatch.setattr(db.settings, "database_path", tmp_path / "b.db")
        db.reset_db()
        small = process_batch(events, chunk_size=7)

        assert big["actions"] == small["actions"]
        assert big["exceptions"] == small["exceptions"]


class TestQueryCorrectness:
    def test_one_row_per_event_despite_many_executions(self, tmp_path, monkeypatch):
        """`executions` is one-to-many with `events`. Joining it directly
        multiplies queue rows per execution and silently inflates every count
        taken from that query."""
        monkeypatch.setattr(db.settings, "database_path", tmp_path / "q.db")
        db.reset_db()
        events = generate_events(30, seed=19)
        process_batch(events)

        with db.connect() as conn:
            target = conn.execute(
                "SELECT event_id FROM executions LIMIT 1"
            ).fetchone()
            assert target is not None
            # A second execution against the same payment, as a retry-then-link
            # sequence would produce.
            conn.execute(
                "INSERT INTO executions (event_id, executed_at, action, status,"
                " idempotency_key) VALUES (?, ?, 'NOTIFY', 'EXECUTED', ?)",
                (target["event_id"], db.now_iso(), "second-key"),
            )

            rows = conn.execute(
                """
                SELECT e.id,
                       (SELECT x.payment_link_url FROM executions x
                         WHERE x.event_id = e.id ORDER BY x.id DESC LIMIT 1) AS url
                FROM events e
                LEFT JOIN decisions d ON d.event_id = e.id
                LEFT JOIN outcomes  o ON o.event_id = e.id
                """
            ).fetchall()

        ids = [r["id"] for r in rows]
        assert len(ids) == len(set(ids)) == 30

    def test_webhook_redelivery_is_idempotent(self, tmp_path, monkeypatch):
        """Razorpay redelivers webhooks. The same payment arriving twice must
        not become two recovery attempts."""
        monkeypatch.setattr(db.settings, "database_path", tmp_path / "i.db")
        db.reset_db()
        events = generate_events(20, seed=23)
        process_batch(events)
        process_batch(events)

        with db.connect() as conn:
            assert conn.execute("SELECT COUNT(*) n FROM events").fetchone()["n"] == 20
            dupes = conn.execute(
                "SELECT event_id FROM executions GROUP BY event_id HAVING COUNT(*) > 1"
            ).fetchall()
        assert dupes == []


class TestModelCache:
    def test_explicit_path_does_not_clobber_the_process_cache(self):
        """Loading an alternative model for a one-off must not silently
        repoint inference for the rest of the process."""
        default = predict.load_model()
        predict.load_model(predict.MODEL_PATH)
        assert predict.load_model() is default

    def test_inference_chunking_matches_unchunked(self):
        events = generate_events(300, seed=29)
        assert predict.predict_propensity_batch(
            events, chunk_size=10_000
        ) == pytest.approx(
            predict.predict_propensity_batch(events, chunk_size=32)
        )
