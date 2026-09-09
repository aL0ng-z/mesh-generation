import { QueryCache, QueryClient } from '@tanstack/react-query';
import { ApiError } from '../api/client';

export const AUTH_SESSION_QUERY_KEY = ['auth-session'] as const;

export const queryClient = new QueryClient({
  queryCache: new QueryCache({
    onError(error) {
      // 会话过期或 Cookie 被清除后，任何数据请求 401 都触发会话查询重取，
      // AuthGate 全局回落到登录页。
      if (error instanceof ApiError && error.status === 401) {
        void queryClient.invalidateQueries({ queryKey: AUTH_SESSION_QUERY_KEY });
      }
    },
  }),
  defaultOptions: {
    queries: {
      staleTime: 5_000,
      retry(failureCount, error) {
        const status = (error as { status?: number }).status;
        return failureCount < 2 && status !== 401 && status !== 404 && status !== 409;
      },
      refetchOnWindowFocus: false,
    },
    mutations: { retry: false },
  },
});
