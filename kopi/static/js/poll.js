const root = document.querySelector("[data-status-url]");

if (root) {
    const url = root.dataset.statusUrl;
    const order = (root.dataset.phases || "").split(" ").filter(Boolean);
    let busy = false;
    let timer = 0;

    const tick = async () => {
        if (busy) {
            return;
        }
        busy = true;
        try {
            const response = await fetch(url, {
                headers: { Accept: "application/json" },
                cache: "no-store",
                redirect: "manual",
            });
            if (response.type === "opaqueredirect" || !response.ok) {
                return;
            }
            const payload = await response.json();
            if (!payload || payload.status !== "running") {
                window.clearInterval(timer);
                window.location.reload();
                return;
            }
            paint(root, order, payload);
        } catch (_error) {
            // The next tick tries again.
        } finally {
            busy = false;
        }
    };

    timer = window.setInterval(tick, 1000);
    tick();
}

function paint(root, order, payload) {
    const shown = megabytes(payload.bytes_moved);
    document.querySelectorAll("[data-mb]").forEach((node) => {
        node.textContent = shown;
    });
    const phase = String(payload.phase || "");
    if (!/^[a-z]+$/.test(phase)) {
        return;
    }
    const current = root.querySelector(`[data-phase="${phase}"]`);
    if (!current) {
        return;
    }
    document.querySelectorAll("[data-phase-label]").forEach((node) => {
        node.textContent = current.dataset.label || "";
    });
    const caption = root.querySelector("[data-phase-caption]");
    if (caption) {
        caption.textContent = current.dataset.caption || "";
    }
    const index = order.indexOf(phase);
    root.querySelectorAll("[data-phase]").forEach((node) => {
        const at = order.indexOf(node.dataset.phase);
        node.classList.toggle("done", at !== -1 && index !== -1 && at < index);
        node.classList.toggle("now", node.dataset.phase === phase);
    });
}

function megabytes(bytes) {
    const value = Number(bytes) / (1024 * 1024);
    return value.toLocaleString("en-US", {
        minimumFractionDigits: 1,
        maximumFractionDigits: 1,
    });
}
