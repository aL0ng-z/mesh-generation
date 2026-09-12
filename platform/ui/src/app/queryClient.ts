import { MutationCache, QueryCache, QueryClient } from '@tanstack/react-query';
import { ApiError } from '../api/client';

export const AUTH_SESSION_QUERY_KEY = ['auth-session'] as const;

function handleAuthenticationError(error: Error) {
  if (error instanceof ApiError && error.status === 401) {
    void queryClient.invalidateQueries({ queryKey: AUTH_SESSION_QUERY_KEY });
  }
}

export const queryClient = new QueryClient({
  queryCache: new QueryCache({
    onError: handleAuthenticationError,
  }),
  mutationCache: new MutationCache({ onError: handleAuthenticationError }),
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
