"use client";

import { useRef, useEffect, useState } from "react";
import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

type GeoJSONFeature = GeoJSON.Feature<GeoJSON.Geometry, { id?: number; name?: string; designation?: string }>;
type GeoJSONFC = GeoJSON.FeatureCollection<GeoJSON.Geometry, { id?: number; name?: string; designation?: string }>;

export default function Map() {
  const mapRef = useRef<HTMLDivElement>(null);
  const mapInstance = useRef<maplibregl.Map | null>(null);
  const [zones, setZones] = useState<GeoJSONFC | null>(null);
  const [zonesVisible, setZonesVisible] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetch(`${API_URL}/zones`)
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then((data: GeoJSONFC) => setZones(data))
      .catch((e) => setError(e.message));
  }, []);

  useEffect(() => {
    if (!mapRef.current) return;

    const map = new maplibregl.Map({
      container: mapRef.current,
      style: "https://demotiles.maplibre.org/style.json",
      center: [-119.5, 36.5],
      zoom: 5,
    });

    map.addControl(new maplibregl.NavigationControl(), "top-right");
    mapInstance.current = map;

    return () => {
      map.remove();
      mapInstance.current = null;
    };
  }, []);

  useEffect(() => {
    const map = mapInstance.current;
    if (!map || !zones) return;

    const sourceId = "mpa-zones";
    const layerFillId = "mpa-zones-fill";
    const layerLineId = "mpa-zones-line";

    if (map.getSource(sourceId)) {
      (map.getSource(sourceId) as maplibregl.GeoJSONSource).setData(zones);
    } else {
      map.addSource(sourceId, { type: "geojson", data: zones });
      map.addLayer({
        id: layerFillId,
        type: "fill",
        source: sourceId,
        paint: {
          "fill-color": "#088",
          "fill-opacity": 0.35,
        },
      });
      map.addLayer({
        id: layerLineId,
        type: "line",
        source: sourceId,
        paint: {
          "line-color": "#066",
          "line-width": 1.5,
        },
      });
    }

    map.setLayoutProperty(layerFillId, "visibility", zonesVisible ? "visible" : "none");
    map.setLayoutProperty(layerLineId, "visibility", zonesVisible ? "visible" : "none");
  }, [zones, zonesVisible]);

  return (
    <div style={{ position: "relative", width: "100%", height: "100%" }}>
      <div ref={mapRef} id="map" />
      <div
        style={{
          position: "absolute",
          top: 12,
          left: 12,
          background: "white",
          padding: "10px 14px",
          borderRadius: 8,
          boxShadow: "0 1px 4px rgba(0,0,0,0.2)",
          fontFamily: "system-ui",
          fontSize: 14,
        }}
      >
        <div style={{ fontWeight: 600, marginBottom: 8 }}>California MPAs</div>
        {error && <div style={{ color: "#c00", marginBottom: 8 }}>{error}</div>}
        <label style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer" }}>
          <input
            type="checkbox"
            checked={zonesVisible}
            onChange={(e) => setZonesVisible(e.target.checked)}
          />
          Show MPA boundaries
        </label>
        <div style={{ marginTop: 8, color: "#666", fontSize: 12 }}>
          {zones ? `${zones.features.length} zones` : "Loading…"}
        </div>
      </div>
    </div>
  );
}
