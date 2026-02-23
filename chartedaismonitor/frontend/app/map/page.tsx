import Link from "next/link";
import Map from "./Map";

export default function MapPage() {
  return (
    <div style={{ height: "100vh", display: "flex", flexDirection: "column" }}>
      <header
        style={{
          padding: "8px 16px",
          background: "#1a1a2e",
          color: "#eee",
          display: "flex",
          alignItems: "center",
          gap: 16,
        }}
      >
        <Link href="/" style={{ color: "#eee", textDecoration: "none" }}>
          ← Home
        </Link>
        <span style={{ fontWeight: 600 }}>California MPAs</span>
      </header>
      <div style={{ flex: 1, minHeight: 0 }}>
        <Map />
      </div>
    </div>
  );
}
