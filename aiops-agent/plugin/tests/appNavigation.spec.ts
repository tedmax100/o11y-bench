import { test, expect } from './fixtures';
import { ROUTES } from '../src/constants';

test.describe('navigating app', () => {
  test('chat page should render', async ({ gotoPage, page }) => {
    await gotoPage(`/${ROUTES.Chat}`);
    await expect(page.getByPlaceholder('Ask the agent...')).toBeVisible();
  });

  test('root redirects to chat', async ({ gotoPage, page }) => {
    await gotoPage('/');
    await expect(page.getByPlaceholder('Ask the agent...')).toBeVisible();
  });
});
