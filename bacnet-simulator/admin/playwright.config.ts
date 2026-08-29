import { defineConfig, devices } from '@playwright/test'

// Points at an already-running bacnet-simulator instance (e.g. the combined
// container's web admin on :47900) rather than starting its own dev server --
// login needs a real backend + real account, which `vite dev`'s proxy alone
// doesn't provide. Override with E2E_BASE_URL for a different target.
export default defineConfig({
  testDir: './e2e',
  fullyParallel: true,
  retries: process.env.CI ? 1 : 0,
  reporter: 'list',
  use: {
    baseURL: process.env.E2E_BASE_URL ?? 'http://localhost:47900',
    trace: 'on-first-retry',
  },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
  ],
})
