import pluginJson from './plugin.json';

export const PLUGIN_BASE_URL = `/a/${pluginJson.id}`;

export enum ROUTES {
  Chat = 'chat',
}

export const DEFAULT_AGENT_SERVICE_URL = 'http://localhost:8000';
