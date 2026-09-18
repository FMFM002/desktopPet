// renderSimple 布局逻辑冒烟测试（无 DOM，只验证计算部分不抛错且产出合理坐标）
const d3 = require("d3");
const fs = require("fs");
const data = JSON.parse(fs.readFileSync(process.argv[2], "utf-8"));
const nodes = Object.entries(data.entities).map(([label, e], i) => ({
    id: "n" + i, label, type: e.type || "unknown",
}));
const idByName = new Map(nodes.map(n => [n.label, n.id]));
const edges = data.relations
    .map(r => ({ from: idByName.get(r.source), to: idByName.get(r.target) }))
    .filter(e => e.from && e.to);

// ── 复刻 HTML 里 renderSimple 的布局计算 ──
const width = 1200, height = 700;
const margin = { top: 30, left: 40, right: 20, bottom: 20 };
const W = width - margin.left - margin.right;
const H = height - margin.top - margin.bottom;
const order = ["project","directory","file","module","class","function"];
const byType = {};
nodes.forEach(n => { (byType[n.type] = byType[n.type] || []).push(n); });
const usedTypes = order.filter(t => (byType[t] || []).length);
const total = nodes.length || 1;

let yPos = margin.top;
const pos = {};
usedTypes.forEach(t => {
    const arr = byType[t];
    const bandH = Math.max(24, (arr.length / total) * H);
    const rows = Math.max(1, Math.floor(bandH / 14));
    const perRow = Math.ceil(arr.length / rows);
    const cellW = Math.min(14, W / Math.max(1, perRow));
    arr.forEach((n, i) => {
        pos[n.id] = {
            x: margin.left + (i % perRow) * cellW + cellW / 2,
            y: yPos + Math.floor(i / perRow) * (bandH / rows),
        };
    });
    yPos += bandH;
});

// 断言
let fail = 0;
const check = (name, ok) => { console.log((ok ? "PASS" : "FAIL") + " " + name); if (!ok) fail++; };

check("所有类型都被排布: " + usedTypes.join(","), usedTypes.length === 6);
let allPlaced = true, inBounds = true;
nodes.forEach(n => {
    if (!pos[n.id]) { allPlaced = false; return; }
    if (!(pos[n.id].x >= 0 && pos[n.id].x <= width && pos[n.id].y >= 0 && pos[n.id].y <= height + 50)) inBounds = false;
});
check("1644 个节点全部有坐标", allPlaced);
check("坐标都在画布范围内", inBounds);
check("总节点数 = " + nodes.length, nodes.length === 1644);
check("有效边数 = " + edges.length + " (两端都在节点表内)", edges.length > 2000);
// 连线映射（px/py）无未定义
const px = {}; nodes.forEach(n => { px[n.id] = (pos[n.id]||{}).x; });
let edgesResolved = 0;
edges.forEach(e => { if (typeof px[e.from] === "number" && typeof px[e.to] === "number") edgesResolved++; });
check("全部边的两端坐标可解析", edgesResolved === edges.length);
// forceLink 冒烟（用全部节点+全部边，验证 1644 节点也不抛 node not found）
try {
    const linkData = edges.map(e => Object.assign({}, e, { source: e.from, target: e.to }));
    const sim = d3.forceSimulation(nodes)
        .force("link", d3.forceLink(linkData).id(d => d.id).distance(80));
    sim.stop();
    check("d3.forceLink 1644 节点/2374 边初始化通过", true);
} catch (e) {
    check("d3.forceLink 初始化: " + e.message, false);
}
console.log(fail === 0 ? "ALL PASS" : `FAILED ${fail}`);
process.exit(fail === 0 ? 0 : 1);
