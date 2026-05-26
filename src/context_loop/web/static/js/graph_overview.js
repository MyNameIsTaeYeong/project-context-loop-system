/* Context Loop — Graph Overview Page (vis-network + Alpine.js) */

// graph.js와 동일한 색상 매핑 (의도된 코드 중복 — 작은 매핑이라 모듈화 비용이 더 큼).
const ENTITY_TYPE_COLORS = {
    person:       { background: "#4CAF50", border: "#388E3C" },
    system:       { background: "#2196F3", border: "#1976D2" },
    team:         { background: "#FF9800", border: "#F57C00" },
    concept:      { background: "#9C27B0", border: "#7B1FA2" },
    service:      { background: "#00BCD4", border: "#0097A7" },
    component:    { background: "#795548", border: "#5D4037" },
    organization: { background: "#607D8B", border: "#455A64" },
    other:        { background: "#9E9E9E", border: "#757575" }
};

function groupColorOf(type) {
    const g = ENTITY_TYPE_COLORS[type] || ENTITY_TYPE_COLORS.other;
    return g.background;
}

function initClusterGraph(containerId, data, onNodeSelect) {
    const container = document.getElementById(containerId);
    if (!container) return null;

    const nodes = new vis.DataSet((data.nodes || []).map(n => ({
        id: n.id,
        label: n.name,
        group: n.entity_type || "other",
        title: `${n.name}\n타입: ${n.entity_type || "?"}\n문서: ${n.document_count || 0}건\ndegree: ${n.degree != null ? n.degree : "?"}`
    })));
    const edges = new vis.DataSet((data.edges || []).map(e => ({
        from: e.source,
        to: e.target,
        label: e.relation_type || ""
    })));

    const options = {
        nodes: {
            shape: "dot",
            size: 18,
            font: { size: 13 }
        },
        edges: {
            arrows: "to",
            font: { size: 10, align: "middle" },
            color: { inherit: "from" },
            smooth: { type: "continuous" }
        },
        groups: ENTITY_TYPE_COLORS,
        physics: {
            stabilization: { iterations: 100 },
            barnesHut: { gravitationalConstant: -3000 }
        },
        interaction: {
            hover: true,
            tooltipDelay: 200
        }
    };

    const network = new vis.Network(container, { nodes, edges }, options);
    network.on("selectNode", (params) => {
        if (params.nodes.length > 0 && typeof onNodeSelect === "function") {
            onNodeSelect(params.nodes[0]);
        }
    });
    return { network, nodes, edges };
}

