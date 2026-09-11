// 数值输入的本地解析：编辑阶段保留原始字符串，
// 预检与提交前才把十进制文本完整解析成数值。

export type NumericParseResult =
  | { status: 'ok'; value: number }
  | { status: 'incomplete'; message: string }
  | { status: 'invalid'; message: string };

// 完整十进制数值：可选符号、整数或小数部分（至少一位数字）、可选指数（1e3、+10）。
const DECIMAL_PATTERN = /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/;
// 完整数值的前缀（继续输入可以补全），用于区分「未完成输入」与「无法解析的文本」。
const PARTIAL_PATTERN = /^(?:[+-]?(?:\d+(?:\.\d*)?|\.\d*)(?:[eE][+-]?\d*)?|[+-]?)$/;

const INCOMPLETE_MESSAGE = '输入未完成，请输入完整的数值';
export const UNSAFE_INTEGER_MESSAGE = '超出安全整数范围（-9007199254740991 ~ 9007199254740991）';
export const NOT_AN_INTEGER_MESSAGE = '必须是整数，不接受小数';

export function parseNumericText(raw: string, integer: boolean): NumericParseResult {
  const trimmed = raw.trim();
  if (!trimmed) {
    return { status: 'incomplete', message: INCOMPLETE_MESSAGE };
  }
  if (!DECIMAL_PATTERN.test(trimmed)) {
    if (PARTIAL_PATTERN.test(trimmed)) {
      return { status: 'incomplete', message: INCOMPLETE_MESSAGE };
    }
    return { status: 'invalid', message: '无法解析为十进制数值' };
  }
  const value = Number(trimmed);
  if (!Number.isFinite(value)) {
    return { status: 'invalid', message: '数值超出可表示范围' };
  }
  if (integer && !Number.isSafeInteger(value)) {
    if (!Number.isInteger(value)) {
      return { status: 'invalid', message: NOT_AN_INTEGER_MESSAGE };
    }
    return { status: 'invalid', message: UNSAFE_INTEGER_MESSAGE };
  }
  return { status: 'ok', value };
}
