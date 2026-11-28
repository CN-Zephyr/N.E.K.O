const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const root = path.resolve(__dirname, '../..');
const materialsSource = fs.readFileSync(path.join(root, 'static/theme-studio/materials.js'), 'utf8');
const appSource = fs.readFileSync(path.join(root, 'static/theme-studio/app.js'), 'utf8');

function createMiniDom() {
  const byIdRegistry = new Map();
  const docListeners = new Map();

  function makeClassList(node) {
    const tokens = new Set();
    const syncClass = () => {
      node._className = Array.from(tokens).join(' ');
    };
    return {
      add(...names) {
        for (const n of names) if (n) tokens.add(String(n));
        syncClass();
      },
      remove(...names) {
        for (const n of names) tokens.delete(String(n));
        syncClass();
      },
      toggle(name, force) {
        const key = String(name);
        const next = force === undefined ? !tokens.has(key) : !!force;
        if (next) tokens.add(key);
        else tokens.delete(key);
        syncClass();
        return next;
      },
      contains(name) {
        return tokens.has(String(name));
      },
      _replaceFromString(str) {
        tokens.clear();
        for (const part of String(str || '').split(/\s+/)) {
          if (part) tokens.add(part);
        }
        syncClass();
      },
    };
  }

  function createNode(tagName, nodeType = 1) {
    const listeners = new Map();
    const attrs = new Map();
    const style = {
      setProperty(k, v) { this[k] = String(v); },
      removeProperty(k) { delete this[k]; },
    };
    const node = {
      nodeType,
      tagName: String(tagName || '').toUpperCase(),
      children: [],
      parentNode: null,
      dataset: {},
      style,
      value: '',
      checked: false,
      disabled: false,
      hidden: false,
      _id: '',
      _className: '',
      _text: '',
      get id() { return this._id; },
      set id(val) {
        if (this._id && byIdRegistry.get(this._id) === this) byIdRegistry.delete(this._id);
        this._id = String(val || '');
        if (this._id) byIdRegistry.set(this._id, this);
      },
      get className() { return this._className; },
      set className(val) { this.classList._replaceFromString(val); },
      get firstChild() { return this.children[0] || null; },
      get textContent() {
        if (this.nodeType === 3) return this._text;
        if (!this.children.length) return this._text;
        return this.children.map((c) => c.textContent).join('');
      },
      set textContent(val) {
        if (this.nodeType === 3) {
          this._text = String(val ?? '');
          return;
        }
        this.children.length = 0;
        this._text = String(val ?? '');
      },
      appendChild(child) {
        if (!child) return child;
        child.parentNode = this;
        this.children.push(child);
        return child;
      },
      removeChild(child) {
        const idx = this.children.indexOf(child);
        if (idx >= 0) {
          this.children.splice(idx, 1);
          child.parentNode = null;
        }
        return child;
      },
      insertBefore(child, ref) {
        const idx = ref ? this.children.indexOf(ref) : -1;
        child.parentNode = this;
        if (idx >= 0) this.children.splice(idx, 0, child);
        else this.children.push(child);
        return child;
      },
      setAttribute(name, value) {
        const key = String(name);
        const val = String(value);
        attrs.set(key, val);
        if (key === 'id') this.id = val;
        if (key === 'class') this.className = val;
      },
      getAttribute(name) {
        return attrs.has(String(name)) ? attrs.get(String(name)) : null;
      },
      removeAttribute(name) {
        attrs.delete(String(name));
      },
      addEventListener(type, fn) {
        if (!listeners.has(type)) listeners.set(type, new Set());
        listeners.get(type).add(fn);
      },
      removeEventListener(type, fn) {
        const set = listeners.get(type);
        if (set) set.delete(fn);
      },
      dispatchEvent(event) {
        const ev = event && typeof event === 'object' ? event : { type: String(event) };
        ev.target = ev.target || this;
        ev.currentTarget = this;
        if (typeof ev.preventDefault !== 'function') ev.preventDefault = () => {};
        if (typeof ev.stopPropagation !== 'function') ev.stopPropagation = () => {};
        const set = listeners.get(ev.type);
        if (set) {
          for (const fn of Array.from(set)) fn.call(this, ev);
        }
        return true;
      },
      click() {
        this.dispatchEvent({ type: 'click' });
      },
      contains(other) {
        if (other === this) return true;
        return this.children.some((c) => c.contains && c.contains(other));
      },
      closest() { return null; },
      getBoundingClientRect() { return { left: 0, top: 0, width: 100, height: 100 }; },
    };
    node.classList = makeClassList(node);
    return node;
  }

  function findDescendants(rootNode, predicate, out = []) {
    for (const child of rootNode.children || []) {
      if (predicate(child)) out.push(child);
      findDescendants(child, predicate, out);
    }
    return out;
  }

  const document = {
    readyState: 'loading',
    activeElement: null,
    body: createNode('body'),
    documentElement: { lang: 'zh-CN' },
    createElement(tag) { return createNode(tag, 1); },
    createTextNode(text) {
      const node = createNode('#text', 3);
      node._text = String(text ?? '');
      return node;
    },
    getElementById(id) {
      return byIdRegistry.get(String(id)) || null;
    },
    addEventListener(type, fn) {
      if (!docListeners.has(type)) docListeners.set(type, new Set());
      docListeners.get(type).add(fn);
    },
    removeEventListener(type, fn) {
      const set = docListeners.get(type);
      if (set) set.delete(fn);
    },
    hasFocus() { return true; },
  };

  return { document, createNode, findDescendants, docListeners };
}

