"use client";
/**
 * What: one button that switches between the light and dark themes.
 * Use when: in the console header.
 * Inputs: none.
 * Output: an icon button with an accessible label naming the target theme.
 * Side effects: changes the theme (next-themes writes localStorage).
 * Role: theme control.
 */
import { Moon, Sun } from "lucide-react";
import { useTheme } from "next-themes";
import { useSyncExternalStore } from "react";

import { Button } from "@/components/ui/button";

const subscribe = () => () => {};

/**
 * What: the toggle component.
 * Use when: see module.
 * Inputs: none.
 * Output: JSX button (rendered after hydration so the label matches the stored theme).
 * Side effects: see module.
 * Role: theme control.
 */
export function ThemeToggle() {
  const { resolvedTheme, setTheme } = useTheme();
  const mounted = useSyncExternalStore(subscribe, () => true, () => false);
  const dark = mounted && resolvedTheme === "dark";
  const label = dark ? "Switch to light theme" : "Switch to dark theme";
  return (
    <Button variant="ghost" size="icon" aria-label={label} title={label} onClick={() => setTheme(dark ? "light" : "dark")}>
      {dark ? <Sun aria-hidden /> : <Moon aria-hidden />}
    </Button>
  );
}
