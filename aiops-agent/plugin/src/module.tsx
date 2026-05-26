import React, { Suspense, lazy } from 'react';
import { AppPlugin, type AppRootProps } from '@grafana/data';
import { initPluginTranslations } from '@grafana/i18n';
import { LoadingPlaceholder } from '@grafana/ui';
import type { AppConfigProps, AppPluginSettings } from './components/AppConfig/AppConfig';
import pluginJson from './plugin.json';

// @grafana/scenes internally calls t() from @grafana/i18n. Without a plugin
// translation init here, the first render of any Scenes component throws
// "t() was called before i18n was initialized". No translations of our own
// to load — passing an empty loader list is enough to flip the init flag.
void initPluginTranslations(pluginJson.id, []);

const LazyApp = lazy(() => import('./components/App/App'));
const LazyAppConfig = lazy(() => import('./components/AppConfig/AppConfig'));

const App = (props: AppRootProps<AppPluginSettings>) => (
  <Suspense fallback={<LoadingPlaceholder text="" />}>
    <LazyApp {...props} />
  </Suspense>
);

const AppConfig = (props: AppConfigProps) => (
  <Suspense fallback={<LoadingPlaceholder text="" />}>
    <LazyAppConfig {...props} />
  </Suspense>
);

export const plugin = new AppPlugin<AppPluginSettings>().setRootPage(App).addConfigPage({
  title: 'Configuration',
  icon: 'cog',
  body: AppConfig,
  id: 'configuration',
});