function makePresetSpecParams(blurVal) {
  return {
    global: { blur: blurVal },
    lighting: {},
    refraction: {},
    surface: {},
    animation: {},
  };
}

function makeBaseMaterials(blurVal, customPresets) {
  return {
    version: 1,
    enabled: true,
    preset: 'custom',
    quality: 'balanced',
    lock: false,
    ...makePresetSpecParams(blurVal),
    dark: {},
    components: {},
    custom_presets: customPresets,
  };
}

function createStudioHarness(initialMaterials, options = {}) {
  const { document, createNode, findDescendants, docListeners } = createMiniDom();

  const materialsRoot = createNode('div');
  materialsRoot.id = 'materials-root';

  const themeNameInput = createNode('input');
  themeNameInput.id = 'theme-name';
  const themeEnabledInput = createNode('input');
  themeEnabledInput.id = 'theme-enabled';
  const metaNameNode = createNode('span');
  metaNameNode.id = 'meta-name';
  const metaUpdatedNode = createNode('span');
  metaUpdatedNode.id = 'meta-updated';

  const winListeners = new Map();
  let customEntryHandler = options.entryHandler || null;
  const windowObj = {
    location: { origin: 'http://127.0.0.1:48911', pathname: '/theme_studio', search: '' },
    document,
    i18n: { isInitialized: true },
    __NTS_PANEL_BOOTED__: true,
    showPrompt: options.showPrompt || null,
    addEventListener(type, fn) {
      if (!winListeners.has(type)) winListeners.set(type, new Set());
      winListeners.get(type).add(fn);
    },
    removeEventListener(type, fn) {
      const set = winListeners.get(type);
      if (set) set.delete(fn);
    },
    NekoPanel: {
      call(entryId, args) {
        if (entryId === 'material_spec') {
          return Promise.resolve({
            version: 1,
            params: [
              { key: 'blur', section: 'global', kind: 'px', min: 0, max: 60, default: 22, label: '模糊半径' },
            ],
            presets: {
              clear: { label: '清透', desc: 'Clear', ...makePresetSpecParams(14) },
            },
            quality: [{ id: 'balanced', label: '均衡' }],
            current: JSON.parse(JSON.stringify(initialMaterials)),
          });
        }
        if (entryId === 'preview_materials') {
          return Promise.resolve({ vars: {}, warnings: [] });
        }
        if (customEntryHandler) {
          const handled = customEntryHandler(entryId, args);
          if (handled !== undefined) return Promise.resolve(handled);
        }
        return Promise.resolve({});
      },
      toast() {},
    },
  };
  windowObj.window = windowObj;
  windowObj.top = windowObj;

  let nextTimerId = 1;
  let fetchHandler = async () => ({
    ok: true,
    status: 200,
    async json() { return { ok: true, result: {} }; },
  });
  const context = vm.createContext({
    window: windowObj,
    document,
    navigator: { clipboard: null },
    fetch(url, opts) { return fetchHandler(url, opts); },
    AbortController: typeof AbortController !== 'undefined' ? AbortController : undefined,
    localStorage: {
      _data: new Map(),
      getItem(k) { return this._data.has(k) ? this._data.get(k) : null; },
      setItem(k, v) { this._data.set(k, String(v)); },
      removeItem(k) { this._data.delete(k); },
    },
    setTimeout() { return nextTimerId++; },
    clearTimeout() {},
    setInterval() { return nextTimerId++; },
    clearInterval() {},
    console,
    Promise,
    JSON,
    Object,
    Array,
    String,
    Number,
    Boolean,
    Math,
    Date,
    RegExp,
    Error,
    isFinite,
    parseFloat,
    parseInt,
    encodeURIComponent,
  });

  vm.runInContext(appSource, context, { filename: 'static/theme-studio/app.js' });
  const beforeMatHandlers = new Set(docListeners.get('DOMContentLoaded') || []);
  vm.runInContext(materialsSource, context, { filename: 'static/theme-studio/materials.js' });
  const matDomHandlers = Array.from(docListeners.get('DOMContentLoaded') || [])
    .filter((fn) => !beforeMatHandlers.has(fn));

  return {
    window: windowObj,
    document,
    materialsRoot,
    themeNameInput,
    themeEnabledInput,
    metaNameNode,
    metaUpdatedNode,
    findDescendants,
    createNode,
    setFetchHandler(fn) { fetchHandler = fn; },
    async bootMaterials() {
      for (const fn of matDomHandlers) fn();
      await new Promise((resolve) => setImmediate(resolve));
    },
  };
}

