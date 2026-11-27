/**
 * What: PostCSS config that runs Tailwind CSS v4.
 * Use when: Next.js compiles src/app/globals.css.
 * Inputs: none.
 * Output: the PostCSS plugin list.
 * Side effects: none.
 * Role: build config for apps/web styling.
 */
const config = {
  plugins: {
    "@tailwindcss/postcss": {},
  },
};

export default config;
