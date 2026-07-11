import {defineConfig} from "@playwright/test";

export default defineConfig({
  testDir: ".",
  testMatch: "studio.spec.mjs",
  fullyParallel: false,
  use: {
    baseURL: process.env.STUDIO_BASE_URL || "http://127.0.0.1:8765",
    trace: "retain-on-failure",
  },
  projects: [{name: "chromium", use: {browserName: "chromium"}}],
});