test('submitPayload submits only added/edited custom_presets and keepEdits sync reconciles concurrent changes', async () => {
  const basePresets = {
    DeletedByB: makePresetSpecParams(10),
    EditedByB: makePresetSpecParams(12),
    EditedByA: makePresetSpecParams(16),
  };
  const initial = makeBaseMaterials(18, basePresets);
  const harness = createStudioHarness(initial);
  await harness.bootMaterials();

  const { NekoMaterials, __ntsPanel } = harness.window;
  NekoMaterials.sync({ enabled: true, materials: JSON.parse(JSON.stringify(initial)) });
  assert.equal(NekoMaterials.isDirty(), false);

  const clickTab = (label) => {
    const nav = harness.document.getElementById('mt-tabs');
    const tab = nav.children.find((b) => b.textContent === label);
    assert.ok(tab, `missing tab ${label}`);
    tab.click();
  };

  // Modify blur via the base tab slider control.
  clickTab('基础材质');
  const pane = harness.document.getElementById('mt-pane');
  const rangeInput = harness.findDescendants(pane, (n) => n.tagName === 'INPUT' && n.getAttribute('type') === 'range')[0];
  assert.ok(rangeInput, 'expected blur range input');
  rangeInput.value = '26';
  rangeInput.dispatchEvent({ type: 'input' });
  assert.equal(NekoMaterials.isDirty(), true);

  // Add a brand-new custom preset AddedByA on the presets tab.
  clickTab('预设与画质');
  const saveCustomPreset = (name) => {
    const input = harness.document.getElementById('mt-preset-name');
    const saveRow = input.parentNode;
    const saveBtn = saveRow.children.find((n) => n.tagName === 'BUTTON');
    input.value = name;
    saveBtn.click();
  };
  saveCustomPreset('AddedByA');

  // Edit blur again and overwrite local draft preset EditedByA so it differs from BASELINE.
  clickTab('基础材质');
  const rangeAgain = harness.findDescendants(pane, (n) => n.tagName === 'INPUT' && n.getAttribute('type') === 'range')[0];
  rangeAgain.value = '29';
  rangeAgain.dispatchEvent({ type: 'input' });
  clickTab('预设与画质');
  saveCustomPreset('EditedByA');

  // submitPayload includes ONLY AddedByA and EditedByA; DeletedByB and EditedByB are excluded.
  const payload = NekoMaterials.submitPayload();
  assert.deepEqual(Object.keys(payload.custom_presets).sort(), ['AddedByA', 'EditedByA']);
  assert.equal(payload.custom_presets.EditedByA.global.blur, 29);
  assert.equal(payload.global.blur, 29);

  // currentMaterialsPayload() delegates to submitPayload(), whereas snapshot() keeps the full library.
  const panelPayload = __ntsPanel.currentMaterialsPayload();
  assert.deepEqual(Object.keys(panelPayload.custom_presets).sort(), ['AddedByA', 'EditedByA']);
  const fullSnap = NekoMaterials.snapshot();
  assert.deepEqual(
    Object.keys(fullSnap.custom_presets).sort(),
    ['AddedByA', 'DeletedByB', 'EditedByA', 'EditedByB'],
  );

  // Normal save readback clears dirty state when no edits happened during the save.
  const diskAfterSave = makeBaseMaterials(29, {
    EditedByB: makePresetSpecParams(12),
    EditedByA: makePresetSpecParams(29),
    AddedByA: makePresetSpecParams(26),
  });
  NekoMaterials.sync({ enabled: true, materials: diskAfterSave }, { submitted: fullSnap });
  assert.equal(NekoMaterials.isDirty(), false);

  // Concurrent edit while save is in flight (keepEdits=true):
  // Panel A sets blur=32 and submits, then edits blur=35 before readback returns.
  clickTab('基础材质');
  const rangeSave = harness.findDescendants(pane, (n) => n.tagName === 'INPUT' && n.getAttribute('type') === 'range')[0];
  rangeSave.value = '32';
  rangeSave.dispatchEvent({ type: 'input' });
  const inflightSnap = NekoMaterials.snapshot();

  rangeSave.value = '35';
  rangeSave.dispatchEvent({ type: 'input' });

  // Meanwhile, Panel B deleted EditedByA on disk and added AddedByB.
  const concurrentDisk = makeBaseMaterials(32, {
    EditedByB: makePresetSpecParams(40),
    AddedByA: makePresetSpecParams(26),
    AddedByB: makePresetSpecParams(15),
  });
  NekoMaterials.sync({ enabled: true, materials: concurrentDisk }, { submitted: inflightSnap });

  assert.equal(NekoMaterials.isDirty(), true);
  const afterKeepEdits = NekoMaterials.snapshot();
  assert.equal(afterKeepEdits.global.blur, 35);
  assert.equal('EditedByA' in afterKeepEdits.custom_presets, false, 'concurrent delete must not remain in draft');
  assert.equal(afterKeepEdits.custom_presets.EditedByB.global.blur, 40);
  assert.equal(afterKeepEdits.custom_presets.AddedByB.global.blur, 15);
  assert.deepEqual(Object.keys(NekoMaterials.submitPayload().custom_presets), []);
});

