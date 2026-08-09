import { useQuery } from '@tanstack/react-query';
import { api } from '../../api/client';
import { healthWarnings } from './healthStatus';
import styles from './SystemHealthBanner.module.css';

export function SystemHealthBanner({ className }: { className?: string }) {
  const healthQuery = useQuery({
    queryKey: ['health'],
    queryFn: api.getHealth,
    refetchInterval: 10_000,
  });
  const statusUnknown = healthQuery.isError;
  const warnings = statusUnknown
    ? ['无法获取求解服务状态，当前状态未知，请检查服务连接。']
    : healthWarnings(healthQuery.data);
  if (!warnings.length) return null;
  return (
    <div className={`${styles.notice} ${className ?? ''}`.trim()} role="status" aria-live="polite">
      <strong>{statusUnknown ? '求解服务状态未知' : '求解服务不可用'}</strong>
      <span>{warnings.join(' ')}</span>
    </div>
  );
}
