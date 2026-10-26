// Minimal DOM used by the cockpit frontend tests.
//
// `children` and `querySelectorAll()` return collections that expose only what
// the real HTMLCollection / NodeList expose (length, indexes, item(), iteration
// and, for NodeList, forEach/keys/values/entries). Array methods such as
// .some/.map/.filter/.find/.at are deliberately absent, so code that would
// throw in a browser (e.g. `select.children.some(...)`) throws here too.

class TextNode {
  constructor(value) { this.value = String(value); this.parentNode = null; }
  get textContent() { return this.value; }
  set textContent(value) { this.value = String(value); }
  cloneNode() { return new TextNode(this.value); }
}

function liveCollection(items, extra = {}) {
  return new Proxy(items, {
    get(target, prop) {
      if (prop === 'length') return target.length;
      if (typeof prop === 'string' && /^\d+$/.test(prop)) return target[Number(prop)];
      if (prop === Symbol.iterator) return target[Symbol.iterator].bind(target);
      if (prop === 'item') return index => target[index] || null;
      if (prop in extra) return extra[prop](target);
      return undefined;
    },
    set() { return false; },
  });
}

const htmlCollection = items => liveCollection(items);
const nodeList = items => liveCollection(items, {
  forEach: target => (callback, thisArg) => target.forEach((node, index) => callback.call(thisArg, node, index, nodeList(target))),
  keys: target => () => target.keys(),
  values: target => () => target.values(),
  entries: target => () => target.entries(),
});

class Element {
  constructor(tag = 'div', className = '') {
    this.tagName = tag.toUpperCase(); this.className = className; this.id = '';
    this._kids = []; this.parentNode = null; this.dataset = {}; this.attributes = {};
    this.listeners = {}; this.hidden = false; this.disabled = false; this.scrollTop = 0;
    this.value = ''; this.checked = false;
    this.style = { removeProperty(name) { delete this[name]; } };
    this.clientWidth = 1200; this.clientHeight = 600;
    this._text = '';
    this.classList = {
      contains: name => this.className.split(/\s+/).includes(name),
      add: (...names) => { this.className = [...new Set([...this.className.split(/\s+/).filter(Boolean), ...names])].join(' '); },
      remove: (...names) => { this.className = this.className.split(/\s+/).filter(name => name && !names.includes(name)).join(' '); },
      toggle: (name, force) => {
        const enabled = force === undefined ? !this.classList.contains(name) : force;
        if (enabled) this.classList.add(name); else this.classList.remove(name);
        return enabled;
      },
    };
  }
  get children() { return htmlCollection(this._kids.filter(child => child instanceof Element)); }
  get childNodes() { return nodeList(this._kids); }
  get textContent() { return this._text + this._kids.map(child => child.textContent).join(''); }
  set textContent(value) { this._text = String(value); this._kids.forEach(child => { child.parentNode = null; }); this._kids = []; }
  get lastChild() { return this._kids.at(-1) || null; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return name in this.attributes ? this.attributes[name] : null; }
  removeAttribute(name) { delete this.attributes[name]; }
  append(...nodes) {
    for (const node of nodes) {
      const child = typeof node === 'string' ? new TextNode(node) : node;
      if (child.parentNode) child.parentNode._kids = child.parentNode._kids.filter(item => item !== child);
      child.parentNode = this; this._kids.push(child);
    }
  }
  replaceChildren(...nodes) { this._kids.forEach(child => { child.parentNode = null; }); this._kids = []; this._text = ''; this.append(...nodes); }
  remove() { if (this.parentNode) this.parentNode._kids = this.parentNode._kids.filter(item => item !== this); this.parentNode = null; }
  cloneNode(deep) {
    const copy = new Element(this.tagName, this.className);
    copy.id = this.id; copy.dataset = { ...this.dataset }; copy.attributes = { ...this.attributes };
    copy.hidden = this.hidden; copy._text = this._text; copy.value = this.value; copy.checked = this.checked;
    if (deep) copy.append(...this._kids.map(child => child.cloneNode(true)));
    return copy;
  }
  addEventListener(name, callback) { (this.listeners[name] ||= []).push(callback); }
  dispatch(name, target = this, extra = {}) {
    const event = { target, currentTarget: this, button: 0, preventDefault() {}, stopPropagation() {}, ...extra };
    for (const callback of this.listeners[name] || []) callback(event);
  }
  requestSubmit() { this.dispatch('submit'); }
  focus() {}
  scrollIntoView() {}
  matches(selector) {
    if (selector === ':last-child') return this.parentNode?._kids.at(-1) === this;
    const attribute = selector.match(/\[data-pane="([^"]+)"\]/);
    if (attribute && this.dataset.pane !== attribute[1]) return false;
    const withoutAttribute = selector.replace(/\[data-pane="[^"]+"\]/, '');
    const classes = [...withoutAttribute.matchAll(/\.([\w-]+)/g)].map(match => match[1]);
    if (classes.some(name => !this.classList.contains(name))) return false;
    const tag = withoutAttribute.split(/[.:[\s]/)[0];
    return !tag || this.tagName === tag.toUpperCase();
  }
  descendants() { return this._kids.filter(child => child instanceof Element).flatMap(child => [child, ...child.descendants()]); }
  _all(selector) {
    const elements = this._kids.filter(child => child instanceof Element);
    if (selector.startsWith(':scope > ')) return elements.filter(child => child.matches(selector.slice(9)));
    if (selector.includes(' > ')) {
      const [parent, child] = selector.split(' > ');
      return this._all(parent).flatMap(node => node._kids.filter(item => item instanceof Element && item.matches(child.replace(':last-child', ''))
        && (!child.endsWith(':last-child') || node._kids.at(-1) === item)));
    }
    if (selector.includes(' ')) {
      const [parent, child] = selector.split(' ');
      return this._all(parent).flatMap(node => node._all(child));
    }
    return this.descendants().filter(node => node.matches(selector));
  }
  querySelectorAll(selector) { return nodeList(this._all(selector)); }
  querySelector(selector) { return this._all(selector)[0] || null; }
  closest(selector) { for (let node = this; node; node = node.parentNode) if (node instanceof Element && node.matches(selector)) return node; return null; }
}

const el = (tag, className = '', text) => {
  const node = new Element(tag, className);
  if (text !== undefined) node.textContent = text;
  return node;
};
const nodeWith = (tag, className, ...children) => { const node = el(tag, className); node.append(...children); return node; };

module.exports = { TextNode, Element, el, nodeWith, htmlCollection, nodeList };