test('renderMeta tracks name and enabled drafts independently across palette readbacks', () => {
  const initial = makeBaseMaterials(22, {});
  const harness = createStudioHarness(initial);
  const { __ntsPanel } = harness.window;

  __ntsPanel.state.palette = { name: 'Old', enabled: false, updated_at: '2026-11-26T10:00:00Z' };
  __ntsPanel.renderMeta({ discard: true });
  assert.equal(harness.themeNameInput.value, 'Old');
  assert.equal(harness.themeEnabledInput.checked, false);
  assert.equal(__ntsPanel.metaDirtyCount(), 0);

  // User edits only the theme name while enabled remains false.
  harness.themeNameInput.value = 'RenamedDraft';
  assert.equal(__ntsPanel.metaDirtyCount(), 1);

  // Scheduled theme switch enables the newly selected theme on disk and triggers readback without discard.
  __ntsPanel.state.palette = { name: 'Scheduled', enabled: true, updated_at: '2026-11-26T12:00:00Z' };
  __ntsPanel.renderMeta();

  assert.equal(harness.themeNameInput.value, 'RenamedDraft');
  assert.equal(harness.themeEnabledInput.checked, true);
  assert.equal(__ntsPanel.state.metaName, 'Scheduled');
  assert.equal(__ntsPanel.state.metaEnabled, true);
  assert.equal(__ntsPanel.metaDirtyCount(), 1);

  // Reverse case: discard resets both, then user flips only enabled=false while disk renames the theme.
  __ntsPanel.renderMeta({ discard: true });
  assert.equal(harness.themeNameInput.value, 'Scheduled');
  assert.equal(harness.themeEnabledInput.checked, true);
  assert.equal(__ntsPanel.metaDirtyCount(), 0);

  harness.themeEnabledInput.checked = false;
  assert.equal(__ntsPanel.metaDirtyCount(), 1);

  __ntsPanel.state.palette = { name: 'Evening', enabled: true, updated_at: '2026-11-26T18:00:00Z' };
  __ntsPanel.renderMeta();

  assert.equal(harness.themeNameInput.value, 'Evening');
  assert.equal(harness.themeEnabledInput.checked, false);
  assert.equal(__ntsPanel.state.metaName, 'Evening');
  assert.equal(__ntsPanel.state.metaEnabled, true);
  assert.equal(__ntsPanel.metaDirtyCount(), 1);
});

function mountPanelDomStubs(harness) {
  for (const id of [
    'toast', 'btn-apply', 'status', 'notice', 'notice-text',
    'mode-light', 'mode-dark',
    'preview-mode', 'preview', 'pv-body', 'pv-topbar', 'pv-user',
    'pv-ai', 'pv-link', 'pv-chip', 'pv-input-area', 'pv-input', 'pv-send',
    'dump', 'dump-foot', 'empty-hint', 'audit-list', 'audit-summary',
    'health-state', 'changes-card', 'changes-list', 'changes-count',
    'btn-undo', 'btn-redo',
  ]) {
    const node = harness.createNode('div');
    node.id = id;
  }
}

test('applyTheme omits unchanged enabled flag and includes it only when toggled', async () => {
  const initial = makeBaseMaterials(22, {});
  const harness = createStudioHarness(initial);
  const { __ntsPanel } = harness.window;
  mountPanelDomStubs(harness);

  let diskPalette = {
    name: 'Old',
    enabled: true,
    vars: {},
    dark_vars: {},
    updated_at: '2026-11-28T10:00:00Z',
  };
  const applyCalls = [];
  harness.setFetchHandler(async (url, opts) => {
    const body = opts && opts.body ? JSON.parse(opts.body) : {};
    if (String(url).endsWith('/run/apply_theme')) {
      applyCalls.push(body);
      if (Object.prototype.hasOwnProperty.call(body, 'name')) diskPalette.name = body.name;
      if (Object.prototype.hasOwnProperty.call(body, 'enabled')) diskPalette.enabled = body.enabled;
      return { ok: true, status: 200, async json() { return { ok: true, result: { rejected: [] } }; } };
    }
    if (String(url).endsWith('/run/get_theme')) {
      return {
        ok: true,
        status: 200,
        async json() { return { ok: true, result: JSON.parse(JSON.stringify(diskPalette)) }; },
      };
    }
    return { ok: true, status: 200, async json() { return { ok: true, result: {} }; } };
  });

  __ntsPanel.state.palette = JSON.parse(JSON.stringify(diskPalette));
  __ntsPanel.renderMeta({ discard: true });

  // Simulate another panel disabling the theme on disk while Panel A has an unapplied name edit.
  diskPalette.enabled = false;
  harness.themeNameInput.value = 'RenamedByA';
  await __ntsPanel.applyTheme();

  assert.equal(applyCalls.length, 1);
  assert.equal(applyCalls[0].name, 'RenamedByA');
  assert.equal(Object.prototype.hasOwnProperty.call(applyCalls[0], 'enabled'), false);
  assert.equal(diskPalette.enabled, false, 'unrelated Apply must keep disk enabled=false');
  assert.equal(harness.themeEnabledInput.checked, false, 'readback refreshes untouched toggle');

  // When the user explicitly toggles enabled, applyTheme includes enabled in the payload.
  harness.themeEnabledInput.checked = true;
  await __ntsPanel.applyTheme();

  assert.equal(applyCalls.length, 2);
  assert.equal(Object.prototype.hasOwnProperty.call(applyCalls[1], 'enabled'), true);
  assert.equal(applyCalls[1].enabled, true);
  assert.equal(diskPalette.enabled, true);
});

