import { QueryClient } from '@tanstack/react-query';

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 5_000,
      retry(failureCount, error) {
        const status = (error as { status?: number }).status;
        return failureCount < 2 && status !== 404 && status !== 409;
      },
      refetchOnWindowFocus: false,
    },
    mutations: { retry: false },
  },
});
