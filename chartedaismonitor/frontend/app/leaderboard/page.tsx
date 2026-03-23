"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

function iso2ToFlagEmoji(iso2: string | null | undefined): string {
  if (!iso2 || iso2.length !== 2) return "";
  const u = iso2.toUpperCase();
  const a = u.charCodeAt(0) - 65 + 0x1f1e6;
  const b = u.charCodeAt(1) - 65 + 0x1f1e6;
  if (a < 0x1f1e6 || a > 0x1f1ff || b < 0x1f1e6 || b > 0x1f1ff) return "";
  return String.fromCodePoint(a, b);
}

type LeaderboardRow = {
  rank: number;
  mmsi: string;
  name?: string | null;
  country?: string | null;
  country_iso2?: string | null;
  callsign?: string | null;
  violation_count: number;
  last_violation_ts?: string | null;
};

export default function LeaderboardPage() {
  const [rows, setRows] = useState<LeaderboardRow[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const fetchData = async () => {
      try {
        const res = await fetch(`${API_URL}/vessels/leaderboard?limit=100`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data: LeaderboardRow[] = await res.json();
        setRows(data);
      } catch (e: any) {
        setError(e?.message || String(e));
      } finally {
        setLoading(false);
      }
    };
    fetchData();
  }, []);

  return (
    <main style={{ minHeight: "100vh", fontFamily: "system-ui", background: "#0b1020", color: "#f5f5f5" }}>
      <header
        style={{
          padding: "12px 20px",
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          borderBottom: "1px solid rgba(255,255,255,0.08)",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 16 }}>
          <Link href="/" style={{ color: "#d0d4ff", textDecoration: "none", fontSize: 14 }}>
            ← Home
          </Link>
          <Link href="/map" style={{ color: "#d0d4ff", textDecoration: "none", fontSize: 14 }}>
            Map
          </Link>
        </div>
        <div style={{ fontWeight: 600 }}>MPA Violations Leaderboard</div>
      </header>

      <section style={{ padding: "20px" }}>
        {error && (
          <div style={{ marginBottom: 12, color: "#ff6b6b", fontSize: 14 }}>
            Failed to load leaderboard: {error}
          </div>
        )}
        {loading && !error && <div style={{ color: "#cbd5ff", fontSize: 14 }}>Loading…</div>}
        {!loading && !error && rows.length === 0 && (
          <div style={{ color: "#cbd5ff", fontSize: 14 }}>No recorded MPA violations yet.</div>
        )}
        {!loading && !error && rows.length > 0 && (
          <div
            style={{
              borderRadius: 12,
              border: "1px solid rgba(255,255,255,0.08)",
              overflow: "hidden",
              background: "rgba(11,16,32,0.9)",
            }}
          >
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
              <thead style={{ background: "rgba(255,255,255,0.03)" }}>
                <tr>
                  <th style={{ textAlign: "left", padding: "10px 12px" }}>#</th>
                  <th style={{ textAlign: "left", padding: "10px 12px" }}>Vessel</th>
                  <th style={{ textAlign: "left", padding: "10px 12px" }}>MMSI</th>
                  <th style={{ textAlign: "left", padding: "10px 12px" }}>Flag</th>
                  <th style={{ textAlign: "left", padding: "10px 12px" }}>Callsign</th>
                  <th style={{ textAlign: "right", padding: "10px 12px" }}>Violations</th>
                  <th style={{ textAlign: "left", padding: "10px 12px" }}>Last violation</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row, idx) => (
                  <tr
                    key={row.mmsi}
                    style={{
                      background: idx % 2 === 0 ? "rgba(255,255,255,0.01)" : "transparent",
                    }}
                  >
                    <td style={{ padding: "8px 12px", color: "#aab1ff" }}>{row.rank}</td>
                    <td style={{ padding: "8px 12px" }}>{row.name || "—"}</td>
                    <td style={{ padding: "8px 12px", fontFamily: "monospace" }}>{row.mmsi}</td>
                    <td style={{ padding: "8px 12px" }}>
                      {(() => {
                        const f = iso2ToFlagEmoji(row.country_iso2);
                        const label = row.country || row.country_iso2;
                        if (!label) return "—";
                        return (
                          <>
                            {f ? `${f} ` : ""}
                            {label}
                          </>
                        );
                      })()}
                    </td>
                    <td style={{ padding: "8px 12px" }}>{row.callsign || "—"}</td>
                    <td style={{ padding: "8px 12px", textAlign: "right", fontWeight: 600 }}>{row.violation_count}</td>
                    <td style={{ padding: "8px 12px", color: "#cbd5ff" }}>
                      {row.last_violation_ts ? row.last_violation_ts : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </main>
  );
}

