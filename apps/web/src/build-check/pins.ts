/**
 * What: pure helpers for the bundle PIN check: read PINs from the seeds text, merge them with
 *   the fixture PINs, and find PINs bundled as string literals.
 * Use when: src/build-output.pins.check.ts scans the build; pins.test.ts tests the rules.
 * Inputs: seed file text (or null when absent), fixture PINs, file names and texts.
 * Output: PIN lists and "file: pin" hit strings.
 * Side effects: none (callers read the files).
 * Role: review PR #8 I-1. PINs are strings in every contract shape, so a leak shows up as a
 *   quoted literal ("4821", '4821' or `4821`); bare numbers such as years or sizes are ignored,
 *   which keeps a real seeded PIN from colliding with unrelated digits in the bundle.
 */

/**
 * What: the 4-digit values of any key containing "pin" (any case) in YAML or JSON text.
 * Use when: reading data/seeds/personas.yaml.
 * Inputs: the file text.
 * Output: PIN strings in file order (may repeat).
 * Side effects: none.
 * Role: tolerant of key spellings (pin, support_pin, pin_code, PIN, "pin") and quoting.
 */
export function seedPins(text: string): string[] {
  const re = /["']?[A-Za-z_]*pin[A-Za-z_]*["']?\s*:\s*["']?(\d{4})(?!\d)["']?/gi;
  return [...text.matchAll(re)].map((m) => m[1]!);
}

/**
 * What: the PINs the scan looks for: fixtures plus seeds, unique.
 * Use when: before scanning the build output.
 * Inputs: fixture PINs; the seeds file text, or null when the file does not exist.
 * Output: unique PIN strings.
 * Side effects: none.
 * Role: throws when the seeds file exists but yields no PIN (a format the parser does not
 *   know must not pass silently), and when there is no PIN at all (the scan would prove nothing).
 */
export function collectPins(fixturePins: readonly string[], seedsText: string | null): string[] {
  const fromSeeds = seedsText === null ? [] : seedPins(seedsText);
  if (seedsText !== null && fromSeeds.length === 0) {
    throw new Error("data/seeds/personas.yaml exists but no PIN was read from it; update seedPins for its format");
  }
  const pins = [...new Set([...fixturePins, ...fromSeeds])];
  if (pins.length === 0) throw new Error("no PIN to look for: fixtures and seeds are both empty");
  return pins;
}

/**
 * What: files whose text holds a PIN as a whole string literal.
 * Use when: scanning the build output.
 * Inputs: files as {name, text}; PINs.
 * Output: "name: pin" for each hit, in input order.
 * Side effects: none.
 * Role: the measurement.
 */
export function findLeaks(files: readonly { name: string; text: string }[], pins: readonly string[]): string[] {
  const hits: string[] = [];
  for (const { name, text } of files) {
    for (const pin of pins) {
      if (new RegExp(`(["'\`])${pin}\\1`).test(text)) hits.push(`${name}: ${pin}`);
    }
  }
  return hits;
}
