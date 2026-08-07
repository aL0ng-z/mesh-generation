import type { SessionDetail } from '../../api/types';
import { isActiveRun } from '../runs/runTreeModel';

export type WorkspaceTab = 'viewer' | 'quality' | 'events' | 'artifacts';

export function parseWorkspaceTab(value: string | null): WorkspaceTab {
  return value === 'quality' || value === 'events' || value === 'artifacts' ? value : 'viewer';
}

export function pollingIntervalForSession(session?: SessionDetail): 3000 | false {
  return session?.runs.some(isActiveRun) ? 3000 : false;
}

export function isSessionFrozen(session?: Pick<SessionDetail, 'status'>): boolean {
  return session?.status === 'COMPLETED';
}

export function parseCompareIds(value: string | null, successfulIds: ReadonlySet<string>): [string, string] | undefined {
  const ids = value?.split(',') ?? [];
  if (ids.length !== 2 || ids[0] === ids[1] || !ids[0] || !ids[1]) return undefined;
  if (!successfulIds.has(ids[0]) || !successfulIds.has(ids[1])) return undefined;
  return [ids[0], ids[1]];
}
