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

    // HTMX는 폼 데이터를 먼저 수집한 뒤 htmx:configRequest를 발생시킨다.
    // codemirror.save()로는 이미 수집된 파라미터를 바꿀 수 없으므로,
    // evt.detail.parameters를 직접 수정해 EasyMDE 내용을 주입한다.
    var form = textarea.closest("form");
    if (form) {
        form.addEventListener("htmx:configRequest", function(evt) {
            evt.detail.parameters["content"] = easyMDE.value();
        });
    }
});
