/**
 * What: root layout: fonts, global tokens, theme provider and the toast region.
 * Use when: Next.js renders any route.
 * Inputs: the route's children.
 * Output: the <html> document.
 * Side effects: loads Inter and JetBrains Mono (self-hosted by next/font at build time).
 * Role: app shell shared by the access gate (/) and the console (/console).
 */
import type { Metadata, Viewport } from "next";
import { Inter, JetBrains_Mono } from "next/font/google";
import type { ReactNode } from "react";

import { Providers } from "@/components/providers";

import "./globals.css";

/**
 * What: Inter font loader, exposed as the --font-inter variable.
 * Use when: the root html class list.
 * Inputs: none.
 * Output: a next/font object.
 * Side effects: next/font downloads and self-hosts the font at build time.
 * Role: body and label typeface.
 */
const inter = Inter({ subsets: ["latin"], variable: "--font-inter", display: "swap" });
/**
 * What: JetBrains Mono font loader, exposed as --font-jetbrains-mono.
 * Use when: the root html class list.
 * Inputs: none.
 * Output: a next/font object.
 * Side effects: next/font downloads and self-hosts the font at build time.
 * Role: typeface for IDs, timings, tokens and cost.
 */
const jetbrainsMono = JetBrains_Mono({ subsets: ["latin"], variable: "--font-jetbrains-mono", display: "swap" });

/**
 * What: page metadata for every route.
 * Use when: Next.js builds the document head.
 * Inputs: none.
 * Output: a Metadata object.
 * Side effects: none.
 * Role: default title and description.
 */
export const metadata: Metadata = {
  title: "Quillstack Support",
  description: "Support for Quillstack by chat and voice, with every step shown.",
};

/**
 * What: viewport theme colors for light and dark.
 * Use when: Next.js builds the document head.
 * Inputs: none.
 * Output: a Viewport object.
 * Side effects: none.
 * Role: browser chrome matches Paper.
 */
export const viewport: Viewport = {
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#fcfaf6" },
    { media: "(prefers-color-scheme: dark)", color: "#171310" },
  ],
};

/**
 * What: the root layout component.
 * Use when: Next.js App Router wraps every page.
 * Inputs: { children }.
 * Output: JSX html and body.
 * Side effects: none beyond font loading.
 * Role: root of the React tree.
 */
export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" className={`${inter.variable} ${jetbrainsMono.variable}`} suppressHydrationWarning>
      <body className="min-h-dvh bg-background text-foreground">
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
