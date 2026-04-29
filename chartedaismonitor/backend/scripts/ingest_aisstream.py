#!/usr/bin/env python3
"""
Ingest AIS data from AISStream.io WebSocket API (California coast only).

Uses AISSTREAM_API_KEY from environment. Subscribes to the California bounding box
and writes each position report to vessels, vessel_positions, and mpa_violations.

Docs: https://aisstream.io/documentation
WebSocket: wss://stream.aisstream.io/v0/stream
"""
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
import websockets

_backend = Path(__file__).resolve().parent.parent
if str(_backend) not in sys.path:
    sys.path.insert(0, str(_backend))
from database import DATABASE_URL
from mmsi_mid import mmsi_to_country
from scripts.ingest_ais import (
    StaticVesselRecord,
    VesselRecord,
    _normalize_vessel_type,
    ensure_core_schema,
    parse_ais_angle_deg,
    parse_ais_sog_kn,
    process_batch,
    process_static_batch,
)

# California coast bounding box (same as other ingest)
# Format: [[[lat1, lon1], [lat2, lon2]]]
CA_BOUNDING_BOXES = [[[32.0, -125.0], [42.5, -114.0]]]

AISSTREAM_WS_URL = "wss://stream.aisstream.io/v0/stream"


def _parse_aisstream_time(raw: str) -> datetime:
    """Parse AISStream time_utc e.g. '2022-12-29 18:22:32.318353 +0000 UTC'."""
    if not raw or not isinstance(raw, str):
        return datetime.now(timezone.utc)
    s = raw.strip()
    s = s.replace(" +0000 UTC", "").replace(" UTC", "").strip()
    try:
        if "." in s:
            return datetime.strptime(s[:26], "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=timezone.utc)
        return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)


def message_to_record(msg: dict) -> VesselRecord | None:
    """
    Build VesselRecord from an AISStream position-bearing message:
    PositionReport / ExtendedClassBPositionReport / StandardClassBPositionReport.
    Returns None if the message has no position payload.
    """
    meta = msg.get("MetaData") or msg.get("Metadata") or {}
    lat = meta.get("latitude") or meta.get("Latitude")
    lon = meta.get("longitude") or meta.get("Longitude")
    mmsi = meta.get("MMSI")
    pr = None
    if "Message" in msg:
        m = msg["Message"]
        pr = m.get("PositionReport") or m.get("ExtendedClassBPositionReport") or m.get("StandardClassBPositionReport")
        # Backfill from the position payload when MetaData is missing fields
        # (AISStream usually duplicates them but tests / older feeds may not).
        if pr is not None:
            if lat is None:
                lat = pr.get("Latitude")
            if lon is None:
                lon = pr.get("Longitude")
            if mmsi is None:
                mmsi = pr.get("UserID")
    if mmsi is None or lat is None or lon is None:
        return None
    try:
        lat_f = float(lat)
        lon_f = float(lon)
    except (TypeError, ValueError):
        return None
    name = meta.get("ShipName") or meta.get("shipname")
    callsign = meta.get("CallSign") or meta.get("Callsign") or meta.get("callSign")
    ts = _parse_aisstream_time(meta.get("time_utc") or "")
    country = None
    country_iso2 = None
    info = mmsi_to_country(str(mmsi))
    if info:
        country = info[0]
        country_iso2 = info[1]

    cog = None
    true_heading = None
    sog = None
    ais_ship_type_code = None
    vessel_type = None
    if pr is not None:
        cog = parse_ais_angle_deg(pr.get("Cog"))
        true_heading = parse_ais_angle_deg(pr.get("TrueHeading"))
        sog = parse_ais_sog_kn(pr.get("Sog"))
        # ExtendedClassBPositionReport carries ShipType inline; PositionReport (Type 1/2/3) does not.
        raw_type = pr.get("ShipType") or pr.get("Type")
        if raw_type is not None:
            ais_ship_type_code, vessel_type = _normalize_vessel_type(raw_type)

    return VesselRecord(
        mmsi=str(mmsi),
        lat=lat_f,
        lon=lon_f,
        ts=ts,
        name=name,
        callsign=callsign,
        country=country,
        country_iso2=country_iso2,
        cog=cog,
        true_heading=true_heading,
        sog=sog,
        ais_ship_type_code=ais_ship_type_code,
        vessel_type=vessel_type,
        bucket_source="ais" if vessel_type else None,
    )


