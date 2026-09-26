import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "DataScout",
  description: "Find the right data product before querying a local snapshot.",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
