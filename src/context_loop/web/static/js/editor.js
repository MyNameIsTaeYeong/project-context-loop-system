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

    // HTMX 폼 제출 전에 EasyMDE 내용을 textarea에 동기화
    // HTMX 2.x에서 parameters는 FormData 객체이므로 .set() 사용
    document.body.addEventListener("htmx:configRequest", function(event) {
        if (event.detail.parameters instanceof FormData && event.detail.parameters.has("content")) {
            event.detail.parameters.set("content", easyMDE.value());
        }
    });
});
