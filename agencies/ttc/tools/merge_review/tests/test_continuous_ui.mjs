/** Pure DOM-card checks: saved evidence only, English-only UI, no scorer execution. */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import test from 'node:test';
import vm from 'node:vm';

const staticRoot = resolve(import.meta.dirname, '../static');
const source = readFileSync(resolve(staticRoot, 'app.js'), 'utf8');
const helper = source.slice(source.indexOf('  function appendGTFSContinuousEvidence('), source.indexOf('  function initGTFS()'));
assert.ok(helper.includes('function appendGTFSContinuousEvidence'));
class Element {
  constructor(tag, className = '', text = '') { this.tag = tag; this.className = className; this.ownText = text; this.children = []; this.dataset = {}; this.attributes = {}; }
  append(...children) { this.children.push(...children); }
  setAttribute(key, value) { this.attributes[key] = value; }
  get textContent() { return this.ownText + this.children.map(child => child.textContent).join(' '); }
}
const flatten = node => [node, ...node.children.flatMap(flatten)];
const keys = ['absolute_delay_level_cost', 'conditioned_fit_cost', 'delay_dynamics_cost', 'APC_stop_time_cost', 'projection_and_coverage_cost', 'pattern_progress_cost', 'vehicle_sequence_cost'];
function candidate() {
  return { scorer: 'continuous_passage_v1', score_components: Object.fromEntries(keys.map((key, index) => [key, index / 10])), score_components_unweighted: Object.fromEntries(keys.map(key => [key, 2])), score_component_weights: Object.fromEntries(keys.map(key => [key, 0.5])), coverage_metrics: { projection: { point_ratio: 0.9, duration_ratio: 0.8 }, schedule: { point_ratio: 0.7, duration_ratio: 0.6 }, end_to_end: { point_ratio: 0.63, duration_ratio: 0.48 } }, estimated_delay_s: 60, delay_uncertainty_s: 30, delay_prior_source: 'local_scheduled_headway', headway_summary_s: { min: 300, max: 600 }, apc_support: { supported_stops: 3 }, continuous_support_windows: 4 };
}
function runtime() {
  const context = vm.createContext({ node: (tag, className, text) => new Element(tag, className, text), finite: value => typeof value === 'number' && Number.isFinite(value), fmt: value => value == null ? '—' : String(value), gtfsPrecise: value => value == null ? 'Unknown' : String(value), arr: value => Array.isArray(value) ? value : [], localStorage: { getItem: () => 'en', setItem() {} } });
  context.window = context;
  vm.runInContext(helper, context);
  return context;
}

test('continuous card renders all seven persisted raw/weight/contribution rows', () => {
  const context = runtime(), input = candidate(), before = structuredClone(input), card = new Element('div');
  context.appendGTFSContinuousEvidence(card, input);
  const rows = flatten(card).filter(node => node.dataset.component);
  assert.deepEqual(rows.map(node => node.dataset.component), keys);
  for (let index = 0; index < rows.length; index++) assert.equal(rows[index].children[3].ownText, String(index / 10));
  assert.deepEqual(input, before);
  assert.equal(card.dataset.scorer, 'continuous_passage_v1');
  assert.ok(!flatten(card).some(node => node.className === 'gtfs-delay-penalty'));
});
test('three coverage tiers retain distinct point and duration denominators', () => {
  const context = runtime(), card = new Element('div'); context.appendGTFSContinuousEvidence(card, candidate());
  const rows = flatten(card).filter(node => node.dataset.coverage);
  assert.deepEqual(rows.map(node => node.dataset.coverage), ['projection', 'schedule', 'end_to_end']);
  assert.equal(rows[0].children[1].ownText, '90%');
  assert.equal(rows[1].children[1].ownText, '70%');
  assert.equal(rows[2].children[1].ownText, '63%');
  assert.equal(rows[2].children[2].ownText, '48%');
});
test('every continuous card label, tooltip and accessibility label is English', () => {
  const context = runtime(), card = new Element('div'); context.appendGTFSContinuousEvidence(card, candidate());
  for (const node of flatten(card)) {
    for (const text of [node.ownText, node.title, node.attributes['aria-label']].filter(Boolean)) {
      assert.ok(!/[\u3400-\u9fff]/.test(text), text);
    }
  }
});
test('continuous missing evidence renders unknown rather than invented zeros', () => {
  const context = runtime(), card = new Element('div'); context.appendGTFSContinuousEvidence(card, { scorer: 'continuous_passage_v1' });
  assert.equal(flatten(card).filter(node => node.dataset.component).length, 7);
  assert.equal(flatten(card).find(node => node.dataset.component).children[1].ownText, 'Unknown');
  assert.equal(flatten(card).find(node => node.dataset.coverage).children[1].ownText, 'Unknown');
});
test('gating labels and the parameter warning are English and keep their IDs', () => {
  assert.ok(!/[\u3400-\u9fff]/.test(source), 'app.js must contain no CJK text');
  assert.ok(source.includes('Legacy DTW and hourly time-shift parameters apply only to the old scorer.'));
  assert.ok(source.includes('independently calibrated credible set'));
  assert.ok(source.includes('if (!continuousCandidate && finite(c.estimated_delay_s) && finite(data.config?.score_delay_prior_weight))'));
  assert.ok(source.includes('const MATCHING_MODES = { global:'));
});

test('04c renders a separate displacement component and English occurrence diagnostics', () => {
  const context = runtime(), input = candidate(), card = new Element('div');
  input.occurrence_model_enabled = true;
  input.score_components.occurrence_displacement_cost = 2;
  input.score_components_unweighted.occurrence_displacement_cost = 2;
  input.score_component_weights.occurrence_displacement_cost = 1;
  input.occurrence_displacement = { crossed_occurrences: 2, outside_cell_s: 430, context: { previous_headway_s: 610, next_headway_s: 720, headway_source: 'local_confusability_class', headway_confidence: 0.9 } };
  input.occurrence_decision_guard = { matched_allowed: false, reason: 'DISPLACED_OCCURRENCE_WITHOUT_INDEPENDENT_SUPPORT' };
  const before = structuredClone(input);
  context.appendGTFSContinuousEvidence(card, input);
  assert.equal(flatten(card).filter(node => node.dataset.component).length, 8);
  assert.ok(card.textContent.includes('610'));
  assert.ok(card.textContent.includes('720'));
  for (const node of flatten(card)) for (const text of [node.ownText, node.title, node.attributes['aria-label']].filter(Boolean)) assert.ok(!/[\u3400-\u9fff]/.test(text), text);
  assert.deepEqual(input, before);
});