test('preset delete, rename, and duplicate callbacks reconcile untouched custom_presets against server baseline', async () => {
  async function setupPresetScenario(entryHandler, promptAnswer) {
    const basePresets = {
      P: makePresetSpecParams(10),
      R: makePresetSpecParams(12),
      Q: makePresetSpecParams(14),
    };
    const initial = makeBaseMaterials(18, basePresets);
    const harness = createStudioHarness(initial, {
      entryHandler,
      showPrompt: promptAnswer ? () => promptAnswer : null,
    });
    await harness.bootMaterials();
    const { NekoMaterials } = harness.window;
    NekoMaterials.sync({ enabled: true, materials: JSON.parse(JSON.stringify(initial)) });

    const nav = harness.document.getElementById('mt-tabs');
    const presetsTab = nav.children.find((b) => b.textContent === '预设与画质');
    assert.ok(presetsTab);
    presetsTab.click();

    const findPresetAction = (presetName, actionLabel) => {
      const pane = harness.document.getElementById('mt-pane');
      const wraps = harness.findDescendants(pane, (n) => n.classList.contains('mt-preset-wrap'));
      const targetWrap = wraps.find((w) => {
        const b = harness.findDescendants(w, (n) => n.tagName === 'B')[0];
        return b && b.textContent === presetName;
      });
      assert.ok(targetWrap, `missing preset card wrapper for ${presetName}`);
      const btn = harness.findDescendants(
        targetWrap,
        (n) => n.tagName === 'BUTTON' && n.textContent === actionLabel,
      )[0];
      assert.ok(btn, `missing ${actionLabel} button for ${presetName}`);
      return btn;
    };

    return { harness, NekoMaterials, findPresetAction };
  }

  // 1. Delete Q while another panel updated P to blur=30 and deleted R.
  {
    const { NekoMaterials, findPresetAction } = await setupPresetScenario((entryId, args) => {
      if (entryId === 'delete_material_preset') {
        assert.equal(args.name, 'Q');
        return {
          materials: makeBaseMaterials(18, { P: makePresetSpecParams(30) }),
        };
      }
    });
    findPresetAction('Q', '删除').click();
    await new Promise((resolve) => setImmediate(resolve));

    assert.equal(NekoMaterials.isDirty(), false);
    assert.deepEqual(Object.keys(NekoMaterials.submitPayload().custom_presets), []);
    const snap = NekoMaterials.snapshot();
    assert.deepEqual(Object.keys(snap.custom_presets), ['P']);
    assert.equal(snap.custom_presets.P.global.blur, 30);
  }

  // 2. Rename Q -> Q2 while another panel updated P=30 and deleted R.
  {
    const { NekoMaterials, findPresetAction } = await setupPresetScenario((entryId, args) => {
      if (entryId === 'rename_material_preset') {
        assert.equal(args.name, 'Q');
        assert.equal(args.new_name, 'Q2');
        return {
          name: 'Q2',
          materials: makeBaseMaterials(18, {
            P: makePresetSpecParams(30),
            Q2: makePresetSpecParams(14),
          }),
        };
      }
    }, 'Q2');
    findPresetAction('Q', '改名').click();
    await new Promise((resolve) => setImmediate(resolve));

    assert.equal(NekoMaterials.isDirty(), false);
    assert.deepEqual(Object.keys(NekoMaterials.submitPayload().custom_presets), []);
    const snap = NekoMaterials.snapshot();
    assert.deepEqual(Object.keys(snap.custom_presets).sort(), ['P', 'Q2']);
    assert.equal(snap.custom_presets.P.global.blur, 30);
  }

  // 3. Duplicate Q -> Q Copy while another panel updated P=30 and deleted R.
  {
    const { NekoMaterials, findPresetAction } = await setupPresetScenario((entryId, args) => {
      if (entryId === 'duplicate_material_preset') {
        assert.equal(args.name, 'Q');
        assert.equal(args.new_name, 'Q Copy');
        return {
          name: 'Q Copy',
          materials: makeBaseMaterials(18, {
            P: makePresetSpecParams(30),
            Q: makePresetSpecParams(14),
            'Q Copy': makePresetSpecParams(14),
          }),
        };
      }
    }, 'Q Copy');
    findPresetAction('Q', '复制').click();
    await new Promise((resolve) => setImmediate(resolve));

    assert.equal(NekoMaterials.isDirty(), false);
    assert.deepEqual(Object.keys(NekoMaterials.submitPayload().custom_presets), []);
    const snap = NekoMaterials.snapshot();
    assert.deepEqual(Object.keys(snap.custom_presets).sort(), ['P', 'Q', 'Q Copy']);
    assert.equal(snap.custom_presets.P.global.blur, 30);
  }
});

