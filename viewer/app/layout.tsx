import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Notch — a clearing layer for agent micropayments",
  description:
    "Agents accrue hash-committed notches on a shared tab; each cycle nets to one signed statement, and a counterparty disputes the statement rather than the transaction. Live on GenLayer StudioNet.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
