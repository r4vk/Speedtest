"""Tests for speedtest_app.iperf_udp: command building and result parsing."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from speedtest_app.iperf_udp import (
    IntervalStat,
    LoadTestParams,
    LoadTestResult,
    ResultValidationError,
    _pick_udp_views,
    build_command,
    parse_result,
    summarize_for_report,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "iperf"


def load_fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text()


# ---------------------------------------------------------------------------
# build_command
# ---------------------------------------------------------------------------


class TestBuildCommand:
    def test_udp_upload_default(self):
        p = LoadTestParams(server="iperf.example.com")
        assert build_command(p) == [
            "iperf3", "-c", "iperf.example.com", "-p", "5201", "-J", "-t", "10",
            "-u", "-b", "10M", "-l", "1200",
        ]

    def test_udp_download(self):
        p = LoadTestParams(server="iperf.example.com", direction="download")
        assert build_command(p) == [
            "iperf3", "-c", "iperf.example.com", "-p", "5201", "-J", "-t", "10",
            "-u", "-b", "10M", "-l", "1200", "-R",
        ]

    def test_tcp_upload(self):
        p = LoadTestParams(server="iperf.example.com", kind="iperf_tcp")
        assert build_command(p) == [
            "iperf3", "-c", "iperf.example.com", "-p", "5201", "-J", "-t", "10",
        ]

    def test_tcp_download(self):
        p = LoadTestParams(server="iperf.example.com", kind="iperf_tcp", direction="download")
        assert build_command(p) == [
            "iperf3", "-c", "iperf.example.com", "-p", "5201", "-J", "-t", "10", "-R",
        ]

    def test_parallel_streams_appended(self):
        p = LoadTestParams(server="iperf.example.com", kind="iperf_tcp", parallel=4)
        assert build_command(p) == [
            "iperf3", "-c", "iperf.example.com", "-p", "5201", "-J", "-t", "10", "-P", "4",
        ]

    def test_parallel_one_is_not_appended(self):
        p = LoadTestParams(server="iperf.example.com", kind="iperf_tcp", parallel=1)
        assert "-P" not in build_command(p)

    def test_custom_port_duration_bitrate_datagram(self):
        p = LoadTestParams(
            server="10.0.0.5", port=6201, duration_seconds=30,
            udp_bitrate="500K", datagram_len=64,
        )
        assert build_command(p) == [
            "iperf3", "-c", "10.0.0.5", "-p", "6201", "-J", "-t", "30",
            "-u", "-b", "500K", "-l", "64",
        ]


class TestBuildCommandValidation:
    def test_invalid_server(self):
        p = LoadTestParams(server="not a host!!")
        with pytest.raises(ValueError):
            build_command(p)

    @pytest.mark.parametrize("port", [0, -1, 65536, 100000])
    def test_invalid_port(self, port):
        p = LoadTestParams(server="10.0.0.1", port=port)
        with pytest.raises(ValueError, match="port"):
            build_command(p)

    @pytest.mark.parametrize("duration", [0, -5, 61, 120])
    def test_invalid_duration(self, duration):
        p = LoadTestParams(server="10.0.0.1", duration_seconds=duration)
        with pytest.raises(ValueError, match="duration_seconds"):
            build_command(p)

    @pytest.mark.parametrize("length", [0, 63, 65508, 100000])
    def test_invalid_datagram_len(self, length):
        p = LoadTestParams(server="10.0.0.1", datagram_len=length)
        with pytest.raises(ValueError, match="datagram_len"):
            build_command(p)

    @pytest.mark.parametrize("bitrate", ["", "abc", "10X", "10 M", "-5M"])
    def test_invalid_bitrate(self, bitrate):
        p = LoadTestParams(server="10.0.0.1", udp_bitrate=bitrate)
        with pytest.raises(ValueError, match="udp_bitrate"):
            build_command(p)

    @pytest.mark.parametrize("bitrate", ["10M", "10.5M", "1000", "2G", "500K"])
    def test_valid_bitrate_formats(self, bitrate):
        p = LoadTestParams(server="10.0.0.1", udp_bitrate=bitrate)
        build_command(p)  # must not raise


# ---------------------------------------------------------------------------
# parse_result: happy paths
# ---------------------------------------------------------------------------


class TestParseUdpOk:
    def test_udp_upload_ok(self):
        result = parse_result(
            load_fixture("udp_upload_ok.json"), kind="iperf_udp", direction="upload"
        )
        assert isinstance(result, LoadTestResult)
        assert result.kind == "iperf_udp"
        assert result.direction == "upload"
        assert result.protocol == "UDP"
        assert result.version == "iperf 3.12"
        assert result.duration_seconds == 10.0
        assert result.receiver["packets"] == 8500
        assert result.receiver["lost_packets"] == 17
        assert result.receiver["lost_percent"] == pytest.approx(0.2)
        assert result.receiver["jitter_ms"] == pytest.approx(0.42)
        assert "lost_percent_recomputed" not in result.receiver
        assert len(result.intervals) == 10
        total_packets = sum(iv.packets for iv in result.intervals)
        assert total_packets == 8500
        for iv in result.intervals:
            assert isinstance(iv, IntervalStat)
            assert iv.jitter_ms == pytest.approx(0.42)
            assert iv.bits_per_second == pytest.approx(10_000_000)

    def test_udp_download_ok(self):
        result = parse_result(
            load_fixture("udp_download_ok.json"), kind="iperf_udp", direction="download"
        )
        assert result.direction == "download"
        assert result.protocol == "UDP"
        assert result.receiver["packets"] == 9000
        assert result.receiver["lost_packets"] == 9
        assert result.receiver["lost_percent"] == pytest.approx(0.1)
        assert "lost_percent_recomputed" not in result.receiver

    def test_udp_sum_only_old_style(self):
        result = parse_result(
            load_fixture("udp_sum_only.json"), kind="iperf_udp", direction="upload"
        )
        assert result.receiver["packets"] == 5000
        assert result.receiver["lost_packets"] == 5
        assert result.receiver["lost_percent"] == pytest.approx(0.1)
        # No sum_sent/sum_received in the fixture -> sender view is {} -> all-None shape.
        assert result.sender == {
            "packets": None,
            "lost_packets": None,
            "lost_percent": None,
            "jitter_ms": None,
            "bits_per_second": None,
        }

    def test_tcp_ok(self):
        result = parse_result(load_fixture("tcp_ok.json"), kind="iperf_tcp", direction="download")
        assert result.protocol == "TCP"
        assert result.version == "iperf 3.12"
        assert result.receiver["bits_per_second"] == pytest.approx(938_500_000)
        assert result.receiver["bytes"] == 1173125000
        assert result.receiver["retransmits"] is None
        assert result.sender["bits_per_second"] == pytest.approx(941_000_000)
        assert result.sender["bytes"] == 1176250000
        assert result.sender["retransmits"] == 3


# ---------------------------------------------------------------------------
# parse_result: validation failures
# ---------------------------------------------------------------------------


class TestParseValidationErrors:
    def test_zero_packets_is_never_a_synthetic_zero_loss(self):
        with pytest.raises(ResultValidationError, match="no packets received"):
            parse_result(load_fixture("udp_zero_packets.json"), kind="iperf_udp", direction="upload")

    def test_missing_jitter_field(self):
        with pytest.raises(ResultValidationError, match="missing receiver field jitter_ms"):
            parse_result(load_fixture("udp_missing_jitter.json"), kind="iperf_udp", direction="upload")

    def test_protocol_mismatch(self):
        with pytest.raises(ResultValidationError, match="protocol mismatch"):
            parse_result(load_fixture("tcp_ok.json"), kind="iperf_udp", direction="upload")

    def test_iperf_reported_error(self):
        with pytest.raises(
            ResultValidationError, match="unable to connect to server: Connection refused"
        ):
            parse_result(
                load_fixture("error_connect_refused.json"), kind="iperf_udp", direction="upload"
            )

    def test_garbage_is_not_json(self):
        with pytest.raises(ResultValidationError, match="invalid JSON"):
            parse_result(load_fixture("garbage.txt"), kind="iperf_udp", direction="upload")

    def test_result_validation_error_carries_reason(self):
        try:
            parse_result(load_fixture("udp_zero_packets.json"), kind="iperf_udp", direction="upload")
        except ResultValidationError as exc:
            assert exc.reason == "no packets received"
        else:
            pytest.fail("expected ResultValidationError")


class TestLostPercentRecompute:
    def test_inconsistent_lost_percent_is_recomputed_and_flagged(self):
        result = parse_result(
            load_fixture("udp_lost_percent_inconsistent.json"), kind="iperf_udp", direction="upload"
        )
        assert result.receiver["lost_packets"] == 100
        assert result.receiver["packets"] == 1000
        # JSON claimed 0% despite 100/1000 lost -> recomputed from counters.
        assert result.receiver["lost_percent"] == pytest.approx(10.0)
        assert result.receiver["lost_percent_recomputed"] is True

    def test_consistent_lost_percent_is_not_flagged(self):
        result = parse_result(
            load_fixture("udp_upload_ok.json"), kind="iperf_udp", direction="upload"
        )
        assert "lost_percent_recomputed" not in result.receiver


# ---------------------------------------------------------------------------
# Intervals: length and None handling
# ---------------------------------------------------------------------------


class TestIntervals:
    def test_udp_intervals_fully_populated(self):
        result = parse_result(
            load_fixture("udp_upload_ok.json"), kind="iperf_udp", direction="upload"
        )
        assert len(result.intervals) == 10
        for iv in result.intervals:
            assert iv.packets is not None
            assert iv.lost_packets is not None
            assert iv.jitter_ms is not None
            assert iv.bits_per_second is not None
            assert isinstance(iv.start_s, float)
            assert isinstance(iv.end_s, float)

    def test_tcp_intervals_missing_udp_only_fields_are_none(self):
        result = parse_result(load_fixture("tcp_ok.json"), kind="iperf_tcp", direction="upload")
        assert len(result.intervals) == 5
        for iv in result.intervals:
            assert iv.packets is None
            assert iv.lost_packets is None
            assert iv.jitter_ms is None
            assert iv.bits_per_second is not None


# ---------------------------------------------------------------------------
# summarize_for_report
# ---------------------------------------------------------------------------


class TestSummarizeForReport:
    def test_loss_from_counters(self):
        result = parse_result(
            load_fixture("udp_lost_percent_inconsistent.json"), kind="iperf_udp", direction="upload"
        )
        summary = summarize_for_report(result)
        assert summary["loss_pct"] == pytest.approx(10.0)
        assert summary["lost"] == 100
        assert summary["packets"] == 1000
        assert summary["recomputed"] is True
        assert summary["direction"] == "upload"
        assert summary["kind"] == "iperf_udp"
        assert summary["jitter_ms"] == pytest.approx(0.5)

    def test_summary_shape_for_ok_udp(self):
        result = parse_result(
            load_fixture("udp_upload_ok.json"), kind="iperf_udp", direction="upload"
        )
        summary = summarize_for_report(result)
        assert set(summary.keys()) == {
            "loss_pct", "lost", "packets", "jitter_ms", "mbps", "direction", "kind", "recomputed",
        }
        assert summary["mbps"] == pytest.approx(10.0)
        assert summary["recomputed"] is False

    def test_summary_for_tcp_has_no_udp_loss_but_has_mbps(self):
        result = parse_result(load_fixture("tcp_ok.json"), kind="iperf_tcp", direction="download")
        summary = summarize_for_report(result)
        assert summary["packets"] is None
        assert summary["lost"] is None
        assert summary["loss_pct"] is None
        assert summary["mbps"] == pytest.approx(938.5)
        assert summary["recomputed"] is False


# ---------------------------------------------------------------------------
# to_json / round-trip
# ---------------------------------------------------------------------------


class TestToJson:
    def test_round_trips_through_json_dumps(self):
        result = parse_result(
            load_fixture("udp_upload_ok.json"), kind="iperf_udp", direction="upload"
        )
        payload = result.to_json()
        dumped = json.dumps(payload)
        reloaded = json.loads(dumped)
        assert reloaded == payload
        assert reloaded["kind"] == "iperf_udp"
        assert len(reloaded["intervals"]) == 10
        assert isinstance(reloaded["intervals"][0], dict)
        assert reloaded["intervals"][0]["packets"] > 0

    def test_tcp_round_trip(self):
        result = parse_result(load_fixture("tcp_ok.json"), kind="iperf_tcp", direction="upload")
        payload = result.to_json()
        json.dumps(payload)  # must not raise
        assert payload["protocol"] == "TCP"
        assert payload["receiver"]["retransmits"] is None


# ---------------------------------------------------------------------------
# _pick_udp_views (documented mapping)
# ---------------------------------------------------------------------------


class TestPickUdpViews:
    def test_modern_prefers_sum_sent_and_sum_received(self):
        end = {
            "sum_sent": {"packets": 1, "sender": True},
            "sum_received": {"packets": 2, "sender": False},
            "sum": {"packets": 3, "sender": False},
        }
        receiver, sender = _pick_udp_views(end)
        assert receiver == {"packets": 2, "sender": False}
        assert sender == {"packets": 1, "sender": True}

    def test_old_style_receiver_from_sum_when_sender_false(self):
        end = {"sum": {"packets": 3, "sender": False}}
        receiver, sender = _pick_udp_views(end)
        assert receiver == {"packets": 3, "sender": False}
        assert sender == {}

    def test_old_style_sender_only_has_no_receiver_view(self):
        end = {"sum": {"packets": 3, "sender": True}}
        with pytest.raises(ResultValidationError, match="missing receiver view"):
            _pick_udp_views(end)

    def test_no_sum_at_all_has_no_receiver_view(self):
        with pytest.raises(ResultValidationError, match="missing receiver view"):
            _pick_udp_views({})
