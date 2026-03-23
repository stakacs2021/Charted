"use client";

import { useRef, useEffect, useState } from "react";
import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

/** Regional-indicator flag emoji from ISO 3166-1 alpha-2 (e.g. US -> 🇺🇸). */
function iso2ToFlagEmoji(iso2: string | null | undefined): string {
  if (!iso2 || iso2.length !== 2) return "";
  const u = iso2.toUpperCase();
  const a = u.charCodeAt(0) - 65 + 0x1f1e6;
  const b = u.charCodeAt(1) - 65 + 0x1f1e6;
  if (a < 0x1f1e6 || a > 0x1f1ff || b < 0x1f1e6 || b > 0x1f1ff) return "";
  return String.fromCodePoint(a, b);
}

type GeoJSONFeature = GeoJSON.Feature<GeoJSON.Geometry, { id?: number; name?: string; designation?: string }>;
type GeoJSONFC = GeoJSON.FeatureCollection<GeoJSON.Geometry, { id?: number; name?: string; designation?: string }>;

type LiveVessel = {
  mmsi: string;
  name?: string | null;
  country?: string | null;
  callsign?: string | null;
  country_iso2?: string | null;
  cog?: number | null;
  true_heading?: number | null;
  bearing_deg?: number | null;
  lat: number;
  lon: number;
  last_ts?: string | null;
  inside_any_mpa?: boolean;
  has_mpa_violations?: boolean;
};

type VesselTrailResponse = {
  mmsi: string;
  hours: number;
  count: number;
  line: GeoJSON.Feature<GeoJSON.LineString> | null;
};

