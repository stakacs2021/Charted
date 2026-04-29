"""Unit tests for AISStream message parsing (no DB)."""
from __future__ import annotations

from scripts.ingest_aisstream import message_to_record, message_to_static_record
from scripts.ingest_ais import parse_ais_sog_kn


def test_parse_sog_handles_sentinels_and_units():
    assert parse_ais_sog_kn(None) is None
    assert parse_ais_sog_kn("") is None
    assert parse_ais_sog_kn(1023) is None
    # Tenths-of-knot integer encoding (e.g. AIVDM raw)
    assert parse_ais_sog_kn(123) == 12.3
    # Direct knots
    assert parse_ais_sog_kn(8.5) == 8.5
    # Out of range
    assert parse_ais_sog_kn(9999) is None
    # Negative -> None
    assert parse_ais_sog_kn(-1) is None


def test_position_report_extracts_sog_and_type_when_extended():
    msg = {
        "MetaData": {
            "MMSI": 123456789,
            "ShipName": "Test PR",
            "time_utc": "2026-04-13 10:00:00 +0000 UTC",
        },
        "Message": {
            "ExtendedClassBPositionReport": {
                "UserID": 123456789,
                "Latitude": 36.5,
                "Longitude": -121.9,
                "Cog": 90.0,
                "TrueHeading": 92.0,
                "Sog": 4.5,
                "ShipType": 70,
            }
        },
    }
    rec = message_to_record(msg)
    assert rec is not None
    assert rec.mmsi == "123456789"
    assert rec.lat == 36.5
    assert rec.lon == -121.9
    assert rec.sog == 4.5
    assert rec.vessel_type == "cargo"
    assert rec.ais_ship_type_code == 70
    assert rec.bucket_source == "ais"


def test_ship_static_data_yields_static_record_only():
    msg = {
        "MetaData": {
            "MMSI": 366970320,
            "ShipName": "Test Static",
            "CallSign": "WTEST",
        },
        "Message": {
            "ShipStaticData": {
                "UserID": 366970320,
                "Name": "Test Static",
                "CallSign": "WTEST",
                "Type": 30,
            }
        },
    }
    static = message_to_static_record(msg)
    assert static is not None
    assert static.mmsi == "366970320"
    assert static.vessel_type == "fishing"
    assert static.ais_ship_type_code == 30
    assert static.bucket_source == "ais"

    # The same message must NOT also produce a position-bearing record
    rec = message_to_record(msg)
    assert rec is None


def test_position_report_does_not_produce_static_record():
    msg = {
        "MetaData": {"MMSI": 999, "ShipName": "Pos Only"},
        "Message": {
            "PositionReport": {
                "UserID": 999,
                "Latitude": 0.0,
                "Longitude": 0.0,
                "Cog": 0.0,
            }
        },
    }
    assert message_to_static_record(msg) is None
    assert message_to_record(msg) is not None
