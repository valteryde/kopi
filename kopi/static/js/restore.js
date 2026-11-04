const backdrop = document.getElementById("backdrop");

if (backdrop) {
    const form = document.getElementById("restore-form");
    const label = document.getElementById("restore-label");
    const dest = document.getElementById("restore-dest");
    const hint = document.getElementById("restore-hint");
    const go = document.getElementById("restore-go");
    let opener = null;

    const open = (button) => {
        const data = button.dataset;
        opener = button;
        form.setAttribute("action", data.action || "");
        document.getElementById("restore-name").textContent = data.name || "";
        document.getElementById("restore-object").textContent = data.object || "";
        document.getElementById("restore-size").textContent = data.size || "";
        document.getElementById("restore-sum").textContent = data.sum || "";
        dest.value = data.dest || "";
        if (data.kind === "folder") {
            fill(label, "folder-open", "Folder path");
            fill(hint, "info", "Files are mode 0600. Directories are mode 0700.");
            dest.removeAttribute("pattern");
        } else {
            fill(label, "database", "Database name");
            fill(hint, "database", "The dump is restored into this database.");
            dest.pattern = "[A-Za-z_][A-Za-z0-9_]{0,62}";
        }
        if (go) {
            go.disabled = false;
        }
        backdrop.hidden = false;
        dest.focus();
    };

    const close = () => {
        backdrop.hidden = true;
        if (opener) {
            opener.focus();
        }
    };

    document.querySelectorAll("[data-open-restore]").forEach((button) => {
        button.addEventListener("click", (event) => {
            event.preventDefault();
            event.stopPropagation();
            open(button);
        });
    });

    document.getElementById("restore-cancel").addEventListener("click", close);
    backdrop.addEventListener("click", close);
    form.addEventListener("click", (event) => event.stopPropagation());
    form.addEventListener("submit", () => {
        if (go) {
            go.disabled = true;
        }
    });
    document.addEventListener("keydown", (event) => {
        if (event.key === "Escape" && !backdrop.hidden) {
            close();
        }
    });
}

function fill(node, icon, text) {
    const mark = document.createElement("i");
    mark.className = `ph ph-${icon}`;
    node.replaceChildren(mark, document.createTextNode(` ${text}`));
}
