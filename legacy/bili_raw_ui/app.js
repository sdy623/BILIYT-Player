(() => {
  const $ = (id) => document.getElementById(id);
  const elements = {};
  let running = false;

  const timestamp = () => new Date().toLocaleTimeString('zh-CN', {
    hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false
  });

  function appendLog(message, tone = '') {
    const row = document.createElement('p');
    const time = document.createElement('time');
    const copy = document.createElement('span');
    time.textContent = timestamp();
    copy.textContent = message;
    if (tone) copy.className = tone;
    row.append(time, copy);
    elements.log.append(row);
    elements.log.scrollTop = elements.log.scrollHeight;
  }

  function setRunning(value) {
    running = value;
    elements.download.disabled = value;
    elements.browse.disabled = value;
    elements.source.readOnly = value;
    elements.output.readOnly = value;
    elements.download.querySelector('span').textContent = value
      ? '正在下载与封装…'
      : '开始下载';
  }

  function setProgress(payload) {
    const percent = payload.percent ?? 0;
    elements.progress.classList.toggle('indeterminate', Boolean(payload.indeterminate));
    elements.fill.style.width = payload.indeterminate ? '' : `${percent}%`;
    elements.progress.setAttribute('aria-valuenow', String(percent));
    elements.metric.textContent = payload.indeterminate ? '···' : `${percent.toFixed(1)}%`;
    elements.status.textContent = payload.message;
    elements.detail.textContent = payload.indeterminate ? '正在处理，请稍候' : '多连接续传与完整性检查进行中';
    elements.size.textContent = payload.currentLabel
      ? `${payload.currentLabel}${payload.totalLabel ? ` / ${payload.totalLabel}` : ''}`
      : '';
  }

  async function initialize() {
    const state = await window.pywebview.api.get_initial_state();
    elements.source.value = state.source || '';
    elements.output.value = state.output || '';
    if (state.source) appendLog('已从剪贴板或命令行识别哔哩哔哩链接。');
    if (state.autoStart) elements.form.requestSubmit();
    else elements.source.focus({ preventScroll: true });
  }

  async function startDownload(event) {
    event.preventDefault();
    if (running) return;
    elements.error.textContent = '';
    const response = await window.pywebview.api.start_download(
      elements.source.value.trim(), elements.output.value.trim()
    );
    if (!response.accepted) {
      elements.error.textContent = response.error;
      return;
    }
    setRunning(true);
    elements.open.disabled = true;
    appendLog('正在读取视频信息，并选择实际可用的最高质量视频和音频。');
    setProgress({ message: '正在读取登录状态与最高质量原流', indeterminate: true });
  }

  async function chooseDirectory() {
    const selected = await window.pywebview.api.choose_output_dir(elements.output.value.trim());
    if (selected) elements.output.value = selected;
  }

  async function openResult() {
    const response = await window.pywebview.api.open_result();
    if (!response.opened && response.error) elements.error.textContent = response.error;
  }

  window.BiliDownloader = {
    onReport(payload) {
      setProgress(payload);
      if (payload.messageChanged) appendLog(payload.message);
    },
    onError(payload) {
      setRunning(false);
      elements.error.textContent = payload.error;
      elements.status.textContent = '下载未完成';
      elements.detail.textContent = '已保留可续传分片；修正问题后可直接重试';
      elements.metric.textContent = 'ERR';
      elements.progress.classList.remove('indeterminate');
      elements.fill.style.width = '0';
      appendLog(`失败：${payload.error}`, 'error');
    },
    onComplete(payload) {
      setRunning(false);
      elements.open.disabled = false;
      elements.progress.classList.remove('indeterminate');
      elements.fill.style.width = '100%';
      elements.progress.setAttribute('aria-valuenow', '100');
      elements.metric.textContent = '100%';
      if (payload.kind === 'collection') {
        elements.status.textContent = `合集完成：成功 ${payload.success}，失败 ${payload.failed}`;
        elements.detail.textContent = payload.output;
        appendLog(`${payload.title}：成功 ${payload.success}，失败 ${payload.failed}`, payload.failed ? 'error' : 'success');
        for (const item of payload.failures) appendLog(`失败 ${item.bvid} ${item.title}：${item.error}`, 'error');
      } else {
        elements.status.textContent = `已验证：${payload.mode}`;
        elements.detail.textContent = payload.output;
        appendLog(`成品验证通过：${payload.mode}`, 'success');
        for (const track of payload.tracks) appendLog(`音轨 ${track.index}：${track.title} · ${track.details}`, 'success');
      }
    }
  };

  document.addEventListener('DOMContentLoaded', () => {
    Object.assign(elements, {
      form: $('download-form'), source: $('source-input'), output: $('output-input'),
      browse: $('browse-button'), download: $('download-button'), open: $('open-button'),
      error: $('form-error'), progress: $('progress-bar'), fill: $('progress-fill'),
      metric: $('progress-metric'), status: $('status-title'), detail: $('progress-detail'),
      size: $('progress-size'), log: $('activity-log')
    });
    elements.form.addEventListener('submit', startDownload);
    elements.browse.addEventListener('click', chooseDirectory);
    elements.open.addEventListener('click', openResult);
    window.addEventListener('pywebviewready', initialize, { once: true });
  });
})();
