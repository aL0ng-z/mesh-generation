import { expect, it } from 'vitest';
import {
  calculateQualityDeltas,
  formatQualityNumber,
  normalizeQualityMetrics,
  qualityReportStatus,
} from './qualityDelta';

it('仅计算两轮共有的有限数值质量差值（右减左）', () => {
  const delta = calculateQualityDeltas(
    { metrics: [{ key: 'min_angle', label: '最小角', value: 18, unit: '°' }, { key: 'text', value: 'unknown' }] },
    { metrics: [{ key: 'min_angle', label: '最小角', value: '21.5', unit: '°' }, { key: 'only_right', value: 1 }] },
  );
  expect(delta).toEqual([{ key: 'min_angle', label: '最小角', left: 18, right: 21.5, delta: 3.5, unit: '°' }]);
});

it('兼容根质量 Schema v3 的 metrics 字典与 result.status', () => {
  const left = {
    schema_version: 3,
    metrics: { negative_cells: 0, number_of_points: 1200, min_skewness_angle: 17.5 },
    result: { status: 'PASS' as const, accepted: true, reasons: [] },
  };
  const right = {
    schema_version: 3,
    metrics: { negative_cells: 0, number_of_points: 1350, min_skewness_angle: 19 },
    result: { status: 'PASS' as const, accepted: true, reasons: [] },
  };
  expect(normalizeQualityMetrics(left).find((metric) => metric.key === 'number_of_points')?.value).toBe(1200);
  expect(qualityReportStatus(left)).toBe('PASS');
  expect(calculateQualityDeltas(left, right).find((item) => item.key === 'min_skewness_angle')?.delta).toBe(1.5);
});

it('格式化质量数值时保留极小差值并消除负零', () => {
  expect(formatQualityNumber(-5.2e-9)).toBe('-5.2E-9');
  expect(formatQualityNumber(-0)).toBe('0');
  expect(formatQualityNumber(1_464_289)).toBe('1,464,289');
  expect(formatQualityNumber(14.8742)).toBe('14.874');
});
