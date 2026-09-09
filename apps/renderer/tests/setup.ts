import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';
import { afterEach } from 'vitest';

// `globals: false`, so Testing Library's automatic cleanup never registers.
afterEach(() => {
  cleanup();
});
