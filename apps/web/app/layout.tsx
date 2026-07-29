import type { Metadata, Viewport } from "next";
import { Bodoni_Moda, JetBrains_Mono, Source_Serif_4 } from "next/font/google";
import "./globals.css";

/*
 * Three faces, and the pairing is the concept: the law is old, the machine is new.
 *
 *   Bodoni Moda    a didone — the typeface of official gazettes and title pages. Used
 *                  only at display size, where its thick/thin contrast reads.
 *   Source Serif 4 statutory body copy. This app renders real legislation and it is
 *                  meant to be read at length, which is what a serif is for.
 *   JetBrains Mono the instrument layer, and nothing else: ranks, scores, timings,
 *                  article numbers in the margin. If it is monospaced, a machine
 *                  produced it.
 *
 * next/font self-hosts all three at build time, so the strict CSP needs no external
 * origin and there is no layout shift from a font swap.
 */
const bodoni = Bodoni_Moda({
  subsets: ["latin"],
  weight: ["400", "600", "700"],
  style: ["normal", "italic"],
  variable: "--font-bodoni",
  display: "swap",
});

const sourceSerif = Source_Serif_4({
  subsets: ["latin"],
  weight: ["400", "600"],
  variable: "--font-source-serif",
  display: "swap",
});

const jetbrains = JetBrains_Mono({
  subsets: ["latin"],
  weight: ["400", "500", "700"],
  variable: "--font-jetbrains",
  display: "swap",
});

export const metadata: Metadata = {
  title: "Lexora — Statute Instrument",
  description:
    "Answers only from indexed UAE labour and Dubai tenancy law, cited to the clause, "
    + "and refused when the corpus does not cover the question.",
  robots: { index: false, follow: false },
};

export const viewport: Viewport = {
  themeColor: "#faf9f5",
  width: "device-width",
  initialScale: 1,
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html
      lang="en"
      className={`${bodoni.variable} ${sourceSerif.variable} ${jetbrains.variable}`}
    >
      <body className="min-h-dvh bg-paper text-ink antialiased">
        <div aria-hidden className="pointer-events-none fixed inset-0 -z-10 paper-tooth" />
        {children}
      </body>
    </html>
  );
}
