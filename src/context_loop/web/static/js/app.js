/* Context Loop Dashboard — Main JS */

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