def message_to_static_record(msg: dict) -> StaticVesselRecord | None:
    """
    Build a StaticVesselRecord from AISStream `Message.ShipStaticData` (Type 5 / 24).
    These messages carry identity + ship type but no lat/lon, so they only update
    the static fields on the `vessels` row.
    """
    if "Message" not in msg:
        return None
    static = msg["Message"].get("ShipStaticData")
    if static is None:
        return None

    meta = msg.get("MetaData") or msg.get("Metadata") or {}
    mmsi = meta.get("MMSI") or static.get("UserID")
    if mmsi is None:
        return None

    name = meta.get("ShipName") or meta.get("shipname") or static.get("Name")
    callsign = (
        meta.get("CallSign")
        or meta.get("Callsign")
        or meta.get("callSign")
        or static.get("CallSign")
    )
    raw_type = static.get("Type")
    ais_ship_type_code, vessel_type = _normalize_vessel_type(raw_type)

    country = None
    country_iso2 = None
    info = mmsi_to_country(str(mmsi))
    if info:
        country = info[0]
        country_iso2 = info[1]

    return StaticVesselRecord(
        mmsi=str(mmsi),
        name=str(name).strip() if isinstance(name, str) else name,
        callsign=str(callsign).strip() if isinstance(callsign, str) else callsign,
        country=country,
        country_iso2=country_iso2,
        ais_ship_type_code=ais_ship_type_code,
        vessel_type=vessel_type,
        bucket_source="ais" if vessel_type else None,
    )


async def run_stream(api_key: str):
    conn = psycopg2.connect(DATABASE_URL)
    try:
        ensure_core_schema(conn)
    except Exception as e:
        print(f"Schema setup failed: {e}")
        conn.close()
        return

    async for websocket in websockets.connect(
        AISSTREAM_WS_URL,
        ping_interval=20,
        ping_timeout=20,
        close_timeout=5,
    ):
        try:
            subscribe = {
                "APIKey": api_key,
                "BoundingBoxes": CA_BOUNDING_BOXES,
                "FilterMessageTypes": [
                    "PositionReport",
                    "ShipStaticData",
                    "ExtendedClassBPositionReport",
                    "StandardClassBPositionReport",
                ],
            }
            await websocket.send(json.dumps(subscribe))
            print("Subscribed to AISStream (California coast). Processing messages…")

            async for raw in websocket:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if "error" in msg:
                    print("AISStream error:", msg["error"])
                    break
                # Route ShipStaticData (Type 5 / 24) into the static-only upsert path so
                # vessel_type / ais_ship_type_code populate even if no PositionReport
                # ever carries the type field.
                static = message_to_static_record(msg)
                if static is not None:
                    try:
                        process_static_batch(conn, [static])
                    except Exception as e:
                        print(f"DB static write failed for {static.mmsi}: {e}")
                    # Static-only messages have no lat/lon; nothing more to do.
                    continue
                record = message_to_record(msg)
                if record is None:
                    continue
                try:
                    process_batch(conn, [record])
                except Exception as e:
                    print(f"DB write failed for {record.mmsi}: {e}")
        except websockets.exceptions.ConnectionClosed as e:
            print("Connection closed, reconnecting…", e)
        except Exception as e:
            print("Stream error:", e)
        await asyncio.sleep(2)


def main():
    api_key = os.getenv("AISSTREAM_API_KEY")
    if not api_key:
        raise SystemExit("AISSTREAM_API_KEY must be set in environment (e.g. in .env)")
    asyncio.run(run_stream(api_key.strip()))


if __name__ == "__main__":
    main()
