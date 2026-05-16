import React, { ChangeEvent, useState } from 'react';
import { lastValueFrom } from 'rxjs';
import { css } from '@emotion/css';
import { AppPluginMeta, GrafanaTheme2, PluginConfigPageProps, PluginMeta } from '@grafana/data';
import { getBackendSrv } from '@grafana/runtime';
import { Button, Field, FieldSet, Input, useStyles2 } from '@grafana/ui';
import { testIds } from '../testIds';
import { DEFAULT_AGENT_SERVICE_URL } from '../../constants';

export type AppPluginSettings = {
  agentServiceUrl?: string;
};

type State = {
  agentServiceUrl: string;
};

export interface AppConfigProps extends PluginConfigPageProps<AppPluginMeta<AppPluginSettings>> {}

const AppConfig = ({ plugin }: AppConfigProps) => {
  const s = useStyles2(getStyles);
  const { enabled, pinned, jsonData } = plugin.meta;
  const [state, setState] = useState<State>({
    agentServiceUrl: jsonData?.agentServiceUrl || '',
  });

  const isSubmitDisabled = !state.agentServiceUrl;

  const onChange = (event: ChangeEvent<HTMLInputElement>) => {
    setState({
      ...state,
      [event.target.name]: event.target.value.trim(),
    });
  };

  const onSubmit = (event: React.FormEvent) => {
    event.preventDefault();
    if (isSubmitDisabled) {
      return;
    }
    updatePluginAndReload(plugin.meta.id, {
      enabled,
      pinned,
      jsonData: { agentServiceUrl: state.agentServiceUrl },
    });
  };

  return (
    <form onSubmit={onSubmit}>
      <FieldSet label="Agent service">
        <Field
          label="Agent Service URL"
          description="Base URL of the LangGraph agent backend (e.g. http://localhost:8000)."
          className={s.marginTop}
        >
          <Input
            width={60}
            name="agentServiceUrl"
            id="config-agent-service-url"
            data-testid={testIds.appConfig.agentServiceUrl}
            value={state.agentServiceUrl}
            placeholder={DEFAULT_AGENT_SERVICE_URL}
            onChange={onChange}
          />
        </Field>

        <div className={s.marginTop}>
          <Button type="submit" data-testid={testIds.appConfig.submit} disabled={isSubmitDisabled}>
            Save settings
          </Button>
        </div>
      </FieldSet>
    </form>
  );
};

export default AppConfig;

const getStyles = (theme: GrafanaTheme2) => ({
  marginTop: css`
    margin-top: ${theme.spacing(3)};
  `,
});

const updatePluginAndReload = async (pluginId: string, data: Partial<PluginMeta<AppPluginSettings>>) => {
  try {
    await updatePlugin(pluginId, data);
    window.location.reload();
  } catch (e) {
    console.error('Error while updating the plugin', e);
  }
};

const updatePlugin = async (pluginId: string, data: Partial<PluginMeta>) => {
  const response = await getBackendSrv().fetch({
    url: `/api/plugins/${pluginId}/settings`,
    method: 'POST',
    data,
  });
  return lastValueFrom(response);
};
