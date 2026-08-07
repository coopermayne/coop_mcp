/*
 * Tailwind build config for the compiled stylesheet (static/tailwind.css).
 * We compile instead of using the Play CDN (cdn.tailwindcss.com): the CDN is a
 * runtime compiler meant for prototyping — it re-parses the DOM on every load,
 * injects its styles AFTER base.html's <style> block (forcing specificity
 * hacks), and vanishes offline, which matters for an installed PWA.
 *
 * Rebuild after adding/removing classes in templates or static JS:
 *   cd webapp && npx -y tailwindcss@3.4.17 -i tailwind.input.css -o static/tailwind.css --minify
 *
 * Content globs cover the Jinja templates and our own JS (which builds class
 * strings at runtime — trainer.js, chat.js, mentions.js); vendor JS is excluded.
 */
module.exports = {
  content: ["./templates/**/*.html", "./static/*.js"],
  // Dark mode is driven by the data-theme attribute the head script sets before
  // paint. Templates currently theme via the central override table in base.html
  // rather than dark: variants, but declare the selector so dark: works if used.
  darkMode: ["selector", '[data-theme="dark"]'],
  theme: {
    extend: {
      fontFamily: { sans: ["Inter", "system-ui", "sans-serif"] },
    },
  },
};
