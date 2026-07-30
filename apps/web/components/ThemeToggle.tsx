"use client";

import { useEffect, useState } from "react";

type Theme = "dark" | "light";

/**
 * Dark and light are both designed; this picks between them and remembers the choice.
 *
 * The initial value is read from the DOM rather than from storage, because a script in
 * `layout.tsx` has already applied the saved theme before first paint. Reading storage
 * again here would race that script and could flip the page on hydration.
 */
export function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>("dark");
  const [mounted, setMounted] = useState(false);

  useEffect(() => {
    setTheme(document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark");
    setMounted(true);
  }, []);

  function apply(next: Theme) {
    setTheme(next);
    if (next === "light") document.documentElement.setAttribute("data-theme", "light");
    else document.documentElement.removeAttribute("data-theme");
    try {
      localStorage.setItem("lexora-theme", next);
    } catch {
      /* private browsing: the choice holds for this page only */
    }
  }

  return (
    <button
      type="button"
      className="btn btn-quiet px-2.5"
      // Before mount the label would describe the server's guess rather than what is on
      // screen, which a screen reader would read out wrongly.
      aria-label={mounted ? `Switch to ${theme === "dark" ? "light" : "dark"} theme` : "Switch theme"}
      onClick={() => apply(theme === "dark" ? "light" : "dark")}
    >
      <span aria-hidden className="text-base leading-none">
        {theme === "dark" ? "◐" : "◑"}
      </span>
    </button>
  );
}
