import { createBrowserRouter } from 'react-router-dom';
import { SessionListPage } from '../features/sessions/SessionListPage';
import { SessionWorkspacePage } from '../features/workspace/SessionWorkspacePage';

export const router = createBrowserRouter([
  { path: '/', element: <SessionListPage /> },
  { path: '/sessions/:id', element: <SessionWorkspacePage /> },
]);
