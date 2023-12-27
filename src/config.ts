// Place any global data in this file.
// You can import this data from anywhere in your site by using the `import` keyword.

export const SITE_TITLE = "yet another engineering blog";
export const SITE_DESCRIPTION =
  "a journey blending technical expertise with life's everyday wonders.";
export const TWITTER_HANDLE = "@leventanilozen";
export const MY_NAME = "Levent Anil Ozen";

// setup in astro.config.mjs
const BASE_URL = new URL(import.meta.env.SITE);
export const SITE_URL = BASE_URL.origin;
