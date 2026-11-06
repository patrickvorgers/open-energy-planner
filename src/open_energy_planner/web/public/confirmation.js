/** Show one app-owned confirmation using native modal focus and keyboard behavior. */
let pending = false;

/** Return explicit consent only. Concurrent requests and every dismissal cancel safely. */
export async function confirmAction({title, message, confirmLabel, returnFocus = document.activeElement}) {
    if (pending) return false;
    pending = true;
    const opener = returnFocus;
    const dialog = document.createElement("dialog");
    dialog.className = "confirmation-dialog";
    dialog.setAttribute("role", "alertdialog");
    dialog.setAttribute("aria-labelledby", "confirmation-title");
    dialog.setAttribute("aria-describedby", "confirmation-message");
    dialog.innerHTML = '<form method="dialog"><h2 id="confirmation-title"></h2><p id="confirmation-message"></p><footer class="confirmation-actions"><button type="submit" value="cancel" class="secondary" autofocus>Cancel</button><button type="submit" value="confirm" class="confirmation-accept"></button></footer></form>';
    dialog.querySelector("h2").textContent = title;
    dialog.querySelector("p").textContent = message;
    dialog.querySelector(".confirmation-accept").textContent = confirmLabel;
    dialog.addEventListener("keydown", event => {
        if (event.key !== "Tab") return;
        const buttons = dialog.querySelectorAll("button");
        if (event.shiftKey && document.activeElement === buttons[0]) { event.preventDefault(); buttons[1].focus(); }
        else if (!event.shiftKey && document.activeElement === buttons[1]) { event.preventDefault(); buttons[0].focus(); }
    });
    dialog.addEventListener("click", event => {
        const bounds = dialog.getBoundingClientRect();
        if (event.target === dialog && (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom)) dialog.close("cancel");
    });
    document.body.append(dialog);
    try {
        return await new Promise(resolve => {
            dialog.addEventListener("close", () => resolve(dialog.returnValue === "confirm"), {once: true});
            dialog.showModal();
        });
    } finally {
        dialog.remove();
        pending = false;
        if (opener?.isConnected && !opener.disabled) opener.focus({preventScroll: true});
    }
}
