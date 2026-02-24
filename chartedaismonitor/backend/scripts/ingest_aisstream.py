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
from scripts.ingest_ais import VesselRecord, ensure_core_schema, process_batch

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
    """Build VesselRecord from AISStream message (MetaData or Message.PositionReport)."""
    meta = msg.get("MetaData") or msg.get("Metadata") or {}
    lat = meta.get("latitude") or meta.get("Latitude")
    lon = meta.get("longitude") or meta.get("Longitude")
    mmsi = meta.get("MMSI")
    if mmsi is None and "Message" in msg:
        pr = msg["Message"].get("PositionReport") or msg["Message"].get("ExtendedClassBPositionReport") or msg["Message"].get("StandardClassBPositionReport")
        if pr is not None:
            lat = lat or pr.get("Latitude")
            lon = lon or pr.get("Longitude")
            mmsi = mmsi or pr.get("UserID")
    if mmsi is None or lat is None or lon is None:
        return None
    try:
        lat_f = float(lat)
        lon_f = float(lon)
    except (TypeError, ValueError):
        return None
    name = meta.get("ShipName") or meta.get("shipname")
    ts = _parse_aisstream_time(meta.get("time_utc") or "")
    return VesselRecord(mmsi=str(mmsi), lat=lat_f, lon=lon_f, ts=ts, name=name)


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
