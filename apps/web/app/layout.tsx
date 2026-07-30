import type { Metadata, Viewport } from "next";
import { Inter, JetBrains_Mono, Sora } from "next/font/google";
import "./globals.css";

/*
 * Three faces. The pairing states the thesis: this is an instrument, and the thing it
 * reads happens to be law.
 *
 *   Sora           display. Geometric, slightly mechanical, with a flat-sided 'a' and a
 *                  tight aperture — it looks engineered rather than printed. Used large,
 *                  never as body copy.
 *   Inter          body. This app renders real legislation and contract text that people
 *                  read at length on a screen; Inter is drawn for exactly that, and its
 *                  tall x-height survives the dark background a serif would smear on.
 *   JetBrains Mono the instrument layer and nothing else: scores, ranks, page numbers,
 *                  citation keys. If it is monospaced, a machine produced it.
 *
 * next/font self-hosts all three at build time, so the strict CSP in next.config.ts needs
 * no external origin and there is no layout shift from a font swap.
 */
const sora = Sora({
  subsets: ["latin"],
  weight: ["500", "600", "700"],
  variable: "--font-sora",
  display: "swap",
});

const inter = Inter({
  subsets: ["latin"],
  weight: ["400", "500", "600"],
  variable: "--font-inter",
  display: "swap",
});

const jetbrains = JetBrains_Mono({
  subsets: ["latin"],
  weight: ["400", "500", "700"],
  variable: "--font-jetbrains",
  display: "swap",
});

export const metadata: Metadata = {
  title: "Lexora — ask your documents",
  description:
    "Bring a contract, a law, a scan or a link. Ask in plain English and get the answer "
    + "with the exact clause attached — or an honest \"not in here\".",
  robots: { index: false, follow: false },
};

export const viewport: Viewport = {
  themeColor: "#08090f",
  width: "device-width",
  initialScale: 1,
};

/*
 * Applied before first paint, so a reader who chose light does not get a dark flash on
 * every navigation. It runs from a string because it has to execute ahead of hydration;
 * it reads one key and sets one attribute, and touches nothing else.
 */
const THEME_BOOTSTRAP = `
(function () {
  try {
    var saved = localStorage.getItem("lexora-theme");
    var prefersLight = window.matchMedia("(prefers-color-scheme: light)").matches;
    var theme = saved || (prefersLight ? "light" : "dark");
    if (theme === "light") document.documentElement.setAttribute("data-theme", "light");
  } catch (e) {}
})();
`;

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={`${sora.variable} ${inter.variable} ${jetbrains.variable}`}>
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_BOOTSTRAP }} />
      </head>
      <body className="min-h-dvh bg-paper text-ink antialiased">
        <div aria-hidden className="pointer-events-none fixed inset-0 -z-10 paper-tooth" />
        {children}
      </body>
    </html>
  );
}
