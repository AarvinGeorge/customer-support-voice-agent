/**
 * What: `cn`, merges class names and resolves Tailwind conflicts.
 * Use when: composing className strings in components (shadcn convention).
 * Inputs: class values (strings, arrays, conditional objects).
 * Output: one class string.
 * Side effects: none.
 * Role: shared UI utility.
 */
import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}
