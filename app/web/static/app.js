// -*- coding: utf-8 -*-
// 全局前端助手：被各页面内联脚本复用，必须在 app.js 中定义（base.html 先于页面脚本加载）

// 转义 HTML 特殊字符，防止 XSS（用户内容渲染到 innerHTML 前必须转义）
function escapeHtml(str) {
    if (str === null || str === undefined) return '';
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

// 通用 JSON GET 封装（带错误兜底）
async function apiGet(url) {
    const r = await fetch(url);
    return r.json();
}

// 通用 JSON POST 封装
async function apiPost(url, body) {
    const r = await fetch(url, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body || {}),
    });
    return r.json();
}
