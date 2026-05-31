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

type LogsPanelProps = {
  expr: string;
  datasourceUid?: string;
  title?: string;
  from?: string;
  to?: string;
  height?: number;
  maxLines?: number;
};

// Renders a fenced ```logql block from the assistant as a live Loki logs panel,
// mirroring PromqlPanel. Uses the built-in `logs` viz so the user sees actual
// log lines (with level colouring) instead of monospace text.
export function LogsPanel({
  expr,
  datasourceUid = 'loki',
  title,
  from = 'now-1h',
  to = 'now',
  height = 260,
  maxLines = 100,
}: LogsPanelProps) {
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
                pluginId: 'logs',
                title: title ?? truncate(expr, 60),
                $data: new SceneQueryRunner({
                  datasource: { uid: datasourceUid },
                  queries: [{ refId: 'A', expr, queryType: 'range', maxLines }],
                }),
              }),
            }),
          ],
        }),
      }),
    [expr, datasourceUid, from, to, height, title, maxLines]
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
