/** Pure localization tests; browser tests cover DOM/data/draft preservation. */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import test from 'node:test';
import vm from 'node:vm';

const staticRoot = resolve(import.meta.dirname, '../static');
function runtime(saved, blocked = false) {
  const storage = new Map(saved == null ? [] : [['tides-review-language', saved]]);
  const context = vm.createContext({ localStorage: {
    getItem(key) { if (blocked) throw new Error('Storage denied'); return storage.get(key) ?? null; },
    setItem(key, value) { if (blocked) throw new Error('Storage denied'); storage.set(key, value); },
  } });
  context.window = context;
  for (const name of ['i18n-static.js', 'i18n-dynamic.js', 'i18n.js']) vm.runInContext(readFileSync(resolve(staticRoot, name), 'utf8'), context, { filename: name });
  return { api: context.TidesI18n, context, storage };
}

test('defaults to Chinese and only persists the supported language preference', () => {
  const { api, storage } = runtime();
  assert.equal(api.getLanguage(), 'zh-CN');
  assert.equal(api.translate('全天总览'), '全天总览');
  api.setLanguage('en');
  assert.equal(api.translate('全天总览'), 'Day overview');
  assert.deepEqual([...storage.keys()], ['tides-review-language']);
  assert.equal(storage.get('tides-review-language'), 'en');
});
test('restores English; unsupported preferences cannot become arbitrary HTML language', () => {
  assert.equal(runtime('en').api.getLanguage(), 'en');
  const { api } = runtime('unsupported');
  assert.equal(api.getLanguage(), 'zh-CN');
  assert.equal(api.setLanguage('<script>'), false);
  assert.equal(api.getLanguage(), 'zh-CN');
});
test('storage-denied environments still support both directions', () => {
  const { api } = runtime(null, true);
  api.setLanguage('en');
  assert.equal(api.translate('保存'), 'Save');
  api.setLanguage('zh-CN');
  assert.equal(api.translate('保存'), '保存');
});
test('exact messages preserve surrounding whitespace and allow empty translations', () => {
  const { api } = runtime('en');
  assert.equal(api.translate(' \n保存  '), ' \nSave  ');
  assert.equal(api.translate('页'), '');
  assert.equal(api.translate('direction:9021:0000'), 'direction:9021:0000');
  assert.equal(api.translate('2023-11-06 05:16:18 · 46413314 · NORTH'), '2023-11-06 05:16:18 · 46413314 · NORTH');
});
test('switching back never reuses an English cached result in Chinese', () => {
  const { api } = runtime('en');
  const first = api.translate('GTFS 决策方式');
  assert.ok(!/[\u3400-\u9fff]/.test(first));
  api.setLanguage('zh-CN');
  assert.equal(api.translate('GTFS 决策方式'), 'GTFS 决策方式');
  api.setLanguage('en');
  assert.equal(api.translate('GTFS 决策方式'), first);
});
test('sourceText reads original text without modifying form values or raw objects', () => {
  const { api } = runtime('en');
  const row = { nodeType: 1, value: '用户未保存笔记', childNodes: [{ nodeType: 3, nodeValue: '保存' }] };
  const original = structuredClone(row);
  assert.equal(api.sourceText(row), '保存');
  assert.deepEqual(row, original);
});
test('raw JSON diagnostic text and tooltips are never localized', () => {
  const { api } = runtime('en');
  for (const value of ['{"label":"保存","trip_id":"46413314"}', '["直接支持","APC"]']) assert.equal(api.translate(value), value);
});
