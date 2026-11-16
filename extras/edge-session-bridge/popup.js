const statusEl = document.getElementById('status');

function show(message, error = false) {
  statusEl.textContent = message;
  statusEl.classList.toggle('error', error);
}

document.getElementById('send').addEventListener('click', async () => {
  const token = document.getElementById('token').value.trim();
  if (!token) {
    show('请粘贴一次性配对码。', true);
    return;
  }
  show('正在读取 bilibili.com…');
  try {
    const cookie = await chrome.cookies.get({
      url: 'https://www.bilibili.com/',
      name: 'SESSDATA'
    });
    if (!cookie || !cookie.value) {
      throw new Error('Edge 中没有检测到 Bilibili 登录');
    }
    const response = await fetch('http://127.0.0.1:48129/biliyt-session', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      cache: 'no-store',
      body: JSON.stringify({token, sessdata: cookie.value})
    });
    if (!response.ok) {
      throw new Error(response.status === 403 ? '配对码错误或已过期' : `本机接收失败 (${response.status})`);
    }
    document.getElementById('token').value = '';
    show('导入成功。回到 BiliYTPlayer 点击“重试”。');
  } catch (error) {
    show(error.message || '导入失败', true);
  }
});
