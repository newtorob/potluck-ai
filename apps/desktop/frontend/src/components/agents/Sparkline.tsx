/**
 * Sparkline — hand-rolled SVG sparkline for tool call activity.
 *
 * Renders up to N=20 values as a filled path. Pads with zeros if fewer.
 * No tooltips — pure visual indicator.
 * Redraws on values change; no animation.
 */

interface SparklineProps {
  values: number[];
  width?: number;
  height?: number;
  color?: string;
}

const N = 20;

export function Sparkline({ values, width = 80, height = 28, color = 'currentColor' }: SparklineProps) {
  // Normalise to exactly N entries, padded with zeros on the left
  const padded: number[] = Array.from({ length: N }, (_, i) => {
    const offset = N - values.length;
    const srcIdx = i - offset;
    return srcIdx >= 0 && srcIdx < values.length ? (values[srcIdx] ?? 0) : 0;
  });

  const max = Math.max(...padded, 1); // avoid division by zero

  // Map each value to an (x, y) coordinate
  const step = width / (N - 1);
  const points = padded.map((v, i) => ({
    x: i * step,
    y: height - (v / max) * (height - 2) - 1, // 1px bottom padding
  }));

  // Build SVG path: line through all points
  const linePath = points
    .map((p, i) => `${i === 0 ? 'M' : 'L'}${p.x.toFixed(1)},${p.y.toFixed(1)}`)
    .join(' ');

  // Close into a filled area (bottom)
  const lastPoint = points[points.length - 1];
  const firstPoint = points[0];
  const areaPath =
    lastPoint !== undefined && firstPoint !== undefined
      ? `${linePath} L${lastPoint.x.toFixed(1)},${height} L${firstPoint.x.toFixed(1)},${height} Z`
      : '';

  const allZero = padded.every((v) => v === 0);

  return (
    <svg
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      aria-hidden="true"
      className="overflow-visible"
    >
      {allZero ? (
        // Flat baseline when no data
        <line
          x1={0}
          y1={height - 1}
          x2={width}
          y2={height - 1}
          stroke={color}
          strokeWidth={1}
          strokeOpacity={0.3}
        />
      ) : (
        <>
          {/* Fill area */}
          <path d={areaPath} fill={color} fillOpacity={0.12} />
          {/* Line */}
          <path d={linePath} fill="none" stroke={color} strokeWidth={1.5} strokeLinejoin="round" strokeLinecap="round" />
        </>
      )}
    </svg>
  );
}
