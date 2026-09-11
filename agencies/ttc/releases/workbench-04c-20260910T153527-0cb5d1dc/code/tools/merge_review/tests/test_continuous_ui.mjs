/** Pure DOM-card checks: saved evidence only, bilingual, no scorer execution. */
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
  const context = vm.createContext({ node: (tag, className, text) => new Element(tag, className, text), finite: value => typeof value === 'number' && Number.isFinite(value), fmt: value => value == null ? '—' : String(value), gtfsPrecise: value => value == null ? '未知' : String(value), arr: value => Array.isArray(value) ? value : [], localStorage: { getItem: () => 'en', setItem() {} } });
  context.window = context;
  for (const file of ['i18n-static.js', 'i18n-dynamic.js', 'i18n.js']) vm.runInContext(readFileSync(resolve(staticRoot, file), 'utf8'), context);
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
test('every continuous card label, tooltip and accessibility label is bilingual', () => {
  const context = runtime(), card = new Element('div'); context.appendGTFSContinuousEvidence(card, candidate());
  for (const node of flatten(card)) {
    for (const text of [node.ownText, node.title, node.attributes['aria-label']].filter(Boolean)) {
      const english = context.TidesI18n.translate(text);
      assert.ok(!/[\u3400-\u9fff]/.test(english), `${text} => ${english}`);
      context.TidesI18n.setLanguage('zh-CN'); assert.equal(context.TidesI18n.translate(text), text); context.TidesI18n.setLanguage('en');
    }
  }
});
test('continuous missing evidence renders unknown rather than invented zeros', () => {
  const context = runtime(), card = new Element('div'); context.appendGTFSContinuousEvidence(card, { scorer: 'continuous_passage_v1' });
  assert.equal(flatten(card).filter(node => node.dataset.component).length, 7);
  assert.equal(flatten(card).find(node => node.dataset.component).children[1].ownText, '未知');
  assert.equal(flatten(card).find(node => node.dataset.coverage).children[1].ownText, '未知');
});
test('continuous gating labels and parameter warning translate without changing IDs', () => {
  const context = runtime();
  for (const text of ['独立校准的班次可信集：false；要求 == true', '质量或校准可信集门槛未通过 · 仅供排查，不计入最终班次候选', '第二名减第一名成本（非概率）', '旧 DTW 与每小时时移参数仅用于旧评分器；连续时空评分请使用 continuous_* 参数。旧值保留，不随界面提示改变。']) assert.ok(!/[\u3400-\u9fff]/.test(context.TidesI18n.translate(text)), text);
  assert.ok(source.includes('if (!continuousCandidate && finite(c.estimated_delay_s) && finite(data.config?.score_delay_prior_weight))'));
  assert.ok(source.includes('const MATCHING_MODES = { global:'));
});

test('04c renders a separate displacement component and bilingual occurrence diagnostics', () => {
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
  for (const node of flatten(card)) for (const text of [node.ownText, node.title, node.attributes['aria-label']].filter(Boolean)) assert.ok(!/[\u3400-\u9fff]/.test(context.TidesI18n.translate(text)), text);
  assert.deepEqual(input, before);
});
