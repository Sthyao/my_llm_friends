const backupPage = (() => {
  const receiptKey = 'thought-journal-backup-receipt';
  const channel = typeof BroadcastChannel === 'function' ? new BroadcastChannel('thought-journal-restore') : null;
  let pending = null;
  let status = null;

  function render() {
    return `<div class="backup-panel" id="backup-panel">
      <p class="card backup-status" role="status" aria-live="polite" hidden></p>
      <div class="backup-grid">
        <section class="card">
          <h2>导出备份</h2>
          <p>将会话、记忆、人格、运维配置和调用记录打包成 ZIP。备份含私人对话，API 密钥留在本机。</p>
          <button type="button" class="btn primary" data-backup-export>下载备份</button>
        </section>
        <section class="card">
          <h2>导入备份</h2>
          <p>导入会替换本机数据，并先备份当前数据。API 的名称、类型和地址一致时会沿用本机已配置的密钥。</p>
          <form class="form-grid" data-backup-import>
            <label class="field"><span>备份文件（ZIP）</span><input name="archive" type="file"
              accept=".zip,application/zip,application/x-zip-compressed" required></label>
            <button type="submit" class="btn danger" disabled>导入并恢复</button>
          </form>
        </section>
      </div>
    </div>`;
  }

  function update() {
    const panel = document.getElementById('backup-panel');
    if (!panel) return;
    const report = panel.querySelector('.backup-status');
    report.hidden = !status;
    report.textContent = status?.text ?? '';
    report.setAttribute('role', status?.error ? 'alert' : 'status');
    report.classList.toggle('inline-error', Boolean(status?.error));
    panel.setAttribute('aria-busy', String(Boolean(pending)));
    const exportButton = panel.querySelector('[data-backup-export]');
    exportButton.disabled = Boolean(pending);
    exportButton.textContent = pending === 'export' ? '正在打包…' : '下载备份';
    const form = panel.querySelector('[data-backup-import]');
    form.elements.archive.disabled = Boolean(pending);
    const importButton = form.querySelector('button[type="submit"]');
    importButton.disabled = Boolean(pending) || !form.elements.archive.files.length;
    importButton.textContent = pending === 'import' ? '正在恢复…' : '导入并恢复';
  }

  async function backupRequest(action, archive) {
    const response = await fetch('/api/backup/' + action, archive ? {
      method: 'POST',
      headers: { 'Content-Type': 'application/zip' },
      body: archive,
    } : undefined);
    if (!response.ok) {
      const detail = await response.json().catch(() => null);
      throw new Error(typeof detail?.detail === 'string'
        ? detail.detail : '备份请求失败（' + response.status + '）');
    }
    return response;
  }

  function downloadFilename(response) {
    const header = response.headers.get('Content-Disposition') ?? '';
    const extended = /filename\*=UTF-8''([^;]+)/i.exec(header)?.[1];
    let filename = /filename=(?:"([^"]+)"|([^;]+))/i.exec(header)?.slice(1).find(Boolean);
    if (extended) {
      try { filename = decodeURIComponent(extended); } catch { /* Use the regular filename. */ }
    }
    return filename?.replace(/[\\/\u0000-\u001f]+/g, '_').trim() || 'thought-journal-backup.zip';
  }

  async function exportBackup() {
    if (pending) return;
    pending = 'export';
    status = { text: '正在打包备份…' };
    update();
    try {
      const response = await backupRequest('export');
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement('a');
      link.href = url;
      link.download = downloadFilename(response);
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 30_000);
      status = { text: '备份已生成，请查看浏览器下载。' };
    } catch (error) {
      status = { text: error.message || '导出失败。', error: true };
    } finally {
      pending = null;
      update();
    }
  }

  function refreshAfterRestore() {
    history.replaceState(null, '', '#ops/backup');
    location.reload();
  }

  async function importBackup(form) {
    const archive = form.elements.archive.files[0];
    if (!archive || pending) return;
    if (!confirm(`导入「${archive.name}」会替换想法簿的本机数据，清空输入框里尚未发送的文字。当前数据会先备份一份。继续导入？`)) return;
    pending = 'import';
    status = { text: '正在检查并恢复备份…' };
    update();
    try {
      const result = await (await backupRequest('import', archive)).json();
      const { sessions, messages } = result.restored ?? {};
      const detail = Number.isInteger(sessions) && Number.isInteger(messages)
        ? `已恢复 ${sessions} 个会话和 ${messages} 条消息。` : '备份已恢复。';
      const receipt = typeof result.safety_backup === 'string' && result.safety_backup
        ? detail + ' 导入前备份为「' + result.safety_backup + '」。' : detail;
      try { sessionStorage.setItem(receiptKey, receipt); } catch { /* Restore still succeeded. */ }
      channel?.postMessage('restored');
      refreshAfterRestore();
    } catch (error) {
      status = { text: error.message || '导入失败。', error: true };
      pending = null;
      update();
    }
  }

  function mount() {
    const panel = document.getElementById('backup-panel');
    if (!panel) return;
    try {
      const receipt = sessionStorage.getItem(receiptKey);
      if (receipt) {
        status = { text: receipt };
        sessionStorage.removeItem(receiptKey);
      }
    } catch { /* Storage can be unavailable in restricted browsers. */ }
    panel.querySelector('[data-backup-export]').addEventListener('click', exportBackup);
    const form = panel.querySelector('[data-backup-import]');
    form.elements.archive.addEventListener('change', update);
    form.addEventListener('submit', event => {
      event.preventDefault();
      void importBackup(form);
    });
    update();
  }

  channel?.addEventListener('message', event => {
    if (event.data === 'restored') refreshAfterRestore();
  });
  return { render, mount };
})();
