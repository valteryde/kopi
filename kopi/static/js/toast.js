// Client-side toast notifications.

export function showToast(text, icon = "check") {
    let container = document.getElementById("toast");
    if (!container) {
        container = document.createElement("div");
        container.id = "toast";
        container.setAttribute("aria-live", "polite");
        document.body.appendChild(container);
    }
    const el = document.createElement("p");
    el.className = "toast";
    if (icon === "warning") {
        el.classList.add("warning");
    } else if (icon === "x") {
        el.classList.add("bad");
    }

    const iconEl = document.createElement("i");
    iconEl.className = `ph ph-${icon}`;
    el.appendChild(iconEl);

    el.appendChild(document.createTextNode(` ${text}`));

    bindDismiss(el);
    container.appendChild(el);
}

function bindDismiss(el) {
    el.addEventListener("click", () => el.remove());
    el.addEventListener("animationend", (event) => {
        if (event.animationName === "toast-out") {
            el.remove();
        }
    });
}

document.querySelectorAll(".toast").forEach(bindDismiss);

window.showToast = showToast;
