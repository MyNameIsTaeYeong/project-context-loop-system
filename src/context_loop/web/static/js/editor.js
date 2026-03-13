/* Context Loop — EasyMDE Editor Initialization */

document.addEventListener("DOMContentLoaded", function() {
    var textarea = document.getElementById("editor-content");
    if (!textarea) return;

    var easyMDE = new EasyMDE({
        element: textarea,
        spellChecker: false,
        autosave: { enabled: false },
        status: ["lines", "words"],
        toolbar: [
            "bold", "italic", "heading", "|",
            "code", "quote", "unordered-list", "ordered-list", "|",
            "link", "image", "table", "|",
            "preview", "side-by-side", "fullscreen", "|",
            "guide"
        ]
    });

    // HTMX가 폼 데이터를 수집하기 전에 EasyMDE 내용을 textarea에 동기화한다.
    // EasyMDE는 CodeMirror 기반으로 자체 에디터를 사용하기 때문에
    // HTMX가 직접 textarea 값을 읽으면 변경된 내용이 누락된다.
    var form = textarea.closest("form");
    if (form) {
        form.addEventListener("htmx:configRequest", function() {
            easyMDE.codemirror.save();
        });
        // 일반 submit 이벤트에도 동기화 (폴백)
        form.addEventListener("submit", function() {
            easyMDE.codemirror.save();
        });
    }
});
