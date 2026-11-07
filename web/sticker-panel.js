/* 表情包面板：分类、搜索、最近使用、上传删除与分类管理。 */
(function () {
  'use strict';

  const RECENT_KEY = 'starlit-sticker-recent-v1';
  const ALL = 'ALL';
  const RECENT = '__recent__';

  class StickerPanelController {
    constructor(panel) {
      this.panel = panel;
      this.items = [];
      this.sets = [];
      this.recent = this.readRecent();
      this.selected = ALL;
      this.manage = false;
      this.loaded = false;
      this.loading = null;
      this.busy = false;
      this.sending = false;
      this.els = {};
      for (const id of ['skTabs', 'skManage', 'skSearch', 'skClose', 'skAdmin', 'skCreateSet',
        'skDeleteSet', 'skUpload', 'skFile', 'skGrid', 'skStatus']) {
        this.els[id] = document.getElementById(id);
      }
      this.bind();
      this.render();
    }

    bind() {
      const e = this.els;
      e.skManage.addEventListener('click', () => this.toggleManage());
      e.skClose.addEventListener('click', () => this.close());
      e.skSearch.addEventListener('input', () => this.renderGrid());
      e.skCreateSet.addEventListener('click', () => this.createCategory());
      e.skDeleteSet.addEventListener('click', () => this.deleteCategory());
      e.skUpload.addEventListener('click', () => this.pickFiles());
      e.skFile.addEventListener('change', () => this.uploadFiles());
    }

    async request(path, options) {
      const response = await fetch(path, options || {});
      const contentType = response.headers.get('content-type') || '';
      const data = contentType.includes('json') ? await response.json() : {};
      if (!response.ok || data.error || data.ok === false) {
        throw new Error(data.error || `请求失败（${response.status}）`);
      }
      return data;
    }

    post(path, body) {
      return this.request(path, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
      });
    }

    status(text) {
      this.els.skStatus.textContent = text || '';
    }

    notify(text) {
      this.status(text);
      if (typeof window.toast === 'function') window.toast(text);
    }

    readRecent() {
      try {
        const value = JSON.parse(localStorage.getItem(RECENT_KEY) || '[]');
        return Array.isArray(value) ? value.filter(item => item && item.file).slice(0, 20) : [];
      } catch (error) {
        return [];
      }
    }

    saveRecent() {
      try { localStorage.setItem(RECENT_KEY, JSON.stringify(this.recent.slice(0, 20))); }
      catch (error) { /* 隐私模式禁用存储时，最近使用仅在当前页面有效。 */ }
    }

    remember(item) {
      this.recent = [item, ...this.recent.filter(old => old.file !== item.file)].slice(0, 20);
      this.saveRecent();
    }

    async load(force) {
      if (this.loading && !force) return this.loading;
      this.loading = (async () => {
        if (!this.loaded) this.status('正在读取表情包…');
        try {
          const [data, setData] = await Promise.all([
            this.request('/api/stickers'), this.request('/api/sticker-sets')
          ]);
          this.items = Array.isArray(data.items) ? data.items : [];
          this.sets = setData.sets && typeof setData.sets === 'object'
            ? Object.values(setData.sets) : (data.sets || []);
          const files = new Set(this.items.map(item => item.file));
          this.recent = this.recent.filter(item => files.has(item.file));
          this.saveRecent();
          if (this.selected !== ALL && this.selected !== RECENT &&
              !this.sets.some(set => set.key === this.selected)) this.selected = ALL;
          this.loaded = true;
          this.status('');
          this.render();
        } catch (error) {
          this.status(error.message || '表情包读取失败');
          this.renderGrid();
          throw error;
        } finally {
          this.loading = null;
        }
      })();
      return this.loading;
    }

    async reload() {
      return this.load(true);
    }

    async open() {
      const emoji = document.getElementById('emopanel');
      if (emoji) emoji.classList.remove('on');
      this.panel.classList.add('on');
      this.render();
      try { await this.load(false); }
      catch (error) { /* 错误已显示在面板内。 */ }
    }

    close() {
      this.panel.classList.remove('on');
      this.manage = false;
      if (this.selected === RECENT && !this.recent.length) this.selected = ALL;
      this.render();
    }

    toggle() {
      if (this.panel.classList.contains('on')) this.close();
      else this.open();
    }

    toggleManage() {
      this.manage = !this.manage;
      if (this.manage && this.selected === RECENT) this.selected = ALL;
      this.status(this.manage ? '管理修改只影响本地表情包目录。' : '');
      this.render();
    }

    selectSet(key) {
      this.selected = key;
      this.renderTabs();
      this.renderGrid();
      this.syncAdmin();
    }

    render() {
      this.renderTabs();
      this.renderGrid();
      this.syncAdmin();
    }

    renderTabs() {
      const box = this.els.skTabs;
      box.textContent = '';
      const tabs = [];
      if (!this.manage && this.recent.length) tabs.push({ key: RECENT, name: '最近使用', count: this.recent.length });
      tabs.push({ key: ALL, name: '全部', count: this.items.length });
      for (const set of this.sets) {
        tabs.push({ key: set.key || set.name, name: set.name || set.key, count: Number(set.count || 0) });
      }
      for (const tab of tabs) {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'sk-tab' + (this.selected === tab.key ? ' on' : '');
        button.textContent = `${tab.name}(${tab.count})`;
        button.addEventListener('click', () => this.selectSet(tab.key));
        box.appendChild(button);
      }
      this.els.skManage.textContent = this.manage ? '完成' : '管理';
      this.els.skManage.classList.toggle('on', this.manage);
    }

    syncAdmin() {
      this.els.skAdmin.hidden = !this.manage;
      this.els.skDeleteSet.disabled = this.busy || this.selected === ALL || this.selected === RECENT;
      this.els.skCreateSet.disabled = this.busy;
      this.els.skUpload.disabled = this.busy;
    }

    visibleItems() {
      let items;
      if (this.selected === RECENT) items = this.recent.slice();
      else {
        items = this.items.slice();
        if (this.selected !== ALL) {
          items = items.filter(item => (item.set || item.source || '默认') === this.selected);
        }
      }
      const query = this.els.skSearch.value.trim().toLowerCase();
      if (query) {
        items = items.filter(item => {
          const fields = [item.file, item.name, item.desc, ...(item.emotions || []), ...(item.tags || [])];
          return fields.some(value => String(value || '').toLowerCase().includes(query));
        });
      }
      return items;
    }

    renderGrid() {
      const grid = this.els.skGrid;
      grid.textContent = '';
      const items = this.visibleItems();
      if (this.manage) {
        const add = document.createElement('div');
        add.className = 'sk-item sk-item-add';
        add.textContent = '+';
        add.title = '添加图片';
        add.addEventListener('click', () => this.pickFiles());
        grid.appendChild(add);
      }
      if (!items.length) {
        const empty = document.createElement('div');
        empty.className = 'sk-empty';
        empty.textContent = '没有找到这个表情…';
        grid.appendChild(empty);
        return;
      }
      for (const item of items) {
        const cell = document.createElement('div');
        cell.className = 'sk-item';
        cell.title = item.name || item.desc || item.file;
        const img = document.createElement('img');
        img.src = item.url;
        img.alt = item.name || '表情';
        img.loading = 'lazy';
        cell.appendChild(img);
        cell.addEventListener('click', () => {
          if (!this.manage) this.send(item);
        });
        if (this.manage) {
          const remove = document.createElement('button');
          remove.type = 'button';
          remove.className = 'sk-delete';
          remove.textContent = '×';
          remove.title = '删除表情';
          remove.addEventListener('click', event => {
            event.stopPropagation();
            this.deleteSticker(item);
          });
          cell.appendChild(remove);
        }
        grid.appendChild(cell);
      }
    }

    async send(item) {
      if (this.sending) return;
      const current = typeof S !== 'undefined' ? S.cur : null;
      if (!current) {
        this.notify('请先选择一个角色再发送表情');
        return;
      }
      this.sending = true;
      try {
        const data = await this.post('/api/stickers/send', { slug: current.slug, file: item.file });
        this.remember(item);
        if (typeof addSticker === 'function') addSticker('user', data.url || item.url);
        this.close();
      } catch (error) {
        this.notify(error.message || '表情发送失败');
      } finally {
        this.sending = false;
      }
    }

    async deleteSticker(item) {
      if (this.busy || !confirm(`删除表情「${item.name || item.file}」？`)) return;
      this.busy = true;
      this.syncAdmin();
      this.status('正在删除表情…');
      try {
        await this.post('/api/stickers/delete', { file: item.file });
        this.recent = this.recent.filter(old => old.file !== item.file);
        this.saveRecent();
        await this.reload();
        this.status('表情已删除');
      } catch (error) {
        this.status(error.message || '删除失败');
      } finally {
        this.busy = false;
        this.syncAdmin();
      }
    }

    targetSet() {
      if (this.selected !== ALL && this.selected !== RECENT) return this.selected;
      return this.sets.length ? (this.sets[0].key || this.sets[0].name) : '默认';
    }

    pickFiles() {
      if (!this.manage || this.busy) return;
      this.els.skFile.value = '';
      this.els.skFile.click();
    }

    readFile(file) {
      return new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result || ''));
        reader.onerror = () => reject(new Error(`读取失败：${file.name}`));
        reader.readAsDataURL(file);
      });
    }

    async uploadFiles() {
      const files = Array.from(this.els.skFile.files || []);
      if (!files.length || this.busy) return;
      this.busy = true;
      this.syncAdmin();
      const target = this.targetSet();
      let uploaded = 0;
      let skipped = 0;
      let lastError = '';
      for (const file of files) {
        if (file.size > 8 * 1024 * 1024) {
          skipped += 1;
          continue;
        }
        try {
          this.status(`正在上传 ${file.name}…`);
          const data = await this.readFile(file);
          await this.post('/api/stickers/upload', { name: file.name, data, set: target });
          uploaded += 1;
        } catch (error) {
          lastError = error.message || `${file.name} 上传失败`;
        }
      }
      this.selected = target;
      try { await this.reload(); }
      catch (error) { lastError = error.message || '列表刷新失败'; }
      this.busy = false;
      this.syncAdmin();
      this.selected = target;
      this.render();
      const parts = [];
      if (uploaded) parts.push(`已上传 ${uploaded} 张到「${target}」`);
      if (skipped) parts.push(`${skipped} 张超过 8MB，已跳过`);
      if (lastError) parts.push(lastError);
      this.status(parts.join('；') || '没有可上传的图片');
    }

    async createCategory() {
      if (this.busy) return;
      const name = (prompt('新分类名称') || '').trim();
      if (!name) return;
      this.busy = true;
      this.syncAdmin();
      try {
        await this.post('/api/sticker-category/create', { name });
        this.selected = name;
        await this.reload();
        this.selected = name;
        this.render();
        this.status(`分类「${name}」已创建`);
      } catch (error) {
        this.status(error.message || '分类创建失败');
      } finally {
        this.busy = false;
        this.syncAdmin();
      }
    }

    async deleteCategory() {
      if (this.busy || this.selected === ALL || this.selected === RECENT) return;
      const name = this.selected;
      if (!confirm(`删除空分类「${name}」？`)) return;
      this.busy = true;
      this.syncAdmin();
      try {
        await this.post('/api/sticker-category/delete', { name });
        this.selected = ALL;
        await this.reload();
        this.status(`分类「${name}」已删除`);
      } catch (error) {
        this.status(error.message || '分类删除失败；只能删除空分类');
      } finally {
        this.busy = false;
        this.syncAdmin();
      }
    }
  }

  function boot() {
    const panel = document.querySelector('[data-sticker-panel]');
    if (panel) window.stickerPanel = new StickerPanelController(panel);
  }
  window.toggleStickers = function () {
    if (window.stickerPanel) window.stickerPanel.toggle();
  };
  window.openStickerPick = function () {
    if (window.stickerPanel) window.stickerPanel.open();
  };
  window.closeStk = function () {
    if (window.stickerPanel) window.stickerPanel.close();
  };
  window.renderStickers = function () {
    if (window.stickerPanel) window.stickerPanel.render();
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
