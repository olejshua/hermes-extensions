(() => {
  "use strict";

  const canvas = document.getElementById("graph");
  const ctx = canvas.getContext("2d");
  const search = document.getElementById("search");
  const typeFilters = document.getElementById("typeFilters");
  const namespaceFilters = document.getElementById("namespaceFilters");
  const details = document.getElementById("details");
  const summary = document.getElementById("summary");
  const buttons = {
    fit: document.getElementById("fit"),
    reset: document.getElementById("reset"),
    refresh: document.getElementById("refresh"),
  };

  let graph = {nodes: [], links: [], stats: {}};
  let visible = new Set();
  let selected = null;
  let hover = null;
  let drag = null;
  let pan = {x: 0, y: 0};
  let zoom = 1;
  let pointer = {x: 0, y: 0};
  let typeState = new Map();
  let namespaceState = new Map();
  let needsFit = true;

  const palette = new Map([
    ["root", "#edf4ff"],
    ["source", "#61d7e8"],
    ["folder", "#8fd17f"],
    ["resource", "#e2c36d"],
    ["aggregate", "#df7f95"],
  ]);

  function resize() {
    const rect = canvas.getBoundingClientRect();
    const ratio = window.devicePixelRatio || 1;
    canvas.width = Math.max(1, Math.floor(rect.width * ratio));
    canvas.height = Math.max(1, Math.floor(rect.height * ratio));
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  }

  async function loadGraph() {
    const response = await fetch("/api/graph", {cache: "no-store"});
    graph = await response.json();
    hydrateGraph();
    buildFilters();
    applyFilters();
    needsFit = true;
    renderDetails(selected);
  }

  function hydrateGraph() {
    const byId = new Map();
    graph.nodes.forEach((node, index) => {
      node.x = Math.cos(index * 2.399) * (80 + index % 17 * 7);
      node.y = Math.sin(index * 2.399) * (80 + index % 19 * 7);
      node.vx = 0;
      node.vy = 0;
      node.radius = Math.max(5, Math.min(24, Number(node.size || 8)));
      byId.set(node.id, node);
    });
    graph.links.forEach((link) => {
      link.sourceNode = byId.get(link.source);
      link.targetNode = byId.get(link.target);
    });
  }

  function buildFilters() {
    const kinds = [...new Set(graph.nodes.map((node) => node.kind))].sort();
    const namespaces = [...new Set(graph.nodes.filter((node) => node.namespace).map((node) => node.namespace))].sort();
    syncState(typeState, kinds);
    syncState(namespaceState, namespaces);
    renderChecks(typeFilters, typeState, applyFilters);
    renderChecks(namespaceFilters, namespaceState, applyFilters);
  }

  function syncState(state, values) {
    for (const key of [...state.keys()]) if (!values.includes(key)) state.delete(key);
    values.forEach((value) => { if (!state.has(value)) state.set(value, true); });
  }

  function renderChecks(container, state, onChange) {
    container.replaceChildren();
    for (const [value, checked] of state) {
      const label = document.createElement("label");
      const box = document.createElement("input");
      const text = document.createElement("span");
      box.type = "checkbox";
      box.checked = checked;
      box.addEventListener("change", () => {
        state.set(value, box.checked);
        onChange();
      });
      text.textContent = value;
      label.append(box, text);
      container.append(label);
    }
  }

  function applyFilters() {
    const q = search.value.trim().toLowerCase();
    visible = new Set();
    graph.nodes.forEach((node) => {
      const typeOk = typeState.get(node.kind) !== false;
      const namespaceOk = !node.namespace || namespaceState.get(node.namespace) !== false;
      const queryOk = !q || [node.label, node.kind, node.group, node.namespace, node.status].filter(Boolean).join(" ").toLowerCase().includes(q);
      if (typeOk && namespaceOk && queryOk) visible.add(node.id);
    });
    const stats = graph.stats || {};
    const truncated = stats.truncated ? " capped" : "";
    summary.textContent = `${visible.size} visible of ${graph.nodes.length} nodes, ${stats.resources || 0}${truncated} resources`;
  }

  function tick() {
    const nodes = graph.nodes.filter((node) => visible.has(node.id));
    const links = graph.links.filter((link) => visible.has(link.source) && visible.has(link.target));
    for (const link of links) {
      const a = link.sourceNode, b = link.targetNode;
      if (!a || !b) continue;
      const dx = b.x - a.x, dy = b.y - a.y;
      const distance = Math.max(1, Math.hypot(dx, dy));
      const target = link.kind === "contains" ? 62 : 92;
      const force = (distance - target) * 0.012;
      const fx = dx / distance * force, fy = dy / distance * force;
      if (!a.fixed) { a.vx += fx; a.vy += fy; }
      if (!b.fixed) { b.vx -= fx; b.vy -= fy; }
    }
    for (let i = 0; i < nodes.length; i++) {
      const a = nodes[i];
      for (let j = i + 1; j < nodes.length; j++) {
        const b = nodes[j];
        const dx = b.x - a.x, dy = b.y - a.y;
        const distance = Math.max(1, Math.hypot(dx, dy));
        const force = Math.min(1.2, 90 / (distance * distance));
        const fx = dx / distance * force, fy = dy / distance * force;
        if (!a.fixed) { a.vx -= fx; a.vy -= fy; }
        if (!b.fixed) { b.vx += fx; b.vy += fy; }
      }
    }
    nodes.forEach((node) => {
      if (node.fixed) return;
      node.vx = (node.vx - node.x * 0.0009) * 0.84;
      node.vy = (node.vy - node.y * 0.0009) * 0.84;
      node.x += node.vx;
      node.y += node.vy;
    });
  }

  function draw() {
    const rect = canvas.getBoundingClientRect();
    ctx.clearRect(0, 0, rect.width, rect.height);
    ctx.save();
    ctx.translate(pan.x, pan.y);
    ctx.scale(zoom, zoom);
    const links = graph.links.filter((link) => visible.has(link.source) && visible.has(link.target));
    ctx.lineWidth = 1 / zoom;
    links.forEach((link) => {
      const a = link.sourceNode, b = link.targetNode;
      if (!a || !b) return;
      ctx.strokeStyle = link.kind === "private-aggregate" ? "rgba(223,127,149,0.42)" : "rgba(111,139,176,0.28)";
      ctx.beginPath();
      ctx.moveTo(a.x, a.y);
      ctx.lineTo(b.x, b.y);
      ctx.stroke();
    });
    graph.nodes.forEach((node) => {
      if (!visible.has(node.id)) return;
      const active = node === selected || node === hover;
      const color = palette.get(node.kind) || "#91a4bd";
      ctx.beginPath();
      ctx.fillStyle = color;
      ctx.globalAlpha = active ? 1 : 0.84;
      ctx.arc(node.x, node.y, node.radius, 0, Math.PI * 2);
      ctx.fill();
      ctx.globalAlpha = 1;
      ctx.strokeStyle = active ? "#ffffff" : "rgba(255,255,255,0.22)";
      ctx.lineWidth = active ? 2 / zoom : 1 / zoom;
      ctx.stroke();
      if (shouldLabel(node, active)) {
        ctx.font = `${Math.max(10, 12 / Math.sqrt(zoom))}px ui-sans-serif, system-ui`;
        ctx.fillStyle = "#edf4ff";
        ctx.textAlign = "center";
        ctx.textBaseline = "top";
        ctx.fillText(node.label, node.x, node.y + node.radius + 5 / zoom, 180 / zoom);
      }
    });
    ctx.restore();
  }

  function shouldLabel(node, active) {
    if (active) return true;
    if (node.kind !== "resource") return zoom >= 0.34;
    // Resource labels are intentionally progressive: rendering every filename
    // at overview scale turns a useful map into an unreadable word cloud.
    return zoom >= 1.8 && visible.size <= 72;
  }

  function loop() {
    if (needsFit) fit();
    tick();
    draw();
    requestAnimationFrame(loop);
  }

  function screenToWorld(evt) {
    const rect = canvas.getBoundingClientRect();
    return {x: (evt.clientX - rect.left - pan.x) / zoom, y: (evt.clientY - rect.top - pan.y) / zoom};
  }

  function hitTest(world) {
    let best = null;
    for (const node of graph.nodes) {
      if (!visible.has(node.id)) continue;
      const distance = Math.hypot(node.x - world.x, node.y - world.y);
      if (distance <= node.radius + 5 / zoom && (!best || distance < best.distance)) best = {node, distance};
    }
    return best && best.node;
  }

  function fit() {
    const nodes = graph.nodes.filter((node) => visible.has(node.id));
    const rect = canvas.getBoundingClientRect();
    if (!nodes.length || !rect.width || !rect.height) return;
    const xs = nodes.map((node) => node.x), ys = nodes.map((node) => node.y);
    const minX = Math.min(...xs), maxX = Math.max(...xs), minY = Math.min(...ys), maxY = Math.max(...ys);
    const width = Math.max(80, maxX - minX), height = Math.max(80, maxY - minY);
    zoom = Math.max(0.18, Math.min(2.2, Math.min((rect.width - 90) / width, (rect.height - 90) / height)));
    pan.x = rect.width / 2 - (minX + width / 2) * zoom;
    pan.y = rect.height / 2 - (minY + height / 2) * zoom;
    needsFit = false;
  }

  function reset() {
    zoom = 1;
    const rect = canvas.getBoundingClientRect();
    pan = {x: rect.width / 2, y: rect.height / 2};
  }

  function renderDetails(node) {
    details.replaceChildren();
    const rows = node ? [
      ["Name", node.label],
      ["Type", node.kind],
      ["Group", node.group],
      ["Namespace", node.namespace],
      ["Status", node.status],
      ["Count", node.count],
      ["Bytes", node.size_bytes],
    ] : [["Name", "None"], ["Type", "Select or hover a node"]];
    rows.filter((row) => row[1] !== undefined && row[1] !== "").forEach(([key, value]) => {
      const dt = document.createElement("dt");
      const dd = document.createElement("dd");
      dt.textContent = key;
      dd.textContent = String(value);
      details.append(dt, dd);
    });
  }

  search.addEventListener("input", applyFilters);
  buttons.fit.addEventListener("click", fit);
  buttons.reset.addEventListener("click", reset);
  buttons.refresh.addEventListener("click", loadGraph);
  window.addEventListener("resize", () => { resize(); needsFit = true; });
  canvas.addEventListener("pointerdown", (evt) => {
    pointer = {x: evt.clientX, y: evt.clientY};
    const node = hitTest(screenToWorld(evt));
    if (node) {
      selected = node;
      drag = node;
      node.fixed = true;
      canvas.setPointerCapture(evt.pointerId);
      renderDetails(node);
    } else {
      drag = {pan: true, start: {...pan}};
    }
  });
  canvas.addEventListener("pointermove", (evt) => {
    const world = screenToWorld(evt);
    hover = hitTest(world);
    if (drag && drag.pan) {
      pan.x = drag.start.x + evt.clientX - pointer.x;
      pan.y = drag.start.y + evt.clientY - pointer.y;
    } else if (drag) {
      drag.x = world.x;
      drag.y = world.y;
      drag.vx = 0;
      drag.vy = 0;
    }
  });
  canvas.addEventListener("pointerup", () => {
    if (drag && !drag.pan) drag.fixed = false;
    drag = null;
  });
  canvas.addEventListener("wheel", (evt) => {
    evt.preventDefault();
    const rect = canvas.getBoundingClientRect();
    const before = {x: (evt.clientX - rect.left - pan.x) / zoom, y: (evt.clientY - rect.top - pan.y) / zoom};
    zoom = Math.max(0.12, Math.min(4, zoom * Math.exp(-evt.deltaY * 0.001)));
    pan.x = evt.clientX - rect.left - before.x * zoom;
    pan.y = evt.clientY - rect.top - before.y * zoom;
  }, {passive: false});
  canvas.addEventListener("dblclick", fit);

  resize();
  reset();
  loadGraph().then(loop).catch((error) => {
    summary.textContent = "Failed to load graph";
    console.error(error);
  });
})();
