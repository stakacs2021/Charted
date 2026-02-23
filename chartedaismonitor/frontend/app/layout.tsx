import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "AIS MPA Monitor",
  description: "Monitor vessel locations near the California coast and MPAs",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
