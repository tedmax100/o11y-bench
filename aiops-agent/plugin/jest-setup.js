// Jest setup provided by Grafana scaffolding
import './.config/jest-setup';

// jsdom has neither ResizeObserver nor IntersectionObserver, and @grafana/ui's
// overlay components (Drawer, Modal) construct both on mount — so without these
// every test that opens one fails on a missing browser API rather than on
// anything about the component.
global.ResizeObserver =
  global.ResizeObserver ||
  class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };

global.IntersectionObserver =
  global.IntersectionObserver ||
  class {
    observe() {}
    unobserve() {}
    disconnect() {}
    takeRecords() {
      return [];
    }
  };
