import React from 'react';
import { Route, Routes, Navigate } from 'react-router-dom';
import { AppRootProps } from '@grafana/data';
import { ROUTES, DEFAULT_AGENT_SERVICE_URL } from '../../constants';
import type { AppPluginSettings } from '../AppConfig/AppConfig';

const ChatPage = React.lazy(() => import('../../pages/ChatPage'));
const TraceExplorerPage = React.lazy(() => import('../../pages/TraceExplorerPage'));

function App(props: AppRootProps<AppPluginSettings>) {
  const agentServiceUrl = props.meta.jsonData?.agentServiceUrl || DEFAULT_AGENT_SERVICE_URL;
  return (
    <Routes>
      <Route path={ROUTES.Chat} element={<ChatPage agentServiceUrl={agentServiceUrl} />} />
      <Route path={ROUTES.Traces} element={<TraceExplorerPage agentServiceUrl={agentServiceUrl} />} />
      <Route path="*" element={<Navigate to={ROUTES.Chat} replace />} />
    </Routes>
  );
}

export default App;
