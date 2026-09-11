import { describe, expect, it } from 'vitest';
import {
  NOT_AN_INTEGER_MESSAGE,
  parseNumericText,
  UNSAFE_INTEGER_MESSAGE,
} from './numericInput';

describe('数值输入解析', () => {
  it('整数支持科学计数法与显式正号', () => {
    expect(parseNumericText('1e3', true)).toEqual({ status: 'ok', value: 1000 });
    expect(parseNumericText('+10', true)).toEqual({ status: 'ok', value: 10 });
    expect(parseNumericText('-10', true)).toEqual({ status: 'ok', value: -10 });
    expect(parseNumericText('1E3', true)).toEqual({ status: 'ok', value: 1000 });
    expect(parseNumericText('1e+3', true)).toEqual({ status: 'ok', value: 1000 });
  });

  it('安全整数边界：2^53-1 通过，2^53 报错', () => {
    expect(parseNumericText('9007199254740991', true)).toEqual({ status: 'ok', value: 9007199254740991 });
    expect(parseNumericText('-9007199254740991', true)).toEqual({ status: 'ok', value: -9007199254740991 });
    expect(parseNumericText('9007199254740992', true)).toEqual({
      status: 'invalid',
      message: UNSAFE_INTEGER_MESSAGE,
    });
    // 2^53+1 会被 Number 舍入到 2^53，同样必须报错而不是静默提交。
    expect(parseNumericText('9007199254740993', true)).toEqual({
      status: 'invalid',
      message: UNSAFE_INTEGER_MESSAGE,
    });
  });

  it('整数拒绝小数与指数溢出，不静默截断', () => {
    expect(parseNumericText('1.9', true)).toEqual({
      status: 'invalid',
      message: NOT_AN_INTEGER_MESSAGE,
    });
    expect(parseNumericText('1e309', true)).toEqual({
      status: 'invalid',
      message: '数值超出可表示范围',
    });
  });

  it('number 类型允许小数与科学计数法，仍拒绝非有限值', () => {
    expect(parseNumericText('1.9', false)).toEqual({ status: 'ok', value: 1.9 });
    expect(parseNumericText('1.5e-3', false)).toEqual({ status: 'ok', value: 0.0015 });
    expect(parseNumericText('1e309', false)).toEqual({
      status: 'invalid',
      message: '数值超出可表示范围',
    });
  });

  it('空串与纯空白属于未完成输入', () => {
    for (const raw of ['', '   ', '\t']) {
      expect(parseNumericText(raw, true).status).toBe('incomplete');
      expect(parseNumericText(raw, false).status).toBe('incomplete');
    }
  });

  it('纯符号与未完成指数属于未完成输入', () => {
    for (const raw of ['+', '-', '.', '+.', '1e', '1e+', '1e-', '1.e', '1.5e']) {
      expect(parseNumericText(raw, true).status).toBe('incomplete');
      expect(parseNumericText(raw, false).status).toBe('incomplete');
    }
  });

  it('无法解析的文本明确报错', () => {
    for (const raw of ['abc', '1,000', '0x10', '1 2', '1.5.5', '1e3x', 'NaN', 'Infinity']) {
      expect(parseNumericText(raw, true)).toEqual({
        status: 'invalid',
        message: '无法解析为十进制数值',
      });
    }
  });

  it('首位与末位空白可接受，其余空白不接受', () => {
    expect(parseNumericText(' 1e3 ', true)).toEqual({ status: 'ok', value: 1000 });
    expect(parseNumericText('1 0', true).status).toBe('invalid');
  });
});