export default function Map() {
  const mapRef = useRef<HTMLDivElement>(null);
  const mapInstance = useRef<maplibregl.Map | null>(null);
  const [mapLoaded, setMapLoaded] = useState(false);
  const [zones, setZones] = useState<GeoJSONFC | null>(null);
  const [zonesVisible, setZonesVisible] = useState(true);
  const [vesselsVisible, setVesselsVisible] = useState(true);
  const [violatorTrailsVisible, setViolatorTrailsVisible] = useState(true);
  const [allVesselTrailsVisible, setAllVesselTrailsVisible] = useState(false);
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
    const onLoad = () => setMapLoaded(true);
    map.once("load", onLoad);

    return () => {
      map.remove();
      mapInstance.current = null;
    };
  }, []);

  useEffect(() => {
    const map = mapInstance.current;
    if (!mapLoaded || !map || !zones) return;

    const sourceId = "mpa-zones";
    const layerFillId = "mpa-zones-fill";
    const layerLineId = "mpa-zones-line";

    if (map.getSource(sourceId)) {
      (map.getSource(sourceId) as maplibregl.GeoJSONSource).setData(zones);
    } else {
      const onLoad = () => {
        if (!map.getSource(sourceId)) {
          map.addSource(sourceId, { type: "geojson", data: zones });
        }
        if (!map.getLayer(layerFillId)) {
          map.addLayer({
            id: layerFillId,
            type: "fill",
            source: sourceId,
            paint: {
              "fill-color": "#088",
              "fill-opacity": 0.35,
            },
          });
        }
        if (!map.getLayer(layerLineId)) {
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
      };
      if (map.isStyleLoaded()) onLoad();
      else map.once("load", onLoad);
    }

    if (map.getLayer(layerFillId)) map.setLayoutProperty(layerFillId, "visibility", zonesVisible ? "visible" : "none");
    if (map.getLayer(layerLineId)) map.setLayoutProperty(layerLineId, "visibility", zonesVisible ? "visible" : "none");
  }, [mapLoaded, zones, zonesVisible]);

  useEffect(() => {
    const map = mapInstance.current;
    if (!mapLoaded || !map) return;

    const sourceId = "live-vessels";
    const layerId = "live-vessels-points";

    const emptyFc: GeoJSON.FeatureCollection<GeoJSON.Point, { mmsi: string; name?: string; inside?: boolean }> = {
      type: "FeatureCollection",
      features: [],
    };

    const shipIconGreen =
      "data:image/svg+xml;base64," +
      btoa(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path d="M12 2 L22 22 L2 22 Z" fill="#1d7" stroke="#fff" stroke-width="1"/></svg>'
      );
    const shipIconRed =
      "data:image/svg+xml;base64," +
      btoa(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path d="M12 2 L22 22 L2 22 Z" fill="#d14" stroke="#fff" stroke-width="1"/></svg>'
      );

    const addShipIcons = () => {
      if (map.hasImage("ship-green") && map.hasImage("ship-red")) return;
      const loadImg = (name: string, src: string) =>
        new Promise<void>((resolve) => {
          const img = new Image();
          img.onload = () => {
            if (!map.hasImage(name)) map.addImage(name, img);
            resolve();
          };
          img.src = src;
        });
      Promise.all([loadImg("ship-green", shipIconGreen), loadImg("ship-red", shipIconRed)]);
    };

    const upsertSourceAndLayer = () => {
      if (!map.getSource(sourceId)) {
        map.addSource(sourceId, { type: "geojson", data: emptyFc });
      }
      addShipIcons();
      if (!map.getLayer(layerId)) {
        map.addLayer({
          id: layerId,
          type: "symbol",
          source: sourceId,
          layout: {
            "icon-image": ["case", ["boolean", ["get", "inside"], false], "ship-red", "ship-green"],
            "icon-size": 0.6,
            "icon-rotate": ["coalesce", ["get", "bearing_deg"], 0],
            "icon-rotation-alignment": "map",
            "icon-allow-overlap": true,
            "icon-ignore-placement": true,
          },
        });
      }
    };

    const allTrailSourceId = "all-vessel-trails";
    const allTrailLayerId = "all-vessel-trails-line";
    const MAX_ALL_VESSEL_TRAILS = 40;
    const ALL_TRAIL_HOURS = 3;
    const ALL_TRAIL_LIMIT = 200;

    const violatorTrailSourceId = "violator-trails";
    const violatorTrailLayerId = "violator-trails-line";
    const MAX_VIOLATOR_TRAILS = 25;

    const ensureAllVesselTrailLayer = () => {
      const emptyFc: GeoJSON.FeatureCollection<GeoJSON.LineString> = { type: "FeatureCollection", features: [] };
      if (!map.getSource(allTrailSourceId)) {
        map.addSource(allTrailSourceId, { type: "geojson", data: emptyFc });
      }
      if (!map.getLayer(allTrailLayerId)) {
        map.addLayer(
          {
            id: allTrailLayerId,
            type: "line",
            source: allTrailSourceId,
            paint: {
              "line-color": "#0369a1",
              "line-width": 1.5,
              "line-opacity": 0.65,
              "line-dasharray": [2, 2],
            },
          },
          layerId
        );
      }
    };

    const ensureViolatorTrailLayer = () => {
      const emptyFc: GeoJSON.FeatureCollection<GeoJSON.LineString> = { type: "FeatureCollection", features: [] };
      if (!map.getSource(violatorTrailSourceId)) {
        map.addSource(violatorTrailSourceId, { type: "geojson", data: emptyFc });
      }
      if (!map.getLayer(violatorTrailLayerId)) {
        map.addLayer(
          {
            id: violatorTrailLayerId,
            type: "line",
            source: violatorTrailSourceId,
            paint: {
              "line-color": "#c00",
              "line-width": 2,
              "line-opacity": 0.8,
              "line-dasharray": [1, 2],
            },
          },
          layerId
        );
      }
    };

    const fetchAndUpdate = async () => {
      try {
        const bounds = map.getBounds();
        const params = new URLSearchParams({
          min_lat: bounds.getSouth().toString(),
          max_lat: bounds.getNorth().toString(),
          min_lon: bounds.getWest().toString(),
          max_lon: bounds.getEast().toString(),
          limit: "500",
        });
        const res = await fetch(`${API_URL}/vessels/live?${params.toString()}`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data: LiveVessel[] = await res.json();

        const fc: GeoJSON.FeatureCollection<
          GeoJSON.Point,
          {
            mmsi: string;
            name?: string;
            country?: string | null;
            country_iso2?: string | null;
            callsign?: string | null;
            bearing_deg?: number | null;
            inside?: boolean;
            has_mpa_violations?: boolean;
            last_ts?: string | null;
          }
        > = {
          type: "FeatureCollection",
          features: data
            .filter((v) => Number.isFinite(v.lat) && Number.isFinite(v.lon))
            .map((v) => ({
              type: "Feature",
              id: v.mmsi,
              properties: {
                mmsi: v.mmsi,
                name: v.name ?? undefined,
                country: v.country ?? null,
                country_iso2: v.country_iso2 ?? null,
                callsign: v.callsign ?? null,
                bearing_deg:
                  v.bearing_deg != null && Number.isFinite(v.bearing_deg) ? v.bearing_deg : null,
                inside: v.inside_any_mpa,
                has_mpa_violations: v.has_mpa_violations,
                last_ts: v.last_ts ?? null,
              },
              geometry: { type: "Point", coordinates: [v.lon, v.lat] },
            })),
        };

        const src = map.getSource(sourceId) as maplibregl.GeoJSONSource | undefined;
        if (src) src.setData(fc);

        ensureAllVesselTrailLayer();
        if (allVesselTrailsVisible) {
          const allMmsis = data
            .filter((v) => Number.isFinite(v.lat) && Number.isFinite(v.lon))
            .map((v) => v.mmsi)
            .slice(0, MAX_ALL_VESSEL_TRAILS);
          if (allMmsis.length > 0) {
            const allTrailResponses = await Promise.all(
              allMmsis.map((mmsi) =>
                fetch(
                  `${API_URL}/vessels/${encodeURIComponent(mmsi)}/trail?hours=${ALL_TRAIL_HOURS}&limit=${ALL_TRAIL_LIMIT}`
                ).then((r) => (r.ok ? r.json() : null))
              )
            );
            const allFeatures: GeoJSON.Feature<GeoJSON.LineString>[] = allTrailResponses
              .filter((r): r is VesselTrailResponse => r != null && r.line != null)
              .map((r) => r.line);
            const allFc: GeoJSON.FeatureCollection<GeoJSON.LineString> = {
              type: "FeatureCollection",
              features: allFeatures,
            };
            const allSrc = map.getSource(allTrailSourceId) as maplibregl.GeoJSONSource | undefined;
            if (allSrc) allSrc.setData(allFc);
          } else {
            const allSrc = map.getSource(allTrailSourceId) as maplibregl.GeoJSONSource | undefined;
            if (allSrc) allSrc.setData({ type: "FeatureCollection", features: [] });
          }
        } else {
          const allSrc = map.getSource(allTrailSourceId) as maplibregl.GeoJSONSource | undefined;
          if (allSrc) allSrc.setData({ type: "FeatureCollection", features: [] });
        }

        const violatorMmsis = data
          .filter((v) => v.has_mpa_violations && Number.isFinite(v.lat) && Number.isFinite(v.lon))
          .map((v) => v.mmsi)
          .slice(0, MAX_VIOLATOR_TRAILS);
        ensureViolatorTrailLayer();
        if (violatorMmsis.length > 0) {
          const trailResponses = await Promise.all(
            violatorMmsis.map((mmsi) =>
              fetch(`${API_URL}/vessels/${encodeURIComponent(mmsi)}/trail?hours=6&limit=1000`).then((r) =>
                r.ok ? r.json() : null
              )
            )
          );
          const trailFeatures: GeoJSON.Feature<GeoJSON.LineString>[] = trailResponses
            .filter((r): r is VesselTrailResponse => r != null && r.line != null)
            .map((r) => r.line);
          const trailFc: GeoJSON.FeatureCollection<GeoJSON.LineString> = {
            type: "FeatureCollection",
            features: trailFeatures,
          };
          const trailSrc = map.getSource(violatorTrailSourceId) as maplibregl.GeoJSONSource | undefined;
          if (trailSrc) trailSrc.setData(trailFc);
        } else {
          const trailSrc = map.getSource(violatorTrailSourceId) as maplibregl.GeoJSONSource | undefined;
          if (trailSrc) trailSrc.setData({ type: "FeatureCollection", features: [] });
        }
      } catch (e: any) {
        setError(e?.message || String(e));
      }
    };

    upsertSourceAndLayer();
    fetchAndUpdate();

    const interval = window.setInterval(fetchAndUpdate, 5_000);
    return () => window.clearInterval(interval);
  }, [mapLoaded, allVesselTrailsVisible]);

  useEffect(() => {
    const map = mapInstance.current;
    if (!map) return;
    const layerId = "live-vessels-points";
    if (!map.getLayer(layerId)) return;
    map.setLayoutProperty(layerId, "visibility", vesselsVisible ? "visible" : "none");
  }, [vesselsVisible]);

  useEffect(() => {
    const map = mapInstance.current;
    if (!map) return;
    const layerId = "violator-trails-line";
    if (!map.getLayer(layerId)) return;
    map.setLayoutProperty(layerId, "visibility", violatorTrailsVisible ? "visible" : "none");
  }, [violatorTrailsVisible]);

  useEffect(() => {
    const map = mapInstance.current;
    if (!map) return;
    const layerId = "all-vessel-trails-line";
    if (!map.getLayer(layerId)) return;
    map.setLayoutProperty(layerId, "visibility", allVesselTrailsVisible ? "visible" : "none");
  }, [allVesselTrailsVisible]);

  useEffect(() => {
    const map = mapInstance.current;
    if (!mapLoaded || !map) return;

    const vesselsLayerId = "live-vessels-points";
    const mpaFillLayerId = "mpa-zones-fill";
    const trailSourceId = "selected-vessel-trail";
    const trailLayerId = "selected-vessel-trail-line";

    const ensureTrailLayer = () => {
      const emptyFc: GeoJSON.FeatureCollection<GeoJSON.LineString> = { type: "FeatureCollection", features: [] };
      if (!map.getSource(trailSourceId)) {
        map.addSource(trailSourceId, { type: "geojson", data: emptyFc });
      }
      if (!map.getLayer(trailLayerId)) {
        map.addLayer({
          id: trailLayerId,
          type: "line",
          source: trailSourceId,
          paint: {
            "line-color": "#111",
            "line-width": 2,
            "line-opacity": 0.85,
            "line-dasharray": [1, 2.2],
          },
        });
      }
    };

    ensureTrailLayer();

    const onClick = async (e: maplibregl.MapMouseEvent & maplibregl.EventData) => {
      const layers = [vesselsLayerId, mpaFillLayerId];
      const features = map.queryRenderedFeatures(e.point, { layers });
      const top = features?.[0];
      if (!top) return;

      const layerId = top.layer?.id ?? (top as any).layerId;

      if (layerId === mpaFillLayerId) {
        const zoneIdRaw = (top.properties as any)?.id;
        const zoneId = zoneIdRaw != null ? Number(zoneIdRaw) : NaN;
        if (!Number.isFinite(zoneId)) return;
        try {
          const res = await fetch(`${API_URL}/zones/${zoneId}/stats`);
          if (!res.ok) throw new Error(`HTTP ${res.status}`);
          const stats = await res.json();
          const name = stats?.name ?? `Zone ${zoneId}`;
          const designation = stats?.designation ?? "";
          const violationCount = stats?.violation_count ?? 0;
          const lastTs = stats?.last_violation_ts ?? null;
          new maplibregl.Popup({ closeButton: true, closeOnClick: true })
            .setLngLat(e.lngLat)
            .setHTML(
              `<div style="font-family:system-ui;font-size:13px;max-width:280px;">
                <div style="font-weight:600;margin-bottom:4px;">${name}</div>
                ${designation ? `<div style="color:#555;margin-bottom:6px;">${designation}</div>` : ""}
                <div><span style="font-weight:600;">Violations:</span> ${violationCount}</div>
                ${lastTs ? `<div style="color:#555;margin-top:2px;"><span style="font-weight:600;">Last:</span> ${lastTs}</div>` : ""}
              </div>`
            )
            .addTo(map);
        } catch (err: any) {
          setError(err?.message || String(err));
        }
        return;
      }

      if (layerId === vesselsLayerId) {
        const props = top.properties as {
          mmsi?: string;
          name?: string;
          country?: string | null;
          country_iso2?: string | null;
          callsign?: string | null;
          bearing_deg?: number | null;
          has_mpa_violations?: boolean;
          last_ts?: string | null;
        } | undefined;
        const mmsi = props?.mmsi;
        if (!mmsi) return;
        const hasViolations = props?.has_mpa_violations === true;
        try {
          let trailCount = 0;
          if (hasViolations) {
            const res = await fetch(`${API_URL}/vessels/${encodeURIComponent(mmsi)}/trail?hours=6&limit=2000`);
            if (res.ok) {
              const data: VesselTrailResponse = await res.json();
              trailCount = data.count;
              const fc: GeoJSON.FeatureCollection<GeoJSON.LineString> = {
                type: "FeatureCollection",
                features: data.line ? [data.line] : [],
              };
              const src = map.getSource(trailSourceId) as maplibregl.GeoJSONSource | undefined;
              if (src) src.setData(fc);
            }
          } else {
            const src = map.getSource(trailSourceId) as maplibregl.GeoJSONSource | undefined;
            if (src) src.setData({ type: "FeatureCollection", features: [] });
          }
          const flag = iso2ToFlagEmoji(props?.country_iso2 ?? undefined);
          new maplibregl.Popup({ closeButton: true, closeOnClick: true })
            .setLngLat(e.lngLat)
            .setHTML(
              `<div style="font-family:system-ui;font-size:13px;">
                <div style="font-weight:600;">${flag ? `${flag} ` : ""}${(props?.name as string) || "Vessel"} ${mmsi}</div>
                ${
                  props?.country
                    ? `<div style="color:#555;">${props.country}${props?.country_iso2 ? ` (${props.country_iso2})` : ""}</div>`
                    : ""
                }
                ${
                  props?.callsign
                    ? `<div style="color:#555;">Callsign: ${props.callsign}</div>`
                    : ""
                }
                ${
                  props?.bearing_deg != null && Number.isFinite(props.bearing_deg)
                    ? `<div style="color:#444;font-size:12px;">Course: ${props.bearing_deg.toFixed(0)}°</div>`
                    : ""
                }
                ${
                  props?.last_ts
                    ? `<div style="color:#777;font-size:12px;margin-top:2px;">Last update: ${props.last_ts}</div>`
                    : ""
                }
                ${
                  hasViolations
                    ? `<div style="color:#c00;margin-top:4px;">Passed through MPA</div><div style="color:#555;">Trail points: ${trailCount}</div>`
                    : '<div style="color:#555;margin-top:4px;">No MPA passage</div>'
                }
              </div>`
            )
            .addTo(map);
        } catch (err: any) {
          setError(err?.message || String(err));
        }
      }
    };

    const onMouseMove = (e: maplibregl.MapMouseEvent) => {
      const features = map.queryRenderedFeatures(e.point, { layers: [vesselsLayerId, mpaFillLayerId] });
      map.getCanvas().style.cursor = features.length > 0 ? "pointer" : "";
    };

    map.on("click", onClick);
    map.on("mousemove", onMouseMove);

    return () => {
      map.off("click", onClick);
      map.off("mousemove", onMouseMove);
    };
  }, [mapLoaded, zones]);

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
        <label style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer", marginTop: 8 }}>
          <input
            type="checkbox"
            checked={vesselsVisible}
            onChange={(e) => setVesselsVisible(e.target.checked)}
          />
          Show live vessels
        </label>
        <label style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer", marginTop: 8 }}>
          <input
            type="checkbox"
            checked={violatorTrailsVisible}
            onChange={(e) => setViolatorTrailsVisible(e.target.checked)}
          />
          Show MPA violator trails
        </label>
        <label style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer", marginTop: 8 }}>
          <input
            type="checkbox"
            checked={allVesselTrailsVisible}
            onChange={(e) => setAllVesselTrailsVisible(e.target.checked)}
          />
          Show trails (all vessels in view, max 40)
        </label>
        <div style={{ marginTop: 8, color: "#666", fontSize: 12 }}>
          {zones ? `${zones.features.length} zones` : "Loading…"}
        </div>
      </div>
    </div>
  );
}