test('renderMeta clears submitted empty name to default and refreshes untouched focused name input', () => {
  const initial = makeBaseMaterials(22, {});
  const harness = createStudioHarness(initial);
  const { __ntsPanel } = harness.window;

  __ntsPanel.state.palette = { name: 'CustomName', enabled: true, updated_at: '2026-11-28T10:00:00Z' };
  __ntsPanel.renderMeta({ discard: true });
  assert.equal(harness.themeNameInput.value, 'CustomName');

  // Clearing #theme-name and applying nameSubmitted='' restores server default name cleanly.
  harness.themeNameInput.value = '';
  __ntsPanel.state.palette = { name: '默认主题', enabled: true, updated_at: '2026-11-28T10:01:00Z' };
  __ntsPanel.renderMeta({ nameSubmitted: '' });
  assert.equal(harness.themeNameInput.value, '默认主题');
  assert.equal(__ntsPanel.state.metaName, '默认主题');
  assert.equal(__ntsPanel.metaDirtyCount(), 0);

  // When focus rests on an untouched #theme-name input, external rename still refreshes the input.
  harness.document.activeElement = harness.themeNameInput;
  __ntsPanel.state.palette = { name: 'ExternalName', enabled: true, updated_at: '2026-11-28T10:02:00Z' };
  __ntsPanel.renderMeta();
  assert.equal(harness.themeNameInput.value, 'ExternalName');
  assert.equal(__ntsPanel.metaDirtyCount(), 0);

  // While server name has not changed, trailing whitespace typed mid-edit is preserved.
  harness.themeNameInput.value = 'ExternalName ';
  __ntsPanel.state.palette = { name: 'ExternalName', enabled: true, updated_at: '2026-11-28T10:02:30Z' };
  __ntsPanel.renderMeta();
  assert.equal(harness.themeNameInput.value, 'ExternalName ');
  assert.equal(__ntsPanel.metaDirtyCount(), 0);

  // If another panel renames the theme while focus sits on a whitespace-only variation, refresh cleanly.
  __ntsPanel.state.palette = { name: 'AnotherExternal', enabled: true, updated_at: '2026-11-28T10:03:00Z' };
  __ntsPanel.renderMeta();
  assert.equal(harness.themeNameInput.value, 'AnotherExternal');
  assert.equal(__ntsPanel.metaDirtyCount(), 0);

  // Real non-whitespace edits stay intact even if another panel renames the theme while focused.
  harness.themeNameInput.value = 'AnotherExternal Draft';
  __ntsPanel.state.palette = { name: 'YetAnother', enabled: true, updated_at: '2026-11-28T10:04:00Z' };
  __ntsPanel.renderMeta();
  assert.equal(harness.themeNameInput.value, 'AnotherExternal Draft');
});

test('reconcileUntouchedPresets canonicalizes key order to prevent false dirty state', async () => {
  const initial = makeBaseMaterials(18, { A: makePresetSpecParams(10) });
  const harness = createStudioHarness(initial, {
    showPrompt: () => 'A Copy',
    entryHandler(entryId, args) {
      if (entryId === 'duplicate_material_preset') {
        assert.equal(args.name, 'A');
        assert.equal(args.new_name, 'A Copy');
        // Another panel added B before A Copy was appended on the server.
        return {
          name: 'A Copy',
          materials: makeBaseMaterials(18, {
            A: makePresetSpecParams(10),
            B: makePresetSpecParams(20),
            'A Copy': makePresetSpecParams(10),
          }),
        };
      }
    },
  });
  await harness.bootMaterials();
  const { NekoMaterials } = harness.window;
  NekoMaterials.sync({ enabled: true, materials: JSON.parse(JSON.stringify(initial)) });

  const nav = harness.document.getElementById('mt-tabs');
  nav.children.find((b) => b.textContent === '预设与画质').click();
  const pane = harness.document.getElementById('mt-pane');
  const dupBtn = harness.findDescendants(
    pane,
    (n) => n.tagName === 'BUTTON' && n.textContent === '复制',
  )[0];
  assert.ok(dupBtn);
  dupBtn.click();
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(NekoMaterials.isDirty(), false);
  assert.deepEqual(Object.keys(NekoMaterials.submitPayload().custom_presets), []);
  assert.deepEqual(Object.keys(NekoMaterials.snapshot().custom_presets), ['A', 'B', 'A Copy']);
});

