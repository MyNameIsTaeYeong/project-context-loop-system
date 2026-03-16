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

    // EasyMDE 내용 변경 시 원본 textarea에 즉시 동기화
    // HTMX가 폼 값을 수집할 때 최신 내용을 읽을 수 있도록 함
    easyMDE.codemirror.on("change", function() {
        easyMDE.codemirror.save();
    });
});
