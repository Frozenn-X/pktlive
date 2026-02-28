"""Property-based / fuzz tests for silver_transform — no crashes on arbitrary input."""

from __future__ import annotations

from datetime import datetime, timezone

from hypothesis import given, strategies as st

from network_interface.storage.streaming_pipeline import silver_transform


@st.composite
def bronze_like_dict(draw: st.DrawFn) -> dict:
    """Generate dicts that resemble Bronze records (may be invalid)."""
    ts = draw(
        st.one_of(
            st.just(datetime.now(timezone.utc).isoformat()),
            st.datetimes().map(lambda d: d.isoformat()),
            st.text(min_size=1, max_size=200),
        )
    )
    return {
        "timestamp": ts,
        "src_ip": draw(st.text(max_size=50)),
        "dst_ip": draw(st.text(max_size=50)),
        "src_port": draw(st.one_of(st.none(), st.integers(0, 65535))),
        "dst_port": draw(st.one_of(st.none(), st.integers(0, 65535))),
        "protocol": draw(st.text(max_size=20)),
        "length": draw(st.integers(0, 65535)),
        "ttl": draw(st.one_of(st.none(), st.integers(0, 255))),
        "flags": draw(st.one_of(st.none(), st.text(max_size=20))),
        "agent_id": draw(st.text(max_size=32)),
    }


@given(record=bronze_like_dict())
def test_silver_transform_never_crashes(record: dict) -> None:
    """silver_transform must not raise; returns None or a dict with expected keys."""
    result = silver_transform(record)
    if result is not None:
        assert "event_ts" in result
        assert "event_date" in result
        assert "protocol" in result
        assert "src_ip" in result
        assert "dst_ip" in result
        assert "src_network" in result
