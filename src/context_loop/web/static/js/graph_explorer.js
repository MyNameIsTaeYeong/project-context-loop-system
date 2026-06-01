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

// hop 거리를 라벨에 부기 (시드=0). depth 별 위치를 시각적으로 구분.
function _annotateHops(data) {
    (data.nodes || []).forEach(function (n) {
        if (typeof n.hop === "number" && !n.seed) {
            n.label = n.label + " (" + n.hop + ")";
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
    var depthInput = document.getElementById("graph-depth");
    var depthRaw = (depthInput && depthInput.value || "").trim();
    var url = "/api/graph/explore?keyword=" + encodeURIComponent(keyword);
    if (depthRaw !== "") {
        url += "&depth=" + encodeURIComponent(depthRaw);
    }
    _setStatus("graph-explore-status", "탐색 중...");
    try {
        var resp = await fetch(url);
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
        _annotateHops(data);
        var scope = depthRaw !== "" ? ("depth " + depthRaw + " 이내") : "연결 전체";
        _setStatus("graph-explore-status",
            "'" + keyword + "' " + scope + " — 노드 " + data.stats.shown_nodes +
            "개, 엣지 " + data.stats.shown_edges + "개 (시드 " +
            (data.stats.seed_count || 0) + "개, 최대 " +
            (data.stats.max_hop || 0) + "-hop)");
        initGraph("graph-explore-container", data, {
            onNodeClick: graphShowNodeDetail,
        });
    } catch (e) {
        _setStatus("graph-explore-status", "오류: " + e.message);
    }
}

// 노드 클릭 → 출처 문서·병합 내역 상세 패널 렌더.
async function graphShowNodeDetail(nodeId) {
    var panel = document.getElementById("graph-node-detail");
    if (!panel) return;
    panel.innerHTML = "<p><small>불러오는 중...</small></p>";
    try {
        var resp = await fetch("/api/graph/node/" + encodeURIComponent(nodeId));
        if (!resp.ok) {
            panel.innerHTML = "<p>상세 조회 실패: " + resp.status + "</p>";
            return;
        }
        var d = await resp.json();

        var html = "<h4>" + _escapeHtml(d.entity_name) + "</h4>";
        html += "<p><small>타입: " + _escapeHtml(d.entity_type) + "</small></p>";
        if (d.description) {
            html += "<p><small>" + _escapeHtml(d.description) + "</small></p>";
        }

        // 출처 문서
        html += "<h5>출처 문서 (" + (d.documents || []).length + ")</h5>";
        if ((d.documents || []).length) {
            html += "<ul>";
            (d.documents).forEach(function (doc) {
                var label = _escapeHtml(doc.title);
                if (doc.source_type) {
                    label += " <small>[" + _escapeHtml(doc.source_type) + "]</small>";
                }
                html += '<li><a href="/documents/' + doc.document_id +
                        '" target="_blank">' + label + "</a></li>";
            });
            html += "</ul>";
        } else {
            html += "<p><small>연결된 문서 없음</small></p>";
        }

        // 병합 내역
        html += "<h5>병합 내역 (" + (d.merges || []).length + ")</h5>";
        if ((d.merges || []).length) {
            html += "<table><thead><tr><th>표기</th><th>방식</th><th>문서</th>" +
                    "</tr></thead><tbody>";
            (d.merges).forEach(function (m) {
                html += "<tr><td>" + _escapeHtml(m.raw_entity_name) + "</td>" +
                        "<td>" + _escapeHtml(m.merge_method) + "</td>" +
                        "<td>#" + _escapeHtml(m.source_document_id) + "</td></tr>";
            });
            html += "</tbody></table>";
            html += "<p><small>방식: exact(정확 일치) · normalized(표기 정규화 병합)" +
                    " · new(신규 생성)</small></p>";
        } else {
            html += "<p><small>병합 기록 없음</small></p>";
        }

        panel.innerHTML = html;
    } catch (e) {
        panel.innerHTML = "<p>오류: " + _escapeHtml(e.message) + "</p>";
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
window.graphShowNodeDetail = graphShowNodeDetail;
window.graphLoadFull = graphLoadFull;
window.graphLoadMerges = graphLoadMerges;
