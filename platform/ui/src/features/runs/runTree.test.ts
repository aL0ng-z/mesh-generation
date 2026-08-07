import { describe, expect, it } from 'vitest';
import type { RunSummary } from '../../api/types';
import { buildRunForest, latestRunId } from './runTreeModel';

function run(id: string, sequence: number, parent?: string, retry?: string): RunSummary {
  return {
    id,
    sequence,
    session_id: 'session-1',
    parent_run_id: parent,
    retry_of_run_id: retry,
    status: 'SUCCEEDED',
    quality_status: 'PASS',
    created_at: '2026-08-06T00:00:00Z',
  };
}

describe('buildRunForest', () => {
  it('按序号建立分支与重试节点且不修改输入顺序', () => {
    const input = [run('retry', 4, 'base', 'failed'), run('base', 1), run('failed', 3, 'base'), run('child', 2, 'base')];
    const idsBefore = input.map((item) => item.id);
    const forest = buildRunForest(input);

    expect(input.map((item) => item.id)).toEqual(idsBefore);
    expect(forest).toHaveLength(1);
    expect(forest[0]?.run.id).toBe('base');
    expect(forest[0]?.children.map((node) => node.run.id)).toEqual(['child', 'failed', 'retry']);
  });

  it('保留父节点缺失的孤立节点', () => {
    expect(buildRunForest([run('orphan', 5, 'missing')])[0]?.run.id).toBe('orphan');
  });
});

it('选取最新运行', () => {
  expect(latestRunId([run('one', 1), run('three', 3), run('two', 2)])).toBe('three');
});
