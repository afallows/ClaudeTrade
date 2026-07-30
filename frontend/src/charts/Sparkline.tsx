import { useMemo } from 'react';
import type { Data, Layout } from 'plotly.js-dist-min';
import Plot from './Plot';

const LONG_COLOR = '#3987e5';
const SHORT_COLOR = '#e66767';

function hexToRgba(hex: string, alpha: number): string {
  const r = parseInt(hex.slice(1, 3), 16);
  const g = parseInt(hex.slice(3, 5), 16);
  const b = parseInt(hex.slice(5, 7), 16);
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

/** Minimal, axis-free line -- a compact equity trend for a dashboard tile.
 * Mirrors ui/charts.py's create_sparkline. */
export function Sparkline({
  sessions,
  values,
  height = 90,
}: {
  sessions: string[];
  values: number[];
  height?: number;
}) {
  const { data, layout } = useMemo(() => {
    if (sessions.length === 0 || values.length === 0) {
      return { data: [] as Data[], layout: { height } as Partial<Layout> };
    }
    const color = values[values.length - 1] >= values[0] ? LONG_COLOR : SHORT_COLOR;
    const trace: Data = {
      type: 'scatter',
      mode: 'lines',
      x: sessions,
      y: values,
      line: { color, width: 2 },
      fill: 'tozeroy',
      fillcolor: hexToRgba(color, 0.15),
      hoverinfo: 'skip',
    } as Data;
    const layoutOut: Partial<Layout> = {
      paper_bgcolor: 'rgba(0,0,0,0)',
      plot_bgcolor: 'rgba(0,0,0,0)',
      height,
      margin: { l: 0, r: 0, t: 4, b: 4 },
      showlegend: false,
      xaxis: { visible: false },
      yaxis: { visible: false },
    };
    return { data: [trace], layout: layoutOut };
  }, [sessions, values, height]);

  if (data.length === 0) return null;

  return (
    <Plot
      data={data}
      layout={layout}
      config={{ displayModeBar: false, staticPlot: true }}
      style={{ width: '100%', height: `${height}px` }}
      useResizeHandler
    />
  );
}
