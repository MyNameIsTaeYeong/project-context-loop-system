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

    // EasyMDE가 변경될 때마다 원본 textarea에 동기화한다.
    // HTMX는 FormData(form)로 폼 데이터를 수집하므로,
    // textarea 값이 최신 상태여야 PUT 요청에 내용이 포함된다.
    easyMDE.codemirror.on("change", function() {
        easyMDE.codemirror.save();
    });
});
