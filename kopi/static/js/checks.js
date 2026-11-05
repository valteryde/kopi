// Test a source, a target, or a link before saving it. The values sitting in
// the form go to that screen's test URL, and the lines that come back take
// the place of this area. The request saves nothing.
document.querySelectorAll("form[data-check]").forEach((form) => {
    const url = form.dataset.check;
    const button = form.querySelector("[data-check-button]");
    const out = form.querySelector("[data-check-out]");
    if (!url || !button || !out) {
        return;
    }
    button.addEventListener("click", () => {
        run(form, button, out, url);
    });
});

async function run(form, button, out, url) {
    button.disabled = true;
    label(button, "circle-notch", "Testing…", true);
    out.hidden = false;
    out.replaceChildren(line("Checking. This can take a few seconds.", { spin: true }));
    let page = null;
    try {
        const response = await fetch(url, {
            method: "POST",
            body: new FormData(form),
            credentials: "same-origin",
            cache: "no-store",
        });
        if (!response.ok) {
            throw new Error("the check was refused");
        }
        page = new DOMParser().parseFromString(await response.text(), "text/html");
    } catch (error) {
        refuse(button, out);
        return;
    }
    // Only the check's own lines are copied in. An unexpected page, such as
    // the sign-in form behind an expired session, cannot paint itself here.
    const found = page.querySelectorAll("[data-check-line], [data-check-group]");
    if (!found.length) {
        refuse(button, out);
        return;
    }
    out.replaceChildren(...found);
    const failed = out.querySelector("[data-bad]") !== null;
    button.classList.toggle("is-bad", failed);
    button.classList.toggle("is-ok", !failed);
    label(button, failed ? "warning" : "check", failed ? "Not ready" : "Ready");
    button.disabled = false;
}

function refuse(button, out) {
    out.replaceChildren(line("The check did not answer. Try again.", { icon: "warning" }));
    button.classList.add("is-bad");
    button.classList.remove("is-ok");
    label(button, "plugs", "Test");
    button.disabled = false;
}

function label(button, icon, text, spin = false) {
    button.replaceChildren(mark(icon, spin), document.createTextNode(` ${text}`));
}

function line(text, { icon = "info", spin = false } = {}) {
    const row = document.createElement("p");
    row.className = "check-line info";
    row.setAttribute("data-check-line", "");
    row.replaceChildren(mark(icon, spin), document.createTextNode(` ${text}`));
    return row;
}

function mark(icon, spin) {
    const node = document.createElement("i");
    node.className = spin ? "ph ph-circle-notch checking" : `ph ph-${icon}`;
    return node;
}
