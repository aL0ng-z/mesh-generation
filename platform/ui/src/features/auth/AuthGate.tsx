import { useQuery } from '@tanstack/react-query';
import { api } from '../../api/client';
import { AUTH_SESSION_QUERY_KEY } from '../../app/queryClient';
import { LoginPage } from './LoginPage';

interface AuthGateProps {
  children: React.ReactNode;
}

export function AuthGate({ children }: AuthGateProps) {
  const sessionQuery = useQuery({
    queryKey: AUTH_SESSION_QUERY_KEY,
    queryFn: () => api.getAuthSession(),
    staleTime: 60_000,
    retry: false,
  });

  if (sessionQuery.isPending) {
    return (
      <main className="auth-loading" role="status">
        正在确认登录状态…
      </main>
    );
  }

  const session = sessionQuery.data;
  if (sessionQuery.isError || !session || !session.enabled || session.authenticated) {
    // 鉴权未配置、已认证，或会话查询本身失败（如网络不可达）时放行，
    // 让应用自身的数据请求与错误提示接管。
    return <>{children}</>;
  }

  return (
    <LoginPage
      onSuccess={() => {
        void sessionQuery.refetch();
      }}
    />
  );
}