test('applyTheme waits for in-flight preset op and stale preset response respects BASELINE_GEN', async () => {
  // Case A: applyTheme waits for in-flight delete_material_preset before reading materials payload.
  {
    let resolveDelete;
    const deleteDeferred = new Promise((resolve) => { resolveDelete = resolve; });
    const initial = makeBaseMaterials(18, {
      P: makePresetSpecParams(10),
      Q: makePresetSpecParams(14),
    });
    const harness = createStudioHarness(initial, {
      entryHandler(entryId) {
        if (entryId === 'delete_material_preset') return deleteDeferred;
      },
    });
    mountPanelDomStubs(harness);
    await harness.bootMaterials();
    const { NekoMaterials, __ntsPanel } = harness.window;

    let diskPalette = {
      name: 'Default',
      enabled: true,
      vars: {},
      dark_vars: {},
      materials: JSON.parse(JSON.stringify(initial)),
      updated_at: '2026-11-28T10:00:00Z',
    };
    const applyCalls = [];
    harness.setFetchHandler(async (url, opts) => {
      const body = opts && opts.body ? JSON.parse(opts.body) : {};
      if (String(url).endsWith('/run/apply_theme')) {
        applyCalls.push(body);
        if (body.vars) Object.assign(diskPalette.vars, body.vars);
        return { ok: true, status: 200, async json() { return { ok: true, result: { rejected: [] } }; } };
      }
      if (String(url).endsWith('/run/get_theme')) {
        return {
          ok: true,
          status: 200,
          async json() { return { ok: true, result: JSON.parse(JSON.stringify(diskPalette)) }; },
        };
      }
      return { ok: true, status: 200, async json() { return { ok: true, result: {} }; } };
    });

    __ntsPanel.state.palette = JSON.parse(JSON.stringify(diskPalette));
    __ntsPanel.renderMeta({ discard: true });
    NekoMaterials.sync(diskPalette);

    const nav = harness.document.getElementById('mt-tabs');
    nav.children.find((b) => b.textContent === '预设与画质').click();
    const pane = harness.document.getElementById('mt-pane');
    const qWrap = harness.findDescendants(pane, (n) => n.classList.contains('mt-preset-wrap')).find((w) => {
      const b = harness.findDescendants(w, (n) => n.tagName === 'B')[0];
      return b && b.textContent === 'Q';
    });
    assert.ok(qWrap);
    const delBtn = harness.findDescendants(qWrap, (n) => n.tagName === 'BUTTON' && n.textContent === '删除')[0];
    delBtn.click();
    assert.equal(NekoMaterials.isPresetOpPending(), true);

    __ntsPanel.state.edited.light['--nts-accent'] = '#123456';
    const applyPromise = __ntsPanel.applyTheme();
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(applyCalls.length, 0, 'applyTheme must wait while delete_material_preset is in flight');

    diskPalette.materials = makeBaseMaterials(18, { P: makePresetSpecParams(10) });
    resolveDelete({ materials: JSON.parse(JSON.stringify(diskPalette.materials)) });
    await applyPromise;

    assert.equal(applyCalls.length, 1);
    assert.equal(applyCalls[0].vars['--nts-accent'], '#123456');
    assert.equal(Object.prototype.hasOwnProperty.call(applyCalls[0], 'materials'), false);
    assert.equal(NekoMaterials.isDirty(), false);
  }

  // Case B: intervening sync() advances BASELINE_GEN so a stale delete response does not erase newly synced preset Y.
  {
    let resolveDelete;
    const deleteDeferred = new Promise((resolve) => { resolveDelete = resolve; });
    const initial = makeBaseMaterials(18, {
      P: makePresetSpecParams(10),
      Q: makePresetSpecParams(14),
    });
    const harness = createStudioHarness(initial, {
      entryHandler(entryId) {
        if (entryId === 'delete_material_preset') return deleteDeferred;
      },
    });
    await harness.bootMaterials();
    const { NekoMaterials } = harness.window;
    NekoMaterials.sync({ enabled: true, materials: JSON.parse(JSON.stringify(initial)) });

    const nav = harness.document.getElementById('mt-tabs');
    nav.children.find((b) => b.textContent === '预设与画质').click();
    const pane = harness.document.getElementById('mt-pane');
    const qWrap = harness.findDescendants(pane, (n) => n.classList.contains('mt-preset-wrap')).find((w) => {
      const b = harness.findDescendants(w, (n) => n.tagName === 'B')[0];
      return b && b.textContent === 'Q';
    });
    const delBtn = harness.findDescendants(qWrap, (n) => n.tagName === 'BUTTON' && n.textContent === '删除')[0];
    delBtn.click();

    // Intervening readback arrives before the delete response and introduces preset Y.
    NekoMaterials.sync({
      enabled: true,
      materials: makeBaseMaterials(18, {
        P: makePresetSpecParams(10),
        Y: makePresetSpecParams(24),
      }),
    });

    // Stale delete response lacks Y; targeted delta keeps Y intact.
    resolveDelete({
      materials: makeBaseMaterials(18, { P: makePresetSpecParams(10) }),
    });
    await new Promise((resolve) => setImmediate(resolve));

    const snap = NekoMaterials.snapshot();
    assert.equal('Y' in snap.custom_presets, true);
    assert.equal('Q' in snap.custom_presets, false);
    assert.equal(NekoMaterials.isDirty(), false);
  }
});

