/* Local presentation-only localization. Never changes application data or inputs. */
"use strict";
(() => {
  const root = globalThis, doc = root.document, STORAGE_KEY = "tides-review-language";
  const supported = new Set(["zh-CN", "en"]), attributes = ["title", "aria-label", "placeholder", "aria-valuetext"];
  const exact = { ...(root.TIDES_I18N_STATIC || {}), ...(root.TIDES_I18N_DYNAMIC?.exact || {}) };
  const patterns = root.TIDES_I18N_DYNAMIC?.patterns || [];
  const cache = new Map(), texts = new WeakMap(), attrs = new WeakMap();
  const excluded = 'pre, code, script, style, [contenteditable], [data-i18n="off"], [translate="no"], #language-toggle';
  let language = "zh-CN", observer;
  try { const saved = root.localStorage?.getItem(STORAGE_KEY); if (supported.has(saved)) language = saved; } catch { /* Private/storage-disabled sessions still support switching. */ }

  function translate(value) {
    if (value == null) return value;
    const original = String(value);
    if (language !== "en" || !/[\u3400-\u9fff]/.test(original)) return original;
    // JSON tooltips are source diagnostics, not prose UI messages.
    if (/^\s*[\[{]/.test(original)) { try { const parsed = JSON.parse(original); if (parsed && typeof parsed === "object") return original; } catch { /* Non-JSON display text can still be localized. */ } }
    if (cache.has(original)) return cache.get(original);
    const [, leading, source, trailing] = original.match(/^(\s*)([\s\S]*?)(\s*)$/);
    let result = exact[source];
    if (result === undefined) {
      result = source;
      for (const [pattern, replacement] of patterns) { pattern.lastIndex = 0; result = result.replace(pattern, replacement); }
      result = exact[result] ?? result;
    }
    result = leading + result + trailing;
    if (cache.size >= 20000) cache.clear();
    cache.set(original, result);
    return result;
  }

  function protectedNode(element) { return !element || !!element.closest(excluded); }
  function renderText(node) {
    const parent = node.parentElement;
    if (protectedNode(parent) || parent.closest("textarea")) return;
    // Translating an implicit option value would change application state.
    if (parent.tagName === "OPTION" && !parent.hasAttribute("value")) return;
    const previous = texts.get(node), current = node.nodeValue;
    if (!previous && (language === "zh-CN" || !/[\u3400-\u9fff]/.test(current))) return;
    const source = previous && current === previous.rendered ? previous.source : current;
    const rendered = translate(source);
    if (current !== rendered) node.nodeValue = rendered;
    texts.set(node, { source, rendered });
  }
  function renderAttributes(element) {
    if (protectedNode(element)) return;
    let records = attrs.get(element);
    if (!records && (language === "zh-CN" || !attributes.some(name => element.hasAttribute(name)))) return;
    if (!records) { records = new Map(); attrs.set(element, records); }
    for (const name of attributes) {
      if (!element.hasAttribute(name)) { records.delete(name); continue; }
      const current = element.getAttribute(name), previous = records.get(name);
      const source = previous && current === previous.rendered ? previous.source : current;
      const rendered = translate(source);
      if (current !== rendered) element.setAttribute(name, rendered);
      records.set(name, { source, rendered });
    }
  }
  function sourceText(element) {
    if (!element) return "";
    if (element.nodeType === 3) {
      const previous = texts.get(element);
      return previous && element.nodeValue === previous.rendered ? previous.source : element.nodeValue;
    }
    return [...element.childNodes].map(sourceText).join("");
  }
  function visit(node) {
    if (!node) return;
    if (node.nodeType === 3) { renderText(node); return; }
    if (node.nodeType === 1) {
      if (protectedNode(node)) return;
      renderAttributes(node);
      if (node.tagName === "TEXTAREA") return;
    }
    for (const child of node.childNodes) visit(child);
  }
  function observe() {
    if (observer) observer.observe(doc.documentElement, { subtree: true, childList: true, characterData: true, attributes: true, attributeFilter: attributes });
  }
  function apply(node = doc?.documentElement) {
    if (!doc || !node) return;
    observer?.disconnect();
    try { visit(node); } finally { observe(); }
  }
  function updateControl() {
    if (!doc) return;
    doc.documentElement.lang = language;
    const button = doc.getElementById("language-toggle");
    if (!button) return;
    button.setAttribute("aria-checked", String(language === "en"));
    const label = language === "en" ? "Interface language: English. Switch to Chinese" : "界面语言：中文。切换为英文";
    button.setAttribute("aria-label", label); button.title = label;
    button.dataset.language = language;
    for (const span of button.querySelectorAll("[data-language]")) span.classList.toggle("active", span.dataset.language === language);
  }
  function setLanguage(next, { persist = true } = {}) {
    if (!supported.has(next)) return false;
    language = next;
    if (persist) { try { root.localStorage?.setItem(STORAGE_KEY, next); } catch { /* Language still changes for this page. */ } }
    apply(); updateControl();
    if (doc) root.dispatchEvent(new CustomEvent("tides-language-change", { detail: { language } }));
    return true;
  }
  root.TidesI18n = Object.freeze({ translate, sourceText, apply, setLanguage, getLanguage: () => language, storageKey: STORAGE_KEY });

  function start() {
    observer = new MutationObserver((records) => {
      if (language === "zh-CN") return;
      observer.disconnect();
      try {
        const added = new Set();
        for (const record of records) {
          if (record.type === "childList") for (const node of record.addedNodes) added.add(node);
          else if (record.type === "characterData") renderText(record.target);
          else if (record.type === "attributes") renderAttributes(record.target);
        }
        for (const node of added) if (node.isConnected) visit(node);
      } finally { observe(); }
    });
    doc.getElementById("language-toggle")?.addEventListener("click", () => setLanguage(language === "en" ? "zh-CN" : "en"));
    apply(); updateControl();
    root.addEventListener("storage", event => { if (event.key === STORAGE_KEY && supported.has(event.newValue)) setLanguage(event.newValue, { persist: false }); });
  }
  if (doc) { if (doc.readyState === "loading") doc.addEventListener("DOMContentLoaded", start, { once: true }); else start(); }
})();
