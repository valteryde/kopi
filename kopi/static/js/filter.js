document.querySelectorAll("[data-filter]").forEach((input) => {
    const emptyName = input.dataset.emptyFor;
    const empty = emptyName ? document.querySelector(`[data-empty="${emptyName}"]`) : null;
    input.addEventListener("input", () => {
        const query = input.value.trim().toLowerCase();
        const rows = [...document.querySelectorAll(input.dataset.filter)];
        rows.forEach((row) => {
            row.hidden = Boolean(query) && !row.innerText.toLowerCase().includes(query);
        });
        if (empty) {
            const showing = rows.some((row) => !row.hidden);
            empty.hidden = showing || query === "";
        }
    });
});
