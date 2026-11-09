import "@testing-library/jest-dom/vitest";
import { configure } from "@testing-library/react";

// Windows 并行加载懒路由时允许正常的模块解析时间，不放宽业务断言。
configure({ asyncUtilTimeout: 3000 });

// HeroUI 列表会将键盘选中项滚入视口；JSDOM 不实现布局，真实滚动由浏览器验收覆盖。
Object.defineProperty(HTMLElement.prototype, "scrollIntoView", {
  configurable: true,
  value: () => undefined,
});

// 弹出层通过 ResizeObserver 计算布局；测试只验证交互，不模拟像素位置。
class TestResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

Object.defineProperty(globalThis, "ResizeObserver", {
  configurable: true,
  value: TestResizeObserver,
});

// 浮层定位使用 Range；JSDOM 没有布局引擎，返回零尺寸即可。
Object.defineProperty(Range.prototype, "getBoundingClientRect", {
  configurable: true,
  value: () => ({
    bottom: 0,
    height: 0,
    left: 0,
    right: 0,
    top: 0,
    width: 0,
    x: 0,
    y: 0,
    toJSON: () => ({}),
  }),
});

// React Aria 读取设备能力；JSDOM 使用固定桌面视口，像素与触控在浏览器中验收。
Object.defineProperty(window, "matchMedia", {
  configurable: true,
  writable: true,
  value: (query: string): MediaQueryList => ({
    matches: false,
    media: query,
    onchange: null,
    addListener() {},
    removeListener() {},
    addEventListener() {},
    removeEventListener() {},
    dispatchEvent: () => true,
  }),
});

// 页签的共享元素动画依赖 Web Animations；JSDOM 不执行视觉动画。
Object.defineProperty(Element.prototype, "getAnimations", {
  configurable: true,
  value: () => [],
});
