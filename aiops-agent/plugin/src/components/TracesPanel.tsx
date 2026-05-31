import React, { useMemo } from 'react';
import { css } from '@emotion/css';
import { GrafanaTheme2 } from '@grafana/data';
import { useStyles2 } from '@grafana/ui';
import {
  EmbeddedScene,
  SceneFlexItem,
  SceneFlexLayout,
  SceneQueryRunner,
  SceneTimeRange,
  VizPanel,
} from '@grafana/scenes';

type TracesPanelProps = {
  query: string;
  datasourceUid?: string;
  title?: string;
  from?: string;
  to?: string;
  height?: number;
  limit?: number;
};

// Renders a fenced ```traceql block as a live Tempo search. A TraceQL search
// returns a list of matching traces, so we use the `table` viz (Trace ID,
// service, duration); the Trace ID links into Grafana's trace view.
export function TracesPanel({
  query,
  datasourceUid = 'tempo',
  title,
  from = 'now-1h',
  to = 'now',
  height = 260,
  limit = 20,
}: TracesPanelProps) {
  const styles = useStyles2(getStyles);

  const scene = useMemo(
    () =>
      new EmbeddedScene({
        $timeRange: new SceneTimeRange({ from, to }),
        body: new SceneFlexLayout({
          direction: 'column',
          children: [
            new SceneFlexItem({
              minHeight: height,
              body: new VizPanel({
                pluginId: 'table',
                title: title ?? truncate(query, 60),
                $data: new SceneQueryRunner({
                  datasource: { uid: datasourceUid },
                  queries: [
                    { refId: 'A', queryType: 'traceql', query, tableType: 'traces', limit },
                  ],
                }),
              }),
            }),
          ],
        }),
      }),
    [query, datasourceUid, from, to, height, title, limit]
  );

  return (
    <div className={styles.wrapper} style={{ height }}>
      <scene.Component model={scene} />
    </div>
  );
}

function truncate(s: string, max: number): string {
  return s.length <= max ? s : s.slice(0, max - 1) + '…';
}

const getStyles = (theme: GrafanaTheme2) => ({
  wrapper: css`
    margin: ${theme.spacing(1)} 0;
    border: 1px solid ${theme.colors.border.weak};
    border-radius: ${theme.shape.radius.default};
    overflow: hidden;
  `,
});
