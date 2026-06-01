/* Context Loop — Graph 탭 탐색기.
 *
 * graph.js 의 initGraph(vis-network)를 재사용하여 세 가지 뷰를 제공한다:
 *   - 키워드 탐색: /api/graph/explore?keyword=...
 *   - 전체 그래프: /api/graph/full
 *   - 병합된 노드: /api/graph/merges
 */

function _setStatus(id, msg) {
    var el = document.getElementById(id);
    if (el) el.textContent = msg || "";
}

// 시드 노드를 시각적으로 강조 (테두리 굵게 + 별표 라벨).
function _highlightSeeds(data) {
    (data.nodes || []).forEach(function (n) {
        if (n.seed) {
            n.label = "★ " + n.label;
            n.borderWidth = 4;
            n.font = { size: 16, bold: true };
        }
    });
    return data;
}

async function graphExplore() {
    var input = document.getElementById("graph-keyword");
    var keyword = (input && input.value || "").trim();
    if (!keyword) {
        _setStatus("graph-explore-status", "키워드를 입력하세요.");
        return;
    }
    _setStatus("graph-explore-status", "탐색 중...");
    try {
        var resp = await fetch("/api/graph/explore?keyword=" + encodeURIComponent(keyword));
        if (!resp.ok) {
            _setStatus("graph-explore-status", "요청 실패: " + resp.status);
            return;
        }
        var data = await resp.json();
        if (!data.stats || !data.stats.matched) {
            _setStatus("graph-explore-status",
                "'" + keyword + "' 와(과) 일치하는 엔티티를 찾지 못했습니다.");
            initGraph("graph-explore-container", { nodes: [], edges: [] });
            return;
        }
        _highlightSeeds(data);
        _setStatus("graph-explore-status",
            "'" + keyword + "' 연결 그래프 — 노드 " + data.stats.shown_nodes +
            "개, 엣지 " + data.stats.shown_edges + "개 (시드 " +
            (data.stats.seed_count || 0) + "개)");
        initGraph("graph-explore-container", data);
    } catch (e) {
        _setStatus("graph-explore-status", "오류: " + e.message);
    }
}

async function graphLoadFull() {
    _setStatus("graph-full-status", "불러오는 중...");
    try {
        var resp = await fetch("/api/graph/full");
        if (!resp.ok) {
            _setStatus("graph-full-status", "요청 실패: " + resp.status);
            return;
        }
        var data = await resp.json();
        var s = data.stats || {};
        var msg = "노드 " + s.shown_nodes + "/" + s.total_nodes +
                  "개, 엣지 " + s.shown_edges + "/" + s.total_edges + "개";
        if (s.truncated) {
            msg += " — 연결 수 많은 상위 노드만 표시합니다.";
        }
        if (s.total_nodes === 0) {
            msg = "그래프가 비어 있습니다. 문서를 인덱싱하면 노드가 생성됩니다.";
        }
        _setStatus("graph-full-status", msg);
        initGraph("graph-full-container", data);
    } catch (e) {
        _setStatus("graph-full-status", "오류: " + e.message);
    }
}

function _escapeHtml(s) {
    return String(s == null ? "" : s)
        .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

async function graphLoadMerges() {
    _setStatus("graph-merges-status", "불러오는 중...");
    var container = document.getElementById("graph-merges-table");
    try {
        var resp = await fetch("/api/graph/merges");
        if (!resp.ok) {
            _setStatus("graph-merges-status", "요청 실패: " + resp.status);
            return;
        }
        var data = await resp.json();
        var groups = data.groups || [];
        if (!groups.length) {
            _setStatus("graph-merges-status",
                "병합된 노드가 없습니다. (서로 다른 표기/여러 문서가 한 노드로 수렴한 경우만 표시)");
            if (container) container.innerHTML = "";
            return;
        }
        _setStatus("graph-merges-status", "병합된 노드 " + groups.length + "개");
        var rows = groups.map(function (g) {
            var variants = (g.variant_names || []).map(_escapeHtml).join("<br>");
            var methods = (g.methods || []).map(_escapeHtml).join(", ");
            var docs = (g.document_ids || []).length;
            return "<tr>" +
                "<td>" + _escapeHtml(g.entity_name) + "</td>" +
                "<td>" + _escapeHtml(g.entity_type) + "</td>" +
                "<td>" + variants + "</td>" +
                "<td>" + (g.variant_names || []).length + "</td>" +
                "<td>" + docs + "</td>" +
                "<td>" + methods + "</td>" +
                "</tr>";
        }).join("");
        container.innerHTML =
            "<table><thead><tr>" +
            "<th>정규 노드</th><th>타입</th><th>흡수된 표기</th>" +
            "<th>표기 수</th><th>문서 수</th><th>병합 방식</th>" +
            "</tr></thead><tbody>" + rows + "</tbody></table>";
    } catch (e) {
        _setStatus("graph-merges-status", "오류: " + e.message);
    }
}

window.graphExplore = graphExplore;
window.graphLoadFull = graphLoadFull;
window.graphLoadMerges = graphLoadMerges;
