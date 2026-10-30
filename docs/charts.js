// Renders every <figure class="chart" data-chart="name"> from the page's chart data.
// Data comes from <script type="application/json" id="chart-data"> when present,
// otherwise from data.json next to the page. Schema: publish-kit README.md.
(function () {
  "use strict";

  var NS = "http://www.w3.org/2000/svg";
  // Charts are drawn at the figure's own width (between MIN_WIDTH and MAX_WIDTH) so text
  // stays at its set size on narrow screens; below NARROW the hbar labels move above the bars.
  var MAX_WIDTH = 660, MIN_WIDTH = 280, NARROW = 480;
  var SERIES = ["var(--series-1)", "var(--series-2)", "var(--series-3)", "var(--series-4)"];
  var MAX_SERIES = 4;
  var END_LABEL_ROOM = 96;

  function fmt(value, style) {
    if (value === null || value === undefined) return "";
    if (style === "percent") {
      var scaled = value * 100;
      var places = Math.abs(scaled - Math.round(scaled)) < 0.05 ? 0 : Math.abs(scaled) < 1 ? 2 : 1;
      return scaled.toFixed(places) + "%";
    }
    if (style === "decimal2") return value.toFixed(2);
    if (style === "decimal3") return value.toFixed(3);
    return Math.abs(value) >= 1000 ? Math.round(value).toLocaleString("en-US") : String(value);
  }

  function niceTicks(low, high, count) {
    if (high <= low) high = low + 1;
    var raw = (high - low) / (count || 5);
    var magnitude = Math.pow(10, Math.floor(Math.log10(raw)));
    var step = [1, 2, 2.5, 5, 10].map(function (m) { return m * magnitude; })
      .find(function (s) { return s >= raw; });
    var ticks = [];
    for (var v = Math.floor(low / step) * step; v <= high + step * 0.001; v += step) {
      ticks.push(Math.round(v * 1e10) / 1e10);
    }
    if (ticks[ticks.length - 1] < high) ticks.push(ticks[ticks.length - 1] + step);
    return ticks;
  }

  function node(tag, attrs, parent, ns) {
    var element = ns === false ? document.createElement(tag) : document.createElementNS(NS, tag);
    Object.keys(attrs || {}).forEach(function (key) {
      if (key === "text") element.textContent = attrs[key];
      else if (key === "fill" || key === "stroke") element.style.setProperty(key, attrs[key]);
      else element.setAttribute(key, attrs[key]);
    });
    if (parent) parent.appendChild(element);
    return element;
  }

  function html(tag, attrs, parent) { return node(tag, attrs, parent, false); }

  function label(svg, x, y, content, size, fill, weight, anchor) {
    return node("text", { x: x, y: y, "font-size": size, "font-weight": weight || 400,
      "text-anchor": anchor || "start", fill: fill || "var(--ink)", text: content }, svg);
  }

  function barPath(x, y, w, h, direction) {
    var r = Math.max(0, Math.min(4, direction === "up" ? w / 2 : h / 2, direction === "right" ? w : h));
    if (direction === "right") {
      return "M" + x + "," + y + "h" + (w - r) + "a" + r + "," + r + " 0 0 1 " + r + "," + r +
        "v" + (h - 2 * r) + "a" + r + "," + r + " 0 0 1 -" + r + "," + r + "h-" + (w - r) + "z";
    }
    return "M" + x + "," + (y + h) + "v-" + (h - r) + "a" + r + "," + r + " 0 0 1 " + r + ",-" + r +
      "h" + (w - 2 * r) + "a" + r + "," + r + " 0 0 1 " + r + "," + r + "v" + (h - r) + "z";
  }

  function tooltipFor(figure) {
    var tip = html("div", { class: "tooltip", role: "status", hidden: "" }, figure);
    return {
      show: function (event, head, rows) {
        tip.replaceChildren();
        html("div", { class: "tt-head", text: head }, tip);
        rows.forEach(function (row) {
          var line = html("div", { class: "tt-row" }, tip);
          var key = html("span", { class: "tt-key" }, line);
          key.style.background = row.color;
          html("strong", { text: row.value }, line);
          if (row.name) html("span", { text: row.name }, line);
        });
        tip.hidden = false;
        var box = figure.getBoundingClientRect();
        var point = event.clientX !== undefined && event.clientX !== 0 ? event
          : event.target.getBoundingClientRect();
        var x = (point.clientX !== undefined ? point.clientX : point.left) - box.left + 12;
        var y = (point.clientY !== undefined ? point.clientY : point.top) - box.top + 12;
        tip.style.left = Math.min(x, box.width - tip.offsetWidth - 8) + "px";
        tip.style.top = y + "px";
      },
      hide: function () { tip.hidden = true; }
    };
  }

  function bindHover(target, mark, tip, head, rows) {
    target.setAttribute("tabindex", "0");
    target.setAttribute("aria-label", head + ": " + rows.map(function (r) { return r.value; }).join(", "));
    var show = function (event) { mark.classList.add("mark-hover"); tip.show(event, head, rows); };
    var hide = function () { mark.classList.remove("mark-hover"); tip.hide(); };
    target.addEventListener("pointermove", show);
    target.addEventListener("focus", show);
    target.addEventListener("pointerleave", hide);
    target.addEventListener("blur", hide);
  }

  function renderHbar(chart, svg, tip, width) {
    var style = chart.format, rows = chart.data, stacked = width < NARROW;
    var labelW = stacked ? 0 : Math.min(240, Math.max.apply(null, rows.map(function (r) { return r.label.length * 7.3; })) + 12);
    var domainMax = chart.max || Math.max.apply(null, rows.map(function (r) { return r.value; })) * 1.1;
    var labelGap = stacked ? 18 : 0;
    var x0 = labelW, x1 = width - 96, scale = (x1 - x0) / domainMax, band = 36 + labelGap, thick = 20, top = 22;
    var bottom = top + band * rows.length;
    niceTicks(0, domainMax, 4).filter(function (t) { return t <= domainMax * 1.0001; }).forEach(function (t) {
      var x = x0 + t * scale;
      node("line", { x1: x, y1: top - 6, x2: x, y2: bottom, stroke: "var(--grid)" }, svg);
      label(svg, x, bottom + 16, fmt(t, style), 11, "var(--muted)", 400, "middle");
    });
    if (chart.threshold) {
      var tx = x0 + chart.threshold.value * scale;
      node("line", { x1: tx, y1: top - 12, x2: tx, y2: bottom, stroke: "var(--ink-2)", "stroke-width": 1.5 }, svg);
      label(svg, tx + 4, top - 12, chart.threshold.label, 11, "var(--ink-2)");
    }
    rows.forEach(function (row, i) {
      var y = top + i * band + labelGap + (band - labelGap - thick) / 2, w = Math.max(row.value * scale, 1);
      var color = row.status === "pass" ? "var(--good)" : row.status === "fail" ? "var(--critical)" : SERIES[0];
      var text = fmt(row.value, style) + (row.status ? "  " + row.status : "");
      if (stacked) label(svg, 0, y - 6, row.label, 13);
      else label(svg, 0, y + thick / 2 + 4.5, row.label, 13);
      var mark = node("path", { d: barPath(x0, y, w, thick, "right"), fill: color }, svg);
      node("rect", { x: x0 + w + 3, y: y + 1, width: text.length * 7.6 + 8, height: thick - 2, fill: "var(--surface)" }, svg);
      label(svg, x0 + w + 6, y + thick / 2 + 4.5, text, 13, "var(--ink)", 600);
      var hit = node("rect", { class: "hit", x: 0, y: top + i * band, width: width, height: band }, svg);
      bindHover(hit, mark, tip, row.label, [{ color: color, value: text, name: row.note || "" }]);
    });
    node("line", { x1: x0, y1: top - 6, x2: x0, y2: bottom, stroke: "var(--axis)" }, svg);
    return bottom + 26;
  }

  function yAxis(svg, ticks, scaleY, left, right, style) {
    ticks.forEach(function (t, i) {
      var y = scaleY(t);
      node("line", { x1: left, y1: y, x2: right, y2: y, stroke: i === 0 ? "var(--axis)" : "var(--grid)" }, svg);
      label(svg, left - 8, y + 4, fmt(t, style), 11, "var(--muted)", 400, "end");
    });
  }

  function renderColumns(chart, svg, tip, width) {
    var style = chart.format, series = chart.series.slice(0, MAX_SERIES), cats = chart.categories;
    var ticks = niceTicks(0, chart.max || Math.max.apply(null, series.map(function (s) { return Math.max.apply(null, s.values); })));
    var left = 44, right = width, top = 14, bottom = 254;
    var scaleY = function (v) { return bottom - (v - ticks[0]) / (ticks[ticks.length - 1] - ticks[0]) * (bottom - top); };
    yAxis(svg, ticks, scaleY, left, right, style);
    var slot = (right - left) / cats.length;
    var barW = Math.min(24, (slot * 0.7 - 2 * (series.length - 1)) / series.length);
    var groupW = barW * series.length + 2 * (series.length - 1);
    var catLabelW = Math.max.apply(null, cats.map(function (c) { return String(c).length * 6.4; })) + 8;
    var every = Math.max(1, Math.ceil(catLabelW / slot));
    var valueLabels = series.length === 1 && cats.length <= 12 && slot >= 34;
    cats.forEach(function (cat, ci) {
      var gx = left + ci * slot + (slot - groupW) / 2, group = node("g", {}, svg);
      series.forEach(function (s, si) {
        var x = gx + si * (barW + 2), y = scaleY(s.values[ci]);
        node("path", { d: barPath(x, y, barW, Math.max(bottom - y, 1), "up"), fill: SERIES[si] }, group);
        if (valueLabels) label(svg, x + barW / 2, y - 6, fmt(s.values[ci], style), 11, "var(--ink)", 600, "middle");
      });
      if (ci % every === 0) label(svg, left + ci * slot + slot / 2, bottom + 18, cat, 11, "var(--ink-2)", 400, "middle");
      var hit = node("rect", { class: "hit", x: left + ci * slot, y: top, width: slot, height: bottom - top }, svg);
      bindHover(hit, group, tip, cat, series.map(function (s, si) {
        return { color: SERIES[si], value: fmt(s.values[ci], style), name: series.length > 1 ? s.name : "" };
      }));
    });
    return bottom + 30;
  }

  function renderLine(chart, svg, tip, width) {
    var style = chart.format, series = chart.series.slice(0, MAX_SERIES), xs = chart.x;
    var values = [].concat.apply([], series.map(function (s) { return s.values; })).filter(function (v) { return v !== null; });
    var ticks = niceTicks(chart.min !== undefined ? chart.min : Math.min(0, Math.min.apply(null, values)), chart.max || Math.max.apply(null, values));
    var left = 44, right = width - 110, top = 14, bottom = 254;
    var scaleY = function (v) { return bottom - (v - ticks[0]) / (ticks[ticks.length - 1] - ticks[0]) * (bottom - top); };
    var scaleX = function (i) { return left + i * (right - left) / Math.max(xs.length - 1, 1); };
    yAxis(svg, ticks, scaleY, left, right, style);
    var every = Math.max(1, Math.ceil(xs.length / (width < NARROW ? 4 : 8)));
    xs.forEach(function (x, i) { if ((xs.length - 1 - i) % every === 0) label(svg, scaleX(i), bottom + 18, x, 11, "var(--muted)", 400, "middle"); });
    var cross = node("line", { x1: 0, y1: top, x2: 0, y2: bottom, stroke: "var(--ink-2)", visibility: "hidden" }, svg);
    var ends = [];
    series.forEach(function (s, si) {
      var pts = s.values.map(function (v, i) { return v === null ? null : [scaleX(i), scaleY(v)]; }).filter(Boolean);
      node("polyline", { points: pts.map(function (p) { return p.join(","); }).join(" "), fill: "none", stroke: SERIES[si],
        "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }, svg);
      var end = pts[pts.length - 1];
      node("circle", { cx: end[0], cy: end[1], r: 4, fill: SERIES[si], stroke: "var(--surface)", "stroke-width": 2 }, svg);
      var value = fmt(s.values[s.values.length - 1], style), named = s.name + " " + value;
      ends.push({ x: end[0], y: end[1], text: series.length > 1 && named.length * 12 * 0.56 <= END_LABEL_ROOM ? named : value });
    });
    // Skip all end labels when any two would overlap; the legend and tooltip still identify each series.
    var endYs = ends.map(function (e) { return e.y; }).sort(function (a, b) { return a - b; });
    if (endYs.every(function (y, i) { return i === 0 || y - endYs[i - 1] >= 14; })) {
      ends.forEach(function (e) { label(svg, e.x + 10, e.y + 4, e.text, 12, "var(--ink)", 600); });
    }
    var hit = node("rect", { class: "hit", x: left, y: top, width: right - left, height: bottom - top, tabindex: "0" }, svg);
    hit.setAttribute("aria-label", chart.title + ": use the data table for values");
    hit.addEventListener("pointermove", function (event) {
      var box = svg.getBoundingClientRect(), px = (event.clientX - box.left) * width / box.width;
      var i = Math.max(0, Math.min(xs.length - 1, Math.round((px - left) / ((right - left) / Math.max(xs.length - 1, 1)))));
      cross.setAttribute("x1", scaleX(i)); cross.setAttribute("x2", scaleX(i)); cross.setAttribute("visibility", "visible");
      tip.show(event, String(xs[i]), series.map(function (s, si) { return { color: SERIES[si], value: fmt(s.values[i], style), name: s.name }; }));
    });
    hit.addEventListener("pointerleave", function () { cross.setAttribute("visibility", "hidden"); tip.hide(); });
    return bottom + 30;
  }

  function renderStats(chart, figure) {
    var row = html("div", { class: "stat-row" }, figure);
    chart.tiles.forEach(function (tile) {
      var stat = html("div", { class: "stat" }, row);
      html("div", { class: "stat-value", text: tile.value }, stat);
      html("div", { class: "stat-label", text: tile.label }, stat);
      if (tile.note) html("div", { class: "stat-note", text: tile.note }, stat);
    });
  }

  function tableRows(chart) {
    var style = chart.format;
    if (chart.type === "hbar") return [["Item", "Value"]].concat(chart.data.map(function (r) { return [r.label, fmt(r.value, style) + (r.status ? " (" + r.status + ")" : "")]; }));
    if (chart.type === "stats") return [["Measure", "Value"]].concat(chart.tiles.map(function (t) { return [t.label, t.value]; }));
    var keys = chart.type === "columns" ? chart.categories : chart.x, series = chart.series.slice(0, MAX_SERIES);
    return [[""].concat(series.map(function (s) { return s.name; }))].concat(keys.map(function (k, i) {
      return [String(k)].concat(series.map(function (s) { return fmt(s.values[i], style); }));
    }));
  }

  function addTable(chart, figure, foot) {
    var rows = tableRows(chart), table = html("table", { hidden: "" });
    var head = html("tr", {}, html("thead", {}, table)), body = html("tbody", {}, table);
    rows[0].forEach(function (cell, i) { html("th", { text: cell, class: i ? "num" : "" }, head); });
    rows.slice(1).forEach(function (row) {
      var tr = html("tr", {}, body);
      row.forEach(function (cell, i) { html("td", { text: cell, class: i ? "num" : "" }, tr); });
    });
    var button = html("button", { type: "button", "aria-expanded": "false", text: "Show data table" }, foot);
    button.addEventListener("click", function () {
      table.hidden = !table.hidden;
      button.setAttribute("aria-expanded", String(!table.hidden));
      button.textContent = table.hidden ? "Show data table" : "Hide data table";
    });
    figure.insertBefore(table, foot);
  }

  function addLegend(chart, figure) {
    if (!chart.series || chart.series.length < 2) return;
    var list = html("ul", { class: "chart-legend" }, figure);
    chart.series.slice(0, MAX_SERIES).forEach(function (s, si) {
      var item = html("li", {}, list);
      var key = html("span", { class: "key" + (chart.type === "line" ? " line" : "") }, item);
      key.style.background = SERIES[si];
      html("span", { text: s.name }, item);
    });
  }

  function chartWidth(figure) {
    var padding = parseFloat(getComputedStyle(figure).paddingLeft) + parseFloat(getComputedStyle(figure).paddingRight);
    return Math.round(Math.max(MIN_WIDTH, Math.min(MAX_WIDTH, figure.clientWidth - padding)));
  }

  function renderChart(figure, chart) {
    var width = chartWidth(figure);
    figure.dataset.renderedWidth = String(width);
    figure.replaceChildren();
    html("p", { class: "chart-title", text: chart.title }, figure);
    if (chart.subtitle) html("p", { class: "chart-subtitle", text: chart.subtitle }, figure);
    addLegend(chart, figure);
    if (chart.type === "stats") {
      renderStats(chart, figure);
    } else {
      var svg = node("svg", { role: "img", "aria-label": chart.alt || chart.title }, figure);
      var tip = tooltipFor(figure);
      var renderer = { hbar: renderHbar, columns: renderColumns, line: renderLine }[chart.type];
      var height = renderer(chart, svg, tip, width);
      svg.setAttribute("viewBox", "-4 -16 " + (width + 8) + " " + (height + 16));
    }
    var foot = html("div", { class: "chart-foot" }, figure);
    html("span", { text: "Source: " + chart.source }, foot);
    addTable(chart, figure, foot);
  }

  function loadData() {
    var inline = document.getElementById("chart-data");
    if (inline) return Promise.resolve(JSON.parse(inline.textContent));
    return fetch("data.json").then(function (response) {
      if (!response.ok) throw new Error("data.json: HTTP " + response.status);
      return response.json();
    });
  }

  function rerenderOnResize(figures, byName) {
    if (typeof ResizeObserver === "undefined") return;
    var observer = new ResizeObserver(function (entries) {
      entries.forEach(function (entry) {
        var figure = entry.target, chart = byName[figure.dataset.chart];
        if (!chart || chart.type === "stats") return;
        if (Math.abs(chartWidth(figure) - Number(figure.dataset.renderedWidth)) >= 24) renderChart(figure, chart);
      });
    });
    figures.forEach(function (figure) { observer.observe(figure); });
  }

  function start() {
    var figures = document.querySelectorAll("figure.chart[data-chart]");
    if (!figures.length) return;
    loadData().then(function (data) {
      var byName = {};
      data.charts.forEach(function (chart) { byName[chart.name] = chart; });
      figures.forEach(function (figure) {
        var chart = byName[figure.dataset.chart];
        if (chart) renderChart(figure, chart);
        else figure.textContent = "Chart \"" + figure.dataset.chart + "\" is missing from data.json.";
      });
      rerenderOnResize(figures, byName);
    }).catch(function (error) {
      figures.forEach(function (figure) { figure.textContent = "Charts could not load: " + error.message; });
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
