export function launchUrl(value) {
  const source = new URL(value.trim());
  const parts = source.pathname.replace(/\/$/, "").split("/").filter(Boolean);
  if (source.protocol !== "https:" || source.hostname !== "github.com" || source.port ||
      source.username || source.password || source.search || source.hash ||
      parts.length < 2 || !/^[\w.-]+$/.test(parts[0]) || !/^[\w.-]+$/.test(parts[1]) ||
      (parts.length > 2 && (parts[2] !== "tree" || parts.length < 4))) {
    throw new Error("Use an HTTPS GitHub repository or /tree/branch URL without credentials, queries or fragments.");
  }
  parts[1] = parts[1].replace(/\.git$/, "");
  if (!parts[1]) throw new Error("Enter a repository name.");
  const repository = "https://github.com/" + parts.join("/");
  const command = "vscode://ms-vscode-remote.remote-containers/cloneInVolume?url=" + encodeURIComponent(repository);
  return "https://vscode.dev/redirect?url=" + encodeURIComponent(command);
}

const input = document.querySelector("#repository");
const link = document.querySelector("#launch");
const error = document.querySelector("#error");
function update() {
  try {
    link.href = launchUrl(input.value);
    link.setAttribute("aria-disabled", "false");
    input.setAttribute("aria-invalid", "false");
    error.hidden = true;
  } catch {
    link.removeAttribute("href");
    link.setAttribute("aria-disabled", "true");
    input.setAttribute("aria-invalid", "true");
    error.textContent = "Enter an HTTPS github.com repository or branch URL without credentials, query parameters or fragments.";
    error.hidden = false;
  }
}
input.addEventListener("input", update);
link.addEventListener("click", event => {
  if (link.getAttribute("aria-disabled") === "true") event.preventDefault();
});
update();
