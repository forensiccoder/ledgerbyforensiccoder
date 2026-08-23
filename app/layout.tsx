import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "LedgerLens | Forensic statement analysis",
  description: "Local-first Indian bank statement analysis for cash, NEFT and UPI activity.",
  icons: { icon: "/favicon.svg", shortcut: "/favicon.svg" },
  openGraph: { title: "LedgerLens", description: "Forensic statement analysis", images: ["/og-ey.png"] },
  twitter: { card: "summary_large_image", title: "LedgerLens", description: "Forensic statement analysis", images: ["/og-ey.png"] },
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="en"><body>{children}</body></html>;
}
