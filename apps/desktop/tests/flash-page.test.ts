import { describe, expect, it } from 'vitest';
import { FLASH_MAX_MS, FLASH_MS, flashPageHtml, flashPageUrl } from '../src/main/flash-page.js';

describe('the stop flash page', () => {
  it('lasts the 400 ms UI.md § 7 asks for, and is torn down soon after regardless', () => {
    expect(FLASH_MS).toBe(400);
    expect(FLASH_MAX_MS).toBeGreaterThan(FLASH_MS);
    expect(FLASH_MAX_MS).toBeLessThanOrEqual(2_000);
    expect(flashPageHtml()).toContain('animation:fade 400ms');
  });

  it('has no script and a policy that would refuse one', () => {
    const html = flashPageHtml();
    expect(html).not.toMatch(/<script/i);
    expect(html).toContain("default-src 'none'");
  });

  it('turns the fade into a static border under reduced motion (UI.md § 10)', () => {
    expect(flashPageHtml()).toContain(
      '@media (prefers-reduced-motion: reduce){body{animation:none}}',
    );
  });

  it('is served as a data URL that decodes back to the page', () => {
    const url = flashPageUrl();
    expect(url.startsWith('data:text/html;charset=utf-8,')).toBe(true);
    expect(decodeURIComponent(url.slice(url.indexOf(',') + 1))).toBe(flashPageHtml());
  });
});
