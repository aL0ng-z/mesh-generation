import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api } from '../../api/client';
import { AUTH_SESSION_QUERY_KEY } from '../../app/queryClient';
import { LoginPage } from './LoginPage';

interface AuthGateProps {
  children: React.ReactNode;
}

export function AuthGate({ children }: AuthGateProps) {
  const [opened, setOpened] = useState(false);
  const sessionQuery = useQuery({
    queryKey: AUTH_SESSION_QUERY_KEY,
    queryFn: () => api.getAuthSession(),
    staleTime: 60_000,
    retry: false,
  });

  const session = sessionQuery.data;
  const accessible = !sessionQuery.isPending
    && (sessionQuery.isError || !session || !session.enabled || session.authenticated);
  if (accessible && !opened) setOpened(true);

  return (
    <>
      {(opened || accessible) ? <div hidden={!accessible} inert={!accessible}>{children}</div> : null}
      {sessionQuery.isPending ? <main className="auth-loading" role="status">正在确认登录状态…</main> : null}
      {!sessionQuery.isPending && !accessible ? (
        <LoginPage onSuccess={() => { void sessionQuery.refetch(); }} />
      ) : null}
    </>
  );
}
