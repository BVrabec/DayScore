// Apply the saved theme before the page paints (no flash of the wrong theme).
(() => { let t = "dark"; try { t = localStorage.getItem("theme") || "dark"; } catch {} document.documentElement.dataset.theme = t; })();
