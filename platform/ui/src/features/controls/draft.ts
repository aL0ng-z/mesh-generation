import type { ControlChange } from '../../api/types';

function sameTarget(left: ControlChange, right: Pick<ControlChange, 'key' | 'selector'>) {
  return left.key === right.key && left.selector === right.selector;
}

export function setDraftValue(
  draft: readonly ControlChange[],
  key: string,
  selector: string,
  value: unknown,
): ControlChange[] {
  return [
    ...draft.filter((change) => !sameTarget(change, { key, selector })),
    { key, selector, op: 'set', value },
  ];
}

export function clearDraftValue(
  draft: readonly ControlChange[],
  key: string,
  selector: string,
): ControlChange[] {
  return [
    ...draft.filter((change) => !sameTarget(change, { key, selector })),
    { key, selector, op: 'clear' },
  ];
}

export function removeDraftChange(
  draft: readonly ControlChange[],
  key: string,
  selector: string,
): ControlChange[] {
  return draft.filter((change) => !sameTarget(change, { key, selector }));
}

export function findDraftChange(
  draft: readonly ControlChange[],
  key: string,
  selector: string,
): ControlChange | undefined {
  return draft.find((change) => sameTarget(change, { key, selector }));
}
