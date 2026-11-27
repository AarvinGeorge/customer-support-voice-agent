"use client";
/**
 * What: client-side providers: theme (light default, dark via the .dark class) and Sonner toasts.
 * Use when: mounted once by the root layout.
 * Inputs: { children }.
 * Output: children wrapped in ThemeProvider plus a Toaster.
 * Side effects: next-themes reads and writes the chosen theme in localStorage.
 * Role: app-wide client context.
 */
import { ThemeProvider, useTheme } from "next-themes";
import type { ReactNode } from "react";
import { Toaster } from "sonner";

/**
 * What: Sonner toaster that follows the active theme.
 * Use when: rendered once inside ThemeProvider.
 * Inputs: none.
 * Output: the toast region.
 * Side effects: none.
 * Role: notification surface (429 and 503 banners are later tasks).
 */
function ThemedToaster() {
  const { resolvedTheme } = useTheme();
  return <Toaster theme={resolvedTheme === "dark" ? "dark" : "light"} position="bottom-center" />;
}

/**
 * What: provider wrapper component.
 * Use when: root layout.
 * Inputs: { children }.
 * Output: JSX.
 * Side effects: see module.
 * Role: app-wide client context.
 */
export function Providers({ children }: { children: ReactNode }) {
  return (
    <ThemeProvider attribute="class" defaultTheme="light" enableSystem={false} disableTransitionOnChange>
      {children}
      <ThemedToaster />
    </ThemeProvider>
  );
}
