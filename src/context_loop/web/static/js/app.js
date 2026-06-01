/* Context Loop Dashboard — Main JS */

// Markdown 렌더링 — data-markdown 속성이 있는 엘리먼트의 원본 텍스트를
// (인접한 .md-source 또는 data-markdown-src 로 지정된 요소에서) 읽어
// marked + DOMPurify 로 안전하게 HTML 로 치환한다. HTMX 스왑 후에도
// 자동으로 다시 적용된다.
function _findMdSource(el) {
    var srcId = el.dataset.markdownSrc;
    if (srcId) {
        var byId = document.getElementById(srcId);
        if (byId) return byId;
    }
    var sib = el.nextElementSibling;
    if (sib && sib.classList && sib.classList.contains("md-source")) {
        return sib;
    }
    // Fallback: 같은 부모의 첫 .md-source.
    var parent = el.parentElement;
    if (parent) {
        var inParent = parent.querySelector(":scope > .md-source");
        if (inParent) return inParent;
    }
    return null;
}

function _applyRendered(el, html) {
    if (typeof window.DOMPurify !== "undefined") {
        html = window.DOMPurify.sanitize(html);
    }
    el.innerHTML = html;
    el.dataset.mdRendered = "1";
}

function renderMarkdownTarget(el) {
    if (!el || el.dataset.mdRendered === "1") return;
    if (typeof window.marked === "undefined" || typeof window.marked.parse !== "function") {
        return;
    }
    var srcEl = _findMdSource(el);
    if (!srcEl) return;
    var raw = srcEl.textContent || "";
    window.marked.setOptions({ gfm: true, breaks: false });
    var html;
    try {
        html = window.marked.parse(raw);
    } catch (e) {
        console.error("marked.parse failed", e);
        return;
    }
    // marked v15+ 비동기 모드: Promise 가 반환될 수 있다.
    if (html && typeof html.then === "function") {
        html.then(function(resolved) { _applyRendered(el, resolved); }).catch(function(e) {
            console.error("marked.parse(async) failed", e);
        });
        return;
    }
    _applyRendered(el, html);
}

function renderAllMarkdown(root) {
    var scope = root || document;
    var targets = scope.querySelectorAll ? scope.querySelectorAll("[data-markdown]") : [];
    targets.forEach(renderMarkdownTarget);
    renderAllHtml(scope);
}

// HTML 렌더링 — data-html 속성이 있는 엘리먼트의 원본 HTML 을 (data-html-src
// 로 지정된 .html-source 요소에서) 읽어 DOMPurify 로 sanitize 후 삽입한다.
// 이미 HTML 인 콘텐츠(예: 마크다운 변환 실패 시 Confluence 원본 HTML 폴백)를
// 마크다운 파싱 없이 안전하게 렌더하는 경로.
function renderHtmlTarget(el) {
    if (!el || el.dataset.htmlRendered === "1") return;
    var srcId = el.dataset.htmlSrc;
    var srcEl = srcId ? document.getElementById(srcId) : null;
    if (!srcEl) return;
    var raw = srcEl.textContent || "";
    var html = raw;
    if (typeof window.DOMPurify !== "undefined") {
        html = window.DOMPurify.sanitize(raw);
    }
    el.innerHTML = html;
    el.dataset.htmlRendered = "1";
}

function renderAllHtml(root) {
    var scope = root || document;
    var targets = scope.querySelectorAll ? scope.querySelectorAll("[data-html]") : [];
    targets.forEach(renderHtmlTarget);
}

window.renderAllMarkdown = renderAllMarkdown;
window.renderMarkdownTarget = renderMarkdownTarget;
window.renderAllHtml = renderAllHtml;
window.renderHtmlTarget = renderHtmlTarget;

document.addEventListener("DOMContentLoaded", function() { renderAllMarkdown(); });
document.body.addEventListener("htmx:afterSwap", function(e) {
    renderAllMarkdown(e.target);
});
document.body.addEventListener("htmx:load", function(e) {
    renderAllMarkdown(e.target);
});


// HTMX 토스트 이벤트 핸들러
document.body.addEventListener("showToast", function(event) {
    var detail = event.detail || {};
    var message = detail.message || "Done";
    var type = detail.type || "info";
    var area = document.getElementById("toast-area");
    if (!area) return;
    var toast = document.createElement("div");
    toast.className = "toast toast-" + type;
    toast.textContent = message;
    area.appendChild(toast);
    setTimeout(function() { toast.remove(); }, 3000);
});

// HTMX 에러 처리
document.body.addEventListener("htmx:responseError", function(event) {
    var area = document.getElementById("toast-area");
    if (!area) return;
    var toast = document.createElement("div");
    toast.className = "toast toast-error";
    toast.textContent = "Request failed: " + (event.detail.xhr.status || "unknown error");
    area.appendChild(toast);
    setTimeout(function() { toast.remove(); }, 5000);
});
