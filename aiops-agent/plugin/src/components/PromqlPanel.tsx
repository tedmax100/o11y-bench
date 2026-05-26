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

type PromqlPanelProps = {
  expr: string;
  datasourceUid?: string;
  title?: string;
  from?: string;
  to?: string;
  height?: number;
};

export function PromqlPanel({
  expr,
  datasourceUid = 'prometheus',
  title,
  from = 'now-1h',
  to = 'now',
  height = 220,
}: PromqlPanelProps) {
  const styles = useStyles2(getStyles);

  // Scenes is stateful — keep one EmbeddedScene per (expr, range) pair.
  // Re-creating on every render would lose query results and re-fire requests.
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
                pluginId: 'timeseries',
                title: title ?? truncate(expr, 60),
                $data: new SceneQueryRunner({
                  datasource: { uid: datasourceUid },
                  queries: [{ refId: 'A', expr }],
                }),
              }),
            }),
          ],
        }),
      }),
    [expr, datasourceUid, from, to, height, title]
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
