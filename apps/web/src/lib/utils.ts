/**
 * What: `cn`, merges class names and resolves Tailwind conflicts.
 * Use when: composing className strings in components (shadcn convention).
 * Inputs: class values (strings, arrays, conditional objects).
 * Output: one class string.
 * Side effects: none.
 * Role: shared UI utility.
 */
import { clsx, type ClassValue } from "clsx";
import { extendTailwindMerge } from "tailwind-merge";

/**
 * What: tailwind-merge that knows the DESIGN.md type scale (globals.css --text-*) as font sizes.
 * Use when: cn merges classes.
 * Inputs: none.
 * Output: a merge function.
 * Side effects: none.
 * Role: without this, `text-label` and a text color such as `text-ink` look like the same
 *   group and the size is dropped.
 */
const twMerge = extendTailwindMerge({
  extend: {
    theme: {
      text: ["title", "heading", "body", "body-sm", "label", "label-sm", "caption", "mono", "mono-sm", "mono-lg"],
    },
  },
});

/**
 * What: merges class values and drops conflicting Tailwind classes.
 * Use when: building a className from base, variant and caller classes.
 * Inputs: class values (strings, arrays, conditional objects).
 * Output: one class string.
 * Side effects: none.
 * Role: shared UI utility.
 */
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}
