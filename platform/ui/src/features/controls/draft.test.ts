import { describe, expect, it } from 'vitest';
import { clearDraftValue, findDraftChange, removeDraftChange, setDraftValue } from './draft';

describe('控制草稿', () => {
  it('同一精确目标只保留最后一次 set', () => {
    const once = setDraftValue([], 'blade.count', '#2', 31);
    const twice = setDraftValue(once, 'blade.count', '#2', 33);
    expect(twice).toEqual([{ key: 'blade.count', selector: '#2', op: 'set', value: 33 }]);
  });

  it('clear 替代同目标 set，且可以撤销草稿', () => {
    const set = setDraftValue([], 'topology.mode', '#1', 'H');
    const cleared = clearDraftValue(set, 'topology.mode', '#1');
    expect(findDraftChange(cleared, 'topology.mode', '#1')?.op).toBe('clear');
    expect(removeDraftChange(cleared, 'topology.mode', '#1')).toEqual([]);
  });

  it('不同目标互不覆盖', () => {
    const first = setDraftValue([], 'blade.count', '#1', 31);
    expect(setDraftValue(first, 'blade.count', '#2', 33)).toHaveLength(2);
  });
});
