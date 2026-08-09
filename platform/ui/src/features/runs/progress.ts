/** 后端统一使用 0..1 的归一化进度，网页统一转换为整数百分比。 */
export function progressPercent(value: number): number {
  return Math.round(Math.min(1, Math.max(0, value)) * 100);
}
