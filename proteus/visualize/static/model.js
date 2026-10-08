// Pure presentation helpers: null never becomes zero, missing points break a line.
export const available = (value) => typeof value === 'number' && Number.isFinite(value);
export const escapeHtml = (value) => String(value ?? '').replace(/[&<>"']/g,
  (char) => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[char]));
export function format(value, digits = 2) {
  return available(value) ? new Intl.NumberFormat('en-US', {maximumFractionDigits:digits}).format(value) : '—';
}
export function extent(values) {
  const valid = values.filter(available);
  if (!valid.length) return [0, 1];
  let lower = Math.min(0, ...valid), upper = Math.max(...valid);
  if (lower === upper) upper = lower + 1;
  return [lower, upper];
}
export function seriesPath(entries, value, x, y) {
  let connected = false;
  return entries.map((entry) => {
    const v = value(entry), horizontal = x(entry);
    if (!available(v) || !available(horizontal)) { connected = false; return ''; }
    const command = connected ? 'L' : 'M'; connected = true;
    return `${command}${horizontal.toFixed(2)},${y(v).toFixed(2)}`;
  }).join(' ');
}
export function score(entry, evaluator) {
  const result = entry.evaluations?.[evaluator];
  return result?.status === 'ok' && available(result.score) ? result.score : null;
}
export function metricValue(entry, metric, evaluator) {
  return metric === 'score' ? score(entry, evaluator) : entry[metric];
}
export function axisValue(entry, axis) {
  return axis === 'episode' ? entry.index : entry[axis];
}
export function selectedEpisode(run, index) {
  return run.episodes.find((ep) => ep.index === index) ?? run.episodes.at(-1);
}
export function phaseShare(values) {
  if (!values.every(available)) return null;
  const total = values.reduce((a, b) => a + b, 0);
  return values.map((v) => total ? v / total : 0);
}
