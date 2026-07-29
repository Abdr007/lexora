import type { NextConfig } from "next";

/**
 * Security headers are set here rather than in middleware so they apply to static
 * assets too. The CSP is deliberately strict: the app loads no third-party script,
 * font or image, and `connect-src` is widened at build time only to the API origin
 * the deployment actually talks to.
 */
const apiOrigin = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:7861";
const isDev = process.env.NODE_ENV !== "production";

const csp = [
  "default-src 'self'",
  // Next.js injects inline bootstrap scripts; 'unsafe-inline' is required for them and
  // is safe here because no user-controlled string is ever rendered as HTML.
  //
  // 'unsafe-eval' is added in DEVELOPMENT ONLY. Next's React Refresh runtime evaluates
  // strings to hot-reload modules, and blocking it throws during hydration — which
  // silently disables every effect on the page and looks exactly like an API outage.
  // The shipped production bundle contains no React Refresh and no eval, so the
  // deployed policy stays strict.
  `script-src 'self' 'unsafe-inline'${isDev ? " 'unsafe-eval'" : ""}`,
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data:",
  "font-src 'self' data:",
  `connect-src 'self' ${apiOrigin}`,
  "frame-ancestors 'none'",
  "base-uri 'self'",
  "form-action 'self'",
  "object-src 'none'",
].join("; ");

const nextConfig: NextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,
  // The floating dev badge overlaps the composer and appears in demo captures.
  devIndicators: false,
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "Content-Security-Policy", value: csp },
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "X-Frame-Options", value: "DENY" },
          { key: "Referrer-Policy", value: "no-referrer" },
          {
            key: "Permissions-Policy",
            value: "geolocation=(), microphone=(), camera=(), interest-cohort=()",
          },
        ],
      },
    ];
  },
};

export default nextConfig;
