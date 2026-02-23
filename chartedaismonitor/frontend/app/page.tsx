import Link from "next/link";

export default function Home() {
  return (
    <main style={{ padding: "2rem", fontFamily: "system-ui" }}>
      <h1>AIS MPA Monitor</h1>
      <p>Monitor vessel locations near the California coast and Marine Protected Areas.</p>
      <p>
        <Link href="/map" style={{ color: "#0066cc" }}>View map (California MPAs)</Link>
      </p>
    </main>
  );
}
