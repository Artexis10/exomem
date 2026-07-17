import {defineConfig} from "@playwright/test";

const liveStorageState = process.env.EXOMEM_LIVE_STORAGE_STATE?.trim();

export default defineConfig({
  testDir: ".",
  testMatch: "*.spec.mjs",
  fullyParallel: false,
  use: {
    baseURL: process.env.STUDIO_BASE_URL || "http://127.0.0.1:8765",
    trace: "retain-on-failure",
  },
  projects: [
    {
      name: "chromium",
      testIgnore: "live-transfer.spec.mjs",
      use: {browserName: "chromium"},
    },
    {
      name: "hosted-live-chromium",
      testMatch: "live-transfer.spec.mjs",
      use: {
        browserName: "chromium",
        baseURL: process.env.EXOMEM_LIVE_BASE_URL || "https://substratesystems.io",
        ...(liveStorageState ? {storageState: liveStorageState} : {}),
        // Transfer grants are short-lived credentials. Never retain them in a trace.
        trace: "off",
      },
    },
  ],
});
