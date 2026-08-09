import { expect, it } from 'vitest';
import { progressPercent } from './progress';

it('将 0..1 归一化进度转换为整数百分比', () => {
  expect(progressPercent(0.01)).toBe(1);
  expect(progressPercent(0.5)).toBe(50);
  expect(progressPercent(1)).toBe(100);
});

it('对异常越界值执行安全截断', () => {
  expect(progressPercent(-0.1)).toBe(0);
  expect(progressPercent(1.1)).toBe(100);
});
