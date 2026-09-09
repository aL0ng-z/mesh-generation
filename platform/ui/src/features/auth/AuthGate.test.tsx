import { QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query';
import { cleanup, render, screen, fireEvent, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { ApiError, api } from '../../api/client';
import type { AuthSession } from '../../api/types';
import { queryClient } from '../../app/queryClient';
import { AuthGate } from './AuthGate';

const authorized: AuthSession = { enabled: true, authenticated: true, username: 'shared' };
const unauthenticated: AuthSession = { enabled: true, authenticated: false, username: null };
const disabled: AuthSession = { enabled: false, authenticated: true, username: null };

function withClient(node: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={client}>
      {node}
    </QueryClientProvider>
  );
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  queryClient.clear();
});

it('鉴权未启用时直接渲染应用内容', async () => {
  vi.spyOn(api, 'getAuthSession').mockResolvedValue(disabled);
  render(withClient(<AuthGate>应用内容</AuthGate>));
  expect(await screen.findByText('应用内容')).toBeInTheDocument();
});

it('已认证时渲染应用内容', async () => {
  vi.spyOn(api, 'getAuthSession').mockResolvedValue(authorized);
  render(withClient(<AuthGate>应用内容</AuthGate>));
  expect(await screen.findByText('应用内容')).toBeInTheDocument();
});

it('未认证时渲染登录页', async () => {
  vi.spyOn(api, 'getAuthSession').mockResolvedValue(unauthenticated);
  render(withClient(<AuthGate>应用内容</AuthGate>));
  expect(await screen.findByRole('heading', { name: '叶轮机械网格经验平台' })).toBeInTheDocument();
  expect(screen.queryByText('应用内容')).not.toBeInTheDocument();
});

it('登录成功后进入应用', async () => {
  let session = unauthenticated;
  vi.spyOn(api, 'getAuthSession').mockImplementation(() => Promise.resolve(session));
  const loginSpy = vi.spyOn(api, 'login').mockImplementation(() => {
    session = authorized;
    return Promise.resolve(authorized);
  });
  render(withClient(<AuthGate>应用内容</AuthGate>));
  const heading = await screen.findByRole('heading', { name: '叶轮机械网格经验平台' });
  expect(heading).toBeInTheDocument();

  fireEvent.change(screen.getByLabelText('用户名'), { target: { value: 'shared' } });
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'secret' } });
  fireEvent.click(screen.getByRole('button', { name: '登录' }));

  await waitFor(() => {
    expect(loginSpy).toHaveBeenCalledWith('shared', 'secret');
  });
  await waitFor(() => {
    expect(screen.getByText('应用内容')).toBeInTheDocument();
  });
});

it('登录失败展示错误提示', async () => {
  vi.spyOn(api, 'getAuthSession').mockResolvedValue(unauthenticated);
  vi.spyOn(api, 'login').mockRejectedValue(new Error('用户名或密码错误'));
  render(withClient(<AuthGate>应用内容</AuthGate>));
  await screen.findByRole('heading', { name: '叶轮机械网格经验平台' });

  fireEvent.change(screen.getByLabelText('用户名'), { target: { value: 'shared' } });
  fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'wrong' } });
  fireEvent.click(screen.getByRole('button', { name: '登录' }));

  expect(await screen.findByText('用户名或密码错误')).toBeInTheDocument();
});

it('401 响应使会话查询失效并回落到登录页', async () => {
  // 会话最初已认证；一旦数据请求被服务端以 401 拒绝（如会话过期），
  // 后续会话查询也应返回未认证，AuthGate 回落到登录页。
  let session = authorized;
  vi.spyOn(api, 'getAuthSession').mockImplementation(() => Promise.resolve(session));
  const listSessions = vi
    .spyOn(api, 'listSessions')
    .mockImplementationOnce(() => {
      session = unauthenticated;
      return Promise.reject(
        Object.assign(new ApiError(401, { error: { code: 'UNAUTHORIZED' } }), { status: 401 }),
      );
    });

  // 使用真实 queryClient（带 QueryCache.onError 401 失效逻辑），
  // 让数据查询真实失败并走 onError 路径，而不是手工 invalidate。
  queryClient.clear();
  const DataProbe = () => {
    const query = useQuery({
      queryKey: ['probe-sessions'],
      queryFn: () => api.listSessions(),
      retry: false,
    });
    return query.isError ? <p role="alert">数据加载失败</p> : null;
  };

  render(
    <QueryClientProvider client={queryClient}>
      <AuthGate>
        <DataProbe />
      </AuthGate>
    </QueryClientProvider>,
  );
  expect(await screen.findByRole('alert')).toHaveTextContent('数据加载失败');

  // 数据查询的 401 已触发 QueryCache.onError → 会话查询失效并重取，
  // 重取得到未认证 → AuthGate 回落到登录页。
  await waitFor(() => {
    expect(screen.getByRole('heading', { name: '叶轮机械网格经验平台' })).toBeInTheDocument();
  });
  expect(listSessions).toHaveBeenCalledTimes(1);
});
