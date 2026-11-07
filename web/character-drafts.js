/* 添加角色：草稿生成/导入、编辑、独立试聊、显式提交与本地头像裁剪。 */
(function () {
  'use strict';

  class CharacterDraftController {
    constructor(modal) {
      this.modal = modal;
      this.draft = null;
      this.avatar = '';
      this.trial = [];
      this.dirty = false;
      this.busy = '';
      this.requestSeq = 0;
      this.crop = null;
      this.cropDrag = null;
      this.els = {};
      const ids = [
        'cdPreview', 'cdPickAvatar', 'cdAvatarInput', 'cdCardInput', 'cdName',
        'cdImport', 'cdGenerate', 'cdPreset', 'cdPresetDownload', 'cdPresetUse',
        'cdStatus', 'cdEditor', 'cdSeries', 'cdAliases', 'cdSummary', 'cdInterests', 'cdGender',
        'cdPersona', 'cdRevision', 'cdSave', 'cdExport', 'cdTrialLog', 'cdTrialInput',
        'cdTrialSend', 'cdCancel', 'cdCommit', 'cdClose', 'cdCropMask', 'cdCropStage',
        'cdCropImg', 'cdCropZoom', 'cdCropRepick', 'cdCropCancel', 'cdCropConfirm'
      ];
      for (const id of ids) this.els[id] = document.getElementById(id);
      this.populatePresets();
      this.bind();
      this.reset();
    }

    populatePresets() {
      const select = this.els.cdPreset;
      const presets = window.STARLIT_CHARACTER_PRESETS || {};
      select.textContent = '';
      const empty = document.createElement('option');
      empty.value = '';
      empty.textContent = '选择角色卡预设…';
      select.appendChild(empty);
      for (const [key, preset] of Object.entries(presets)) {
        const option = document.createElement('option');
        option.value = key;
        option.textContent = preset.label || key;
        select.appendChild(option);
      }
    }

    bind() {
      const e = this.els;
      e.cdClose.addEventListener('click', () => this.close());
      e.cdCancel.addEventListener('click', () => this.close());
      e.cdPickAvatar.addEventListener('click', () => this.pickAvatar());
      e.cdPreview.addEventListener('click', () => this.pickAvatar());
      e.cdAvatarInput.addEventListener('change', () => {
        const file = e.cdAvatarInput.files && e.cdAvatarInput.files[0];
        if (file) this.startCrop(file);
      });
      e.cdImport.addEventListener('click', () => {
        e.cdCardInput.value = '';
        e.cdCardInput.click();
      });
      e.cdCardInput.addEventListener('change', () => {
        const file = e.cdCardInput.files && e.cdCardInput.files[0];
        if (file) this.importFile(file);
      });
      e.cdGenerate.addEventListener('click', () => this.generate());
      e.cdPresetUse.addEventListener('click', () => this.usePreset());
      e.cdPresetDownload.addEventListener('click', () => this.downloadPreset());
      e.cdSave.addEventListener('click', () => this.saveDraft().catch(() => {
        // 错误已显示在状态行，这里只避免未处理的 Promise rejection。
      }));
      e.cdExport.addEventListener('click', () => this.exportDraft());
      e.cdTrialSend.addEventListener('click', () => this.sendTrial());
      e.cdCommit.addEventListener('click', () => this.commit());
      for (const el of [e.cdName, e.cdSeries, e.cdAliases, e.cdSummary, e.cdInterests, e.cdPersona]) {
        el.addEventListener('input', () => this.markDirty());
      }
      e.cdName.addEventListener('keydown', (event) => {
        if (event.key === 'Enter' && !this.draft && !event.isComposing) {
          event.preventDefault();
          this.generate();
        }
      });
      e.cdTrialInput.addEventListener('keydown', (event) => {
        if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
          event.preventDefault();
          this.sendTrial();
        }
      });
      this.modal.addEventListener('click', (event) => {
        if (event.target === this.modal) this.close();
      });
      e.cdCropCancel.addEventListener('click', () => this.closeCrop());
      e.cdCropRepick.addEventListener('click', () => {
        e.cdAvatarInput.value = '';
        e.cdAvatarInput.click();
      });
      e.cdCropConfirm.addEventListener('click', () => this.confirmCrop());
      e.cdCropZoom.addEventListener('input', () => this.setZoom(Number(e.cdCropZoom.value) / 100));
      e.cdCropMask.addEventListener('click', (event) => {
        if (event.target === e.cdCropMask) this.closeCrop();
      });
      e.cdCropStage.addEventListener('pointerdown', (event) => this.startDrag(event));
      e.cdCropStage.addEventListener('pointermove', (event) => this.moveDrag(event));
      e.cdCropStage.addEventListener('pointerup', (event) => this.endDrag(event));
      e.cdCropStage.addEventListener('pointercancel', (event) => this.endDrag(event));
      e.cdCropStage.addEventListener('wheel', (event) => {
        if (!this.crop) return;
        event.preventDefault();
        this.setZoom(this.crop.zoom * (event.deltaY < 0 ? 1.12 : 1 / 1.12));
      }, { passive: false });
      window.addEventListener('resize', () => this.layoutCrop());
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

    patch(path, body) {
      return this.request(path, {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
      });
    }

    setStatus(text, kind) {
      const el = this.els.cdStatus;
      el.textContent = text || '';
      el.classList.toggle('busy', kind === 'busy');
      el.classList.toggle('error', kind === 'error');
    }

    setBusy(kind, message) {
      this.busy = kind || '';
      if (message) this.setStatus(message, 'busy');
      this.syncControls();
    }

    syncControls() {
      const e = this.els;
      const busy = !!this.busy;
      const hasDraft = !!this.draft;
      for (const el of [e.cdPickAvatar, e.cdImport, e.cdGenerate, e.cdName, e.cdPreset,
        e.cdPresetDownload, e.cdPresetUse, e.cdSeries, e.cdAliases, e.cdSummary,
        e.cdInterests, e.cdPersona, e.cdTrialInput]) el.disabled = busy;
      e.cdSave.disabled = busy || !hasDraft || !this.dirty;
      e.cdExport.disabled = busy || !hasDraft;
      e.cdTrialSend.disabled = busy || !hasDraft;
      e.cdCommit.disabled = busy || !hasDraft || !this.avatar;
      e.cdGenerate.textContent = this.busy === 'generate' ? '正在生成草稿…' : '联网生成草稿';
      e.cdImport.textContent = this.busy === 'import' ? '正在导入…' : '导入 JSON 角色卡';
      e.cdSave.textContent = this.busy === 'save' ? '保存中…' : '保存修改';
      e.cdTrialSend.textContent = this.busy === 'chat' ? '等待回复…' : '发送';
      e.cdCommit.textContent = this.busy === 'commit' ? '正在正式添加…' : '确认添加角色';
    }

    reset() {
      this.requestSeq += 1;
      this.draft = null;
      this.avatar = '';
      this.trial = [];
      this.dirty = false;
      this.busy = '';
      const e = this.els;
      for (const el of [e.cdName, e.cdSeries, e.cdAliases, e.cdSummary, e.cdInterests,
        e.cdPersona, e.cdTrialInput]) el.value = '';
      e.cdAvatarInput.value = '';
      e.cdCardInput.value = '';
      e.cdPreset.value = '';
      e.cdEditor.hidden = true;
      e.cdRevision.textContent = '草稿 revision 1';
      this.setStatus('草稿与试聊不会写入正式角色、聊天或记忆。');
      this.renderAvatar();
      this.renderTrial();
      this.syncControls();
    }

    async open() {
      this.closeCrop();
      this.reset();
      this.modal.classList.add('on');
      const seq = this.requestSeq;
      setTimeout(() => this.els.cdName.focus(), 60);
      try {
        const data = await this.request('/api/character-drafts');
        if (seq !== this.requestSeq) return;
        const drafts = Array.isArray(data.drafts) ? data.drafts : [];
        if (drafts.length) {
          this.applyDraft(drafts[0]);
          this.setStatus(`已恢复「${drafts[0].name}」的未完成草稿；试聊上下文在重启后已清空。`);
        }
      } catch (error) {
        if (seq === this.requestSeq) this.setStatus(error.message || '草稿读取失败', 'error');
      }
    }

    close() {
      this.requestSeq += 1;
      this.closeCrop();
      this.modal.classList.remove('on');
      this.reset();
    }

    renderAvatar() {
      const box = this.els.cdPreview;
      box.textContent = '';
      if (this.avatar) {
        const img = document.createElement('img');
        img.alt = '角色头像预览';
        img.src = this.avatar;
        box.appendChild(img);
      } else {
        box.textContent = '?';
      }
    }

    renderTrial() {
      const box = this.els.cdTrialLog;
      box.textContent = '';
      if (!this.trial.length) {
        const empty = document.createElement('div');
        empty.className = 'cd-trial-empty';
        empty.textContent = '先说一句你好，看看她的语气是否合适。';
        box.appendChild(empty);
        return;
      }
      for (const turn of this.trial) {
        const row = document.createElement('div');
        row.className = 'cd-trial-row ' + (turn.role === 'user' ? 'from-user' : 'from-character');
        const bubble = document.createElement('div');
        bubble.className = 'cd-trial-bubble';
        bubble.textContent = String(turn.text || '');
        row.appendChild(bubble);
        box.appendChild(row);
      }
      box.scrollTop = box.scrollHeight;
    }

    applyDraft(draft) {
      this.draft = Object.assign({}, draft);
      this.avatar = draft.avatar || '';
      this.trial = Array.isArray(draft.trial) ? draft.trial : [];
      this.dirty = false;
      const e = this.els;
      e.cdName.value = draft.name || '';
      e.cdSeries.value = draft.series || '';
      e.cdGender.value = draft.gender || '';
      e.cdAliases.value = (draft.aliases || []).join('、');
      e.cdSummary.value = draft.summary || '';
      e.cdInterests.value = (draft.interests || []).join('、');
      e.cdPersona.value = draft.persona_md || '';
      e.cdRevision.textContent = `草稿 revision ${draft.revision}`;
      e.cdEditor.hidden = false;
      this.renderAvatar();
      this.renderTrial();
      this.syncControls();
    }

    markDirty() {
      if (!this.draft) return;
      this.dirty = true;
      this.els.cdRevision.textContent = `草稿 revision ${this.draft.revision} · 有未保存修改`;
      this.syncControls();
    }

    splitList(value) {
      return String(value || '').split(/[、，,\n]/).map((item) => item.trim())
        .filter(Boolean).slice(0, 8);
    }

    changes() {
      const e = this.els;
      return {
        name: e.cdName.value.trim(), series: e.cdSeries.value.trim(),
        gender: e.cdGender.value || 'other',
        aliases: this.splitList(e.cdAliases.value), summary: e.cdSummary.value.trim(),
        interests: this.splitList(e.cdInterests.value), persona_md: e.cdPersona.value.trim(),
        avatar: this.avatar
      };
    }

    async saveDraft(options) {
      const quiet = !!(options && options.quiet);
      if (!this.draft || !this.dirty) return this.draft;
      const seq = this.requestSeq;
      if (!quiet) this.setBusy('save', '正在保存草稿修改…');
      try {
        const data = await this.patch('/api/character-drafts', Object.assign({
          draft_id: this.draft.id, revision: this.draft.revision
        }, this.changes()));
        if (seq !== this.requestSeq) throw new Error('添加角色面板已关闭');
        this.applyDraft(data.draft);
        if (!quiet) this.setStatus('修改已保存；试聊上下文已按新内容重新开始。');
        return data.draft;
      } catch (error) {
        if (!quiet && seq === this.requestSeq) this.setStatus(error.message || '保存失败', 'error');
        throw error;
      } finally {
        if (!quiet && seq === this.requestSeq) this.setBusy('');
      }
    }

    async generate() {
      if (this.busy) return;
      const name = this.els.cdName.value.trim();
      if (!name) {
        this.setStatus('请先填写角色名。', 'error');
        this.els.cdName.focus();
        return;
      }
      const seq = this.requestSeq;
      this.setBusy('generate', '正在搜索资料并生成草稿；此阶段不会创建正式角色。');
      try {
        const data = await this.post('/api/character-drafts/generate',
          { name, avatar: this.avatar, gender: this.els.cdGender.value || '' });
        if (seq !== this.requestSeq) return;
        this.applyDraft(data.draft);
        this.setStatus('草稿已生成。可以修改人设、先试聊，再决定是否正式添加。');
      } catch (error) {
        if (seq === this.requestSeq) this.setStatus(error.message || '生成失败，请重试', 'error');
      } finally {
        if (seq === this.requestSeq) this.setBusy('');
      }
    }

    async importFile(file) {
      if (this.busy) return;
      if (file.size > 12 * 1024 * 1024) {
        this.setStatus('角色卡文件太大（上限 12MB）。', 'error');
        return;
      }
      const seq = this.requestSeq;
      this.setBusy('import', '正在校验并导入为草稿…');
      try {
        const card = JSON.parse(await file.text());
        const data = await this.post('/api/character-drafts/import', { card });
        if (seq !== this.requestSeq) return;
        this.applyDraft(data.draft);
        this.setStatus('角色卡已导入为草稿，尚未创建正式角色。请选择头像并检查内容。');
      } catch (error) {
        if (seq === this.requestSeq) {
          this.setStatus(error instanceof SyntaxError ? 'JSON 格式错误，请检查角色卡文件。' : error.message, 'error');
        }
      } finally {
        if (seq === this.requestSeq) this.setBusy('');
      }
    }

    selectedPreset() {
      const key = this.els.cdPreset.value;
      const presets = window.STARLIT_CHARACTER_PRESETS || {};
      if (!key || !presets[key]) {
        this.setStatus('请先选择一个角色卡预设。', 'error');
        return null;
      }
      return presets[key];
    }

    async usePreset() {
      if (this.busy) return;
      const preset = this.selectedPreset();
      if (!preset) return;
      const seq = this.requestSeq;
      this.setBusy('import', '正在把预设载入为草稿；不会调用模型。');
      try {
        const data = await this.post('/api/character-drafts/import', { card: preset.card });
        if (seq !== this.requestSeq) return;
        this.applyDraft(data.draft);
        this.setStatus('预设已载入为草稿；请修改人设、选择头像，再试聊或正式添加。');
      } catch (error) {
        if (seq === this.requestSeq) this.setStatus(error.message || '预设载入失败', 'error');
      } finally {
        if (seq === this.requestSeq) this.setBusy('');
      }
    }

    cardPayload(draft) {
      return {
        spec: 'chara_card_v2', spec_version: '2.0',
        data: {
          name: draft.name || '', description: draft.persona_md || '',
          personality: '', scenario: draft.series || '', first_mes: '', mes_example: '',
          persona_md: draft.persona_md || '', series: draft.series || '',
          aliases: draft.aliases || [], summary: draft.summary || '',
          interests: draft.interests || [], avatar: draft.avatar || this.avatar || ''
        }
      };
    }

    downloadJson(payload, filename) {
      const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json;charset=utf-8' });
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = filename;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    }

    downloadPreset() {
      const preset = this.selectedPreset();
      if (!preset) return;
      const card = preset.card;
      this.downloadJson(this.cardPayload(card), `${card.name || '角色'}-角色卡.json`);
      this.setStatus('所选 JSON 已下载，可修改后通过「导入 JSON 角色卡」载入。');
    }

    async exportDraft() {
      if (!this.draft || this.busy) return;
      try {
        const draft = await this.saveDraft({ quiet: true });
        const current = draft || this.draft;
        this.downloadJson(this.cardPayload(current), `${current.name || '角色'}-角色卡.json`);
        this.setStatus('JSON 角色卡已导出，可用于备份或再次导入。');
      } catch (error) {
        this.setStatus('导出失败：' + (error.message || error), 'error');
      }
    }

    async sendTrial() {
      if (this.busy || !this.draft) return;
      const text = this.els.cdTrialInput.value.trim();
      if (!text) return;
      const seq = this.requestSeq;
      this.setBusy('chat', '正在保存最新人设并准备独立试聊上下文…');
      try {
        await this.saveDraft({ quiet: true });
        if (seq !== this.requestSeq) return;
        const data = await this.post('/api/character-drafts/chat', {
          draft_id: this.draft.id, revision: this.draft.revision, text
        });
        if (seq !== this.requestSeq) return;
        this.trial = Array.isArray(data.trial) ? data.trial : [];
        this.els.cdTrialInput.value = '';
        this.renderTrial();
        this.setStatus('试聊只用于当前草稿，不会出现在正式聊天、记忆或状态中。');
      } catch (error) {
        if (seq === this.requestSeq) this.setStatus(error.message || '试聊失败，请重试', 'error');
      } finally {
        if (seq === this.requestSeq) this.setBusy('');
      }
    }

    async commit() {
      if (this.busy || !this.draft) return;
      const seq = this.requestSeq;
      this.setBusy('commit', '正在校验并正式写入角色数据…');
      try {
        await this.saveDraft({ quiet: true });
        if (!this.avatar) throw new Error('正式添加前请选择头像');
        const data = await this.post('/api/character-drafts/commit', {
          draft_id: this.draft.id, revision: this.draft.revision
        });
        if (seq !== this.requestSeq) return;
        const name = (data.character && data.character.name) || this.draft.name;
        this.close();
        document.dispatchEvent(new CustomEvent('character-committed', { detail: data }));
        if (typeof window.toast === 'function') window.toast(`「${name}」已正式添加`);
      } catch (error) {
        if (seq === this.requestSeq) this.setStatus(error.message || '添加失败，请重试', 'error');
      } finally {
        if (seq === this.requestSeq) this.setBusy('');
      }
    }

    pickAvatar() {
      if (this.busy) return;
      this.els.cdAvatarInput.value = '';
      this.els.cdAvatarInput.click();
    }

    startCrop(file) {
      if (!/^image\//.test(file.type || '')) {
        this.setStatus('请选择图片文件。', 'error');
        return;
      }
      if (file.size > 8 * 1024 * 1024) {
        this.setStatus('头像太大（上限 8MB）。', 'error');
        return;
      }
      this.closeCrop();
      const url = URL.createObjectURL(file);
      const img = new Image();
      const e = this.els;
      e.cdCropZoom.value = '100';
      e.cdCropMask.classList.add('on');
      img.onload = () => {
        const stage = e.cdCropStage;
        this.crop = {
          url, img, stageW: stage.clientWidth || 280, stageH: stage.clientHeight || 280,
          base: 1, zoom: 1, x: 0, y: 0
        };
        e.cdCropImg.src = url;
        e.cdCropImg.draggable = false;
        this.layoutCrop();
      };
      img.onerror = () => {
        URL.revokeObjectURL(url);
        this.closeCrop();
        this.setStatus('图片读取失败，请换一张。', 'error');
      };
      img.src = url;
    }

    layoutCrop() {
      const c = this.crop;
      if (!c) return;
      const stage = this.els.cdCropStage;
      const width = stage.clientWidth;
      const height = stage.clientHeight;
      if (width && height) {
        c.stageW = width;
        c.stageH = height;
      }
      const iw = c.img.naturalWidth || 1;
      const ih = c.img.naturalHeight || 1;
      c.base = Math.max(c.stageW / iw, c.stageH / ih) || 1;
      const scale = c.base * c.zoom;
      const drawW = iw * scale;
      const drawH = ih * scale;
      const maxX = Math.max(0, (drawW - c.stageW) / 2);
      const maxY = Math.max(0, (drawH - c.stageH) / 2);
      c.x = Math.max(-maxX, Math.min(maxX, c.x));
      c.y = Math.max(-maxY, Math.min(maxY, c.y));
      const img = this.els.cdCropImg;
      img.style.width = drawW + 'px';
      img.style.height = drawH + 'px';
      img.style.left = (c.stageW / 2 + c.x - drawW / 2) + 'px';
      img.style.top = (c.stageH / 2 + c.y - drawH / 2) + 'px';
    }

    setZoom(value) {
      if (!this.crop) return;
      this.crop.zoom = Math.max(1, Math.min(4, value));
      this.els.cdCropZoom.value = String(Math.round(this.crop.zoom * 100));
      this.layoutCrop();
    }

    startDrag(event) {
      if (!this.crop) return;
      event.preventDefault();
      this.cropDrag = {
        id: event.pointerId, startX: event.clientX, startY: event.clientY,
        oldX: this.crop.x, oldY: this.crop.y
      };
      this.els.cdCropStage.classList.add('dragging');
      try { this.els.cdCropStage.setPointerCapture(event.pointerId); } catch (error) { /* 浏览器不支持时仍可拖动 */ }
    }

    moveDrag(event) {
      if (!this.crop || !this.cropDrag || this.cropDrag.id !== event.pointerId) return;
      this.crop.x = this.cropDrag.oldX + event.clientX - this.cropDrag.startX;
      this.crop.y = this.cropDrag.oldY + event.clientY - this.cropDrag.startY;
      this.layoutCrop();
    }

    endDrag(event) {
      if (!this.cropDrag || (event && this.cropDrag.id !== event.pointerId)) return;
      this.cropDrag = null;
      this.els.cdCropStage.classList.remove('dragging');
    }

    confirmCrop() {
      const c = this.crop;
      if (!c) return;
      const scale = c.base * c.zoom;
      const drawW = c.img.naturalWidth * scale;
      const left = c.stageW / 2 + c.x - drawW / 2;
      const top = c.stageH / 2 + c.y - c.img.naturalHeight * scale / 2;
      const side = c.stageW / scale;
      const sourceX = -left / scale;
      const sourceY = -top / scale;
      const canvas = document.createElement('canvas');
      canvas.width = 256;
      canvas.height = 256;
      const context = canvas.getContext('2d');
      context.fillStyle = '#fff';
      context.fillRect(0, 0, 256, 256);
      context.imageSmoothingQuality = 'high';
      context.drawImage(c.img, sourceX, sourceY, side, side, 0, 0, 256, 256);
      this.avatar = canvas.toDataURL('image/png');
      this.closeCrop();
      this.renderAvatar();
      this.markDirty();
      this.setStatus(this.draft ? '头像已裁剪，保存修改后会用于正式添加。' : '头像已裁剪为 256×256 PNG。');
    }

    closeCrop() {
      const crop = this.crop;
      this.crop = null;
      this.cropDrag = null;
      if (crop && crop.url) URL.revokeObjectURL(crop.url);
      const e = this.els;
      if (e.cdCropMask) e.cdCropMask.classList.remove('on');
      if (e.cdCropImg) {
        e.cdCropImg.removeAttribute('src');
        e.cdCropImg.removeAttribute('style');
      }
    }
  }

  function boot() {
    const modal = document.querySelector('[data-character-modal]');
    if (modal) window.characterDrafts = new CharacterDraftController(modal);
  }
  window.openAdd = function () {
    if (window.characterDrafts) window.characterDrafts.open();
  };
  window.closeAdd = function () {
    if (window.characterDrafts) window.characterDrafts.close();
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