test('auditRowsForMode models bubble glass tint, gradients, and text-gradient contrast without darkNative leaks', () => {
  const initial = makeBaseMaterials(22, {});
  const harness = createStudioHarness(initial);
  mountPanelDomStubs(harness);
  const { __ntsPanel } = harness.window;

  // 1. Opaque white --nts-bubble-glass-tint overrides black user bubble background; applyAuditFix picks dark text.
  __ntsPanel.state.palette = {
    name: 'GlassTest',
    enabled: true,
    vars: {
      '--nts-chat-bg': '#101010',
      '--nts-user-bubble-bg': '#000000',
      '--nts-user-bubble-text': '#ffffff',
      '--nts-bubble-glass-tint': '#ffffff',
    },
    dark_vars: {},
  };
  __ntsPanel.state.edited = { light: {}, dark: {} };
  __ntsPanel.state.cleared = { light: {}, dark: {} };
  __ntsPanel.state.mode = 'light';

  let lightRows = __ntsPanel.auditRowsForMode('light');
  const userRow = lightRows.find((r) => r.key === '--nts-user-bubble-text');
  assert.ok(userRow);
  assert.equal(userRow.ok, false);
  assert.equal(userRow.ratio, 1);
  assert.equal(userRow.canAutoFix, true);
  assert.equal(__ntsPanel.applyAuditFix(userRow, true), true);
  lightRows = __ntsPanel.auditRowsForMode('light');
  const fixedUserRow = lightRows.find((r) => r.key === '--nts-user-bubble-text');
  assert.ok(fixedUserRow && fixedUserRow.ok);

  // 2. Dark AI bubble gradient over white AI bubble background fails dark AI text.
  __ntsPanel.state.palette = {
    name: 'GradTest',
    enabled: true,
    vars: {
      '--nts-chat-bg': '#ffffff',
      '--nts-ai-bubble-bg': '#ffffff',
      '--nts-ai-bubble-gradient': 'linear-gradient(135deg, #111111 0%, #222222 100%)',
      '--nts-ai-bubble-text': '#111111',
    },
    dark_vars: {},
  };
  __ntsPanel.state.edited = { light: {}, dark: {} };
  lightRows = __ntsPanel.auditRowsForMode('light');
  const aiRow = lightRows.find((r) => r.key === '--nts-ai-bubble-text');
  assert.ok(aiRow);
  assert.equal(aiRow.ok, false);

  // 3. Unreadable --nts-text-gradient stop flags the row as non-autofixable and refuses overwrite.
  __ntsPanel.state.palette = {
    name: 'TextGradTest',
    enabled: true,
    vars: {
      '--nts-chat-bg': '#000000',
      '--nts-user-bubble-bg': '#000000',
      '--nts-user-bubble-text': '#ffffff',
      '--nts-text-gradient': 'linear-gradient(120deg, #ffffff 0%, #111111 100%)',
    },
    dark_vars: {},
  };
  __ntsPanel.state.edited = { light: {}, dark: {} };
  lightRows = __ntsPanel.auditRowsForMode('light');
  const textGradRow = lightRows.find((r) => r.key === '--nts-text-gradient');
  assert.ok(textGradRow);
  assert.equal(textGradRow.ok, false);
  assert.equal(textGradRow.canAutoFix, false);
  assert.equal(__ntsPanel.applyAuditFix(textGradRow, true), false);
  assert.equal(
    Object.prototype.hasOwnProperty.call(__ntsPanel.state.edited.light, '--nts-text-gradient'),
    false,
  );

  // 4. Dark mode without explicit --nts-bubble-glass-tint ignores CATALOG darkNative example tint.
  __ntsPanel.state.palette = {
    name: 'DarkNoTint',
    enabled: true,
    vars: {},
    dark_vars: {
      '--nts-chat-bg': '#000000',
      '--nts-user-bubble-bg': '#000000',
      '--nts-user-bubble-text': '#ffffff',
    },
  };
  __ntsPanel.state.edited = { light: {}, dark: {} };
  const darkRows = __ntsPanel.auditRowsForMode('dark');
  const darkUserRow = darkRows.find((r) => r.key === '--nts-user-bubble-text');
  assert.ok(darkUserRow);
  assert.equal(darkUserRow.ok, true);
  assert.equal(darkUserRow.ratio, 21);
});