function graphOverview() {
    return {
        // 상태
        clusters: null,
        clustersError: null,
        mergeQuality: null,
        mqError: null,
        loading: false,

        selectedType: null,
        clusterQuery: "",
        clusterTotal: 0,
        clusterLoading: false,
        clusterError: null,
        clusterGraph: null,   // { network, nodes, edges }

        nodeDetail: null,
        nodeLoading: false,
        nodeError: null,

        // 초기화
        async init() {
            await Promise.all([this.loadClusters(), this.loadMergeQuality()]);
        },

        async refreshAll() {
            this.loading = true;
            try {
                await Promise.all([this.loadClusters(), this.loadMergeQuality()]);
                if (this.selectedType) {
                    await this.openCluster(this.selectedType, this.clusterQuery);
                }
            } finally {
                this.loading = false;
            }
        },

        async loadClusters() {
            this.clustersError = null;
            try {
                const r = await fetch("/api/graph/clusters");
                if (!r.ok) throw new Error("HTTP " + r.status);
                const json = await r.json();
                this.clusters = json.clusters || [];
                this._totalNodes = json.total_nodes || 0;
                this._totalEdges = json.total_edges || 0;
            } catch (e) {
                console.error("loadClusters failed", e);
                this.clustersError = e.message || String(e);
                this.clusters = [];
            }
        },

        async loadMergeQuality() {
            this.mqError = null;
            try {
                const r = await fetch("/api/graph/merge-quality");
                if (!r.ok) throw new Error("HTTP " + r.status);
                this.mergeQuality = await r.json();
            } catch (e) {
                console.error("loadMergeQuality failed", e);
                this.mqError = e.message || String(e);
            }
        },

        async openCluster(type, q) {
            this.selectedType = type;
            this.clusterQuery = q || "";
            this.clusterLoading = true;
            this.clusterError = null;
            this.nodeDetail = null;
            this.nodeError = null;

            try {
                const params = new URLSearchParams();
                params.set("limit", "200");
                params.set("offset", "0");
                if (this.clusterQuery) params.set("q", this.clusterQuery);
                const url = `/api/graph/cluster/${encodeURIComponent(type)}/nodes?${params.toString()}`;
                const r = await fetch(url);
                if (!r.ok) throw new Error("HTTP " + r.status);
                const data = await r.json();
                this.clusterTotal = data.total || (data.nodes || []).length;

                // vis-network는 Alpine 렌더 직후 컨테이너가 보장돼야 한다 → nextTick.
                this.$nextTick(() => {
                    // 기존 네트워크 정리
                    if (this.clusterGraph && this.clusterGraph.network) {
                        try { this.clusterGraph.network.destroy(); } catch (e) { /* noop */ }
                    }
                    this.clusterGraph = initClusterGraph(
                        "cluster-graph-container",
                        data,
                        (nodeId) => this.openNode(nodeId)
                    );
                });
            } catch (e) {
                console.error("openCluster failed", e);
                this.clusterError = e.message || String(e);
            } finally {
                this.clusterLoading = false;
            }
        },

        closeCluster() {
            if (this.clusterGraph && this.clusterGraph.network) {
                try { this.clusterGraph.network.destroy(); } catch (e) { /* noop */ }
            }
            this.clusterGraph = null;
            this.selectedType = null;
            this.clusterQuery = "";
            this.clusterTotal = 0;
            this.nodeDetail = null;
        },

        async openNode(nodeId) {
            this.nodeLoading = true;
            this.nodeError = null;
            this.nodeDetail = null;

            try {
                const r = await fetch(`/api/graph/node/${encodeURIComponent(nodeId)}`);
                if (!r.ok) throw new Error("HTTP " + r.status);
                this.nodeDetail = await r.json();

                // 클러스터 그래프에 노드가 있다면 해당 노드 강조
                if (this.clusterGraph && this.clusterGraph.network) {
                    try {
                        this.clusterGraph.network.selectNodes([nodeId], false);
                        this.clusterGraph.network.focus(nodeId, {
                            scale: 1.2,
                            animation: { duration: 300, easingFunction: "easeInOutQuad" }
                        });
                    } catch (e) {
                        // 다른 클러스터의 노드일 수 있음 → 자동 펼침 시도
                        const t = this.nodeDetail.entity_type;
                        if (t && t !== this.selectedType) {
                            this.openCluster(t).then(() => {
                                setTimeout(() => {
                                    if (this.clusterGraph && this.clusterGraph.network) {
                                        try {
                                            this.clusterGraph.network.selectNodes([nodeId], false);
                                            this.clusterGraph.network.focus(nodeId, { scale: 1.2 });
                                        } catch (_) { /* noop */ }
                                    }
                                }, 500);
                            });
                        }
                    }
                } else {
                    // 클러스터가 안 펼쳐진 상태에서 노드 클릭 시 → 해당 type 클러스터 펼침
                    const t = this.nodeDetail.entity_type;
                    if (t) {
                        this.openCluster(t);
                    }
                }
            } catch (e) {
                console.error("openNode failed", e);
                this.nodeError = e.message || String(e);
            } finally {
                this.nodeLoading = false;
            }
        },

        highlightDupGroup(group) {
            if (!group || !Array.isArray(group.members) || group.members.length === 0) return;

            // 같은 type 그룹이면 해당 type 클러스터를 열고 멤버 선택
            const types = new Set(group.members.map(m => m.type).filter(Boolean));
            if (types.size === 1) {
                const targetType = group.members[0].type;
                const ids = group.members.map(m => m.id);
                const ensure = (this.selectedType === targetType)
                    ? Promise.resolve()
                    : this.openCluster(targetType);
                ensure.then(() => {
                    setTimeout(() => {
                        if (this.clusterGraph && this.clusterGraph.network) {
                            try {
                                this.clusterGraph.network.selectNodes(ids, false);
                                this.clusterGraph.network.fit({
                                    nodes: ids,
                                    animation: { duration: 400, easingFunction: "easeInOutQuad" }
                                });
                            } catch (e) {
                                console.warn("highlight: some nodes missing in cluster", e);
                            }
                        }
                    }, 600);
                });
            } else {
                // 멤버들이 여러 type에 걸쳐 있으면 첫 멤버의 노드 상세를 열어줌
                this.openNode(group.members[0].id);
            }
        },

        // ===== 포맷터 =====

        formatPct(v) {
            if (v === null || v === undefined) return "—";
            const n = Number(v);
            if (!isFinite(n)) return "—";
            return (n * 100).toFixed(2) + "%";
        },

        groupColor(type) {
            return groupColorOf(type);
        },

        kpiClass(kind, value) {
            // 색상 규칙 (analyst 5.1)
            if (value === null || value === undefined) return "kpi-muted-card";
            const n = Number(value);
            if (!isFinite(n)) return "kpi-muted-card";
            if (kind === "surface" || kind === "semantic") {
                if (n === 0) return "kpi-good";
                if (n < 0.05) return "kpi-warn";
                return "kpi-bad";
            }
            if (kind === "typeconflict" || kind === "orphan") {
                if (n === 0) return "kpi-good";
                return "kpi-bad";
            }
            return "";
        },

        surfaceDupNodesLabel() {
            if (!this.mergeQuality) return "";
            const groups = this.mergeQuality.duplicate_groups || [];
            const surfaceGroups = groups.filter(g => g.kind === "surface_normalized" || g.kind === "surface" || !g.kind);
            const affected = surfaceGroups.reduce((s, g) => s + ((g.members || []).length), 0);
            return affected + " nodes in " + surfaceGroups.length + " groups";
        },

        crossDocLabel() {
            const mq = this.mergeQuality;
            if (!mq) return "";
            const total = mq.total_nodes || this._totalNodes || 0;
            const ratio = mq.metrics.cross_document_node_ratio || 0;
            const cross = Math.round(ratio * total);
            return cross + " / " + total + " nodes";
        },

        clustersSummary() {
            if (!this.clusters) return "";
            const types = this.clusters.length;
            return `${types} types · ${this._totalNodes || 0} nodes · ${this._totalEdges || 0} edges`;
        },

        clusterCountLabel() {
            if (!this.selectedType) return "";
            return ` (${this.clusterTotal} nodes)`;
        }
    };
}

window.graphOverview = graphOverview;
