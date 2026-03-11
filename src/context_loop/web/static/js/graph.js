/* Context Loop — vis.js Graph Visualization */

function initGraph(containerId, data) {
    var container = document.getElementById(containerId);
    if (!container || !data) return;

    var nodes = new vis.DataSet(data.nodes || []);
    var edges = new vis.DataSet(data.edges || []);

    var options = {
        nodes: {
            shape: "dot",
            size: 20,
            font: { size: 14 }
        },
        edges: {
            arrows: "to",
            font: { size: 11, align: "middle" },
            color: { inherit: "from" }
        },
        groups: {
            person:       { color: { background: "#4CAF50", border: "#388E3C" } },
            system:       { color: { background: "#2196F3", border: "#1976D2" } },
            team:         { color: { background: "#FF9800", border: "#F57C00" } },
            concept:      { color: { background: "#9C27B0", border: "#7B1FA2" } },
            service:      { color: { background: "#00BCD4", border: "#0097A7" } },
            component:    { color: { background: "#795548", border: "#5D4037" } },
            organization: { color: { background: "#607D8B", border: "#455A64" } },
            other:        { color: { background: "#9E9E9E", border: "#757575" } }
        },
        physics: {
            stabilization: { iterations: 100 },
            barnesHut: { gravitationalConstant: -3000 }
        },
        interaction: {
            hover: true,
            tooltipDelay: 200
        }
    };

    new vis.Network(container, { nodes: nodes, edges: edges }, options);
}
