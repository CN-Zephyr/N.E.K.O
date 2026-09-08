'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const sourcePath = path.resolve(__dirname, '../../static/app/app-ui/bootstrap-goodbye-and-toasts.js');
const source = fs.readFileSync(sourcePath, 'utf8');

function classList() {
  const values = new Set();
  return {
    add(value) { values.add(value); },
    remove(value) { values.delete(value); },
    contains(value) { return values.has(value); },
  };
}

function createElement(tagName) {
  const element = {
    id: '',
    tagName: String(tagName).toUpperCase(),
    nodeType: 1,
    childNodes: [],
    parentNode: null,
    classList: classList(),
    style: { cssText: '' },
    textContent: '',
    value: '',
    appendChild(child) {
      child.parentNode = this;
      this.childNodes.push(child);
      return child;
    },
    remove() {
      if (!this.parentNode) return;
      this.parentNode.childNodes = this.parentNode.childNodes.filter((child) => child !== this);
      this.parentNode = null;
    },
    querySelector(selector) {
      if (!selector.startsWith('#')) return null;
      return this.childNodes.find((child) => child.id === selector.slice(1)) || null;
    },
    setAttribute() {},
    removeAttribute() {},
    addEventListener() {},
    removeEventListener() {},
    matches() { return false; },
    focus() { document.activeElement = this; },
    blur() { if (document.activeElement === this) document.activeElement = null; },
    select() {},
  };
  return element;
}

let document;

function createDeferred() {
  let resolve;
  const promise = new Promise((resolvePromise) => { resolve = resolvePromise; });
  return { promise, resolve };
}

function createHarness(navigatorClipboard) {
  const statusToast = createElement('div');
  statusToast.id = 'status-toast';
  const body = createElement('body');
  const head = createElement('head');
  const copied = [];
  document = {
    body,
    head,
    activeElement: null,
    createElement,
    getElementById(id) { return id === 'status-toast' ? statusToast : null; },
    querySelector() { return null; },
    execCommand(command) {
      copied.push(command);
      return command === 'copy';
    },
  };
  const window = {
    appUi: {},
    __appUiParts: {},
    appState: { dom: { statusToast }, _statusToastPriority: 0 },
    appConst: {},
    t(_key, options) { return options && options.defaultValue ? options.defaultValue : ''; },
    addEventListener() {},
    dispatchEvent() {},
  };
  vm.runInNewContext(source, {
    window,
    document,
    navigator: navigatorClipboard ? { clipboard: navigatorClipboard } : {},
    console: { log() {}, error() {} },
    Promise,
    Date,
    Array,
    Object,
    Number,
    String,
    Math,
    setTimeout() { return 1; },
    clearTimeout() {},
    CustomEvent: class CustomEvent {
      constructor(type, init) { this.type = type; this.detail = init && init.detail; }
    },
  }, { filename: sourcePath });
  return { window, statusToast, copied };
}

async function clickToastText(harness) {
  harness.window.showStatusToast('API request failed: 401');
  const text = harness.statusToast.querySelector('#status-toast-text');
  assert.ok(text);
  text.onclick({ stopPropagation() {} });
  await Promise.resolve();
  await Promise.resolve();
}

test('ordinary web toast falls back to execCommand when Clipboard API is unavailable', async () => {
  const harness = createHarness(null);

  await clickToastText(harness);

  assert.deepEqual(harness.copied, ['copy']);
});

test('ordinary web toast falls back when Clipboard API rejects', async () => {
  const harness = createHarness({ writeText: () => Promise.reject(new Error('denied')) });

  await clickToastText(harness);

  assert.deepEqual(harness.copied, ['copy']);
});

test('a delayed web copy cannot flash success on a newer toast', async () => {
  const pendingCopy = createDeferred();
  const harness = createHarness({ writeText: () => pendingCopy.promise });
  harness.window.showStatusToast('first error');
  const text = harness.statusToast.querySelector('#status-toast-text');
  text.onclick({ stopPropagation() {} });

  harness.window.showStatusToast('second error');
  pendingCopy.resolve();
  await Promise.resolve();
  await Promise.resolve();

  assert.equal(text.textContent, 'second error');
  assert.equal(text.classList.contains('status-toast-copy-success'), false);
});
