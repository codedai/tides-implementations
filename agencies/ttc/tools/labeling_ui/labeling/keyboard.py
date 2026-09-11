"""Keyboard shortcuts via a tiny JS bridge.

Streamlit has no native keybindings, so we inject a keydown listener into
the parent page that clicks buttons by their (unique) captions. Shortcuts
are ignored while typing in text inputs / textareas / selects.

This is best-effort UI sugar: if the browser blocks the bridge the app
still works with mouse clicks.
"""

from __future__ import annotations

import json

import streamlit.components.v1 as components

# key -> button caption prefix to click
DEFAULT_BINDINGS = {
    "j": "▶ Next",
    "n": "▶ Next",
    "k": "◀ Prev",
    "p": "◀ Prev",
    "s": "💾 Save label",
    "x": "💾 Save & next",
    "a": "⚡ Ambiguous",
    "i": "⚡ Insufficient",
}

_JS = """
<script>
(function() {
  const doc = window.parent.document;
  const bindings = %s;
  if (window.parent.__labelingKeysInstalled) {
    window.parent.__labelingKeyBindings = bindings;
    return;
  }
  window.parent.__labelingKeysInstalled = true;
  window.parent.__labelingKeyBindings = bindings;
  doc.addEventListener('keydown', function(ev) {
    if (ev.metaKey || ev.ctrlKey || ev.altKey) return;
    if (ev.repeat) return;  // held keys must not cascade saves across cases
    const t = ev.target;
    const tag = (t && t.tagName || '').toLowerCase();
    if (tag === 'input' || tag === 'textarea' || tag === 'select') return;
    if (t && t.isContentEditable) return;
    const wanted = window.parent.__labelingKeyBindings[ev.key];
    if (!wanted) return;
    const now = Date.now();
    if (now - (window.parent.__labelingLastFire || 0) < 400) return;
    const buttons = doc.querySelectorAll('button');
    for (const b of buttons) {
      if ((b.innerText || '').trim().startsWith(wanted)) {
        ev.preventDefault();
        window.parent.__labelingLastFire = now;
        b.click();
        return;
      }
    }
  }, true);
})();
</script>
"""


def install(bindings: dict[str, str] | None = None) -> None:
    b = bindings or DEFAULT_BINDINGS
    components.html(_JS % json.dumps(b), height=0, width=0)
