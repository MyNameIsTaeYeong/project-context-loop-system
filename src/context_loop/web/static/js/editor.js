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

    // EasyMDE 내용 변경 시 hidden input에 동기화
    // title과 동일하게 <input>을 통해 HTMX 폼 제출 시 최신 내용이 전달되도록 함
    var hiddenContent = document.getElementById("hidden-content");
    function syncContent() {
        hiddenContent.value = easyMDE.value();
    }
    syncContent();
    easyMDE.codemirror.on("change", syncContent);
});
