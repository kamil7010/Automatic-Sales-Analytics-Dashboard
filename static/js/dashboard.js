// ---- Render charts from the JSON the server embedded in the page ----
const chartDataEl = document.getElementById('chart-data');

if (chartDataEl) {
    const charts = JSON.parse(chartDataEl.textContent);
    const plotConfig = { responsive: true, displaylogo: false };

    Plotly.newPlot('barChart', charts.bar.data, charts.bar.layout, plotConfig);
    Plotly.newPlot('pieChart', charts.pie.data, charts.pie.layout, plotConfig);
    Plotly.newPlot('lineChart', charts.line.data, charts.line.layout, plotConfig);
    Plotly.newPlot('areaChart', charts.area.data, charts.area.layout, plotConfig);

    window.addEventListener('resize', () => {
        ['barChart', 'pieChart', 'lineChart', 'areaChart'].forEach(id => Plotly.Plots.resize(id));
    });
}

// ---- Live status clock ----
const liveTimeEl = document.getElementById('liveTime');

function updateClock() {
    if (!liveTimeEl) return;
    liveTimeEl.textContent = new Date().toLocaleTimeString('en-GB');
}
updateClock();
setInterval(updateClock, 1000);

// ---- Auto-refresh KPI values every 30s ----
const currency = new Intl.NumberFormat('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const integer = new Intl.NumberFormat('en-IN');

function refreshKpis() {
    fetch('/refresh')
        .then(response => response.json())
        .then(data => {
            const salesEl = document.getElementById('kpiSales');
            const profitEl = document.getElementById('kpiProfit');
            const ordersEl = document.getElementById('kpiOrders');

            if (salesEl) salesEl.textContent = `₹${currency.format(data.sales)}`;
            if (profitEl) profitEl.textContent = `₹${currency.format(data.profit)}`;
            if (ordersEl) ordersEl.textContent = integer.format(data.orders);

            updateClock();
        })
        .catch(() => {
            // Silently skip this cycle — a transient network blip shouldn't
            // interrupt someone looking at the dashboard.
        });
}

if (document.getElementById('kpiSales')) {
    setInterval(refreshKpis, 30000);
}