import type { QualityMetric, QualityReport } from '../../api/types';

export interface QualityDelta {
  key: string;
  label: string;
  left: number;
  right: number;
  delta: number;
  unit?: string | null;
}

const metricLabels: Record<string, string> = {
  negative_cells: '负体积单元数',
  number_of_points: '网格点数',
  number_of_cells: '网格单元数',
  min_skewness_angle: '最小偏斜角',
  max_skewness_angle: '最大偏斜角',
  min_volume: '最小体积',
  max_aspect_ratio: '最大长宽比',
};

const decimalFormatter = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 3 });
const scientificFormatter = new Intl.NumberFormat('zh-CN', {
  notation: 'scientific',
  maximumSignificantDigits: 4,
});

/** 保留极小的真实差值，并避免把 IEEE -0 显示为 “-0”。 */
export function formatQualityNumber(value: number): string {
  const normalized = Object.is(value, -0) ? 0 : value;
  if (normalized !== 0 && Math.abs(normalized) < 0.001) {
    return scientificFormatter.format(normalized);
  }
  return decimalFormatter.format(normalized);
}

export function normalizeQualityMetrics(report?: QualityReport | null): QualityMetric[] {
  if (!report?.metrics) return [];
  if (Array.isArray(report.metrics)) return report.metrics;
  return Object.entries(report.metrics).flatMap(([key, value]) => {
    if (value !== null && typeof value !== 'number' && typeof value !== 'string' && typeof value !== 'boolean') return [];
    return [{
      key,
      label: metricLabels[key] ?? key.replaceAll('_', ' '),
      value: typeof value === 'boolean' ? String(value) : value,
    }];
  });
}

export function qualityReportStatus(
  report: QualityReport | null | undefined,
  fallback: QualityReport['status'] = 'UNKNOWN',
) {
  return report?.status ?? report?.result?.status ?? fallback;
}

export function numericQualityMetrics(report?: QualityReport | null): Map<string, QualityMetric & { value: number }> {
  const result = new Map<string, QualityMetric & { value: number }>();
  for (const metric of normalizeQualityMetrics(report)) {
    const numeric = typeof metric.value === 'number' ? metric.value : Number(metric.value);
    if (Number.isFinite(numeric)) result.set(metric.key, { ...metric, value: numeric });
  }
  return result;
}

export function calculateQualityDeltas(
  left?: QualityReport | null,
  right?: QualityReport | null,
): QualityDelta[] {
  const leftMetrics = numericQualityMetrics(left);
  const rightMetrics = numericQualityMetrics(right);
  const deltas: QualityDelta[] = [];
  for (const [key, leftMetric] of leftMetrics) {
    const rightMetric = rightMetrics.get(key);
    if (!rightMetric) continue;
    deltas.push({
      key,
      label: leftMetric.label ?? rightMetric.label ?? key,
      left: leftMetric.value,
      right: rightMetric.value,
      delta: rightMetric.value - leftMetric.value,
      unit: leftMetric.unit ?? rightMetric.unit,
    });
  }
  return deltas;
}
