import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Salvage — Bounded Autonomous Revenue Recovery",
  description:
    "Diagnoses failed Razorpay payments against the published error taxonomy, prices each recovery action, and acts only where it pays.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen antialiased">{children}</body>
    </html>
  );
}
