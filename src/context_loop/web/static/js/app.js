/* Context Loop Dashboard — Main JS */

// Markdown 렌더링 — data-markdown 속성이 있는 엘리먼트의 원본 텍스트를
// (인접한 .md-source 또는 data-markdown-src 로 지정된 요소에서) 읽어
// marked + DOMPurify 로 안전하게 HTML 로 치환한다. HTMX 스왑 후에도
// 자동으로 다시 적용된다.
function renderMarkdownTarget(el) {
    if (!el || el.dataset.mdRendered === "1") return;
    if (typeof window.marked === "undefined") return;
    var srcId = el.dataset.markdownSrc;
    var srcEl = srcId ? document.getElementById(srcId) : null;
    if (!srcEl) {
        var sib = el.nextElementSibling;
        if (sib && sib.classList && sib.classList.contains("md-source")) {
            srcEl = sib;
        }
    }
    if (!srcEl) return;
    var raw = srcEl.textContent || "";
    if (typeof window.marked.parse === "function") {
        window.marked.setOptions({ gfm: true, breaks: false });
    }
    var html = window.marked.parse(raw);
    if (typeof window.DOMPurify !== "undefined") {
        html = window.DOMPurify.sanitize(html);
    }
    el.innerHTML = html;
    el.dataset.mdRendered = "1";
}

function renderAllMarkdown(root) {
    var scope = root || document;
    var targets = scope.querySelectorAll ? scope.querySelectorAll("[data-markdown]") : [];
    targets.forEach(renderMarkdownTarget);
}

window.renderAllMarkdown = renderAllMarkdown;

document.addEventListener("DOMContentLoaded", function() { renderAllMarkdown(); });
document.body.addEventListener("htmx:afterSwap", function(e) {
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
