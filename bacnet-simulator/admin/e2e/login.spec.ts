import { test, expect } from '@playwright/test'

// Exercises the real login flow (LoginView.vue) against a running
// bacnet-simulator instance, then confirms the left device/equipment
// sidebar (LeftSideView.vue's `.inventory-sider`) actually renders
// afterward -- the two things a "did the deploy come up correctly" check
// cares about. Credentials come from the environment, never hardcoded --
// an account has to already exist (LoginView falls into first-run "setup"
// mode otherwise, which this test doesn't attempt to drive).
const username = process.env.E2E_USERNAME
const password = process.env.E2E_PASSWORD

test.skip(!username || !password, 'Set E2E_USERNAME and E2E_PASSWORD to run this test.')

test('logs in and renders the left device tree', async ({ page }) => {
  await page.goto('/')

  const usernameField = page.locator('input[autocomplete="username"]')
  const passwordField = page.locator('input[autocomplete="new-password"]')
  await expect(usernameField).toBeVisible()
  await expect(passwordField).toBeVisible()

  await usernameField.fill(username!)
  await passwordField.fill(password!)
  await page.getByRole('button', { name: 'Sign in' }).click()

  // Login form is gone and the main shell (with the sidebar) has taken over.
  await expect(usernameField).not.toBeVisible()

  const sidebar = page.locator('.inventory-sider')
  await expect(sidebar).toBeVisible()
  await expect(sidebar.getByText(/^Items \(\d+\)$/)).toBeVisible()
  await expect(sidebar.locator('.sidebar-tree')).toBeVisible()
})
