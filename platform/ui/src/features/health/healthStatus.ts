import type { HealthSnapshot } from '../../api/types';

/** 将机器状态转换为普通用户可直接采取行动的提示。 */
export function healthWarnings(health?: HealthSnapshot): string[] {
  if (!health || health.status !== 'degraded') return [];
  const warnings: string[] = [];
  if (!health.worker.online) warnings.push('Worker 离线，排队任务暂时不会开始。');
  if (!health.igg.configured) warnings.push('IGG/AutoGrid 尚未配置，真实网格任务无法执行。');
  else if (!health.igg.available) warnings.push('IGG/AutoGrid 配置路径不可用，真实网格任务无法执行。');
  if (health.queue.queued > 0) warnings.push(`当前有 ${health.queue.queued} 个任务排队。`);
  return warnings.length ? warnings : ['计算服务处于降级状态，请联系运维检查。'];
}
